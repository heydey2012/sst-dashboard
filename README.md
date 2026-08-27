# SST (Signal Sentinels Team)

RSI Spread Pro Indicator v7.1 기반 "시그널 → 눌림 → 반등" 패턴을 매일 자동으로
스캔해서 텔레그램으로 알려주는 4-에이전트 + Leader 구조 시스템입니다.

## 팀 구성

| 역할 | 파일 | 담당 |
|---|---|---|
| SST Leader | `leader.py` | 전체 파이프라인 지휘 + 최종 보고서 작성 |
| 수집 에이전트 | `agents/collector.py` | 키움증권 공식 REST API로 종목별 OHLCV 수집 |
| 계산 에이전트 | `agents/calculator.py` | RSI Spread Pro v7.1 로직으로 롱/숏 시그널 판정 |
| 검증 에이전트 | `agents/verifier.py` | 시그널 후 눌림→반등 패턴 확인 |
| 알림 에이전트 | `agents/notifier.py` | 텔레그램 최종 리포트 발송 |

## 실행 전 꼭 해야 할 것

### 1. 키움증권 공식 REST API 설치 + 인증

키움 Open API+ (기존 OCX 방식)는 32비트 윈도우 전용이라 맥에서는 동작하지 않습니다.
대신 2026년 출시된 **키움 REST API**를 사용합니다 (윈도우/맥/리눅스 모두 지원).

```bash
# 1) 공식 저장소 클론
git clone https://github.com/Kiwoom-Securities/Kiwoom-REST-API.git
cd Kiwoom-REST-API

# 2) Python 3.13+ 확인, uv 설치
python3 --version   # 3.13 이상 필요
brew install uv      # 맥이면 Homebrew로

# 3) CLI 설치 + 인증 (App Key/Secret은 키움 REST API 포털에서 발급)
uv tool install kwcli
kiwoomcli setup      # real(실전) 또는 demo(모의투자) 선택 후 키 입력
                      # -> macOS Keychain에 안전하게 저장됨

# 4) kiwoom 패키지를 SST 프로젝트에서도 import 할 수 있게 설치
uv sync               # 이 저장소 자체 의존성 설치
pip install -e .      # 또는 이 방식으로 SST와 같은 가상환경에 설치
```

`kiwoomcli domestic stocks info --code 005930` 실행해서 정상적으로 데이터가
나오면 인증이 잘 된 것입니다.

### 2. RSI Spread Pro 계산식 — v7.1 원본 기준으로 포팅 완료

`agents/calculator.py`는 제공해주신 "RSI Spread Pro Indicator v7.1" Pine Script를
기준으로 포팅했습니다. RSI/스프레드(부호 방향 판정), 피보나치 0.382 필터,
연속 신호 스트릭, 반전 캔들 진입 트리거, TP/SL 계산까지 반영되어 있습니다.
`ta.pivothigh/pivotlow`의 실시간 확정 지연은 배치 계산 특성상 근사 처리했습니다.

### 3. 종목 리스트 채우기

`tickers.py`에 기존에 만들어두신 98개 종목 코드를 붙여넣으세요.

### 4. 텔레그램 봇 설정

- @BotFather 로 봇 생성 → 토큰 발급
- 봇과 대화 시작 후 `https://api.telegram.org/bot<토큰>/getUpdates` 로 chat_id 확인
- **`.env.example`을 복사해서 `.env`로 저장하고, 그 안에 토큰/chat_id를 채워넣으세요.**
  터미널에 `export`로 설정하면 그 터미널 창이 닫히는 순간 사라지고,
  나중에 cron으로 자동 실행할 때는 아예 인식되지 않습니다. `.env` 파일 방식이라야
  터미널을 새로 열거나 cron으로 돌려도 항상 값을 읽어옵니다.

```bash
cp .env.example .env
# .env 파일을 열어서 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 값 채우기
```

