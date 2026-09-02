"""
대시보드의 [청산] 버튼 처리기.

대시보드 버튼은 GitHub 저장소에 "청산:종목코드" 제목의 이슈를 새로 만드는 링크입니다
(버튼 자체엔 아무 비밀키도 없고, 사용자 본인의 GitHub 로그인으로 이슈가 생성됩니다).
이 스크립트는 3분마다(trader_watch.py와 같은 주기) 열려있는 이슈 목록을 조회해서
"청산:"으로 시작하는 제목을 찾아 해당 종목을 즉시 시장가로 매도하고, 처리한 이슈는
닫습니다 (닫기는 GitHub 토큰이 있어야 하지만, 실패해도 로컬에 처리 기록을 남겨
같은 이슈를 중복 처리하지 않습니다).

사용법: python3 scripts/check_liquidation_requests.py
"""
import json
import os
import re
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from agents.trader import TraderAgent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "heydey2012/sst-dashboard"
PROCESSED_FILE = os.path.join(ROOT, ".liquidation_processed.json")
CRED_FILE = os.path.expanduser("~/.git-credentials-sst")

TITLE_RE = re.compile(r"^청산\s*[:：]\s*(\d{6})")
TITLE_ALL_RE = re.compile(r"^청산전체\s*[:：]\s*(신버전|구버전)")


def _load_processed() -> set:
    if not os.path.exists(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, encoding="utf-8") as f:
        return set(json.load(f))


def _save_processed(processed: set):
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(processed), f)


def _github_token():
    """git push용으로 이미 설정해둔 ~/.git-credentials-sst 에서 토큰을 재사용 (이슈 닫기용)."""
    if not os.path.exists(CRED_FILE):
        return None
    with open(CRED_FILE, encoding="utf-8") as f:
        line = f.read().strip()
    m = re.search(r"https://[^:]+:([^@]+)@github\.com", line)
    return m.group(1) if m else None


def _close_issue(number: int, token: str, comment: str):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        requests.post(f"https://api.github.com/repos/{REPO}/issues/{number}/comments",
                       json={"body": comment}, headers=headers, timeout=10)
        requests.patch(f"https://api.github.com/repos/{REPO}/issues/{number}",
                        json={"state": "closed"}, headers=headers, timeout=10)
    except Exception as e:
        print(f"[Liquidation] 이슈 #{number} 닫기 실패(무시하고 진행): {e}")


def main():
    if not config.TRADER_ENABLED:
        print("[Liquidation] config.TRADER_ENABLED=False - 건너뜁니다.")
        return

    try:
        resp = requests.get(f"https://api.github.com/repos/{REPO}/issues",
                             params={"state": "open"}, timeout=15)
        resp.raise_for_status()
        issues = resp.json()
    except Exception as e:
        print(f"[Liquidation] 이슈 목록 조회 실패: {e}")
        return

    processed = _load_processed()
    token = _github_token()
    trader = TraderAgent()
    legacy_trader = None  # 필요할 때만 생성 (구버전 전체 청산 요청이 있을 때)

    for issue in issues:
        number = issue["number"]
        title = issue.get("title", "")
        if number in processed:
            continue

        m_all = TITLE_ALL_RE.match(title)
        if m_all:
            version = m_all.group(1)
            paper = version == "구버전"
            if paper:
                if legacy_trader is None:
                    legacy_trader = TraderAgent(paper=True)
                trader_v = legacy_trader
            else:
                trader_v = trader

            positions = trader_v._load_positions()
            if not positions:
                processed.add(number)
                _save_processed(processed)
                _close_issue(number, token, f"보유 중인 포지션이 없어 처리할 것이 없습니다 ({version}).")
                continue

            print(f"[Liquidation] 이슈 #{number} 감지 - {version} 전체 청산 요청 ({len(positions)}개 종목)")
            results = []
            all_done = True
            for ticker in list(positions.keys()):
                r = trader_v.close_position_manual(ticker, reason="대시보드 전체 청산 요청")
                results.append((ticker, r))
                if r is False:
                    all_done = False

            if not all_done:
                print(f"[Liquidation] 이슈 #{number} - {version} 일부 종목 매도 실패, 다음 주기에 재시도합니다.")
                continue

            processed.add(number)
            _save_processed(processed)
            lines = [f"- {t}: {'청산 완료' if r else '이미 보유 중 아님'}" for t, r in results]
            _close_issue(number, token, f"전체 청산 완료 ✅ ({version})\n" + "\n".join(lines))
            continue

        m = TITLE_RE.match(title)
        if not m:
            continue

        ticker = m.group(1)
        print(f"[Liquidation] 이슈 #{number} 감지 - {ticker} 청산 요청")
        result = trader.close_position_manual(ticker, reason="대시보드 청산 요청")

        if result is False:
            # 장마감/현재가조회실패/주문거부 등 일시적 사유 - 이슈를 안 닫고 다음 주기에 재시도
            print(f"[Liquidation] 이슈 #{number} - {ticker} 매도 실패, 다음 주기에 재시도합니다.")
            continue

        # True(성공) 또는 None(애초에 보유 중 아님) - 더 재시도할 필요 없으니 종료 처리
        processed.add(number)
        _save_processed(processed)
        comment = f"청산 완료 ✅ ({ticker})" if result else f"이미 보유 중이 아니라 처리할 것이 없습니다 ({ticker})"
        _close_issue(number, token, comment)


if __name__ == "__main__":
    main()
