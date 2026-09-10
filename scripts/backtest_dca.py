"""
98종목 RSI Spread Pro 분할매수(DCA) 백테스트 - agents/tqqq_agent.py와 완전히 동일한
신호/매매 로직(RSI_Spread_Pro_Strategy_v1.pine 포팅)을 캐시된 국내 5분봉 데이터에 적용.

- 일반봉 RSI스프레드 + 하이킨아시 RSI스프레드가 동시에 -15 이하 + 피보나치 0.382
  필터 통과 + 연속신호 반전(양봉 마감) 트리거 -> 분할매수(최대 20회, 물타기)
- 평단가 대비 +3% 익절 / -10% 손절 (원본 Pine엔 손절이 없었지만 물림 리스크 관리를 위해 추가)
- 종목마다 독립적으로 10,000,000원씩 시드 (종목 간 자금 공유 없음)
- 분할 매수 금액은 Pine 원본과 동일하게 "총자금/분할횟수" 고정 (사이클마다 복리 재투자 안 함,
  실현손익은 누적되지만 다음 분할매수 사이클의 투입금액 자체는 항상 동일)

사용법: python3 scripts/backtest_dca.py [timeframe] [initial_capital_per_ticker]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TIMEFRAME = sys.argv[1] if len(sys.argv) > 1 else "5m"
INITIAL_CAPITAL_PER_TICKER = float(sys.argv[2]) if len(sys.argv) > 2 else 10_000_000

os.environ["SST_TIMEFRAME"] = TIMEFRAME

import json

import pandas as pd

import config
from tickers import TICKERS, TICKER_NAMES
from agents.collector import DataCollectorAgent
from agents.calculator import compute_rsi, to_heikin_ashi, find_pivots

SPLIT_COUNT = config.TQQQ_SPLIT_COUNT      # 20
TP_PCT = config.DCA_TP_PCT                 # 0.03 (TQQQ와 별도 - config.DCA_TP_PCT 참고)
SL_PCT = config.DCA_SL_PCT                 # 0.10
RSI_LENGTH = config.TQQQ_RSI_LENGTH
MA_LENGTH = config.TQQQ_MA_LENGTH
SPREAD_LIMIT = config.TQQQ_SPREAD_LIMIT
PIVOT_LEFT = config.TQQQ_PIVOT_LEFT
PIVOT_RIGHT = config.TQQQ_PIVOT_RIGHT
FIB_RATIO = config.TQQQ_FIB_RATIO

BUY_FEE_RATE = config.COMMISSION_RATE
SELL_FEE_RATE = config.COMMISSION_RATE
SELL_TAX_RATE = config.SELL_TAX_RATE


def compute_long_trigger_series(df: pd.DataFrame) -> pd.Series:
    """agents/tqqq_agent.py의 compute_signal()과 완전히 동일한 계산(전 구간 벡터화 버전).
    TQQQ 실거래 코드와 로직이 100% 일치해야 하므로 별도로 재구현하지 않고 그대로 복제."""
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


def backtest_ticker(ticker: str, df: pd.DataFrame) -> dict:
    trigger = compute_long_trigger_series(df)
    split_amount = INITIAL_CAPITAL_PER_TICKER / SPLIT_COUNT

    entries = []       # 현재 진행 중인 분할매수 사이클의 개별 매수 내역
    trades = []         # 완료된(익절된) 사이클
    realized_pnl = 0.0

    closes = df["close"].values
    opens = df["open"].values
    times = df.index

    for i in range(len(df)):
        price = float(closes[i])

        if entries:
            total_qty = sum(e["qty"] for e in entries)
            avg_price = sum(e["qty"] * e["price"] for e in entries) / total_qty
            tp_price = avg_price * (1 + TP_PCT)
            sl_price = avg_price * (1 - SL_PCT)
            # 동시 도달 시 손절 우선(보수적 처리) - 실제로는 tp/sl 거리가 멀어(3%/10%) 한 봉에서
            # 둘 다 걸리는 경우는 사실상 없음
            exit_price = None
            exit_reason = None
            if price <= sl_price:
                exit_price, exit_reason = sl_price, "손절(평단가 대비 -10%)"
            elif price >= tp_price:
                exit_price, exit_reason = tp_price, "익절(평단가 대비 목표 도달)"

            if exit_price is not None:
                buy_cost = sum(e["qty"] * e["price"] for e in entries)
                sell_proceeds = total_qty * exit_price
                buy_fee = buy_cost * BUY_FEE_RATE
                sell_fee = sell_proceeds * (SELL_FEE_RATE + SELL_TAX_RATE)
                pnl = sell_proceeds - buy_cost - buy_fee - sell_fee
                pnl_pct = pnl / buy_cost * 100
                trades.append({
                    "entry_time": entries[0]["time"].isoformat(),
                    "exit_time": times[i].isoformat(),
                    "avg_entry_price": avg_price,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "qty": total_qty,
                    "split_count": len(entries),
                    "pnl": round(pnl, 0),
                    "pnl_pct": round(pnl_pct, 2),
                })
                realized_pnl += pnl
                entries = []
                continue  # 같은 봉에서 청산과 재진입을 동시에 하지 않음

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
            "unrealized_pnl": round(unrealized_pnl, 0),
        }

    final_capital = INITIAL_CAPITAL_PER_TICKER + realized_pnl + unrealized_pnl
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n_trades = len(trades)

    return {
        "ticker": ticker,
        "name": TICKER_NAMES.get(ticker, ticker),
        "data_from": df.index[0].isoformat(),
        "data_to": df.index[-1].isoformat(),
        "bars": len(df),
        "initial_capital": INITIAL_CAPITAL_PER_TICKER,
        "final_capital": round(final_capital, 0),
        "return_pct": round((final_capital - INITIAL_CAPITAL_PER_TICKER) / INITIAL_CAPITAL_PER_TICKER * 100, 2),
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": round(len(wins) / n_trades * 100, 2) if n_trades else 0,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 0) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 0) if losses else 0,
        "open_position": open_position,
        "trades": trades,
    }


def main():
    print(f"[DCA백테스트] 타임프레임={TIMEFRAME}, 종목당 초기자금={INITIAL_CAPITAL_PER_TICKER:,.0f}원, "
          f"분할{SPLIT_COUNT}회, 익절{TP_PCT*100:.0f}%, 손절없음, 대상 {len(TICKERS)}종목")
    collector = DataCollectorAgent()
    min_bars = MA_LENGTH + PIVOT_LEFT + PIVOT_RIGHT + 5

    results = []
    skipped = []
    for i, ticker in enumerate(TICKERS, start=1):
        df = collector.load_cache(ticker)
        if df.empty or len(df) < min_bars:
            skipped.append(ticker)
            continue
        try:
            result = backtest_ticker(ticker, df)
        except Exception as e:
            print(f"  [스킵] {ticker} 처리 실패: {e}")
            skipped.append(ticker)
            continue
        results.append(result)
        if i % 20 == 0 or i == len(TICKERS):
            print(f"  진행 {i}/{len(TICKERS)}종목 처리 완료")

    if skipped:
        print(f"[DCA백테스트] 제외된 종목 {len(skipped)}개: {skipped}")

    total_initial = len(results) * INITIAL_CAPITAL_PER_TICKER
    total_final = sum(r["final_capital"] for r in results)
    total_trades = sum(r["n_trades"] for r in results)
    total_wins = sum(r["n_wins"] for r in results)
    open_positions = sum(1 for r in results if r["open_position"])

    data_from = min((r["data_from"] for r in results), default=None)
    data_to = max((r["data_to"] for r in results), default=None)

    summary = {
        "strategy": "RSI Spread Pro 분할매수(DCA) - RSI_Spread_Pro_Strategy_v1.pine 포팅, 평단가 -10% 손절 추가",
        "timeframe": TIMEFRAME,
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "data_from": data_from,
        "data_to": data_to,
        "tickers_used": len(results),
        "tickers_skipped": skipped,
        "split_count": SPLIT_COUNT,
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT,
        "initial_capital_per_ticker": INITIAL_CAPITAL_PER_TICKER,
        "total_initial_capital": total_initial,
        "total_final_capital": round(total_final, 0),
        "total_return_pct": round((total_final - total_initial) / total_initial * 100, 2) if total_initial else 0,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "overall_win_rate_pct": round(total_wins / total_trades * 100, 2) if total_trades else 0,
        "open_positions_count": open_positions,
    }

    print("\n===== DCA 백테스트 요약 =====")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    out_dir = os.path.join(ROOT, "docs", "results")
    os.makedirs(out_dir, exist_ok=True)

    per_ticker_summary = [
        {k: v for k, v in r.items() if k != "trades"}
        for r in sorted(results, key=lambda r: r["return_pct"], reverse=True)
    ]
    payload = {"summary": summary, "tickers": per_ticker_summary}
    with open(os.path.join(out_dir, "backtest_independent.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[DCA백테스트] 요약 결과 저장: docs/results/backtest_independent.json")

    trades_dir = os.path.join(out_dir, "backtest_trades")
    os.makedirs(trades_dir, exist_ok=True)
    for r in results:
        with open(os.path.join(trades_dir, f"{r['ticker']}.json"), "w", encoding="utf-8") as f:
            json.dump(r["trades"], f, ensure_ascii=False, indent=2)
    print(f"[DCA백테스트] 종목별 상세 거래 저장: docs/results/backtest_trades/*.json")


if __name__ == "__main__":
    main()