`.env` 파일은 절대 깃허브에 올리거나 다른 사람과 공유하지 마세요.

## 데이터 수집 동작 방식 (중요)

## 타임프레임 변경

`config.py` 상단의 `TIMEFRAME` 값 하나만 바꾸면 됩니다 (`"5m"`, `"10m"`, `"15m"`,
`"30m"`, `"1h"`, `"4h"` 지원). 반등 확인 범위, 최신성 기준, 배치 크기가 전부
자동으로 비례 조정됩니다 (예: 4시간봉의 "16시간 이내"가 15분봉에서는 자동으로
"64봉 이내"로 환산됨).

**주의**: 캐시 파일명에 타임프레임이 포함되어 있어서(`005930_15m.csv` 등)
타임프레임을 바꾸면 자동으로 새 히스토리를 처음부터 백필합니다 (기존 데이터와
섞이지 않음).



키움 분봉조회(`ka10080`)는 `base_dt`(기준일자)로 **과거 날짜도 조회 가능**해서
KIS와 달리 히스토리 백필이 됩니다. 다만:

- **최초 실행 시**: `config.HISTORY_DAYS`(기본 25거래일)만큼 과거로 거슬러
  올라가며 1시간봉을 모아 `data_cache/`에 4시간봉으로 저장합니다.
  종목 수 × 25일만큼 API를 호출하므로 **첫 실행은 시간이 꽤 걸립니다**
  (조회 TR 제한: 계좌당 1초 5회 - 코드에 0.25초 딜레이를 넣어뒀습니다).
- **이후 실행 시**: 캐시에 히스토리가 이미 쌓여 있으면 오늘자만 증분 수집합니다.

## 설치 및 환경변수

```bash
pip install -r requirements.txt
cp .env.example .env
# .env 파일을 열어서 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 값 채우기
```

(키움 인증은 `kiwoomcli setup`으로 이미 키체인에 저장되어 있으므로 별도
환경변수가 필요 없습니다.)

## 실행

```bash
python leader.py
```

## 타임프레임별 실행 스크립트

각 타임프레임마다 별도 실행 파일이 있습니다. 모두 같은 `SSTLeader` 로직을
재사용하고, 실행 전에 `SST_TIMEFRAME` 환경변수만 다르게 세팅합니다.

```bash
python leader.py      # 4시간봉
python leader15.py    # 15분봉
python leader05.py    # 5분봉
```

캐시 파일명에 타임프레임이 포함되어 있어서(`005930_4h.csv`, `005930_15m.csv` 등)
서로 다른 타임프레임을 **동시에 운영해도 데이터가 섞이지 않습니다.** cron에도
각각 다른 시각/주기로 등록하면 됩니다.

새 타임프레임을 추가하고 싶으면 `leader15.py`를 복사해서 `SST_TIMEFRAME` 값만
바꾸면 됩니다 (`config.py`의 `TIMEFRAME_MINUTES`, `KIWOOM_NATIVE_TIC_SCOPES`,
`BATCH_SIZE_BY_TIMEFRAME`에 해당 값이 등록되어 있어야 합니다).

## 매일 자동 실행 (맥미니 cron 설정 예시)

```bash
crontab -e
```

```
0 9 * * 1-5 cd /Users/본인계정/SST && /usr/bin/python3 leader.py >> sst.log 2>&1
```

## 다음 확장 아이디어

- 검증 에이전트의 눌림/반등 기준(`PULLBACK_MIN_PCT`, `REBOUND_MIN_PCT`)을
  실제 백테스트로 튜닝
- 시그널 히스토리를 JSON/SQLite로 저장해서 며칠 전 시그널이 지금 반등 중인
  케이스까지 추적
- 이 구조를 그대로 재사용해서 인스타 뉴스 자동화, 크몽 문의 처리 파이프라인에도
  적용 가능 (수집→처리→검증→알림 패턴은 동일)
