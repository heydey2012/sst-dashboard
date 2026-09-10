"""
TQQQ 분할매수 전략 실행 스크립트 - 미국장 시간대(KST 밤~새벽)에 cron으로 반복 실행.
진입 점검(process_entry) + 청산 점검(check_exit)을 한 번에 수행합니다.

사용법: python3 scripts/tqqq_run.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from agents.tqqq_agent import TqqqAgent

LOCK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".sst_tqqq.lock")


def _lock_owner_alive(lock_path: str) -> bool:
    try:
        with open(lock_path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main():
    if not config.TQQQ_ENABLED:
        print("[TQQQ] config.TQQQ_ENABLED=False - 건너뜁니다.")
        return

    if os.path.exists(LOCK_PATH):
        if _lock_owner_alive(LOCK_PATH):
            print("[TQQQ] 이미 실행 중인 것 같습니다 - 이번 실행은 건너뜁니다.")
            return
        print("[TQQQ] 이전 실행이 비정상 종료된 것으로 보입니다 - stale lock 제거 후 진행합니다.")

    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
        agent = TqqqAgent()
        agent.process_entry()
        agent.check_exit()
    finally:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)


if __name__ == "__main__":
    main()
