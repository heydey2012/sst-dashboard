"""
[TQQQ 분할매수 에이전트] RSI Spread Pro Strategy v1 (분할매수 DCA) 포팅
원본: ~/Downloads/RSI_Spread_Pro_Strategy_v1.pine (© 파인스크립트 최고 마스터)

- 미국 ETF 종목(TQQQ) 대상, 키움 해외주식 API로 15분봉 데이터를 받아 일반봉
  RSI스프레드 + 하이킨아시 RSI스프레드가 동시에 -15 이하로 내려가는 [롱 시그널]이
  피보나치 0.382 필터를 통과한 채로 나온 뒤, 첫 양봉 마감이 나오면 분할매수
  (최대 config.TQQQ_SPLIT_COUNT회, 물타기) 진입합니다.
- 손절 없음 - 평단가 대비 config.TQQQ_TP_PCT 도달 시 보유 수량 전체를 익절합니다.
- 원본 전략은 롱+숏 모두 지원하지만, 국내 개인 위탁계좌로 미국주식 공매도가
  불가능해(주문 API에 신용/대주 파라미터 자체가 없음) 롱 전용으로만 포팅했습니다.
- 초기자금은 원화(config.TQQQ_INITIAL_CAPITAL_KRW) 기준이며, 매 진입 시점의
  실시간 환율(키움 API 응답의 base_exrt)로 달러 주문 금액을 환산합니다.

*** 안전장치 ***
매 주문 직전 kiwoom 클라이언트의 auth.mode 가 "demo"가 아니면 즉시 예외를 던지고 중단합니다.
"""
import json
import os
import subprocess
from datetime import datetime, timedelta

import pandas as pd

try:
    from kiwoom import get_client, KiwoomError
except ImportError:
    get_client = None
    KiwoomError = Exception

import config
from agents.calculator import compute_rsi, to_heikin_ashi, find_pivots
from agents.notifier import NotifierAgent

CHART_API_ID = "usa06011"
CHART_API_URL = "/api/us/chart"
QUOTE_API_ID = "usa20100"
QUOTE_API_URL = "/api/us/mrkcond"
ORDER_API_ID_BUY = "ust20000"
ORDER_API_ID_SELL = "ust20001"
ORDER_API_URL = "/api/us/ordr"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _num(v) -> float:
    """키움 응답의 숫자 필드는 종종 +/- 기호가 등락 표시용으로 붙어있어 절대값 처리."""
    try:
        return abs(float(str(v).strip()))
    except (TypeError, ValueError):
        return float("nan")


def _parse_cntr_tm(s: str) -> datetime:
    """미국장 야간(자정 넘김) 봉은 시(HH)가 24 이상으로 표기됨(예: 27:30 = 다음날 03:30,
    같은 거래일(bus_dt) 세션으로 묶기 위함) - 표준 datetime으로 정규화."""
    year, month, day = int(s[0:4]), int(s[4:6]), int(s[6:8])
    hour, minute, second = int(s[8:10]), int(s[10:12]), int(s[12:14])
    extra_days, hour = divmod(hour, 24)
    return datetime(year, month, day, hour, minute, second) + timedelta(days=extra_days)


