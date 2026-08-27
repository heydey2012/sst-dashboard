"""
[수집 에이전트] Data Collector Agent - 키움증권 공식 REST API 연동

*** 사전 준비 (필수) ***
1. 키움 공식 저장소 클론 + 설치 (kiwoom 패키지 확보)
     git clone https://github.com/Kiwoom-Securities/Kiwoom-REST-API.git
     cd Kiwoom-REST-API
     uv sync                      # 또는: pip install -e .
2. CLI 인증 (App Key/Secret이 OS 키체인에 안전하게 저장됨 - macOS Keychain 지원)
     uv tool install kwcli
     kiwoomcli setup              # real(실전) 또는 demo(모의투자) 선택 후 키 입력
3. 이 SST 프로젝트를 실행하는 환경에서 위 kiwoom 패키지를 import할 수 있어야 합니다.
   (Kiwoom-REST-API를 pip install -e 로 같은 가상환경에 설치하거나,
    PYTHONPATH에 그 저장소 경로를 추가하세요.)

*** 사용 API (공식 예제 examples/국내주식/차트/get_domestic_stock_minute_chart.py 기준) ***
- api_id: ka10080 (주식분봉차트조회요청), api_url: /api/dostk/chart
- tic_scope: 1/3/5/10/15/30/45/60(분) 중 선택 - 4시간봉 자체 옵션은 없어서
  60분(1시간)봉을 받아 4개씩 묶어 우리가 직접 리샘플링합니다.
- base_dt(기준일자, YYYYMMDD): 과거 날짜 지정 가능 -> KIS와 달리 히스토리 백필이 됩니다.

*** 동작 방식 ***
- 최초 실행 시: config.HISTORY_DAYS 만큼 과거로 거슬러 올라가며 1시간봉을 모아
  로컬 캐시(data_cache/*.csv)에 4시간봉으로 저장 (다소 시간 걸림, API 호출 제한 있음)
- 이후 실행 시: 캐시에 이미 있으면 오늘자만 추가 조회해서 누적 (빠름)
"""
import os
import time
from datetime import datetime, timedelta

import pandas as pd

try:
    from kiwoom import get_client, KiwoomError
except ImportError:
    get_client = None
    KiwoomError = Exception

import config

API_ID = "ka10080"
API_URL = "/api/dostk/chart"

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache")


