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
RETEST_PROXIMITY_PCT = 0.01   # 재조정 저점이 1차 저점 대비 이 비율(1%) 이내로 근접해야 "진짜 조정"으로 인정

# --- 최근성 필터: 실제 경과시간(달력 기준) 기준 ---
# 예전엔 "봉 개수" 기준으로 걸렀는데, 캐시에 며칠~몇 주씩 수집이 비어있는 구간이 있으면
# (자동화가 며칠 안 돌았거나 네트워크 문제 등) 봉 개수만으론 몇 주 전 시그널이 "최근"으로
# 잘못 판정되는 문제가 있었음 (예: 07/28 시그널이 08/28 스캔에서 "최근 히스토리"로 보고됨).
# 그래서 실제 타임스탬프 차이로 판정하도록 변경.
RECENT_SIGNAL_SAME_DAY_ONLY = True   # True면 오늘 날짜의 시그널만 리포트 대상
FRESH_REBOUND_SAME_DAY_ONLY = True   # True면 반등이 오늘 날짜에 발생했어야 "신선함"(자동매매 진입 대상)
LEGACY_FRESH_MAX_AGE_HOURS = 8       # 구버전(페이퍼) 전략의 신선도 기준 - 개편 전 사용하던 값

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

# ===== 모의투자 자동매매 설정 =====
# 2026-09-10: 신버전/구버전 실시간 자동매매는 종료하고 98종목 5년 백테스트로 대체.
# (leader.py의 trader/legacy_trader 생성, trader_watch.py의 청산 감시, force_liquidate_all.py가
# 모두 이 플래그로 꺼짐. 코드/cron은 남겨두되 전부 조용히 no-op 처리됨)
# 절대 실계좌에서 켜지 않도록 TraderAgent가 매 주문 전 kiwoom 클라이언트의
# auth.mode == "demo" 인지 직접 확인합니다 (config 플래그와 무관하게 이중 안전장치).
TRADER_ENABLED = False
TRADER_TIMEFRAME = "15m"         # 이 타임프레임의 [강추] 신호만 자동매매 진입 트리거로 사용
INITIAL_CAPITAL = 10_000_000     # 모의계좌 초기 자금 (대시보드 수익률 계산 기준)
CAPITAL_PER_TRADE = 1_000_000    # 종목당 매수 금액 (원)
MAX_POSITIONS = 10               # 동시 보유 최대 종목 수
POSITIONS_FILE = "positions.json"
TRADE_LOG_FILE = "trade_log.json"

# 매매 수수료/세금 (2026-09-01 키움 모의투자 실계좌 ka10077 당일실현손익상세 조회로 역산해
# 검증한 값: 현대차 2주 매입394,125/매도404,000 기준 수수료5,570원(매수+매도 합산 거래대금의
# 0.349%) + 세금1,616원(매도금액의 정확히 0.20%) 공제 후 순손익12,564원이 tdy_sel_pl과 정확히
# 일치함. 기존 코드는 이 공제를 전혀 반영하지 않아 손익이 실제보다 부풀려져 있었음.
COMMISSION_RATE = 0.0035         # 매수/매도 각각 거래대금의 0.35%
SELL_TAX_RATE = 0.0020           # 매도 시 거래대금의 0.20% (증권거래세+농특세)

# ===== 구버전(페이퍼) 병행 시뮬레이션 =====
# 실제 주문은 신버전(위 설정) 하나로만 나가고, 구버전은 같은 시세로 가상매매만 하며
# 자체 1,000만원 시드를 따로 추적함 (실계좌가 1개뿐이라 실주문을 두 전략이 같이 쓸 수 없음).
LEGACY_ENABLED = True
LEGACY_INITIAL_CAPITAL = 10_000_000
LEGACY_CAPITAL_PER_TRADE = 1_000_000
LEGACY_MAX_POSITIONS = 10
LEGACY_POSITIONS_FILE = "positions_v1.json"
LEGACY_TRADE_LOG_FILE = "trade_log_v1.json"

