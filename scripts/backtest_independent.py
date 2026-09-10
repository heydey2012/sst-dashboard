"""
SST 98종목 독립 계좌 백테스트 (캐시된 데이터만 사용, 키움 API 호출 없음)

scripts/backtest.py와 같은 신호/TP·SL 로직(신버전 규칙: RSI Spread Pro 콤보 시그널 +
"시그널 -> 눌림 -> 재반등" 확인 + 종가 기준 피벗 0.5 피보나치 1:1 손익비)을 그대로
재사용하되, 포트폴리오 가정만 다릅니다:

  - scripts/backtest.py: 전체 98종목이 하나의 자금(기본 100만원)을 공유하며
    동시에 1종목만 보유(선착순 경쟁)
  - 이 스크립트: 종목마다 독립적으로 1,000만원씩 시드를 배정해서 각자 따로
    시뮬레이션 (종목 간 자금 경쟁 없음, 동시에 여러 포지션 보유 가능한 것과 동일한 효과)

사용법: python3 scripts/backtest_independent.py [timeframe] [initial_capital_per_ticker]
예:     python3 scripts/backtest_independent.py 5m 10000000
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TIMEFRAME = sys.argv[1] if len(sys.argv) > 1 else "5m"
INITIAL_CAPITAL_PER_TICKER = float(sys.argv[2]) if len(sys.argv) > 2 else 10_000_000

os.environ["SST_TIMEFRAME"] = TIMEFRAME

import json

import numpy as np
import pandas as pd

import config
from tickers import TICKERS, TICKER_NAMES
from agents.collector import DataCollectorAgent
from agents.calculator import SignalCalculatorAgent
from agents.verifier import PatternVerifierAgent, _find_rebound_in_range

BUY_FEE_RATE = config.COMMISSION_RATE
SELL_FEE_RATE = config.COMMISSION_RATE
SELL_TAX_RATE = config.SELL_TAX_RATE


def find_signal_runs(sig: pd.Series):
    """combo_long True가 연속되는 구간(런)을 찾아 (start_i, end_i) 리스트로 반환."""
    runs = []
    values = sig.values
    in_run = False
    start = 0
    for i, v in enumerate(values):
        if v and not in_run:
            start = i
            in_run = True
        if not v and in_run:
            runs.append((start, i - 1))
            in_run = False
    if in_run:
        runs.append((start, len(values) - 1))
    return runs


def build_candidates(ticker: str, df: pd.DataFrame, calc: dict, verifier: PatternVerifierAgent) -> list:
    """이 종목의 전체 히스토리에서 나올 수 있었던 모든 매수 후보(반등 확인 시점)를 찾는다."""
    sig = calc["combo_long"]
    rsi_series = calc["rsi_reg_series"]
    pivot_low = calc["recent_pivot_low"]
    runs = find_signal_runs(sig)

    candidates = []
    n = len(df)
    for start_i, end_i in runs:
        sig_loc = end_i
        window_end = min(sig_loc + config.REBOUND_WINDOW_BARS, n - 1)
        if sig_loc + 1 > window_end:
            continue
        found = _find_rebound_in_range(df, sig_loc + 1, window_end)
        if not found:
            continue
        rebound_idx, reasons = found

        best_info = verifier._check_best_signal(df, sig_loc, rebound_idx, rsi_series)

        entry_price = float(df["close"].iloc[rebound_idx])
        sl_raw = pivot_low.iloc[rebound_idx] if rebound_idx < len(pivot_low) else np.nan
        if pd.isna(sl_raw) or sl_raw >= entry_price:
            sl = entry_price * (1 - config.SL_PERCENT)
        else:
            sl = float(sl_raw)
        tp = entry_price + (entry_price - sl)

        candidates.append({
            "ticker": ticker,
            "name": TICKER_NAMES.get(ticker, ticker),
            "signal_time": df.index[sig_loc],
            "entry_idx": rebound_idx,
            "entry_time": df.index[rebound_idx],
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "reasons": reasons,
            "best_signal": best_info is not None,
            "best_reason": best_info,
        })
    return candidates


def simulate_exit(df: pd.DataFrame, entry_idx: int, sl: float, tp: float):
    """entry_idx 다음 바부터 SL/TP 도달 여부를 확인. 못 만나면 데이터 끝에서 종가 청산."""
    n = len(df)
    for i in range(entry_idx + 1, n):
        low = df["low"].iloc[i]
        high = df["high"].iloc[i]
        hit_sl = low <= sl
        hit_tp = high >= tp
        if hit_sl and hit_tp:
            return i, sl, "손절(SL)"
        if hit_sl:
            return i, sl, "손절(SL)"
        if hit_tp:
            return i, tp, "익절(TP)"
    return n - 1, float(df["close"].iloc[-1]), "미청산(데이터 종료)"


def backtest_ticker(ticker: str, df: pd.DataFrame, calc: dict, verifier: PatternVerifierAgent) -> dict:
    """이 종목 하나만 독립된 INITIAL_CAPITAL_PER_TICKER 시드로 처음부터 끝까지
    복리 재투자하며 시뮬레이션 (동시에 1포지션, 청산 후에만 재진입)."""
    candidates = build_candidates(ticker, df, calc, verifier)
    # 신버전 실거래 게이트와 동일하게 [강추](best_signal) 확인된 반등만 진입
    # (백테스트는 "지금 막 확인됨"이라는 실시간 신선도(is_fresh) 개념이 적용될 수
    # 없으므로, 그 대신 강추 게이트만 실거래와 동일하게 유지)
    candidates = [c for c in candidates if c["best_signal"]]
    candidates.sort(key=lambda c: c["entry_time"])

    capital = INITIAL_CAPITAL_PER_TICKER
    available_time = None
    trades = []

    for cand in candidates:
        if available_time is not None and cand["entry_time"] < available_time:
            continue  # 이미 포지션 보유 중이라 겹치는 신호는 건너뜀

        entry_price = cand["entry_price"]
        buy_unit_cost = entry_price * (1 + BUY_FEE_RATE)
        qty = int(capital // buy_unit_cost)
        if qty < 1:
            continue

        buy_cost = qty * entry_price
        buy_fee = buy_cost * BUY_FEE_RATE
        leftover_cash = capital - buy_cost - buy_fee

        exit_idx, exit_price, exit_reason = simulate_exit(df, cand["entry_idx"], cand["sl"], cand["tp"])
        exit_time = df.index[exit_idx]

        sell_proceeds = qty * exit_price
        sell_fee = sell_proceeds * (SELL_FEE_RATE + SELL_TAX_RATE)
        capital_after = leftover_cash + sell_proceeds - sell_fee

        pnl = capital_after - capital
        pnl_pct = pnl / capital * 100

        trades.append({
            "entry_time": cand["entry_time"], "entry_price": entry_price,
            "exit_time": exit_time, "exit_price": exit_price, "exit_reason": exit_reason,
            "qty": qty, "sl": cand["sl"], "tp": cand["tp"], "best_signal": cand["best_signal"],
            "capital_before": capital, "capital_after": capital_after,
            "pnl": pnl, "pnl_pct": pnl_pct, "hold_bars": exit_idx - cand["entry_idx"],
        })

        capital = capital_after
        available_time = exit_time

    n_trades = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    peak = INITIAL_CAPITAL_PER_TICKER
    max_dd = 0.0
    cap = INITIAL_CAPITAL_PER_TICKER
    for t in trades:
        cap = t["capital_after"]
        peak = max(peak, cap)
        dd = (cap - peak) / peak * 100
        max_dd = min(max_dd, dd)

    return {
        "ticker": ticker,
        "name": TICKER_NAMES.get(ticker, ticker),
        "data_from": df.index[0].isoformat(),
        "data_to": df.index[-1].isoformat(),
        "bars": len(df),
        "initial_capital": INITIAL_CAPITAL_PER_TICKER,
        "final_capital": round(capital, 0),
        "return_pct": round((capital - INITIAL_CAPITAL_PER_TICKER) / INITIAL_CAPITAL_PER_TICKER * 100, 2),
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": round(len(wins) / n_trades * 100, 2) if n_trades else 0,
        "avg_win": round(gross_profit / len(wins), 0) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses), 0) if losses else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_pct": round(max_dd, 2),
        "n_best_signal_trades": sum(1 for t in trades if t["best_signal"]),
        "trades": [
            {**t, "entry_time": t["entry_time"].isoformat(), "exit_time": t["exit_time"].isoformat()}
            for t in trades
        ],
    }


def main():
    print(f"[독립백테스트] 타임프레임={TIMEFRAME}, 종목당 초기자금={INITIAL_CAPITAL_PER_TICKER:,.0f}원, "
          f"대상 {len(TICKERS)}종목")
    collector = DataCollectorAgent()
    calculator = SignalCalculatorAgent()
    verifier = PatternVerifierAgent()

    min_bars = config.MA_LENGTH + config.SMA_SLOW + config.PIVOT_LEFT + config.PIVOT_RIGHT

    results = []
    skipped = []

    for i, ticker in enumerate(TICKERS, start=1):
        df = collector.load_cache(ticker)
        if df.empty or len(df) < min_bars:
            skipped.append(ticker)
            continue
        try:
            calc = calculator.compute(ticker, df)
            result = backtest_ticker(ticker, df, calc, verifier)
        except Exception as e:
            print(f"  [스킵] {ticker} 처리 실패: {e}")
            skipped.append(ticker)
            continue
        results.append(result)
        if i % 20 == 0 or i == len(TICKERS):
            print(f"  진행 {i}/{len(TICKERS)}종목 처리 완료")

    if skipped:
        print(f"[독립백테스트] 캐시 데이터 부족/실패로 제외된 종목 {len(skipped)}개: {skipped}")

    total_initial = len(results) * INITIAL_CAPITAL_PER_TICKER
    total_final = sum(r["final_capital"] for r in results)
    total_trades = sum(r["n_trades"] for r in results)
    total_wins = sum(r["n_wins"] for r in results)

    data_from = min((r["data_from"] for r in results), default=None)
    data_to = max((r["data_to"] for r in results), default=None)

    summary = {
        "timeframe": TIMEFRAME,
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "data_from": data_from,
        "data_to": data_to,
        "tickers_used": len(results),
        "tickers_skipped": skipped,
        "initial_capital_per_ticker": INITIAL_CAPITAL_PER_TICKER,
        "total_initial_capital": total_initial,
        "total_final_capital": round(total_final, 0),
        "total_return_pct": round((total_final - total_initial) / total_initial * 100, 2) if total_initial else 0,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "overall_win_rate_pct": round(total_wins / total_trades * 100, 2) if total_trades else 0,
    }

    print("\n===== 독립계좌 백테스트 요약 =====")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # 대시보드용 - trades는 용량이 커서 요약 결과만 별도 저장, 상세 거래는 종목별 파일로 분리
    out_dir = os.path.join(ROOT, "docs", "results")
    os.makedirs(out_dir, exist_ok=True)

    per_ticker_summary = [
        {k: v for k, v in r.items() if k != "trades"}
        for r in sorted(results, key=lambda r: r["return_pct"], reverse=True)
    ]

    payload = {"summary": summary, "tickers": per_ticker_summary}
    with open(os.path.join(out_dir, "backtest_independent.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[독립백테스트] 요약 결과 저장: docs/results/backtest_independent.json")

    trades_dir = os.path.join(out_dir, "backtest_trades")
    os.makedirs(trades_dir, exist_ok=True)
    for r in results:
        with open(os.path.join(trades_dir, f"{r['ticker']}.json"), "w", encoding="utf-8") as f:
            json.dump(r["trades"], f, ensure_ascii=False, indent=2)
    print(f"[독립백테스트] 종목별 상세 거래 저장: docs/results/backtest_trades/*.json")


if __name__ == "__main__":
    main()
