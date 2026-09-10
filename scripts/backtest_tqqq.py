"""
TQQQ RSI Spread Pro 분할매수(DCA) 백테스트 - agents/tqqq_agent.py / scripts/backtest_dca.py와
완전히 동일한 신호·매매 로직을 scripts/backfill_tqqq.py로 모아둔 TQQQ 자체 히스토리에 적용.

- $7,450 시드, 20회 분할매수, 평단가 +3% 익절 / -10% 손절
- 수수료는 agents/tqqq_agent.py의 실거래 계산과 동일하게 반영 안 함
  (해외주식 모의투자 계좌가 막혀있어 실제 수수료율을 아직 확인 못함 - 확인되면 양쪽에 동시 반영 예정)

사용법: python3 scripts/backtest_tqqq.py [timeframe(분)]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TIC_SCOPE = sys.argv[1] if len(sys.argv) > 1 else "5"

import json

import pandas as pd

import config
from agents.calculator import compute_rsi, to_heikin_ashi, find_pivots

CACHE_PATH = os.path.join(ROOT, "data_cache", f"TQQQ_{TIC_SCOPE}m.csv")

SPLIT_COUNT = config.TQQQ_SPLIT_COUNT
TP_PCT = config.TQQQ_TP_PCT
SL_PCT = config.TQQQ_SL_PCT
RSI_LENGTH = config.TQQQ_RSI_LENGTH
MA_LENGTH = config.TQQQ_MA_LENGTH
SPREAD_LIMIT = config.TQQQ_SPREAD_LIMIT
PIVOT_LEFT = config.TQQQ_PIVOT_LEFT
PIVOT_RIGHT = config.TQQQ_PIVOT_RIGHT
FIB_RATIO = config.TQQQ_FIB_RATIO
INITIAL_CAPITAL = config.TQQQ_INITIAL_CAPITAL_USD


def compute_long_trigger_series(df: pd.DataFrame) -> pd.Series:
    """scripts/backtest_dca.py / agents/tqqq_agent.py의 compute_signal()과 동일한 계산."""
    rsi_reg = compute_rsi(df["close"], RSI_LENGTH)
    ma_reg = rsi_reg.rolling(MA_LENGTH).mean()
    spread_reg = rsi_reg - ma_reg

    ha = to_heikin_ashi(df)
    rsi_ha = compute_rsi(ha["close"], RSI_LENGTH)
    ma_ha = rsi_ha.rolling(MA_LENGTH).mean()
    spread_ha = rsi_ha - ma_ha

    long_signal = (spread_reg <= -SPREAD_LIMIT) & (spread_ha <= -SPREAD_LIMIT)

    piv_high = find_pivots(df["high"], PIVOT_LEFT, PIVOT_RIGHT, "high")
    piv_low = find_pivots(df["low"], PIVOT_LEFT, PIVOT_RIGHT, "low")
    last_high = piv_high.ffill()
    last_low = piv_low.ffill()
    fib_long_line = last_high - (last_high - last_low) * FIB_RATIO
    fib_long_pass = fib_long_line.isna() | (df["close"] <= fib_long_line)

    long_signal = long_signal & fib_long_pass

    streak = 0
    prev_streak_vals = []
    for sig in long_signal:
        prev_streak_vals.append(streak)
        streak = streak + 1 if bool(sig) else 0
    prev_streak = pd.Series(prev_streak_vals, index=df.index)

    is_bull_close = df["close"] > df["open"]
    return (prev_streak >= 1) & is_bull_close


def main():
    if not os.path.exists(CACHE_PATH):
        print(f"[TQQQ백테스트] 캐시 없음: {CACHE_PATH} - scripts/backfill_tqqq.py를 먼저 실행하세요.")
        return

    df = pd.read_csv(CACHE_PATH, index_col=0, parse_dates=True)
    print(f"[TQQQ백테스트] {len(df)}봉 로드 ({df.index[0]} ~ {df.index[-1]}), "
          f"초기자금=${INITIAL_CAPITAL:,.0f}, 분할{SPLIT_COUNT}회, 익절+{TP_PCT*100:.0f}%/손절-{SL_PCT*100:.0f}%")

    trigger = compute_long_trigger_series(df)
    split_amount = INITIAL_CAPITAL / SPLIT_COUNT

    entries = []
    trades = []
    realized_pnl = 0.0
    closes = df["close"].values
    times = df.index

    for i in range(len(df)):
        price = float(closes[i])

        if entries:
            total_qty = sum(e["qty"] for e in entries)
            avg_price = sum(e["qty"] * e["price"] for e in entries) / total_qty
            tp_price = avg_price * (1 + TP_PCT)
            sl_price = avg_price * (1 - SL_PCT)
            exit_price = exit_reason = None
            if price <= sl_price:
                exit_price, exit_reason = sl_price, "손절(평단가 대비 -10%)"
            elif price >= tp_price:
                exit_price, exit_reason = tp_price, "익절(평단가 대비 목표 도달)"

            if exit_price is not None:
                buy_cost = sum(e["qty"] * e["price"] for e in entries)
                sell_proceeds = total_qty * exit_price
                pnl = sell_proceeds - buy_cost
                pnl_pct = pnl / buy_cost * 100
                trades.append({
                    "entry_time": entries[0]["time"].isoformat(),
                    "exit_time": times[i].isoformat(),
                    "avg_entry_price": round(avg_price, 4),
                    "exit_price": round(exit_price, 4),
                    "exit_reason": exit_reason,
                    "qty": total_qty,
                    "split_count": len(entries),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                })
                realized_pnl += pnl
                entries = []
                continue

        if bool(trigger.iloc[i]) and len(entries) < SPLIT_COUNT:
            qty = int(split_amount // price)
            if qty >= 1:
                entries.append({"time": times[i], "price": price, "qty": qty})

    unrealized_pnl = 0.0
    open_position = None
    if entries:
        total_qty = sum(e["qty"] for e in entries)
        avg_price = sum(e["qty"] * e["price"] for e in entries) / total_qty
        last_price = float(closes[-1])
        unrealized_pnl = (last_price - avg_price) * total_qty
        open_position = {
            "split_count": len(entries), "qty": total_qty,
            "avg_price": round(avg_price, 4), "current_price": last_price,
            "unrealized_pnl": round(unrealized_pnl, 2),
        }

    final_capital = INITIAL_CAPITAL + realized_pnl + unrealized_pnl
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n_trades = len(trades)

    summary = {
        "strategy": "TQQQ RSI Spread Pro 분할매수(DCA) - RSI_Spread_Pro_Strategy_v1.pine 포팅",
        "ticker": "TQQQ",
        "timeframe": f"{TIC_SCOPE}m",
        "currency": "USD",
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "data_from": df.index[0].isoformat(),
        "data_to": df.index[-1].isoformat(),
        "split_count": SPLIT_COUNT,
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT,
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": round(final_capital, 2),
        "return_pct": round((final_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": round(len(wins) / n_trades * 100, 2) if n_trades else 0,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "open_position": open_position,
    }

    print("\n===== TQQQ 백테스트 요약 =====")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    out_dir = os.path.join(ROOT, "docs", "results")
    os.makedirs(out_dir, exist_ok=True)
    payload = {"summary": summary, "trades": trades}
    with open(os.path.join(out_dir, "backtest_tqqq.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[TQQQ백테스트] 저장 완료: docs/results/backtest_tqqq.json")


if __name__ == "__main__":
    main()
