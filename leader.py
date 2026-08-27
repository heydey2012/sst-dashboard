"""
SST Leader 에이전트
- 수집 -> 계산 -> 검증 -> 알림 순서로 팀원 에이전트를 지휘
- 종목을 배치(config.BATCH_SIZE개씩) 단위로 나눠 처리하고, 배치가 끝날 때마다
  바로 텔레그램으로 결과를 발송합니다 (전체 다 끝날 때까지 기다리지 않음).
- 전체 스캔이 끝나면 docs/results/{timeframe}.json 으로 결과를 저장하고,
  git이 설정되어 있으면 자동으로 커밋+푸시해서 GitHub Pages에 반영합니다.

타임프레임 전환: 이 파일을 직접 실행하면 4시간봉 기준입니다.
15분봉/5분봉/1시간봉으로 돌리려면 leader15.py / leader05.py / leader1h.py 를 실행하세요
(모두 이 파일의 SSTLeader를 그대로 재사용하되 SST_TIMEFRAME 환경변수만 다르게 설정합니다).
"""
import os
os.environ.setdefault("SST_TIMEFRAME", "4h")  # 직접 실행 시 기본값 (leader15.py 등이 먼저 설정했으면 그 값 유지)

import json
import subprocess
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

        self._export_web_json(all_verified, total_signals)

        return all_verified

    def _export_web_json(self, all_verified: list, total_signals: int):
        """docs/results/{timeframe}.json 으로 저장 후 git 커밋+푸시 (설정되어 있으면)"""
        root = os.path.dirname(os.path.abspath(__file__))
        results_dir = os.path.join(root, "docs", "results")
        os.makedirs(results_dir, exist_ok=True)

        payload = {
            "timeframe": config.TIMEFRAME,
            "timeframe_label": config.TIMEFRAME_LABEL,
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "total_tickers": len(TICKERS),
            "total_signals": total_signals,
            "results": [self._serialize(v) for v in all_verified],
        }

        path = os.path.join(results_dir, f"{config.TIMEFRAME}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[SST Leader] 웹 결과 저장 완료: {path}")

        self._git_push(root)

    def _serialize(self, v: dict) -> dict:
        name = TICKER_NAMES.get(v["ticker"], v["ticker"])
        return {
            "ticker": v["ticker"],
            "name": name,
            "is_fresh": v.get("is_fresh", False),
            "best_signal": v.get("best_signal", False),
            "best_reason": v.get("best_reason", ""),
            "has_active_entry": v.get("has_active_entry", False),
            "signal_time": self._fmt_time(v["signal_time"]),
            "signal_price": v["signal_price"],
            "rebound_time": self._fmt_time(v["rebound_time"]),
            "rebound_price": v["rebound_price"],
            "bars_after_signal": v["bars_after_signal"],
            "reasons": v["reasons"],
        }

    def _git_push(self, root: str):
        """git이 설정되어 있으면 자동 커밋+푸시. 설정 안 되어 있으면 조용히 건너뜀."""
        git_dir = os.path.join(root, ".git")
        if not os.path.isdir(git_dir):
            print("[SST Leader] git 저장소가 아직 설정되지 않아 웹 게시는 건너뜁니다 "
                  "(README의 'GitHub Pages 설정' 참고)")
            return
        try:
            subprocess.run(["git", "add", "docs/results"], cwd=root, check=True,
                            capture_output=True)
            msg = f"SST {config.TIMEFRAME_LABEL} 스캔 결과 갱신 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            commit = subprocess.run(["git", "commit", "-m", msg], cwd=root,
                                     capture_output=True, text=True)
            if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
                print(f"[SST Leader] git commit 경고: {commit.stdout.strip()} {commit.stderr.strip()}")
                return
            push = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True)
            if push.returncode != 0:
                print(f"[SST Leader] git push 실패: {push.stderr.strip()}")
            else:
                print("[SST Leader] GitHub로 결과 푸시 완료")
        except Exception as e:
            print(f"[SST Leader] git 자동화 중 오류: {e}")

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