# 당일 청산 원칙: 포지션을 하루도 안 넘기기 위해 매일 15:00부터 신규 매수 중단,
# 15:10에 보유 종목 전량 강제청산 (구버전/신버전 둘 다 적용). 주말/연휴 갭 리스크 회피 목적.

# 이 날짜 이후로는 신규 매수를 멈춤 (기존 보유 종목의 TP/SL 청산 감시는 계속함).
# scripts/generate_final_report.py 가 이 날짜 장 마감 후 cron으로 한 번 실행되어
# 최종 결과 보고서를 텔레그램으로 발송함.
TRADING_END_DATE = "2026-09-30"

# ===== TQQQ 분할매수 전략 (RSI Spread Pro Strategy v1 - DCA 포팅) =====
# ~/Downloads/RSI_Spread_Pro_Strategy_v1.pine 포팅. 원본은 롱+숏이지만 국내 개인
# 위탁계좌로 미국주식 공매도가 불가능해 롱 전용으로만 구현. 손절 없음 - 평단가
# 대비 TQQQ_TP_PCT 도달 시 보유 수량 전체를 익절. 최대 TQQQ_SPLIT_COUNT회까지
# 물타기(분할매수)하며, 회당 매수금액 = TQQQ_INITIAL_CAPITAL_USD / TQQQ_SPLIT_COUNT.
# 신버전과 같은 키움 모의계좌로 실주문.
TQQQ_ENABLED = True
TQQQ_TICKER = "TQQQ"
TQQQ_EXCHANGE = "ND"              # 나스닥
TQQQ_TIMEFRAME = "15"             # 15분봉 (2026-09-10 TP/SL 스윕 결과 5분/1시간봉 대비 리스크 대비 수익 최고)

# 해외주식 모의투자 계좌가 만료 상태(RC4091, 재신청 전까지 실주문 거부됨)라
# 그동안은 페이퍼(가상매매)로 돌림 - 실제 주문 없이 조회한 실시간 시세로만 체결을
# 시뮬레이션. 키움 사이트에서 해외주식 모의투자를 재신청하면 False로 바꿔서
# 실주문으로 전환.
TQQQ_PAPER_MODE = True
TQQQ_INITIAL_CAPITAL_USD = 7_450  # 달러로 직접 시드 고정(원화 환산 안 함, 약 1000만원 상당)
TQQQ_SPLIT_COUNT = 20
# 2026-09-10 TP%/SL% 스윕 결과: 15분봉에서 TP2%/SL20%가 수익률/MDD 비율 1.81로 최고
# (+16.37%, MDD -9.0%) - TP7%/손절없음(+20.05%, MDD -20.4%)보다 낙폭이 훨씬 작아 채택.
# 단, 백테스트 기간이 6개월뿐이라 진짜 하락장은 검증 안 됐음에 유의.
TQQQ_TP_PCT = 0.02                 # 평단가 대비 +2% 익절
TQQQ_SL_PCT = 0.20                 # 평단가 대비 -20% 손절
TQQQ_RSI_LENGTH = 14
TQQQ_MA_LENGTH = 14
TQQQ_SPREAD_LIMIT = 15
TQQQ_PIVOT_LEFT = 10
TQQQ_PIVOT_RIGHT = 10
TQQQ_FIB_RATIO = 0.382

# 98종목 국내주식 DCA 백테스트(scripts/backtest_dca.py)는 TQQQ와 진입신호 로직은
# 공유하지만 TP/SL은 별도 값 사용 - TQQQ_TP_PCT/SL_PCT를 직접 재사용하면 TQQQ용으로
# 튜닝한 값이 국내 백테스트에도 의도치 않게 흘러들어감 (2026-09-10 실제 발생한 문제).
DCA_TP_PCT = 0.03                  # 평단가 대비 +3% 익절
DCA_SL_PCT = 0.10                  # 평단가 대비 -10% 손절

TQQQ_POSITIONS_FILE = "positions_tqqq.json"
TQQQ_TRADE_LOG_FILE = "trade_log_tqqq.json"
