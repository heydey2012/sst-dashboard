"""
[매매 에이전트] Trader Agent - 키움증권 모의투자 자동매매

- config.TRADER_TIMEFRAME 스캔에서 나온 [강추](best_signal) 신호 중, 반등이 오늘 날짜에
  발생한 것(is_fresh)만 매수 트리거로 사용
- 손절가/익절가는 매수 시점 기준 직전 확정 피벗 저점/고점(종가 기준, 피보나치 0.5 대칭)을 사용,
  유효한 피벗이 없으면 6% 고정 손절 + 손절폭 1:1 익절로 대체
- positions.json 에 보유 포지션을, trade_log.json 에 청산된 거래 내역을 기록

*** 안전장치 ***
매 주문 직전 kiwoom 클라이언트의 auth.mode 가 "demo"가 아니면 즉시 예외를 던지고 중단합니다.
config.TRADER_ENABLED 설정과는 별개의 이중 체크입니다 (실계좌로 전환됐을 때 자동매매가
실수로 그대로 켜져 있는 사고를 막기 위함).
"""
import json
import os
import subprocess
import time
from datetime import datetime
from datetime import time as dtime

try:
    from kiwoom import get_client, KiwoomError
except ImportError:
    get_client = None
    KiwoomError = Exception

import config
from tickers import TICKER_NAMES
from agents.notifier import NotifierAgent