class DataCollectorAgent:
    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        if get_client is None:
            print(
                "[Collector] 경고: 'kiwoom' 패키지를 찾을 수 없습니다.\n"
                "  -> https://github.com/Kiwoom-Securities/Kiwoom-REST-API 를 클론해서 "
                "같은 가상환경에 설치했는지, kiwoomcli setup으로 인증했는지 확인하세요."
            )
        self.client = get_client() if get_client else None

    # ------------------------------------------------------------------
    # 키움 API 호출
    # ------------------------------------------------------------------
    def _request_with_retry(self, body: dict, cont_yn, next_key, max_retries: int = 5):
        """429(요청 한도 초과) 발생 시 대기 후 재시도"""
        delay = config.KIWOOM_REQUEST_DELAY
        for attempt in range(max_retries):
            try:
                return self.client.fetch_page(
                    api_id=API_ID, path=API_URL, body=body,
                    cont_yn=cont_yn, next_key=next_key,
                )
            except Exception as e:
                is_rate_limit = "429" in str(e) or "1700" in str(e) or "허용된" in str(e)
                if is_rate_limit and attempt < max_retries - 1:
                    wait = delay * (attempt + 2)  # 점점 더 오래 대기
                    print(f"[Collector] 요청 한도 초과, {wait:.1f}초 대기 후 재시도 ({attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
                raise

    def fetch_minute_bars(self, ticker: str, base_dt: str, max_pages: int = None) -> pd.DataFrame:
        """지정 날짜(base_dt, YYYYMMDD) 기준 분봉 조회 (연속조회 페이징 + 429 재시도 포함)

        max_pages: 이번 호출에서 따라갈 최대 페이지 수. None이면 config.KIWOOM_MAX_PAGES 사용.
        이미 히스토리가 충분한 종목의 "오늘자만" 증분 수집 시에는 작은 값(예: 1~2)을 넘겨서
        불필요하게 과거 페이지까지 따라가며 시간을 낭비하지 않도록 합니다.
        """
        if self.client is None:
            raise RuntimeError("kiwoom 클라이언트가 초기화되지 않았습니다 (패키지 미설치/미인증).")

        max_pages = max_pages if max_pages is not None else config.KIWOOM_MAX_PAGES

        body = {
            "stk_cd": ticker,
            "tic_scope": config.KIWOOM_TIC_SCOPE,
            "upd_stkpc_tp": config.KIWOOM_UPD_STKPC_TP,
            "base_dt": base_dt,
        }

        rows = []
        next_cont_yn, next_key = None, None
        for page in range(max_pages):
            response = self._request_with_retry(body, next_cont_yn, next_key)
            body_data = response.body
            records = body_data.get("stk_min_pole_chart_qry", [])
            rows.extend(r for r in records if isinstance(r, dict))

            next_cont_yn = response.continuation.cont_yn
            next_key = response.continuation.next_key
            if next_cont_yn != "Y":
                break
            time.sleep(config.KIWOOM_REQUEST_DELAY)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.rename(columns={
            "cntr_tm": "datetime", "open_pric": "open", "high_pric": "high",
            "low_pric": "low", "cur_prc": "close", "trde_qty": "volume",
        })

        def parse_dt(v):
            v = str(v)
            if len(v) >= 14:
                return pd.to_datetime(v[:14], format="%Y%m%d%H%M%S")
            return pd.to_datetime(base_dt + v[-6:].zfill(6), format="%Y%m%d%H%M%S")

        df["datetime"] = df["datetime"].map(parse_dt)
        for c in ["open", "high", "low", "close", "volume"]:
            # 키움 API는 종종 전일대비 부호(+/-)가 섞여 내려오는 필드가 있어 절대값 처리
            df[c] = pd.to_numeric(df[c], errors="coerce").abs()
        df = df.set_index("datetime").sort_index()
        return df[["open", "high", "low", "close", "volume"]]

    def _resample(self, minute_df: pd.DataFrame) -> pd.DataFrame:
        """네이티브로 받은 분봉을 config.TIMEFRAME 단위로 묶음.
        키움이 이미 해당 단위를 네이티브 지원하는 경우(예: 15분봉)는 사실상
        그대로 통과되고, 4시간봉처럼 직접 지원 안 하는 경우만 실제로 묶입니다."""
        if minute_df.empty:
            return minute_df
        rule = f"{config._TF_MIN}min"
        return minute_df.resample(rule, origin="09:00:00").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna()

    # ------------------------------------------------------------------
    # 로컬 캐시
    # ------------------------------------------------------------------
    def _cache_path(self, ticker: str) -> str:
        return os.path.join(CACHE_DIR, f"{ticker}_{config.TIMEFRAME}.csv")

    def load_cache(self, ticker: str) -> pd.DataFrame:
        path = self._cache_path(ticker)
        if not os.path.exists(path):
            return pd.DataFrame()
        return pd.read_csv(path, index_col=0, parse_dates=True)

    def append_to_cache(self, ticker: str, new_bars: pd.DataFrame):
        if new_bars.empty:
            return
        os.makedirs(CACHE_DIR, exist_ok=True)  # 저장 직전에 한 번 더 보장 (방어적 처리)
        existing = self.load_cache(ticker)
        combined = pd.concat([existing, new_bars]) if not existing.empty else new_bars
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.to_csv(self._cache_path(ticker))

    # ------------------------------------------------------------------
    # 종목별 수집 (백필 또는 증분)
    # ------------------------------------------------------------------
    def collect_ticker(self, ticker: str) -> pd.DataFrame:
        cache = self.load_cache(ticker)
        min_bars_needed = config.MA_LENGTH + config.SMA_SLOW + config.PIVOT_LEFT + config.PIVOT_RIGHT

        if len(cache) >= min_bars_needed:
            # 이미 히스토리 충분 -> 오늘자만 최소 페이지로 증분 수집 (속도 최적화)
            today = datetime.now().strftime("%Y%m%d")
            day_df = self.fetch_minute_bars(ticker, today, max_pages=config.KIWOOM_INCREMENTAL_MAX_PAGES)
            self.append_to_cache(ticker, self._resample(day_df))
        else:
            # 히스토리 부족 -> 초기 백필 (다소 시간 소요, API 호출 제한 주의)
            d = datetime.now()
            collected_days, attempts = 0, 0
            while collected_days < config.HISTORY_DAYS and attempts < config.HISTORY_DAYS * 2:
                attempts += 1
                base_dt = d.strftime("%Y%m%d")
                if d.weekday() < 5:  # 평일만
                    try:
                        day_df = self.fetch_minute_bars(ticker, base_dt)
                        if not day_df.empty:
                            self.append_to_cache(ticker, self._resample(day_df))
                            collected_days += 1
                            print(f"    ↳ {ticker} 백필 {collected_days}/{config.HISTORY_DAYS}일 ({base_dt})")
                    except Exception as e:
                        print(f"[Collector] {ticker} {base_dt} 조회 실패: {e}")
                    time.sleep(config.KIWOOM_REQUEST_DELAY)
                d -= timedelta(days=1)

        return self.load_cache(ticker)

    def fetch_all(self, tickers: list) -> dict:
        results = {}
        total = len(tickers)
        for i, t in enumerate(tickers, start=1):
            try:
                results[t] = self.collect_ticker(t)
                print(f"[Collector] ({i}/{total}) {t} 수집 완료 - {len(results[t])}봉 확보")
            except Exception as e:
                print(f"[Collector] ({i}/{total}) {t} 수집 실패: {e}")
            time.sleep(config.KIWOOM_REQUEST_DELAY)
        return results
