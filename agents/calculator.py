"""
[계산 에이전트] Signal Calculator Agent
원본: "RSI Spread Pro Indicator v7.1 (연속삼각형 반전진입 + 피보나치 단계별 익절 시각화)" Pine Script v6 포팅

포팅 시 근사/단순화한 지점 (원본과 100% 동일하지 않을 수 있음):
1. ta.pivothigh/pivotlow는 Pine에서는 pivotRightBars가 지나야 확정되지만,
   여기서는 오프라인 배치 계산이라 피벗 발생 즉시 반영합니다.
   (스캐너 용도라 실시간 반복(repaint) 문제와 무관 - 매일 장 마감 후 배치 실행 전제)
2. 피벗 판정은 "해당 봉이 좌우 윈도우 내 최고/최저값과 같다(>=,<=)" 조건으로 근사.
   Pine 원본은 엄격한 대소비교를 사용할 수 있어 극소수 케이스에서 피벗 위치가 다를 수 있음.
"""
import numpy as np
import pandas as pd
import config


# ==========================================================================
# 기본 지표 계산
# ==========================================================================

def compute_rsi(series: pd.Series, length: int) -> pd.Series:
    """Pine ta.rsi와 동일한 RMA(지수이동평균) 기반 RSI"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = np.empty(len(df))
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    close_vals = ha["close"].values
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + close_vals[i - 1]) / 2
    ha["open"] = ha_open
    ha["high"] = pd.concat([df["high"], ha["open"], ha["close"]], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha["open"], ha["close"]], axis=1).min(axis=1)
    return ha


def find_pivots(series: pd.Series, left: int, right: int, mode: str) -> pd.Series:
    """ta.pivothigh / ta.pivotlow 근사 구현 (피벗 발생 바에 값 기록, 그 외 NaN)"""
    n = len(series)
    result = pd.Series(np.nan, index=series.index)
    values = series.values
    for i in range(left, n - right):
        window = values[i - left: i + right + 1]
        center = values[i]
        if mode == "high" and center >= window.max():
            result.iloc[i] = center
        elif mode == "low" and center <= window.min():
            result.iloc[i] = center
    return result


# ==========================================================================
# 계산 에이전트
# ==========================================================================

class SignalCalculatorAgent:
    def compute(self, ticker: str, df: pd.DataFrame) -> dict:
        df = df.copy()
        n = len(df)

        # --- (A) 일반봉 RSI / Spread ---
        rsi_reg = compute_rsi(df["close"], config.RSI_LENGTH)
        ma_reg = rsi_reg.rolling(config.MA_LENGTH).mean()
        spread_reg = rsi_reg - ma_reg

        sma_fast_reg = df["close"].rolling(config.SMA_FAST).mean()
        sma_slow_reg = df["close"].rolling(config.SMA_SLOW).mean()
        trend_bull_reg = sma_fast_reg > sma_slow_reg

        # --- (B) 하이킨아시 RSI / Spread ---
        ha = to_heikin_ashi(df)
        rsi_ha = compute_rsi(ha["close"], config.RSI_LENGTH)
        ma_ha = rsi_ha.rolling(config.MA_LENGTH).mean()
        spread_ha = rsi_ha - ma_ha

        sma_fast_ha = ha["close"].rolling(config.SMA_FAST).mean()
        sma_slow_ha = ha["close"].rolling(config.SMA_SLOW).mean()
        trend_bull_ha = sma_fast_ha > sma_slow_ha

        trend_agree_bull = trend_bull_reg & trend_bull_ha
        trend_agree_bear = (~trend_bull_reg) & (~trend_bull_ha)

        # --- (C) 신호 판정: 부호 방향 그대로 사용 (절대값 아님 - 원본 핵심 포인트) ---
        long_signal_reg = spread_reg <= -config.SPREAD_LIMIT
        short_signal_reg = spread_reg >= config.SPREAD_LIMIT
        long_signal_ha = spread_ha <= -config.SPREAD_LIMIT
        short_signal_ha = spread_ha >= config.SPREAD_LIMIT

        combo_long_raw = long_signal_reg & long_signal_ha
        combo_short_raw = short_signal_reg & short_signal_ha

        # --- (D) 피보나치 0.382 필터 (진입 게이트) ---
        piv_high = find_pivots(df["high"], config.PIVOT_LEFT, config.PIVOT_RIGHT, "high")
        piv_low = find_pivots(df["low"], config.PIVOT_LEFT, config.PIVOT_RIGHT, "low")
        last_high = piv_high.ffill()
        last_low = piv_low.ffill()

        fib_range = last_high - last_low
        fib_long_line = last_high - fib_range * 0.382
        fib_short_line = last_low + fib_range * 0.382

        if config.USE_FIB_FILTER:
            fib_long_pass = fib_long_line.isna() | (df["close"] <= fib_long_line)
            fib_short_pass = fib_short_line.isna() | (df["close"] >= fib_short_line)
        else:
            fib_long_pass = pd.Series(True, index=df.index)
            fib_short_pass = pd.Series(True, index=df.index)

        if config.REQUIRE_TREND_AGREE:
            combo_long = combo_long_raw & trend_agree_bull & fib_long_pass
            combo_short = combo_short_raw & trend_agree_bear & fib_short_pass
        else:
            combo_long = combo_long_raw & fib_long_pass
            combo_short = combo_short_raw & fib_short_pass

        # --- (E) RSI 극값 (라벨/보고용 - 진입 조건과 무관) ---
        long_rsi_extreme = (rsi_reg <= config.RSI_LONG_LEVEL) & (rsi_ha <= config.RSI_LONG_LEVEL)
        short_rsi_extreme = (rsi_reg >= config.RSI_SHORT_LEVEL) & (rsi_ha >= config.RSI_SHORT_LEVEL)

        # --- (F) 연속 신호 스트릭 + 진입 트리거 + TP/SL (순차 계산) ---
        bars_before_streak = self._minutes_to_bars(config.PRE_MOVE_MINUTES, config.TIMEFRAME)

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        open_ = df["open"].values
        cl_long = combo_long.values
        cl_short = combo_short.values
        tb_reg = trend_bull_reg.fillna(False).values

        long_streak = np.zeros(n, dtype=int)
        short_streak = np.zeros(n, dtype=int)
        long_valid = np.zeros(n, dtype=bool)
        short_valid = np.zeros(n, dtype=bool)
        long_tp = np.full(n, np.nan)
        long_sl = np.full(n, np.nan)
        short_tp = np.full(n, np.nan)
        short_sl = np.full(n, np.nan)

        for i in range(1, n):
            long_streak[i] = min(long_streak[i - 1] + 1, config.MAX_STREAK) if cl_long[i] else 0
            short_streak[i] = min(short_streak[i - 1] + 1, config.MAX_STREAK) if cl_short[i] else 0

            prev_long_streak = long_streak[i - 1]
            prev_short_streak = short_streak[i - 1]

            is_bull_close = close[i] > open_[i]
            is_bear_close = close[i] < open_[i]

            long_trigger_ok = prev_long_streak >= 1 and is_bull_close
            short_trigger_ok = prev_short_streak >= 1 and is_bear_close

            if long_trigger_ok:
                lookback_len = max(prev_long_streak + bars_before_streak + 1, 1)
                low_len = max(prev_long_streak + 1, 1)
                swing_high = high[max(0, i - lookback_len + 1): i + 1].max()
                swing_low = low[max(0, i - low_len + 1): i + 1].min()

                ratio_base = (config.FIB_RATIO_1 if prev_long_streak == 1
                              else config.FIB_RATIO_2 if prev_long_streak == 2
                              else config.FIB_RATIO_3)
                ratio_boosted = (config.FIB_RATIO_2 if prev_long_streak == 1
                                 else config.FIB_RATIO_3 if prev_long_streak == 2
                                 else config.FIB_RATIO_4)
                ratio = ratio_boosted if (config.USE_TREND_TP_BOOST and tb_reg[i]) else ratio_base

                tp = swing_low + (swing_high - swing_low) * ratio
                sl = swing_low * (1 - config.SL_PERCENT / 100)

                if tp > close[i] and sl < close[i]:
                    long_valid[i] = True
                    long_tp[i] = tp
                    long_sl[i] = sl
                    long_streak[i] = 0  # 진입 확정 시 스트릭 리셋 (원본과 동일)

            if short_trigger_ok:
                lookback_len = max(prev_short_streak + bars_before_streak + 1, 1)
                high_len = max(prev_short_streak + 1, 1)
                swing_high = high[max(0, i - high_len + 1): i + 1].max()
                swing_low = low[max(0, i - lookback_len + 1): i + 1].min()

                ratio_base = (config.FIB_RATIO_1 if prev_short_streak == 1
                              else config.FIB_RATIO_2 if prev_short_streak == 2
                              else config.FIB_RATIO_3)
                ratio_boosted = (config.FIB_RATIO_2 if prev_short_streak == 1
                                 else config.FIB_RATIO_3 if prev_short_streak == 2
                                 else config.FIB_RATIO_4)
                ratio = ratio_boosted if (config.USE_TREND_TP_BOOST and not tb_reg[i]) else ratio_base

                tp = swing_high - (swing_high - swing_low) * ratio
                sl = swing_high * (1 + config.SL_PERCENT / 100)

                if tp < close[i] and sl > close[i]:
                    short_valid[i] = True
                    short_tp[i] = tp
                    short_sl[i] = sl
                    short_streak[i] = 0

        idx = df.index
        return {
            "ticker": ticker,
            "timestamp": idx[-1],
            "close": float(close[-1]),
            "rsi_reg": float(rsi_reg.iloc[-1]),
            "rsi_ha": float(rsi_ha.iloc[-1]),
            "long_rsi_extreme": bool(long_rsi_extreme.iloc[-1]),
            "short_rsi_extreme": bool(short_rsi_extreme.iloc[-1]),
            "combo_long": pd.Series(cl_long, index=idx),
            "combo_short": pd.Series(cl_short, index=idx),
            "rsi_reg_series": rsi_reg,  # 검증 에이전트의 다이버전스 판정용 전체 RSI 시리즈
            "long_streak": pd.Series(long_streak, index=idx),
            "short_streak": pd.Series(short_streak, index=idx),
            "long_valid": pd.Series(long_valid, index=idx),
            "short_valid": pd.Series(short_valid, index=idx),
            "long_tp": pd.Series(long_tp, index=idx),
            "long_sl": pd.Series(long_sl, index=idx),
            "short_tp": pd.Series(short_tp, index=idx),
            "short_sl": pd.Series(short_sl, index=idx),
            "long_signal": bool(cl_long[-1]),   # 최신 봉 확정 롱 신호(삼각형) 여부
            "short_signal": bool(cl_short[-1]),
        }

    @staticmethod
    def _minutes_to_bars(minutes: int, timeframe: str) -> int:
        tf_minutes = config.TIMEFRAME_MINUTES.get(timeframe, 240)
        return max(round(minutes / tf_minutes), 1)

    def compute_all(self, data: dict) -> list:
        results = []
        min_bars = config.MA_LENGTH + config.SMA_SLOW + config.PIVOT_LEFT + config.PIVOT_RIGHT
        for ticker, df in data.items():
            if df is None or len(df) < min_bars:
                continue
            try:
                results.append(self.compute(ticker, df))
            except Exception as e:
                print(f"[Calculator] {ticker} 계산 실패: {e}")
        return results
