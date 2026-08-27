"""
SST (Signal Sentinels Team) 설정 파일
실행 전 환경변수 또는 .env 파일에 값을 채워넣으세요.

데이터 소스: 키움증권 공식 REST API (Kiwoom-Securities/Kiwoom-REST-API)
인증은 kiwoomcli setup 으로 미리 해두는 것을 권장합니다 (OS 키체인 저장).
"""

import os

# ===== .env 파일 로드 (터미널 환경변수는 세션이 끝나면 사라지고,
#       cron 자동 실행 시에는 아예 상속되지 않으므로 파일 방식을 우선 사용) =====
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ===== 알림 설정 (텔레그램) =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ===== RSI Spread Pro Indicator v7.1 설정 =====
# 원본 Pine Script 기본값 그대로 반영 (봉 단위와 무관하게 "봉 개수" 기준 그대로 사용)
RSI_LENGTH = 14
MA_LENGTH = 14
SPREAD_LIMIT = 15          # 진입 기준 스프레드 (부호 방향 판정, 절대값 아님)
RSI_LONG_LEVEL = 35        # RSI 극값(라벨 색상용) - 롱 기준 (이하일 때)
RSI_SHORT_LEVEL = 65       # RSI 극값(라벨 색상용) - 숏 기준 (이상일 때)
SMA_FAST = 20
SMA_SLOW = 100
REQUIRE_TREND_AGREE = False

# --- 피보나치 0.382 필터 (진입 게이트) ---
USE_FIB_FILTER = True
PIVOT_LEFT = 10
PIVOT_RIGHT = 10

# --- 연속 신호(스트릭) + 진입 트리거 + TP/SL ---
MAX_STREAK = 50
PRE_MOVE_MINUTES = 90       # 무브 시작 전 추가 탐색 시간(분) - 시간 단위라 타임프레임 바뀌어도 자동 환산됨
SL_PERCENT = 0.06
FIB_RATIO_1 = 0.236
FIB_RATIO_2 = 0.382
FIB_RATIO_3 = 0.5
FIB_RATIO_4 = 0.618
USE_TREND_TP_BOOST = True

# ==========================================================================
# ===== 타임프레임 설정 (여기 하나만 바꾸면 나머지가 자동으로 맞춰집니다) =====
# ==========================================================================
# 지원: "5m", "10m", "15m", "30m", "1h", "4h"
# 여러 타임프레임을 동시에 운영하려면 leader.py(4h) / leader15.py(15분) / leader05.py(5분)처럼
# 실행 스크립트를 분리하고, 아래처럼 환경변수로 지정합니다 (직접 이 값을 고칠 필요 없음).
TIMEFRAME = os.getenv("SST_TIMEFRAME", "4h")

TIMEFRAME_MINUTES = {
    "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
}
TIMEFRAME_LABELS = {
    "5m": "5분봉", "10m": "10분봉", "15m": "15분봉", "30m": "30분봉", "1h": "1시간봉", "4h": "4시간봉",
}
# 키움 분봉조회(ka10080) tic_scope는 1/3/5/10/15/30/45/60(분)만 지원.
# 4시간봉처럼 직접 지원 안 되는 단위는 60분봉을 받아서 우리가 직접 묶습니다.
KIWOOM_NATIVE_TIC_SCOPES = {"5m": "5", "10m": "10", "15m": "15", "30m": "30", "1h": "60"}
KIWOOM_TIC_SCOPE = KIWOOM_NATIVE_TIC_SCOPES.get(TIMEFRAME, "60")  # 4h 등은 60분봉을 받아 리샘플링

TIMEFRAME_LABEL = TIMEFRAME_LABELS.get(TIMEFRAME, TIMEFRAME)
_TF_MIN = TIMEFRAME_MINUTES.get(TIMEFRAME, 240)


def _bars_for_minutes(minutes: int) -> int:
    """지정한 '분'에 해당하는 현재 타임프레임 기준 봉 개수로 환산 (최소 1)"""
    return max(round(minutes / _TF_MIN), 1)


# ===== 반등 패턴 검증 설정 (캔들 패턴 기반) =====
# 아래 값들은 전부 "실제 시간(분)" 기준으로 정의하고, 현재 TIMEFRAME에 맞춰
# 봉 개수로 자동 환산합니다. 타임프레임을 바꿔도 이 값들을 따로 조정할 필요가 없습니다.
DOJI_BODY_RATIO = 0.1
CONSECUTIVE_BULLISH_COUNT = 2

REBOUND_WINDOW_BARS = _bars_for_minutes(16 * 60)          # 16시간 이내 1차 반등 확인 (기존 4h 기준 4봉과 동일한 시간)
SECONDARY_WINDOW_BARS = _bars_for_minutes(24 * 60)        # 24시간 이내 2차 저점/재반등 확인
RSI_DIVERGENCE_TOLERANCE = 3.0
RECENT_SIGNAL_MAX_AGE_BARS = _bars_for_minutes(2 * 24 * 60)   # 이틀 이내 시그널만 리포트 대상
FRESH_REBOUND_MAX_AGE_BARS = _bars_for_minutes(8 * 60)        # 8시간 이내 반등이면 "방금 확인됨"

# ===== 데이터 수집(백필) 설정 =====
KIWOOM_MAX_PAGES = 10
KIWOOM_INCREMENTAL_MAX_PAGES = 1
KIWOOM_REQUEST_DELAY = 1.2
KIWOOM_UPD_STKPC_TP = "1"    # 수정주가 반영
HISTORY_DAYS = 25   # 초기 백필 시 확보할 과거 거래일 수

# ===== 배치 처리 설정 =====
# 빠른 타임프레임일수록 한 사이클 안에 처리해야 할 종목이 많아 시간이 부족해지므로
# 배치 크기를 자동으로 줄입니다. 필요하면 아래 값을 직접 조정해도 됩니다.
BATCH_SIZE_BY_TIMEFRAME = {
    "5m": 3, "10m": 5, "15m": 5, "30m": 8, "1h": 10, "4h": 10,
}
BATCH_SIZE = BATCH_SIZE_BY_TIMEFRAME.get(TIMEFRAME, 10)
