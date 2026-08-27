"""
SST Leader 에이전트
- 수집 -> 계산 -> 검증 -> 알림 순서로 팀원 에이전트를 지휘
- 종목을 배치(config.BATCH_SIZE개씩) 단위로 나눠 처리하고, 배치가 끝날 때마다
  바로 텔레그램으로 결과를 발송합니다 (전체 다 끝날 때까지 기다리지 않음).

타임프레임 전환: 이 파일을 직접 실행하면 4시간봉 기준입니다.
15분봉/5분봉으로 돌리려면 leader15.py / leader05.py 를 실행하세요
(둘 다 이 파일의 SSTLeader를 그대로 재사용하되 SST_TIMEFRAME 환경변수만 다르게 설정합니다).
"""
import os
os.environ.setdefault("SST_TIMEFRAME", "4h")  # 직접 실행 시 기본값 (leader15.py 등이 먼저 설정했으면 그 값 유지)

from datetime import datetime

import config
from tickers import TICKERS, TICKER_NAMES
from agents.collector import DataCollectorAgent
from agents.calculator import SignalCalculatorAgent
from agents.verifier import PatternVerifierAgent
from agents.notifier import NotifierAgent


class SSTLeader:
    def __init__(self):
        self.collector = DataCollectorAgent()
        self.calculator = SignalCalculatorAgent()
        self.verifier = PatternVerifierAgent()
        self.notifier = NotifierAgent()

    def _chunk(self, items, size):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def run_daily_scan(self):
        batches = list(self._chunk(TICKERS, config.BATCH_SIZE))
        total_batches = len(batches)
        print(f"[SST Leader] {datetime.now()} 스캔 시작 - 대상 {len(TICKERS)}개 종목 "
              f"({total_batches}개 배치, 배치당 {config.BATCH_SIZE}개)")

        all_verified = []
        total_signals = 0

        for batch_idx, batch in enumerate(batches, start=1):
            raw_data = self.collector.fetch_all(batch)
            calc_results = self.calculator.compute_all(raw_data)
            total_signals += sum(1 for r in calc_results if r["long_signal"])

            verified = self.verifier.verify_all(calc_results, raw_data)
            all_verified.extend(verified)

            print(f"[SST Leader] 배치 {batch_idx}/{total_batches} 완료 - "
                  f"{len(raw_data)}개 수집, 반등 확인 {len(verified)}건")

            report = self._build_batch_report(verified, batch_idx, total_batches, len(batch))
            self.notifier.send(report)

        print(f"[SST Leader] 전체 완료 - 총 {len(TICKERS)}개 종목, "
              f"롱 시그널 {total_signals}건, 반등 확인 {len(all_verified)}건")

        return all_verified

    def _fmt_time(self, ts) -> str:
        try:
            return ts.strftime("%m/%d %H:%M")
        except Exception:
            return str(ts)

    def _build_batch_report(self, verified: list, batch_idx: int, total_batches: int, batch_count: int) -> str:
        header = f"📋 SST 배치 {batch_idx}/{total_batches} ({batch_count}종목 · {datetime.now().strftime('%H:%M')})\n"

        if not verified:
            return header + "\n이번 배치에서는 반등 캔들 패턴이 확인된 종목이 없습니다."

        fresh = sorted([v for v in verified if v.get("is_fresh")], key=lambda v: not v.get("best_signal", False))
        stale = sorted([v for v in verified if not v.get("is_fresh")], key=lambda v: not v.get("best_signal", False))

        lines = [header]

        if fresh:
            lines.append(f"🆕 방금 확인됨 (매수 타점 유효 가능성 있음): {len(fresh)}건\n")
            for v in fresh:
                lines.append(self._fmt_entry(v))
                lines.append("")

        if stale:
            lines.append(f"📚 최근 히스토리 (참고용 · 매수 타점은 이미 지남): {len(stale)}건\n")
            for v in stale:
                lines.append(self._fmt_entry(v))
                lines.append("")

        return "\n".join(lines)

    def _fmt_entry(self, v: dict) -> str:
        name = TICKER_NAMES.get(v["ticker"], v["ticker"])
        reasons_text = ", ".join(v["reasons"])
        pct = (v["rebound_price"] - v["signal_price"]) / v["signal_price"] * 100
        entry_note = " (현재 진입 조건 활성)" if v.get("has_active_entry") else ""
        best_tag = "[강추] " if v.get("best_signal") else ""
        text = (
            f"{best_tag}[{v['ticker']}] {name}{entry_note}\n"
            f"  시그널: {self._fmt_time(v['signal_time'])} ({config.TIMEFRAME_LABEL}) @ {v['signal_price']:,.0f}\n"
            f"  반등봉: {self._fmt_time(v['rebound_time'])} (시그널 후 {v['bars_after_signal']}봉) @ {v['rebound_price']:,.0f} ({pct:+.1f}%)\n"
            f"  패턴: {reasons_text}"
        )
        if v.get("best_signal"):
            text += f"\n  ⭐ {v['best_reason']}"
        return text


if __name__ == "__main__":
    leader = SSTLeader()
    leader.run_daily_scan()


