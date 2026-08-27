"""
[검증 에이전트] Pattern Verifier Agent
- 시그널(확정 롱 삼각형) 발생 이후, 실제 "반등 캔들"이 나타났는지 캔들 패턴으로 판정합니다.
- 1차 반등 이후 재조정이 왔을 때 최우수([강추]) 조건까지 확인합니다.

[1차 반등] 캔들로 인정하는 4가지 패턴 (하나라도 해당하면 반등 확정):
  1. 전 봉(음봉)보다 몸통이 큰 양봉
  2. 양봉형 도지
  3. 음봉형 도지
  4. 연속 양봉 (config.CONSECUTIVE_BULLISH_COUNT개 이상)

[강추] 최우수 신호 (1차 반등 확정 종목에 한해 추가 확인):
  1차 반등 후 다시 눌림이 왔을 때
  - 이전 저점을 지키면서(안 깨고) 재반등 캔들 출현, 또는
  - 이전 저점을 깼지만 RSI가 이전 저점 대비 크게 낮아지지 않은 채(다이버전스) 재반등 캔들 출현
  둘 중 하나면 [강추]로 표시합니다.
"""
import config


def _classify(o, h, l, c):
    body = abs(c - o)
    rng = max(h - l, 1e-9)
    is_doji = (body / rng) <= config.DOJI_BODY_RATIO
    is_bullish = c > o
    return body, is_doji, is_bullish


def _check_candle_reasons(df, i):
    """i번째 캔들이 반등 캔들 조건(패턴 1~3)에 해당하는지 확인"""
    if i < 1:
        return []
    o, h, l, c = df["open"].iloc[i], df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
    po, pc = df["open"].iloc[i - 1], df["close"].iloc[i - 1]

    body, is_doji, is_bullish = _classify(o, h, l, c)
    prev_body = abs(pc - po)
    prev_is_bearish = pc < po

    reasons = []
    if is_bullish and prev_is_bearish and body > prev_body:
        reasons.append("전 봉(음봉)보다 몸통이 큰 양봉")
    if is_doji and is_bullish:
        reasons.append("양봉형 도지")
    if is_doji and not is_bullish:
        reasons.append("음봉형 도지")
    return reasons


def _check_consecutive_bullish(df, end_i, count):
    if end_i - count + 1 < 0:
        return False
    for idx in range(end_i - count + 1, end_i + 1):
        if not (df["close"].iloc[idx] > df["open"].iloc[idx]):
            return False
    return True


def _find_rebound_in_range(df, start_i, end_i):
    """[start_i, end_i] 범위에서 반등 캔들(패턴1~4)을 찾아 (인덱스, 이유목록) 반환. 없으면 None"""
    for i in range(start_i, end_i + 1):
        reasons = _check_candle_reasons(df, i)
        if _check_consecutive_bullish(df, i, config.CONSECUTIVE_BULLISH_COUNT):
            reasons.append(f"연속 양봉 {config.CONSECUTIVE_BULLISH_COUNT}개 이상")
        if reasons:
            return i, reasons
    return None


class PatternVerifierAgent:
    def verify(self, ticker: str, df, signal_series, rsi_series=None) -> dict:
        signal_positions = signal_series[signal_series].index
        if len(signal_positions) == 0:
            return {"ticker": ticker, "rebound_confirmed": False}

        last_signal_idx = signal_positions[-1]
        sig_loc = df.index.get_loc(last_signal_idx)

        # --- 최근성 필터: 데이터의 마지막 봉 기준으로 너무 오래된 시그널은 제외 ---
        # (캐시에는 수개월치 데이터가 쌓여있을 수 있어, 몇 달 전 시그널이 "오늘의 발견"으로
        #  매일 반복 보고되는 것을 방지)
        bars_since_signal = (len(df) - 1) - sig_loc
        if bars_since_signal > config.RECENT_SIGNAL_MAX_AGE_BARS:
            return {"ticker": ticker, "rebound_confirmed": False, "reason": "stale_signal"}

        window_end = min(sig_loc + config.REBOUND_WINDOW_BARS, len(df) - 1)

        found = _find_rebound_in_range(df, sig_loc + 1, window_end)
        if not found:
            return {
                "ticker": ticker,
                "rebound_confirmed": False,
                "signal_time": df.index[sig_loc],
                "signal_price": round(float(df["close"].iloc[sig_loc]), 0),
            }

        rebound_idx, reasons = found
        bars_since_rebound = (len(df) - 1) - rebound_idx

        result = {
            "ticker": ticker,
            "rebound_confirmed": True,
            "signal_time": df.index[sig_loc],
            "signal_price": round(float(df["close"].iloc[sig_loc]), 0),
            "rebound_time": df.index[rebound_idx],
            "rebound_price": round(float(df["close"].iloc[rebound_idx]), 0),
            "reasons": reasons,
            "bars_after_signal": rebound_idx - sig_loc,
            "bars_since_rebound": bars_since_rebound,
            "is_fresh": bars_since_rebound <= config.FRESH_REBOUND_MAX_AGE_BARS,
            "best_signal": False,
        }

        # --- [강추] 최우수 신호 2차 확인 ---
        best_info = self._check_best_signal(df, sig_loc, rebound_idx, rsi_series)
        if best_info:
            result["best_signal"] = True
            result["best_reason"] = best_info

        return result

    def _check_best_signal(self, df, sig_loc, rebound_idx, rsi_series):
        # 1차 저점 = 시그널~1차 반등 사이의 최저 저가
        pre_window = df.iloc[sig_loc:rebound_idx + 1]
        first_low = pre_window["low"].min()
        first_low_idx = sig_loc + pre_window["low"].values.argmin()

        # 2차 구간: 1차 반등 이후 재조정 + 재반등을 찾을 범위
        sec_start = rebound_idx + 1
        sec_end = min(rebound_idx + config.SECONDARY_WINDOW_BARS, len(df) - 1)
        if sec_start > sec_end:
            return None

        sec_window = df.iloc[sec_start:sec_end + 1]
        if sec_window.empty:
            return None

        retest_low = sec_window["low"].min()
        retest_idx = sec_start + sec_window["low"].values.argmin()

        # 재조정 구간 중 retest_idx 이후로 재반등 캔들이 있는지 확인
        second_found = _find_rebound_in_range(df, retest_idx, sec_end)
        if not second_found:
            return None
        second_rebound_idx, _ = second_found

        if retest_low >= first_low:
            return f"1차 저점({first_low:,.0f}) 지지 후 재반등"

        if rsi_series is not None and first_low_idx < len(rsi_series) and retest_idx < len(rsi_series):
            first_low_rsi = rsi_series.iloc[first_low_idx]
            retest_rsi = rsi_series.iloc[retest_idx]
            if retest_rsi >= first_low_rsi - config.RSI_DIVERGENCE_TOLERANCE:
                return (
                    f"저점 이탈({first_low:,.0f}→{retest_low:,.0f})했으나 "
                    f"RSI 다이버전스 동반 재반등 (RSI {first_low_rsi:.1f}→{retest_rsi:.1f})"
                )

        return None

    def verify_all(self, calc_results: list, raw_data: dict) -> list:
        verified = []
        for r in calc_results:
            df = raw_data.get(r["ticker"])
            if df is None:
                continue
            rsi_series = r.get("rsi_reg_series")
            result = self.verify(r["ticker"], df, r["combo_long"], rsi_series)
            if result.get("rebound_confirmed"):
                result["has_active_entry"] = bool(r["long_valid"].iloc[-1]) if len(r["long_valid"]) else False
                verified.append(result)
        return verified