ORDER_API_ID_BUY = "kt10000"
ORDER_API_ID_SELL = "kt10001"
ORDER_API_URL = "/api/dostk/ordr"
QUOTE_API_ID = "ka10001"
QUOTE_API_URL = "/api/dostk/stkinfo"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TraderAgent:
    def __init__(self, paper: bool = False):
        """paper=True면 구버전(페이퍼) 전략: 실주문 없이 시세만으로 가상매매하고,
        별도 포지션/자금(config.LEGACY_*)을 씀. 실계좌가 1개뿐이라 신버전(실주문)과
        구버전을 같은 계좌로 동시에 돌릴 수 없어서 나눔."""
        self.paper = paper
        self.notifier = NotifierAgent()
        # mode="demo"를 명시하면 kiwoom 라이브러리가 프로필(macOS 키체인) 대신
        # 환경변수(APP_KEY_MOCK/APP_SECRET_MOCK, .env)를 먼저 확인함 - cron은
        # 로그인 키체인이 잠기면 접근을 못 하므로 이렇게 해야 안정적으로 동작함.
        self.client = get_client(mode="demo") if get_client else None

        positions_file = config.LEGACY_POSITIONS_FILE if paper else config.POSITIONS_FILE
        trade_log_file = config.LEGACY_TRADE_LOG_FILE if paper else config.TRADE_LOG_FILE
        self._positions_path = os.path.join(ROOT, positions_file)
        self._trade_log_path = os.path.join(ROOT, trade_log_file)
        self._initial_capital = config.LEGACY_INITIAL_CAPITAL if paper else config.INITIAL_CAPITAL
        self._capital_per_trade = config.LEGACY_CAPITAL_PER_TRADE if paper else config.CAPITAL_PER_TRADE
        self._max_positions = config.LEGACY_MAX_POSITIONS if paper else config.MAX_POSITIONS
        self._dashboard_positions_file = "positions_v1.json" if paper else "positions.json"
        self._dashboard_trade_log_file = "trade_log_v1.json" if paper else "trade_log.json"

    # ------------------------------------------------------------------
    # 안전장치
    # ------------------------------------------------------------------
    def _assert_demo_mode(self):
        if self.client is None:
            raise RuntimeError("[Trader] kiwoom 클라이언트가 초기화되지 않았습니다.")
        mode = getattr(self.client.auth, "mode", None)
        if mode != "demo":
            raise RuntimeError(
                f"[Trader] 안전장치 발동: 계좌 모드가 'demo'가 아니라 '{mode}'입니다. "
                "실계좌 자동매매 사고를 방지하기 위해 주문을 중단합니다."
            )

    @staticmethod
    def _is_late_session_freeze() -> bool:
        """매일 15시 이후인지 (15:10 전량 강제청산 전, 신규 진입 미리 차단 - 당일 청산 원칙이라
        포지션을 하루도 안 넘기기 위해 평일마다 적용, 요일/연휴 구분 없음)."""
        return datetime.now().time() >= dtime(15, 0)

    # ------------------------------------------------------------------
    # 로컬 상태 (보유 포지션 / 체결 내역)
    # ------------------------------------------------------------------
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
    # 키움 API 호출
    # ------------------------------------------------------------------
    def _net_pnl(self, entry_price: float, exit_price: float, qty: int) -> tuple:
        """매수+매도 수수료(왕복)와 매도 시 세금을 반영한 순손익.
        실계좌 ka10077 조회로 검증한 config.COMMISSION_RATE/SELL_TAX_RATE 사용."""
        buy_cost = entry_price * qty
        sell_proceeds = exit_price * qty
        commission = (buy_cost + sell_proceeds) * config.COMMISSION_RATE
        tax = sell_proceeds * config.SELL_TAX_RATE
        pnl = sell_proceeds - buy_cost - commission - tax
        pnl_pct = pnl / buy_cost * 100
        return pnl, pnl_pct, commission, tax

    def _order(self, api_id: str, ticker: str, qty: int) -> dict:
        if self.paper:
            # 구버전(페이퍼) 전략 - 실제 주문을 넣지 않고 항상 체결 성공으로 시뮬레이션
            return {"return_code": 0, "ord_no": f"PAPER-{ticker}-{int(time.time())}"}
        self._assert_demo_mode()
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": ticker,
            "ord_qty": str(qty),
            "trde_tp": "3",  # 시장가
            "ord_uv": "",
            "cond_uv": "",
        }
        response = self.client.fetch_page(api_id=api_id, path=ORDER_API_URL, body=body,
                                           cont_yn=None, next_key=None)
        return response.body

    def _current_price(self, ticker: str, max_retries: int = 4):
        if self.client is None:
            return None
        delay = 1.5
        for attempt in range(max_retries):
            try:
                response = self.client.fetch_page(api_id=QUOTE_API_ID, path=QUOTE_API_URL,
                                                   body={"stk_cd": ticker}, cont_yn=None, next_key=None)
                raw = response.body.get("cur_prc")
                if raw is None:
                    return None
                return abs(float(raw))
            except Exception as e:
                is_rate_limit = "429" in str(e) or "1700" in str(e) or "허용된" in str(e)
                if is_rate_limit and attempt < max_retries - 1:
                    wait = delay * (attempt + 1)
                    time.sleep(wait)
                    continue
                print(f"[Trader] {ticker} 현재가 조회 실패: {e}")
                return None

    # ------------------------------------------------------------------
    # 진입 (매수) - leader.py가 config.TRADER_TIMEFRAME 스캔 배치마다 호출
    # ------------------------------------------------------------------
    def process_entries(self, verified: list, calc_by_ticker: dict):
        if self.client is None:
            print("[Trader] kiwoom 클라이언트 없음 - 자동매매 진입을 건너뜁니다.")
            return

        end_date = datetime.strptime(config.TRADING_END_DATE, "%Y-%m-%d").date()
        if datetime.now().date() > end_date:
            print(f"[Trader] 모의매매 종료일({config.TRADING_END_DATE}) 이후 - 신규 진입을 건너뜁니다 "
                  f"(기존 보유 종목 청산 감시는 계속됩니다).")
            return

        if self._is_late_session_freeze():
            print("[Trader] 15시 이후 - 당일 청산 원칙(15:10 전량 강제청산 예정)에 따라 신규 진입을 건너뜁니다.")
            return

        positions = self._load_positions()

        for v in verified:
            if self.paper:
                # 구버전: 1차 반등만 확인되면 매수(강추/근접조건 없음) - 개편 전 원래 게이트
                gate_ok = bool(v.get("is_fresh"))
            else:
                # 신버전: [강추](재조정 후 재반등까지 확인) + 그 반등이 오늘 날짜여야 매수
                gate_ok = bool(v.get("best_signal") and v.get("is_fresh"))
            if not gate_ok:
                continue

            ticker = v["ticker"]
            name = TICKER_NAMES.get(ticker, ticker)

            if ticker in positions:
                continue  # 이미 보유 중 - 중복 진입 방지
            if len(positions) >= self._max_positions:
                print(f"[Trader] 최대 동시보유({self._max_positions}종목) 도달 - {ticker} 진입 건너뜀")
                break

            calc = calc_by_ticker.get(ticker)
            if calc is None:
                continue

            price = calc.get("close")
            if not price:
                continue

            # 고점~저점을 잇는 피보나치 구간에서 0.5(중간값) 기준 1:1 대칭 라인을 익절/손절로 사용.
            # 즉 손절가 = 직전 확정 피벗 저점(0% 라인), 익절가 = 직전 확정 피벗 고점(100% 라인).
            # (0.5 라인이 정확히 두 값의 중간이므로 자동으로 손익비 1:1이 됨)
            pivot_low_series = calc.get("recent_pivot_low")
            pivot_high_series = calc.get("recent_pivot_high")
            pivot_low_raw = float(pivot_low_series.iloc[-1]) if pivot_low_series is not None and len(pivot_low_series) else None
            pivot_high_raw = float(pivot_high_series.iloc[-1]) if pivot_high_series is not None and len(pivot_high_series) else None
            if pivot_low_raw is not None and pd_isnan(pivot_low_raw):
                pivot_low_raw = None
            if pivot_high_raw is not None and pd_isnan(pivot_high_raw):
                pivot_high_raw = None

            sl_valid = pivot_low_raw is not None and pivot_low_raw < price
            tp_valid = pivot_high_raw is not None and pivot_high_raw > price

            if sl_valid and tp_valid:
                # 피벗 저점/고점 둘 다 유효 - 원래 설계대로 그대로 사용
                sl, sl_basis = pivot_low_raw, "피벗저점"
                tp, tp_basis = pivot_high_raw, "피벗고점"
            elif tp_valid:
                # 피벗고점만 유효(저점은 아직 없거나 현재가보다 높은 비정상값) -
                # 임의의 고정% 손절 대신, 유효한 피벗고점과 대칭(1:1)이 되도록 손절을 역산
                # (그래야 익절기준 피벗고점인데 손절기준만 고정%로 따로 노는 비대칭이 안 생김)
                tp, tp_basis = pivot_high_raw, "피벗고점"
                sl, sl_basis = price - (tp - price), "피벗고점 대비 1:1"
            elif sl_valid:
                # 피벗저점만 유효 - 손절폭과 동일하게(손익비 1:1) 익절을 역산
                sl, sl_basis = pivot_low_raw, "피벗저점"
                tp, tp_basis = price + (price - sl), "피벗저점 대비 1:1"
            else:
                # 둘 다 무효(데이터 초기 등) - config.SL_PERCENT 기반 손절 + 1:1 익절
                sl, sl_basis = price * (1 - config.SL_PERCENT), f"고정 {config.SL_PERCENT*100:.0f}%"
                tp, tp_basis = price + (price - sl), "손절폭 1:1"

            qty = int(self._capital_per_trade // price)
            if qty < 1:
                print(f"[Trader] {ticker} 주가({price:,.0f}원)가 종목당 투입금액보다 높아 매수 불가")
                continue

            try:
                result = self._order(ORDER_API_ID_BUY, ticker, qty)
            except Exception as e:
                print(f"[Trader] {ticker} 매수 주문 실패: {e}")
                continue

            if result.get("return_code") not in (None, 0):
                print(f"[Trader] {ticker} 매수 거부: {result.get('return_msg')}")
                continue

            # 진입근거 모달용 - 실제 시그널/반등 수치를 그대로 저장 (뻔한 고정 문구 대신 실데이터)
            rsi_series = calc.get("rsi_reg_series")
            signal_time = v.get("signal_time")
            signal_rsi = None
            if rsi_series is not None and signal_time is not None and signal_time in rsi_series.index:
                signal_rsi = float(rsi_series.loc[signal_time])

            positions[ticker] = {
                "name": name,
                "qty": qty,
                "entry_price": price,
                "entry_time": datetime.now().isoformat(timespec="seconds"),
                "timeframe": config.TRADER_TIMEFRAME,
                "signal_time": str(v.get("signal_time")) if v.get("signal_time") is not None else None,
                "signal_price": v.get("signal_price"),
                "signal_rsi": signal_rsi,
                "rebound_time": str(v.get("rebound_time")) if v.get("rebound_time") is not None else None,
                "rebound_price": v.get("rebound_price"),
                "bars_after_signal": v.get("bars_after_signal"),
                "reasons": v.get("reasons", []),
                "tp": tp,
                "sl": sl,
                "tp_basis": tp_basis,
                "sl_basis": sl_basis,
                "pivot_high": pivot_high_raw,
                "pivot_low": pivot_low_raw,
                "order_no": result.get("ord_no"),
                "best_reason": v.get("best_reason") or ", ".join(v.get("reasons", [])),
            }
            self._save_positions(positions)
            reason_text = positions[ticker]["best_reason"]
            print(f"[Trader] {ticker} 매수 완료 - {qty}주 @ {price:,.0f}원 (TP {tp:,.0f} / SL {sl:,.0f})")
            if self.paper:
                continue  # 구버전(페이퍼)은 실제 체결이 아니므로 텔레그램 알림 생략
            self.notifier.send(
                f"🟢 [자동매매/모의투자] 매수 체결\n"
                f"[{ticker}] {name}\n"
                f"{qty}주 @ {price:,.0f}원 (총 {qty * price:,.0f}원)\n"
                f"목표가 {tp:,.0f} / 손절가 {sl:,.0f}\n"
                f"사유: {reason_text}"
            )
            time.sleep(0.3)

    # ------------------------------------------------------------------
    # 청산 (매도) - trader_watch.py가 주기적으로 호출
    # ------------------------------------------------------------------
    def check_exits(self):
        if self.client is None:
            print("[Trader] kiwoom 클라이언트 없음 - 청산 점검을 건너뜁니다.")
            return

        positions = self._load_positions()
        current_prices = {}

        if not positions:
            print("[Trader] 보유 포지션 없음")
            self.export_dashboard_snapshot(current_prices)
            return

        for ticker in list(positions.keys()):
            pos = positions[ticker]
            price = self._current_price(ticker)
            if price is None:
                continue
            current_prices[ticker] = price

            reason = None
            if price >= pos["tp"]:
                reason = "익절(TP 도달)"
            elif price <= pos["sl"]:
                reason = "손절(SL 도달)"

            if reason is None:
                print(f"[Trader] {ticker} 보유 중 - 현재가 {price:,.0f} "
                      f"(TP {pos['tp']:,.0f} / SL {pos['sl']:,.0f})")
                time.sleep(0.3)
                continue

            self._execute_sell(ticker, pos, price, reason, positions)
            time.sleep(0.3)

        self.export_dashboard_snapshot(current_prices)

    def close_position_manual(self, ticker: str, reason: str = "사용자 강제 청산"):
        """대시보드의 [청산] 요청 등, TP/SL과 무관하게 즉시 시장가로 청산.

        반환값:
          True  - 매도 성공
          False - 매도 실패했지만 재시도할 가치가 있음 (장마감/현재가조회실패/주문거부 등 일시적 사유)
          None  - 애초에 보유 중이 아니었음 (재시도 불필요, 요청 자체를 종료 처리해도 됨)
        """
        positions = self._load_positions()
        pos = positions.get(ticker)
        if pos is None:
            print(f"[Trader] {ticker} 청산 요청 - 보유 중이 아니라 건너뜁니다.")
            return None

        price = self._current_price(ticker)
        if price is None:
            print(f"[Trader] {ticker} 청산 요청 - 현재가 조회 실패로 건너뜁니다 (다음 주기에 재시도).")
            return False

        ok = self._execute_sell(ticker, pos, price, reason, positions)
        self.export_dashboard_snapshot({ticker: price} if ok else {})
        return ok

    def _execute_sell(self, ticker: str, pos: dict, price: float, reason: str, positions: dict) -> bool:
        """실제 매도 주문 + positions/trade_log 갱신 + 텔레그램 발송. positions는 in-place로 수정됨."""
        try:
            result = self._order(ORDER_API_ID_SELL, ticker, pos["qty"])
        except Exception as e:
            print(f"[Trader] {ticker} 매도 주문 실패: {e}")
            return False

        if result.get("return_code") not in (None, 0):
            print(f"[Trader] {ticker} 매도 거부: {result.get('return_msg')}")
            return False

        pnl, pnl_pct, commission, tax = self._net_pnl(pos["entry_price"], price, pos["qty"])

        del positions[ticker]
        self._save_positions(positions)
        self._append_trade_log({
            "ticker": ticker,
            "name": pos.get("name", ticker),
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
            "entry_time": pos["entry_time"],
            "exit_price": price,
            "exit_time": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "pnl": round(pnl, 0),
            "pnl_pct": round(pnl_pct, 2),
            "commission": round(commission, 0),
            "tax": round(tax, 0),
        })

        emoji = "🔵" if pnl >= 0 else "🔴"
        print(f"[Trader] {ticker} 매도 완료 - {reason} @ {price:,.0f}원 "
              f"(손익 {pnl:+,.0f}원, {pnl_pct:+.2f}%)")
        if not self.paper:
            self.notifier.send(
                f"{emoji} [자동매매/모의투자] 매도 체결 - {reason}\n"
                f"[{ticker}] {pos.get('name', ticker)}\n"
                f"{pos['qty']}주 @ {price:,.0f}원\n"
                f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)"
            )
        return True

    # ------------------------------------------------------------------
    # 대시보드 연동 - docs/results/positions.json (보유 포지션 + 계좌 요약)
    # ------------------------------------------------------------------
    def export_dashboard_snapshot(self, current_prices: dict):
        """보유 포지션 + 계좌 요약을 docs/results/positions.json 으로 내보내고 git push
        (대시보드가 이 파일을 읽어 모의투자 현황을 보여줌)."""
        positions = self._load_positions()

        position_rows = []
        for ticker, pos in positions.items():
            price = current_prices.get(ticker)
            pnl = pnl_pct = None
            if price is not None:
                pnl, pnl_pct, _, _ = self._net_pnl(pos["entry_price"], price, pos["qty"])
                pnl = round(pnl, 0)
                pnl_pct = round(pnl_pct, 2)
            position_rows.append({
                "ticker": ticker,
                "name": pos.get("name", ticker),
                "qty": pos["qty"],
                "entry_price": pos["entry_price"],
                "entry_time": pos["entry_time"],
                "current_price": price,
                "tp": pos["tp"],
                "sl": pos["sl"],
                "tp_basis": pos.get("tp_basis"),
                "sl_basis": pos.get("sl_basis"),
                "pivot_high": pos.get("pivot_high"),
                "pivot_low": pos.get("pivot_low"),
                "best_reason": pos.get("best_reason", ""),
                "timeframe": pos.get("timeframe"),
                "signal_time": pos.get("signal_time"),
                "signal_price": pos.get("signal_price"),
                "signal_rsi": pos.get("signal_rsi"),
                "rebound_time": pos.get("rebound_time"),
                "rebound_price": pos.get("rebound_price"),
                "bars_after_signal": pos.get("bars_after_signal"),
                "reasons": pos.get("reasons", []),
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })

        # 상단 수익금/수익률은 "종목별로 보여주는 손익의 합"과 항상 일치하도록,
        # 계좌 API(kt00003)를 따로 조회하지 않고 여기 목록(미실현) + trade_log.json(실현)의
        # 합계로 계산합니다. (kt00003은 체결가/수수료/세금까지 반영된 실제 값이라
        # 우리가 표시하는 종목별 손익 합계와는 미묘하게 달라질 수 있음)
        unrealized_total = sum(p["pnl"] for p in position_rows if p["pnl"] is not None)
        realized_total = sum(t.get("pnl", 0) for t in self._load_trade_log())
        profit_amount = round(unrealized_total + realized_total, 0)
        current_capital = self._initial_capital + profit_amount
        return_pct = round(profit_amount / self._initial_capital * 100, 2)

        payload = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "strategy_version": "legacy" if self.paper else "current",
            "initial_capital": self._initial_capital,
            "current_capital": current_capital,
            "return_pct": return_pct,
            "profit_amount": profit_amount,
            "positions": position_rows,
        }

        results_dir = os.path.join(ROOT, "docs", "results")
        os.makedirs(results_dir, exist_ok=True)
        path = os.path.join(results_dir, self._dashboard_positions_file)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[Trader] 대시보드용 포지션 스냅샷 저장: {path}")

        # 매수 현황보고서(대시보드)용 - 청산 완료 내역도 함께 내보냄
        trade_log_path = os.path.join(results_dir, self._dashboard_trade_log_file)
        with open(trade_log_path, "w", encoding="utf-8") as f:
            json.dump(self._load_trade_log(), f, ensure_ascii=False, indent=2)

        self._git_push()

    def _git_push(self):
        git_dir = os.path.join(ROOT, ".git")
        if not os.path.isdir(git_dir):
            return
        try:
            subprocess.run(["git", "add",
                             f"docs/results/{self._dashboard_positions_file}",
                             f"docs/results/{self._dashboard_trade_log_file}"],
                            cwd=ROOT, check=True, capture_output=True)
            label = "구버전(페이퍼)" if self.paper else ""
            msg = f"SST 모의투자 현황 갱신{(' ' + label) if label else ''} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            commit = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT,
                                     capture_output=True, text=True)
            if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
                print(f"[Trader] git commit 경고: {commit.stdout.strip()} {commit.stderr.strip()}")
                return
            push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
            if push.returncode != 0:
                print(f"[Trader] git push 실패: {push.stderr.strip()}")
            else:
                print("[Trader] GitHub로 포지션 현황 푸시 완료")
        except Exception as e:
            print(f"[Trader] git 자동화 중 오류: {e}")


def pd_isnan(x) -> bool:
    return x != x  # NaN은 자기 자신과 같지 않다는 성질 이용 (pandas import 없이 체크)