class TqqqAgent:
    def __init__(self):
        self.notifier = NotifierAgent()
        self.client = get_client(mode="demo") if get_client else None
        self._positions_path = os.path.join(ROOT, config.TQQQ_POSITIONS_FILE)
        self._trade_log_path = os.path.join(ROOT, config.TQQQ_TRADE_LOG_FILE)

    # ------------------------------------------------------------------
    # 안전장치 / 로컬 상태
    # ------------------------------------------------------------------
    def _assert_demo_mode(self):
        if self.client is None:
            raise RuntimeError("[TQQQ] kiwoom 클라이언트가 초기화되지 않았습니다.")
        mode = getattr(self.client.auth, "mode", None)
        if mode != "demo":
            raise RuntimeError(
                f"[TQQQ] 안전장치 발동: 계좌 모드가 'demo'가 아니라 '{mode}'입니다. "
                "실계좌 자동매매 사고를 방지하기 위해 주문을 중단합니다."
            )

    def _load_positions(self) -> dict:
        if not os.path.exists(self._positions_path):
            return {}
        with open(self._positions_path, encoding="utf-8") as f:
            return json.load(f)

    def _save_positions(self, positions: dict):
        with open(self._positions_path, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)

    def _load_trade_log(self) -> list:
        if not os.path.exists(self._trade_log_path):
            return []
        with open(self._trade_log_path, encoding="utf-8") as f:
            return json.load(f)

    def _append_trade_log(self, entry: dict):
        log = self._load_trade_log()
        log.append(entry)
        with open(self._trade_log_path, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # 시세 / 차트 조회
    # ------------------------------------------------------------------
    def fetch_quote(self):
        if self.client is None:
            return None
        try:
            resp = self.client.fetch_page(
                api_id=QUOTE_API_ID, path=QUOTE_API_URL,
                body={"stex_tp": config.TQQQ_EXCHANGE, "stk_cd": config.TQQQ_TICKER},
            )
            body = resp.body
            price = _num(body.get("cur_prc"))
            fx_rate = _num(body.get("base_exrt"))
            if not price or not fx_rate:
                return None
            return {"price": price, "fx_rate": fx_rate}
        except Exception as e:
            print(f"[TQQQ] 시세 조회 실패: {e}")
            return None

    def fetch_chart(self, bars: int = 200) -> pd.DataFrame:
        if self.client is None:
            return pd.DataFrame()
        try:
            resp = self.client.fetch_page(
                api_id=CHART_API_ID, path=CHART_API_URL,
                body={"stex_tp": config.TQQQ_EXCHANGE, "stk_cd": config.TQQQ_TICKER,
                      "tic_scope": config.TQQQ_TIMEFRAME},
            )
            rows = resp.body.get("result_list", [])
        except Exception as e:
            print(f"[TQQQ] 차트 조회 실패: {e}")
            return pd.DataFrame()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["time"] = df["cntr_tm"].astype(object).map(_parse_cntr_tm)
        df["open"] = df["open_pric"].map(_num)
        df["high"] = df["high_pric"].map(_num)
        df["low"] = df["low_pric"].map(_num)
        df["close"] = df["cur_prc"].map(_num)
        df = df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)
        df = df.set_index("time")
        return df.tail(bars)

    # ------------------------------------------------------------------
    # 신호 계산 (Pine 로직 포팅 - 롱 전용)
    # ------------------------------------------------------------------
    def compute_signal(self, df: pd.DataFrame) -> dict:
        min_bars = config.TQQQ_PIVOT_LEFT + config.TQQQ_PIVOT_RIGHT + config.TQQQ_MA_LENGTH + 5
        if len(df) < min_bars:
            return {"valid": False}

        rsi_reg = compute_rsi(df["close"], config.TQQQ_RSI_LENGTH)
        ma_reg = rsi_reg.rolling(config.TQQQ_MA_LENGTH).mean()
        spread_reg = rsi_reg - ma_reg

        ha = to_heikin_ashi(df)
        rsi_ha = compute_rsi(ha["close"], config.TQQQ_RSI_LENGTH)
        ma_ha = rsi_ha.rolling(config.TQQQ_MA_LENGTH).mean()
        spread_ha = rsi_ha - ma_ha

        long_signal = (spread_reg <= -config.TQQQ_SPREAD_LIMIT) & (spread_ha <= -config.TQQQ_SPREAD_LIMIT)

        # 피보나치 0.382 필터 - 직전 확정 피벗 고점/저점 구간에서 0.382 되돌림 라인 위에
        # 종가가 있어야(즉 너무 많이 되돌리지 않은 상태여야) 롱 신호를 인정
        piv_high = find_pivots(df["high"], config.TQQQ_PIVOT_LEFT, config.TQQQ_PIVOT_RIGHT, "high")
        piv_low = find_pivots(df["low"], config.TQQQ_PIVOT_LEFT, config.TQQQ_PIVOT_RIGHT, "low")
        last_high = piv_high.ffill()
        last_low = piv_low.ffill()
        fib_long_line = last_high - (last_high - last_low) * config.TQQQ_FIB_RATIO
        fib_long_pass = fib_long_line.isna() | (df["close"] <= fib_long_line)

        long_signal = long_signal & fib_long_pass

        # 연속 신호 카운트 + 반전(양봉 마감) 트리거 - Pine [7]번 블록과 동일
        streak = 0
        prev_streak_vals = []
        for sig in long_signal:
            prev_streak_vals.append(streak)
            streak = streak + 1 if bool(sig) else 0
        prev_streak = pd.Series(prev_streak_vals, index=df.index)

        is_bull_close = df["close"] > df["open"]
        long_trigger = (prev_streak >= 1) & is_bull_close

        return {
            "valid": True,
            "long_trigger": bool(long_trigger.iloc[-1]),
            "price": float(df["close"].iloc[-1]),
            "time": df.index[-1],
            "spread_reg": None if pd.isna(spread_reg.iloc[-1]) else float(spread_reg.iloc[-1]),
            "spread_ha": None if pd.isna(spread_ha.iloc[-1]) else float(spread_ha.iloc[-1]),
            "streak_before": int(prev_streak.iloc[-1]),
            "pivot_high": None if pd.isna(last_high.iloc[-1]) else float(last_high.iloc[-1]),
            "pivot_low": None if pd.isna(last_low.iloc[-1]) else float(last_low.iloc[-1]),
        }

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    @staticmethod
    def _round_price(price: float) -> str:
        """모의투자는 지정가만 가능하고, $1 미만은 소수 4자리/$1 이상은 소수 2자리 제한."""
        return f"{price:.4f}" if price < 1 else f"{price:.2f}"

    def _order(self, api_id: str, qty: int, price: float) -> dict:
        self._assert_demo_mode()
        body = {
            "stex_tp": config.TQQQ_EXCHANGE,
            "stk_cd": config.TQQQ_TICKER,
            "ord_qty": str(qty),
            "trde_tp": "00",  # 모의투자는 지정가만 가능(시장가 주문 거부됨)
            "ord_uv": self._round_price(price),
        }
        response = self.client.fetch_page(api_id=api_id, path=ORDER_API_URL, body=body)
        return response.body

    # ------------------------------------------------------------------
    # 진입 (분할매수)
    # ------------------------------------------------------------------
    def process_entry(self):
        if self.client is None:
            print("[TQQQ] kiwoom 클라이언트 없음 - 진입 점검을 건너뜁니다.")
            return

        end_date = datetime.strptime(config.TRADING_END_DATE, "%Y-%m-%d").date()
        if datetime.now().date() > end_date:
            print(f"[TQQQ] 모의매매 종료일({config.TRADING_END_DATE}) 이후 - 신규 진입을 건너뜁니다.")
            return

        positions = self._load_positions()
        pos = positions.get(config.TQQQ_TICKER)
        split_count = pos["split_count"] if pos else 0
        if split_count >= config.TQQQ_SPLIT_COUNT:
            print(f"[TQQQ] 분할매수 한도({config.TQQQ_SPLIT_COUNT}회) 도달 - 신규 진입을 건너뜁니다.")
            return

        df = self.fetch_chart()
        if df.empty:
            print("[TQQQ] 차트 데이터를 가져오지 못했습니다.")
            return

        sig = self.compute_signal(df)
        if not sig["valid"] or not sig["long_trigger"]:
            return

        quote = self.fetch_quote()
        if quote is None:
            print("[TQQQ] 시세/환율 조회 실패 - 진입을 건너뜁니다.")
            return

        price, fx_rate = quote["price"], quote["fx_rate"]
        usd_per_split = (config.TQQQ_INITIAL_CAPITAL_KRW / config.TQQQ_SPLIT_COUNT) / fx_rate
        qty = int(usd_per_split // price)
        if qty < 1:
            print(f"[TQQQ] 분할 금액(${usd_per_split:.2f})이 1주 가격(${price:.2f})보다 작아 진입 불가")
            return

        try:
            result = self._order(ORDER_API_ID_BUY, qty, price)
        except Exception as e:
            print(f"[TQQQ] 매수 주문 실패: {e}")
            return

        if result.get("return_code") not in (None, 0):
            print(f"[TQQQ] 매수 거부: {result.get('return_msg')}")
            return

        entries = pos["entries"] if pos else []
        entries.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "price": price, "qty": qty, "fx_rate": fx_rate,
            "spread_reg": sig["spread_reg"], "spread_ha": sig["spread_ha"],
        })
        total_qty = sum(e["qty"] for e in entries)
        avg_price = sum(e["qty"] * e["price"] for e in entries) / total_qty

        positions[config.TQQQ_TICKER] = {
            "qty": total_qty,
            "avg_price": avg_price,
            "split_count": len(entries),
            "first_entry_time": entries[0]["time"],
            "last_entry_time": entries[-1]["time"],
            "entries": entries,
            "order_no": result.get("ord_no"),
        }
        self._save_positions(positions)
        print(f"[TQQQ] 매수 완료 ({len(entries)}/{config.TQQQ_SPLIT_COUNT}) - {qty}주 @ ${price:.2f} "
              f"(평단가 ${avg_price:.4f})")
        self.notifier.send(
            f"🟢 [TQQQ 분할매수] 진입 {len(entries)}/{config.TQQQ_SPLIT_COUNT}\n"
            f"{qty}주 @ ${price:.2f} (평단가 ${avg_price:.4f})\n"
            f"보유 {total_qty}주"
        )

    # ------------------------------------------------------------------
    # 청산 (평단가 기준 익절만 - 손절 없음)
    # ------------------------------------------------------------------
    def check_exit(self):
        if self.client is None:
            print("[TQQQ] kiwoom 클라이언트 없음 - 청산 점검을 건너뜁니다.")
            return

        positions = self._load_positions()
        pos = positions.get(config.TQQQ_TICKER)
        current_price = None
        current_fx = None
        if pos:
            quote = self.fetch_quote()
            if quote is None:
                print("[TQQQ] 시세 조회 실패 - 청산 점검을 건너뜁니다.")
            else:
                current_price, current_fx = quote["price"], quote["fx_rate"]
                tp_price = pos["avg_price"] * (1 + config.TQQQ_TP_PCT)
                if current_price >= tp_price:
                    self._execute_exit(pos, current_price, positions, current_fx, "익절(평단가 대비 목표 도달)")

        self.export_dashboard_snapshot(current_price, current_fx)

    def _execute_exit(self, pos: dict, price: float, positions: dict, fx_rate: float, reason: str) -> bool:
        qty = pos["qty"]
        try:
            result = self._order(ORDER_API_ID_SELL, qty, price)
        except Exception as e:
            print(f"[TQQQ] 매도 주문 실패: {e}")
            return False

        if result.get("return_code") not in (None, 0):
            print(f"[TQQQ] 매도 거부: {result.get('return_msg')}")
            return False

        pnl = (price - pos["avg_price"]) * qty
        pnl_pct = (price - pos["avg_price"]) / pos["avg_price"] * 100

        del positions[config.TQQQ_TICKER]
        self._save_positions(positions)
        self._append_trade_log({
            "ticker": config.TQQQ_TICKER,
            "name": "TQQQ",
            "qty": qty,
            "avg_entry_price": pos["avg_price"],
            "split_count": pos["split_count"],
            "first_entry_time": pos["first_entry_time"],
            "exit_price": price,
            "exit_time": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "fx_rate": fx_rate,
        })
        print(f"[TQQQ] 전량 청산 완료 - {qty}주 @ ${price:.2f} (손익 ${pnl:+.2f}, {pnl_pct:+.2f}%)")
        self.notifier.send(
            f"🔵 [TQQQ 분할매수] 청산 완료 - {reason}\n{qty}주 @ ${price:.2f}\n"
            f"손익 ${pnl:+.2f} ({pnl_pct:+.2f}%)"
        )
        return True

    def close_position_manual(self, reason: str = "대시보드 전체 청산 요청"):
        """대시보드 전체청산 버튼 처리용. True=성공, False=재시도필요, None=포지션없음."""
        positions = self._load_positions()
        pos = positions.get(config.TQQQ_TICKER)
        if pos is None:
            return None
        quote = self.fetch_quote()
        if quote is None:
            return False
        ok = self._execute_exit(pos, quote["price"], positions, quote["fx_rate"], reason)
        self.export_dashboard_snapshot(quote["price"] if ok else None, quote["fx_rate"] if ok else None)
        return ok

    # ------------------------------------------------------------------
    # 대시보드 연동 - docs/results/positions_tqqq.json / trade_log_tqqq.json
    # ------------------------------------------------------------------
    def export_dashboard_snapshot(self, current_price, current_fx):
        positions = self._load_positions()
        pos = positions.get(config.TQQQ_TICKER)

        position_row = None
        unrealized_krw = 0
        if pos:
            pnl_usd = pnl_pct = None
            if current_price is not None and current_fx is not None:
                pnl_usd = (current_price - pos["avg_price"]) * pos["qty"]
                pnl_pct = (current_price - pos["avg_price"]) / pos["avg_price"] * 100
                unrealized_krw = pnl_usd * current_fx
            position_row = {
                "ticker": config.TQQQ_TICKER,
                "name": "TQQQ (프로셰어즈 3배 레버리지 나스닥100 ETF)",
                "qty": pos["qty"],
                "avg_price": pos["avg_price"],
                "split_count": pos["split_count"],
                "max_splits": config.TQQQ_SPLIT_COUNT,
                "first_entry_time": pos["first_entry_time"],
                "last_entry_time": pos["last_entry_time"],
                "current_price": current_price,
                "tp_price": pos["avg_price"] * (1 + config.TQQQ_TP_PCT),
                "entries": pos["entries"],
                "pnl": pnl_usd,
                "pnl_pct": pnl_pct,
                "fx_rate": current_fx,
            }

        trade_log = self._load_trade_log()
        realized_krw = sum((t.get("pnl") or 0) * (t.get("fx_rate") or 0) for t in trade_log)
        profit_amount_krw = round(unrealized_krw + realized_krw, 0)
        current_capital_krw = config.TQQQ_INITIAL_CAPITAL_KRW + profit_amount_krw
        return_pct = round(profit_amount_krw / config.TQQQ_INITIAL_CAPITAL_KRW * 100, 2)

        payload = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "strategy_version": "tqqq",
            "currency": "USD",
            "initial_capital": config.TQQQ_INITIAL_CAPITAL_KRW,
            "current_capital": current_capital_krw,
            "return_pct": return_pct,
            "profit_amount": profit_amount_krw,
            "positions": [position_row] if position_row else [],
        }

        results_dir = os.path.join(ROOT, "docs", "results")
        os.makedirs(results_dir, exist_ok=True)
        with open(os.path.join(results_dir, "positions_tqqq.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(results_dir, "trade_log_tqqq.json"), "w", encoding="utf-8") as f:
            json.dump(trade_log, f, ensure_ascii=False, indent=2)
        print(f"[TQQQ] 대시보드용 스냅샷 저장 완료")

        self._git_push()

    def _git_push(self):
        git_dir = os.path.join(ROOT, ".git")
        if not os.path.isdir(git_dir):
            return
        try:
            subprocess.run(
                ["git", "add", "docs/results/positions_tqqq.json", "docs/results/trade_log_tqqq.json"],
                cwd=ROOT, check=True, capture_output=True,
            )
            msg = f"SST TQQQ 분할매수 현황 갱신 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            commit = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, capture_output=True, text=True)
            if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
                print(f"[TQQQ] git commit 경고: {commit.stdout.strip()} {commit.stderr.strip()}")
                return
            push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
            if push.returncode != 0:
                print(f"[TQQQ] git push 실패: {push.stderr.strip()}")
            else:
                print("[TQQQ] GitHub로 결과 푸시 완료")
        except Exception as e:
            print(f"[TQQQ] git 자동화 중 오류: {e}")
