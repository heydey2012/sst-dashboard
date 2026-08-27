"""
[알림 에이전트] Notifier Agent
- 텔레그램 봇으로 SST Leader가 정리한 최종 리포트 발송

사전 준비:
1. 텔레그램에서 @BotFather 로 봇 생성 -> TELEGRAM_BOT_TOKEN 발급
2. 생성한 봇과 대화 시작 후, https://api.telegram.org/bot<TOKEN>/getUpdates 로 chat_id 확인
"""
import requests
import config


class NotifierAgent:
    def send(self, message: str):
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            print("[Notifier] 텔레그램 설정 없음 - 콘솔에만 출력합니다.\n")
            print(message)
            return

        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            # 리포트에 대괄호([005930])가 포함되어 있어 Markdown 파싱과 충돌할 수 있으므로
            # parse_mode 없이 일반 텍스트로 발송합니다.
            requests.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": message,
                },
                timeout=10,
            )
        except Exception as e:
            print(f"[Notifier] 발송 실패: {e}\n{message}")
