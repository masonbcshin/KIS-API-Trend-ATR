# KIS Trend-ATR Trading System 사용 가이드 (최신)

이 문서는 `2026-02` 기준 최신 실행 흐름을 설명합니다.

## 1. 빠른 개요
- 표준 실행 엔트리포인트: `python -m kis_trend_atr_trading.apps.kr_trade`
- 전략 엔진: 멀티데이 Trend + ATR
- 권장 진행 순서: `DRY_RUN -> PAPER -> REAL`
- Deprecated 래퍼(`main_multiday.py`, `main_v2.py` 등)는 하위호환용으로만 유지

---

## 2. 실행 모드 정리

### 2-1. EXECUTION_MODE (설정 로딩 기준)
- `DRY_RUN`: 가상 체결 중심 (가장 안전)
- `PAPER`: 모의투자 API 사용
- `REAL`: 실계좌 설정 로드

### 2-2. TRADING_MODE (런타임 계좌 경로 기준)
- `PAPER`: 모의투자 계좌 경로
- `REAL`: 실계좌 경로

### 2-3. 권장 원칙
- 혼선 방지를 위해 두 값을 같은 의미로 맞추세요.
- 예시:
  - 모의투자: `EXECUTION_MODE=PAPER`, `TRADING_MODE=PAPER`
  - 실계좌: `EXECUTION_MODE=REAL`, `TRADING_MODE=REAL`

---

## 3. 설치 및 초기 준비

### 3-1. 의존성 설치
```bash
cd /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading
python3 -m pip install -r requirements.txt
```

### 3-2. .env 생성
```bash
cd /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading
cp .env.example .env
```

### 3-3. 최소 필수 환경변수
```env
# 실행 모드
EXECUTION_MODE=PAPER
TRADING_MODE=PAPER

# KIS API
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=12345678
KIS_ACCOUNT_PRODUCT_CODE=01

# 텔레그램(선택)
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# DB(권장)
DB_ENABLED=true
DB_TYPE=mysql
DB_HOST=localhost
DB_PORT=3306
DB_NAME=kis_trading
DB_USER=root
DB_PASSWORD=...
```

---

## 4. 표준 실행 명령

### 4-1. 거래 실행 (REST)
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed rest --interval 60
```

### 4-2. 거래 실행 (WebSocket)
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed ws --interval 60
```

### 4-3. 종목/수량/반복 횟수 지정
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 -m kis_trend_atr_trading.apps.kr_trade \
  --mode trade \
  --feed rest \
  --stock 005930 \
  --order-quantity 1 \
  --interval 60 \
  --max-runs 10
```

### 4-4. CBT 앱 실행
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 -m kis_trend_atr_trading.apps.kr_cbt --mode cbt --stock 005930 --interval 60
```

도움말:
```bash
python3 -m kis_trend_atr_trading.apps.kr_trade --help
python3 -m kis_trend_atr_trading.apps.kr_cbt --help
```

---

## 5. Universe 설정 (종목선정)
파일: `kis_trend_atr_trading/config/universe.yaml`

핵심 키:
- `universe.selection_method`: `fixed | volume_top | atr_filter | combined`
- `universe.max_stocks`, `universe.universe_size`, `universe.max_positions`
- `universe.candidate_pool_mode`: `yaml | market | kospi200 | volume_top`
- `stocks`: 고정 종목 리스트

권장 시작값:
- `selection_method: fixed`
- `max_positions: 1`
- 종목 1~2개로 충분히 검증

---

## 6. 로그와 알림 해석

### 6-1. 정상 로그 예시
- `[RESYNC][DB] 실계좌 기준 반영: ...`
- `시그널: HOLD | ...`
- `[MULTI] 다음 실행까지 60초 대기`

### 6-2. 자주 보는 정상 메시지
- `kis_api | [KIS][BAL] 캐시 재사용: age=1.93s`
- 의미: 최근 잔고 조회 결과를 재사용 (API 호출 절약), 오류 아님

