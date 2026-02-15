# KIS 자동매매 프로그램 - 완전 초보자용 최신 가이드

이 문서는 `2026-02` 기준 최신 실행 흐름에 맞춘 초보자 가이드입니다.

## 0. 먼저 꼭 읽기
- 이 프로그램은 자동으로 매수/매도를 실행할 수 있습니다.
- `REAL` 모드에서는 실제 돈이 거래됩니다.
- 초보자는 반드시 `DRY_RUN -> PAPER -> REAL` 순서로 진행하세요.

---

## 1. 이 프로그램이 하는 일
- 한국투자증권(KIS) API로 시세를 읽습니다.
- Trend + ATR 전략으로 진입/청산 신호를 계산합니다.
- 조건이 맞으면 주문을 실행하고, DB/로그/알림을 남깁니다.
- 재시작해도 포지션과 주문 상태를 복구합니다.

핵심 구성:
- 실행 앱: `python -m kis_trend_atr_trading.apps.kr_trade`
- 전략: `strategy/multiday_trend_atr.py`
- 실행 엔진: `engine/multiday_executor.py`
- 주문/동기화: `engine/order_synchronizer.py`
- DB 저장소: `db/repository.py`

---

## 2. 구버전 실행 파일 안내
아래 파일들은 하위호환(Deprecated)입니다.
- `main_multiday.py`
- `main.py`, `main_v2.py`, `main_v3.py`, `main_cbt.py`

지금은 아래를 표준으로 사용하세요.
```bash
python -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed rest
```

---

## 3. 준비물
- Python 3.10+ 권장
- KIS Open API 앱키/시크릿
- 계좌번호(모의 또는 실계좌)
- 텔레그램(선택)
- MySQL(권장, 없어도 일부 기능은 JSON 기반으로 동작)

의존성 설치:
```bash
cd /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading
python3 -m pip install -r requirements.txt
```

---

## 4. 환경변수(.env) 설정
가장 쉬운 방법:
```bash
cd /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading
cp .env.example .env
```

최소 필수 키(초보자 기준):
```env
# 모드
EXECUTION_MODE=PAPER
TRADING_MODE=PAPER

# KIS
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

모드 의미:
- `EXECUTION_MODE=DRY_RUN`: 주문 API 미호출(가장 안전)
- `EXECUTION_MODE=PAPER`: 모의계좌 주문
- `EXECUTION_MODE=REAL`: 실계좌 주문(고위험)

권장:
- `TRADING_MODE`도 `PAPER`/`REAL`로 맞춰 주세요.
- 레거시 값 `DEV/PROD`도 내부적으로 매핑되지만, 새 설정은 `PAPER/REAL`을 권장합니다.

---

## 5. 첫 실행(안전 순서)

### 5-1. 도움말 확인
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 -m kis_trend_atr_trading.apps.kr_trade --help
```

### 5-2. DRY_RUN 권장 점검
```bash
cd /home/deploy/KIS-API-Trend-ATR
EXECUTION_MODE=DRY_RUN TRADING_MODE=PAPER \
python3 -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed rest --interval 60 --max-runs 3
```

### 5-3. PAPER(모의투자) 실행
```bash
cd /home/deploy/KIS-API-Trend-ATR
EXECUTION_MODE=PAPER TRADING_MODE=PAPER \
python3 -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed rest --interval 60
```

### 5-4. WS 피드 실행(선택)
```bash
cd /home/deploy/KIS-API-Trend-ATR
EXECUTION_MODE=PAPER TRADING_MODE=PAPER \
python3 -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed ws --interval 60
```

옵션 요약:
- `--mode {trade,paper,cbt}`
- `--feed {rest,ws}`
- `--stock 005930`
- `--interval 60`
- `--max-runs 10`
- `--order-quantity 1`

---

## 6. Universe(종목선정) 설정
파일: `kis_trend_atr_trading/config/universe.yaml`

주요 키:
- `universe.selection_method`: `fixed | volume_top | atr_filter | combined`
- `universe.max_stocks`, `universe.universe_size`, `universe.max_positions`
- `universe.candidate_pool_mode`: `yaml | market | kospi200 | volume_top`
- `stocks`: 고정 목록

초보자 권장:
- 시작은 `selection_method: fixed`
- 종목은 1~2개부터
- `max_positions`를 작게 유지

---

## 7. 로그 읽는 법 (실전에서 자주 보는 것)

### 정상 예시
- `[RESYNC][DB] 실계좌 기준 반영: 005930 qty=...`
- `시그널: HOLD | ...`
- `[MULTI] 다음 실행까지 60초 대기`

