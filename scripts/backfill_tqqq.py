"""
TQQQ 5분봉 히스토리 백필 - usa06011의 continuation(cont_yn/next_key) 페이징을 끝까지 따라가며
data_cache/TQQQ_{timeframe}.csv에 누적 저장합니다 (국내주식과 달리 base_dt 지정이 아니라
현재 시점부터 과거로 계속 이어지는 연속조회 방식).

사용법: python3 scripts/backfill_tqqq.py [timeframe(분)] [target_start YYYY-MM-DD]
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd

import config
from agents.tqqq_agent import _parse_cntr_tm, _num

try:
    from kiwoom import get_client, KiwoomError
except ImportError:
    get_client = None

TIC_SCOPE = sys.argv[1] if len(sys.argv) > 1 else "5"
TARGET_START = pd.Timestamp(sys.argv[2] if len(sys.argv) > 2 else "2025-01-01")
CACHE_PATH = os.path.join(ROOT, "data_cache", f"TQQQ_{TIC_SCOPE}m.csv")
MAX_PAGES = 2000


def load_cache() -> pd.DataFrame:
    if not os.path.exists(CACHE_PATH):
        return pd.DataFrame()
    return pd.read_csv(CACHE_PATH, index_col=0, parse_dates=True)


def save_cache(df: pd.DataFrame):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    df.to_csv(CACHE_PATH)


def main():
    client = get_client(mode="demo")
    existing = load_cache()
    print(f"[TQQQ백필] 목표: {TARGET_START.date()}까지, 기존 캐시 {len(existing)}행"
          f"{'' if existing.empty else f' ({existing.index[0]} ~ {existing.index[-1]})'}")

    all_rows = []
    cont_yn, next_key = None, None
    reached_target = False

    for page in range(MAX_PAGES):
        try:
            resp = client.fetch_page(
                api_id="usa06011", path="/api/us/chart",
                body={"stex_tp": config.TQQQ_EXCHANGE, "stk_cd": config.TQQQ_TICKER, "tic_scope": TIC_SCOPE},
                cont_yn=cont_yn, next_key=next_key,
            )
        except Exception as e:
            print(f"[TQQQ백필] 페이지 {page} 조회 실패: {e}")
            time.sleep(3)
            continue

        rows = resp.body.get("result_list", [])
        if not rows:
            print(f"[TQQQ백필] 페이지 {page}에서 빈 응답 - 더 이상 과거 데이터 없음, 종료")
            break
        all_rows.extend(rows)

        oldest_time = _parse_cntr_tm(rows[-1]["cntr_tm"])
        if page % 20 == 0:
            print(f"  페이지 {page}: 누적 {len(all_rows)}행, 현재까지 {oldest_time} 도달")

        if oldest_time <= TARGET_START:
            reached_target = True
            print(f"[TQQQ백필] 목표 날짜 도달 ({oldest_time}) - 종료")
            break

        cont_yn = resp.continuation.cont_yn
        next_key = resp.continuation.next_key
        if cont_yn != "Y":
            print(f"[TQQQ백필] 연속조회 끝(cont_yn={cont_yn}) - 이 이상 과거 데이터 없음 (도달: {oldest_time})")
            break
        time.sleep(1.2)

    if not all_rows:
        print("[TQQQ백필] 신규로 가져온 데이터 없음")
        return

    df = pd.DataFrame(all_rows)
    df["time"] = df["cntr_tm"].astype(object).map(_parse_cntr_tm)
    df["open"] = df["open_pric"].map(_num)
    df["high"] = df["high_pric"].map(_num)
    df["low"] = df["low_pric"].map(_num)
    df["close"] = df["cur_prc"].map(_num)
    df["volume"] = df["trde_qty"].map(_num)
    df = df[["time", "open", "high", "low", "close", "volume"]].set_index("time")

    combined = pd.concat([existing, df]) if not existing.empty else df
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    save_cache(combined)
    print(f"[TQQQ백필] 저장 완료: {CACHE_PATH} - 총 {len(combined)}행 "
          f"({combined.index[0]} ~ {combined.index[-1]})")
    print(f"[TQQQ백필] 목표 날짜({TARGET_START.date()}) 도달 여부: {reached_target}")


if __name__ == "__main__":
    main()