### 6-3. 텔레그램 알림 규칙
- 발송: 시스템/전략 예외, 주문 실패, 주요 복원/청산 이벤트
- 미발송(현재 설계): 시작 시 계좌→DB 재동기화 중 개별 upsert 실패
  - 이 경우 경고 로그(`포지션 저장 실패/보류`)만 남고 Telegram ERROR는 보내지 않음

상세 배경: `kis_trend_atr_trading/LEGACY_DB_COMPATIBILITY.md`

---

## 7. 리스크 관리 기본값
- 손절/익절: ATR 기반
- 갭 보호: 기본 ON
- Kill switch: 환경변수 `KILL_SWITCH=true` 시 신규 주문 차단
- 일일 손실 한도 초과 시 거래 차단 경로 존재 (`risk_manager`)

실계좌 전환 전 최소 조건:
1. DRY_RUN/PAPER에서 충분한 기간 검증
2. 소량(`ORDER_QUANTITY=1`)으로 시작
3. 로그/텔레그램/복원 동작 사전 점검

---

## 8. 일일 리포트 실행

### 8-1. 수동 실행
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 tools/daily_report.py --test-telegram
python3 tools/daily_report.py --dry-run
python3 tools/daily_report.py --yesterday
```

### 8-2. cron 자동 실행 (월~금 16:05 KST)
```cron
5 16 * * 1-5 cd /home/deploy/KIS-API-Trend-ATR && /usr/bin/python3 tools/daily_report.py >> /home/deploy/KIS-API-Trend-ATR/logs/report.log 2>&1
```

주의:
- crontab에는 반드시 한 줄로 입력
- 터미널에 직접 치면 `5: command not found`가 나는 것이 정상

---

## 9. 재부팅 시 동작
- `crontab` 등록 항목은 유지됩니다.
- `cron` 서비스가 올라오면 스케줄은 계속 실행됩니다.

점검 명령:
```bash
systemctl status cron
crontab -l
mkdir -p /home/deploy/KIS-API-Trend-ATR/logs
```

---

## 10. 자주 발생하는 문제와 대응

### 10-1. `ModuleNotFoundError: yaml`
- 원인: `PyYAML` 미설치
- 조치:
```bash
python3 -m pip install -r kis_trend_atr_trading/requirements.txt
```

### 10-2. `INVALID_CHECK_ACNO`
- 원인: 계좌정보/상품코드/모드 불일치
- 조치:
  - `KIS_ACCOUNT_NO`, `KIS_ACCOUNT_PRODUCT_CODE` 확인
  - PAPER/REAL 계좌-모드 일치 확인

### 10-3. `Unknown column ...` / `Field ... doesn't have a default value`
- 원인: 레거시 DB 스키마 불일치
- 조치:
  - 최신 `main`으로 업데이트
  - 시작 로그의 레거시 컬럼 감지 메시지 확인
  - 상세 문서: `LEGACY_DB_COMPATIBILITY.md`

### 10-4. cron 미실행
- `crontab -l` 확인
- 절대경로 사용 여부 확인
- `logs/report.log` 경로/권한 확인
- 필요 시 `CRON_TZ=Asia/Seoul` 적용

---

## 11. 운영 체크리스트

실행 전:
1. `.env`의 모드/계정/API 키 재확인
2. `--help` 정상 출력 확인
3. 텔레그램 테스트
4. Universe/수량/손실한도 보수 설정

운영 중:
1. `ERROR` 급증 여부 확인
2. `RISK MANAGER STATUS` 확인
3. 재시작 후 복원 로그 확인

실계좌 전환 전:
1. 충분한 모의 검증
2. 단계적 규모 확대
3. 비상 중단 절차 숙지

---

## 12. 관련 문서
- 운영 기준: `kis_trend_atr_trading/README.md`
- 구현 상세: `kis_trend_atr_trading/IMPLEMENTATION_GUIDE.md`
- 초보자 중심: `kis_trend_atr_trading/BEGINNER_GUIDE.md`
- 레거시 DB 호환: `kis_trend_atr_trading/LEGACY_DB_COMPATIBILITY.md`
- 리포트 자동화: `REPORT_AUTOMATION.md`