### 자주 보이는 정상 로그
- `kis_api | [KIS][BAL] 캐시 재사용: age=1.93s`
  - 의미: 잔고 API를 다시 호출하지 않고 최근 결과를 재사용했다는 뜻
  - 오류가 아니라 성능 최적화 로그

---

## 8. 텔레그램 알림 동작

오는 경우(주요):
- 시스템 오류/전략 예외
- 주문 실패
- 포지션 복원 주요 이벤트
- 손절/익절/트레일링/갭보호

안 오는 경우(중요):
- 시작 시 `positions` 동기화 중 개별 DB upsert 실패
- 이 경우는 현재 설계상 소프트 실패로 로그 경고만 남깁니다.
- 대표 로그: `[RESYNC][DB] 포지션 저장 실패/보류: ...`

자세한 배경: `kis_trend_atr_trading/LEGACY_DB_COMPATIBILITY.md`

---

## 9. 일일 리포트 실행
CLI:
```bash
cd /home/deploy/KIS-API-Trend-ATR
python3 tools/daily_report.py --test-telegram
python3 tools/daily_report.py --dry-run
python3 tools/daily_report.py --yesterday
```

자동 실행(cron, 월~금 16:05 KST):
```cron
5 16 * * 1-5 cd /home/deploy/KIS-API-Trend-ATR && /usr/bin/python3 tools/daily_report.py >> /home/deploy/KIS-API-Trend-ATR/logs/report.log 2>&1
```

주의:
- crontab에는 반드시 한 줄로 입력
- 터미널에 직접 입력하면 `5: command not found`가 나는 것이 정상(크론 문법이기 때문)

---

## 10. 서버 재부팅 시
- `crontab` 등록 내용은 유지됩니다.
- `cron` 서비스만 정상 기동하면 스케줄은 계속 동작합니다.

확인:
```bash
systemctl status cron
mkdir -p /home/deploy/KIS-API-Trend-ATR/logs
crontab -l
```

---

## 11. 초보자 체크리스트 (실수 방지)

실행 전:
1. `.env`에 키 입력 완료
2. `EXECUTION_MODE`/`TRADING_MODE` 원하는 값 확인
3. `python3 -m ... --help` 동작 확인
4. 텔레그램 연결 확인(`--test-telegram`)
5. 종목/수량/리스크 값 보수적으로 설정

운영 중:
1. 로그에 `ERROR` 급증 여부 확인
2. `RISK MANAGER STATUS`에서 손실 한도 점검
3. 장중 재시작 후 포지션 복원 로그 확인

REAL 전환 전:
1. DRY_RUN/PAPER 충분히 검증
2. 최대 수량 최소로 시작
3. Kill switch 사용법 숙지
4. 장애 대응 절차 문서화

---

## 12. 자주 막히는 문제

### Q1) `ModuleNotFoundError: yaml`
- 원인: `PyYAML` 미설치
- 해결:
```bash
python3 -m pip install -r kis_trend_atr_trading/requirements.txt
```

### Q2) `INVALID_CHECK_ACNO`
- 원인: 계좌번호/상품코드/모드 불일치
- 해결:
  - `KIS_ACCOUNT_NO`, `KIS_ACCOUNT_PRODUCT_CODE` 재확인
  - PAPER/REAL 모드와 계좌 종류 일치 확인

### Q3) `Unknown column ...` / `Field ... doesn't have a default value`
- 원인: DB 레거시 스키마 불일치
- 해결:
  - 최신 `main` pull
  - 시작 로그에 레거시 컬럼 감지 메시지 확인
  - 상세: `LEGACY_DB_COMPATIBILITY.md`

### Q4) 크론이 실행 안 됨
- `crontab -l`로 등록 확인
- 절대경로 사용 확인
- `logs/report.log` 생성/권한 확인
- 필요 시 `CRON_TZ=Asia/Seoul` 사용

---

## 13. 더 자세한 문서
- 운영 기준: `kis_trend_atr_trading/README.md`
- 구현 상세: `kis_trend_atr_trading/IMPLEMENTATION_GUIDE.md`
- 레거시 DB 이슈: `kis_trend_atr_trading/LEGACY_DB_COMPATIBILITY.md`
- 리포트 자동화: `REPORT_AUTOMATION.md`

---

## 14. 마지막 권장
- 초보자는 반드시 `DRY_RUN -> PAPER -> REAL` 순서로 진행하세요.
- 모르는 로그가 나오면 전체 로그와 함께 문의하세요.
- 본인이 설명 가능한 범위 내에서만 자동매매를 돌리세요.
