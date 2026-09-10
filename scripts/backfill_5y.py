"""
98종목 5분봉 5년치 히스토리 백필 (오래 걸림 - 백그라운드로 계속 실행하는 용도).

이미 캐시에 있는(현재 ~2026-01-20부터) 데이터는 건드리지 않고, 그보다 더 과거
(기본 5년 전)부터 그 시작일 전날까지의 빈 구간만 하루씩 채워나갑니다.
data_cache/*.csv에 누적 저장되며(append_to_cache가 중복 제거), 언제 중단되어도
다음 실행 시 "현재 캐시의 최고(最古) 날짜"부터 이어서 계속합니다 - 재실행 안전.

진행 중 주기적으로(TICKERS_PER_REBUILD 종목마다) scripts/backtest_independent.py를
재실행해서 대시보드 결과가 점진적으로 갱신되도록 합니다.

사용법: python3 scripts/backfill_5y.py [years]
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("SST_TIMEFRAME", "5m")

import config
from tickers import TICKERS
from agents.collector import DataCollectorAgent

YEARS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
TARGET_START = (datetime.now() - timedelta(days=365 * YEARS)).date()
REBUILD_EVERY_N_TICKERS = 10
PYTHON_BIN = sys.executable


def earliest_cached_date(collector: DataCollectorAgent, ticker: str):
    df = collector.load_cache(ticker)
    if df.empty:
        return None
    return df.index[0].date()


def backfill_ticker(collector: DataCollectorAgent, ticker: str) -> int:
    """base_dt로 한 번 조회하면 continuation 페이징(최대 10페이지)으로 실제로는
    몇 주~몇 달 분량이 한 번에 내려오는 경우가 많음(실측 확인함) - 그래서 하루씩
    고정 스텝으로 반복하지 않고, 매 호출 후 "캐시가 실제로 얼마나 더 과거로
    갱신됐는지" 다시 확인해서 그 지점부터 이어서 요청 (불필요한 중복호출 방지)."""
    calls = 0
    while True:
        earliest = earliest_cached_date(collector, ticker)
        if earliest is None:
            print(f"  [{ticker}] 캐시가 비어있음 - 5분봉 스캔이 최소 한 번 돌아야 시작점이 생김, 건너뜀")
            return calls
        if earliest <= TARGET_START:
            return calls

        base_dt = (earliest - timedelta(days=1)).strftime("%Y%m%d")
        try:
            day_df = collector.fetch_minute_bars(ticker, base_dt)
            calls += 1
        except Exception as e:
            print(f"  [{ticker}] {base_dt} 조회 실패(중단): {e}")
            return calls

        if day_df.empty:
            print(f"  [{ticker}] {base_dt} 기준 더 이상 과거 데이터 없음 (현재 {earliest}까지 확보) - 종료")
            return calls

        collector.append_to_cache(ticker, collector._resample(day_df))
        time.sleep(config.KIWOOM_REQUEST_DELAY)

        new_earliest = earliest_cached_date(collector, ticker)
        if new_earliest is not None and new_earliest >= earliest:
            print(f"  [{ticker}] 진행 없음(earliest={new_earliest}) - 무한루프 방지, 종료")
            return calls
        print(f"    ↳ [{ticker}] {calls}회 호출 - 현재 {new_earliest}까지 확보")


def rebuild_backtest():
    print(f"[백필] 중간 재계산 - scripts/backtest_independent.py 실행")
    try:
        subprocess.run([PYTHON_BIN, os.path.join(ROOT, "scripts", "backtest_independent.py"), "5m", "10000000"],
                        cwd=ROOT, check=True)
        subprocess.run(["git", "add", "docs/results/backtest_independent.json", "docs/results/backtest_trades/"],
                        cwd=ROOT, check=True, capture_output=True)
        msg = f"SST 98종목 백테스트 갱신(백필 진행중) {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        commit = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, capture_output=True, text=True)
        if commit.returncode == 0:
            push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
            if push.returncode == 0:
                print("[백필] 대시보드 갱신 푸시 완료")
            else:
                print(f"[백필] push 실패: {push.stderr.strip()}")
        elif "nothing to commit" not in commit.stdout:
            print(f"[백필] commit 경고: {commit.stdout.strip()}")
    except Exception as e:
        print(f"[백필] 재계산/푸시 중 오류: {e}")


def main():
    print(f"[백필] 목표: {YEARS}년 전({TARGET_START})까지 98종목 5분봉 백필 시작")
    collector = DataCollectorAgent()

    for i, ticker in enumerate(TICKERS, start=1):
        print(f"[백필] ({i}/{len(TICKERS)}) {ticker} 처리 중...")
        n = backfill_ticker(collector, ticker)
        if n:
            print(f"  [{ticker}] {n}일 추가 백필 완료")
        if i % REBUILD_EVERY_N_TICKERS == 0:
            rebuild_backtest()

    rebuild_backtest()
    print("[백필] 전체 종목 5년 백필 완료")


if __name__ == "__main__":
    main()
