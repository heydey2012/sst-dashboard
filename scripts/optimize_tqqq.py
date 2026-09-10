"""
TQQQ 분할매수(DCA) 전략의 익절(TP%)/손절(SL%) 조합 최적화 스윕.

진입 신호(RSI Spread Pro 콤보 + 피보나치 필터 + 반전 트리거)는 TP/SL과 무관하게
한 번만 계산하고, 그 위에서 TP%/SL% 조합만 바꿔가며 시뮬레이션을 반복합니다.
(진입 시점 자체는 고정, 출구 조건만 스윕 - 공정한 비교)

수익률만 보면 위험을 못 보므로, 최대낙폭(MDD)과 "수익률/MDD"(Calmar류 비율)도
같이 계산해서 "수익률은 높은데 훨씬 더 위험한" 조합을 가려낼 수 있게 합니다.

사용법: python3 scripts/optimize_tqqq.py [timeframe(분)]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TIC_SCOPE = sys.argv[1] if len(sys.argv) > 1 else "5"

import json

import pandas as pd

import config
from scripts.backtest_tqqq import compute_long_trigger_series

CACHE_PATH = os.path.join(ROOT, "data_cache", f"TQQQ_{TIC_SCOPE}m.csv")
SPLIT_COUNT = config.TQQQ_SPLIT_COUNT
INITIAL_CAPITAL = config.TQQQ_INITIAL_CAPITAL_USD

TP_GRID = [0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10]
SL_GRID = [0.03, 0.05, 0.07, 0.10, 0.15, 0.20, None]  # None = 손절 없음


def simulate(df: pd.DataFrame, trigger: pd.Series, tp_pct: float, sl_pct):
    split_amount = INITIAL_CAPITAL / SPLIT_COUNT
    entries = []
    trades = []
    realized_pnl = 0.0
    closes = df["close"].values
    times = df.index

    equity = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = 0.0

    for i in range(len(df)):
        price = float(closes[i])

        if entries:
            total_qty = sum(e["qty"] for e in entries)
            avg_price = sum(e["qty"] * e["price"] for e in entries) / total_qty
            tp_price = avg_price * (1 + tp_pct)
            sl_price = avg_price * (1 - sl_pct) if sl_pct is not None else None
            exit_price = None
            if sl_price is not None and price <= sl_price:
                exit_price = sl_price
            elif price >= tp_price:
                exit_price = tp_price

            if exit_price is not None:
                buy_cost = sum(e["qty"] * e["price"] for e in entries)
                sell_proceeds = total_qty * exit_price
                pnl = sell_proceeds - buy_cost
                trades.append({"pnl": pnl, "win": pnl > 0})
                realized_pnl += pnl
                entries = []

                equity = INITIAL_CAPITAL + realized_pnl
                peak = max(peak, equity)
                max_dd = min(max_dd, (equity - peak) / peak * 100)
                continue
            else:
                # 매 봉마다 시가평가(mark-to-market) - 포지션이 살아있는 동안의 실제
                # 최대낙폭을 놓치지 않기 위해 청산 시점에만 기록하지 않고 매 봉 갱신
                mtm_unrealized = (price - avg_price) * total_qty
                mtm_equity = INITIAL_CAPITAL + realized_pnl + mtm_unrealized
                peak = max(peak, mtm_equity)
                max_dd = min(max_dd, (mtm_equity - peak) / peak * 100)

        if bool(trigger.iloc[i]) and len(entries) < SPLIT_COUNT:
            qty = int(split_amount // price)
            if qty >= 1:
                entries.append({"time": times[i], "price": price, "qty": qty})

    unrealized_pnl = 0.0
    if entries:
        total_qty = sum(e["qty"] for e in entries)
        avg_price = sum(e["qty"] * e["price"] for e in entries) / total_qty
        last_price = float(closes[-1])
        unrealized_pnl = (last_price - avg_price) * total_qty
        # 미실현 손실도 낙폭에 반영(현재 시점 기준 평가)
        mtm_equity = INITIAL_CAPITAL + realized_pnl + unrealized_pnl
        peak = max(peak, mtm_equity)
        max_dd = min(max_dd, (mtm_equity - peak) / peak * 100)

    final_capital = INITIAL_CAPITAL + realized_pnl + unrealized_pnl
    n_trades = len(trades)
    wins = [t for t in trades if t["win"]]
    return_pct = (final_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    return {
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "final_capital": round(final_capital, 2),
        "return_pct": round(return_pct, 2),
        "n_trades": n_trades,
        "win_rate_pct": round(len(wins) / n_trades * 100, 2) if n_trades else 0,
        "max_drawdown_pct": round(max_dd, 2),
        "return_over_mdd": round(return_pct / abs(max_dd), 2) if max_dd < -0.01 else None,
        "has_open_position": bool(entries),
    }


def main():
    if not os.path.exists(CACHE_PATH):
        print(f"[TQQQ최적화] 캐시 없음: {CACHE_PATH}")
        return
    df = pd.read_csv(CACHE_PATH, index_col=0, parse_dates=True)
    print(f"[TQQQ최적화] {TIC_SCOPE}분봉 {len(df)}봉 로드 ({df.index[0]} ~ {df.index[-1]})")

    trigger = compute_long_trigger_series(df)

    results = []
    for tp in TP_GRID:
        for sl in SL_GRID:
            r = simulate(df, trigger, tp, sl)
            results.append(r)

    results.sort(key=lambda r: r["return_pct"], reverse=True)

    print(f"\n===== TP/SL 스윕 결과 ({TIC_SCOPE}분봉, 수익률 순 상위 15) =====")
    print(f"{'TP%':>6} {'SL%':>6} {'수익률%':>8} {'거래':>5} {'승률%':>7} {'MDD%':>7} {'수익/MDD':>9}")
    for r in results[:15]:
        sl_disp = f"{r['sl_pct']*100:.0f}" if r["sl_pct"] is not None else "없음"
        print(f"{r['tp_pct']*100:>5.0f}% {sl_disp:>6} {r['return_pct']:>7.2f}% {r['n_trades']:>5} "
              f"{r['win_rate_pct']:>6.1f}% {r['max_drawdown_pct']:>6.2f}% "
              f"{r['return_over_mdd'] if r['return_over_mdd'] is not None else '-':>9}")

    # 리스크 조정 관점 - MDD 대비 수익률이 가장 좋은 조합 (지나치게 위험한 고수익 조합 배제)
    risk_adjusted = [r for r in results if r["return_over_mdd"] is not None]
    risk_adjusted.sort(key=lambda r: r["return_over_mdd"], reverse=True)
    print(f"\n===== 리스크 조정(수익률/MDD) 순 상위 10 =====")
    for r in risk_adjusted[:10]:
        sl_disp = f"{r['sl_pct']*100:.0f}" if r["sl_pct"] is not None else "없음"
        print(f"{r['tp_pct']*100:>5.0f}% {sl_disp:>6} {r['return_pct']:>7.2f}% {r['n_trades']:>5} "
              f"{r['win_rate_pct']:>6.1f}% {r['max_drawdown_pct']:>6.2f}% {r['return_over_mdd']:>9}")

    out_dir = os.path.join(ROOT, "docs", "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"tqqq_optimize_{TIC_SCOPE}m.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"timeframe": f"{TIC_SCOPE}m", "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n[TQQQ최적화] 전체 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
