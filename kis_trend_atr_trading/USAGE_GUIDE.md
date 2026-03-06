# KIS Trend-ATR Trading System 사용 가이드 (최신)

이 문서는 `2026-02-23` 배포 기준 최신 실행 흐름을 설명합니다.

## 1. 빠른 개요
- 표준 실행 엔트리포인트: `python3 -m kis_trend_atr_trading.apps.kr_trade`
- 전략 엔진: 멀티데이 Trend + ATR
- 권장 진행 순서: `DRY_RUN -> PAPER -> REAL`
- Deprecated 래퍼(`main_multiday.py` 등)는 하위호환용으로만 유지

## 1-1. 최근 배포 반영 요약 (`d14d305`, `84db5c2`, `d2b82b2`)
- CBT 재시작 동기화 시 `CBT -> PAPER` 강제 보정 제거
- 텔레그램 CBT 시그널 Markdown 이스케이프 안정화
- 종목명 해석 보강: `holdings -> quote -> universe_cache` 순서로 조회
- `websockets>=12.0` 의존성 명시, WS 미사용/미설치 시 REST 경로로 안전 동작
- 멀티종목 호환 실행(`main_multiday`)에서 포지션 파일은 `positions_{mode}_{symbol}.json` 단위로 저장 (`mode`: `DRY_RUN|PAPER|REAL`)

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

참고:
- WS 피드를 사용할 계획이면 `requirements.txt`의 `websockets>=12.0`가 반드시 설치되어야 합니다.

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

### 4-5. 멀티종목 호환 실행(레거시 wrapper)
```bash
cd /home/deploy/KIS-API-Trend-ATR
TRADING_MODE=PAPER python3 -m kis_trend_atr_trading.main_multiday --mode trade --interval 60
```

참고:
- 시작 시 `[DEPRECATED] main_multiday.py -> use ...` 문구가 출력되는 것은 정상입니다.
- 멀티종목 운영은 내부적으로 `deprecated/legacy_main_multiday.py` 경로가 계속 사용됩니다.

### 4-6. 포지션 파일 확인 기준
- 표준 단일 엔트리포인트(`apps.kr_trade`) 기본 경로: `kis_trend_atr_trading/data/positions.json`
- 멀티종목 호환 경로(`main_multiday`) 기본 경로: `kis_trend_atr_trading/data/positions_{mode}_{symbol}.json`
- 따라서 멀티종목 실행 중에는 `positions.json`이 비어 있어도 `positions_{mode}_*.json`이 갱신되면 정상입니다.

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
- `universe.out_of_universe_warn_days`, `universe.out_of_universe_reduce_days`
- `universe.candidate_pool_mode`: `yaml | market | kospi200 | volume_top`
- `stocks`: 고정 종목 리스트

운영 의미:
- `universe_size`: 당일 유니버스 선정 개수
- `max_positions`: 동시 보유 하드캡
- `max_stocks`: 하위호환 fallback(미설정 시 `universe_size`/`max_positions` 기본값)

권장 시작값:
- `selection_method: fixed`
- `max_positions: 1`
- 종목 1~2개로 충분히 검증

### 5-1. Universe 캐시 스키마(운영)
- 캐시 파일: `kis_trend_atr_trading/data/universe_cache.json`
- 주요 필드: `schema_version`, `date`, `db_mode`, `policy_signature`, `cache_key`
- 재사용 조건: 위 식별자가 현재 실행 정책과 일치할 때만 `HIT`
- 구버전(`schema_version=1` 또는 미기재) 캐시는 정책 호환 시 자동 마이그레이션 후 `v2`로 재저장
- 미지원 버전/손상 캐시는 자동 무효화되고 당일 유니버스를 재선정

---

## 6. 로그와 알림 해석

### 6-1. 정상 로그 예시
- `[RESYNC][DB] 실계좌 기준 반영: ...`
- `시그널: HOLD | ...`
- `[MULTI] 다음 실행까지 60초 대기`

### 6-2. 자주 보는 정상 메시지
- `kis_api | [KIS][BAL] 캐시 재사용: age=1.93s`
- 의미: 최근 잔고 조회 결과를 재사용 (API 호출 절약), 오류 아님
- `kis_api | [KIS][HOLDINGS] parsed path=... count=0`
- 의미: 응답 경로는 정상으로 찾았고 현재 무보유 상태라는 뜻 (오류 아님)

### 6-3. 텔레그램 알림 규칙
- 발송: 시스템/전략 예외, 주문 실패, 주요 복원/청산 이벤트
- 미발송(현재 설계): 시작 시 계좌→DB 재동기화 중 개별 upsert 실패
  - 이 경우 경고 로그(`포지션 저장 실패/보류`)만 남고 Telegram ERROR는 보내지 않음
- `UNKNOWN(종목코드)`는 종목명 해석 실패 시의 최종 폴백 표기입니다.
  - 일시적 API 실패, 비보유/비캐시 상태에서 발생할 수 있습니다.
  - 다음 사이클에서 캐시가 채워지면 정상 종목명으로 복구될 수 있습니다.

### 6-4. 슬롯 컷/보유 노화 로그
- `[ENTRY] capacity cutoff applied ...`
  - 의미: 보유 상한(`max_positions`) 대비 슬롯 부족으로 상위 후보만 진입 허용
- `[UNIVERSE][AGING] ...`
  - 의미: 유니버스 밖 보유 종목의 누적 일수 요약(경보/축소 우선순위 계산)
  - 누적 일수는 KRX 영업일 기준(주말/휴장일 제외)으로 계산

### 6-5. 휴장일 캘린더 자동 갱신
- 런타임 파일: `kis_trend_atr_trading/data/market_calendar_krx.json`
- 환경변수로 파일 경로 오버라이드 가능: `MARKET_CALENDAR_FILE=/path/to/file.json`
- 수동 갱신: `python tools/build_market_calendar.py`
- 정기 갱신: GitHub Actions `Refresh KRX Market Calendar` (월 1회 + 수동 실행)

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
- 실운영 트레이딩 프로세스는 systemd 서비스(`auto-trade`) 기준으로 관리하는 것을 권장합니다.

점검 명령:
```bash
systemctl status cron
crontab -l
mkdir -p /home/deploy/KIS-API-Trend-ATR/logs
sudo systemctl status auto-trade
```

재배포 후 재시작(운영 기준):
```bash
cd /home/deploy/KIS-API-Trend-ATR
git pull
sudo systemctl restart auto-trade
sudo systemctl status auto-trade --no-pager
```

참고:
- `.github/workflows/deploy.yml`, `.github/workflows/deploy-oci.yml`의 `nohup python main.py`는 legacy 경로입니다.
- systemd로 운영 중이면 위 `systemctl restart auto-trade`가 실제 재기동 기준입니다.

### 9-1. 추천 프로필 원클릭 적용(REAL + 추세 민감도 튜닝)
서버에서 아래 명령 1회 실행:
```bash
cd /home/deploy/KIS-API-Trend-ATR
tools/deploy_recommended_real_profile.sh
```

적용 내용:
- `.env` 키 업데이트:
  - `EXECUTION_MODE=REAL`
  - `TRADING_MODE=REAL`
  - `ENABLE_REAL_TRADING=true`
  - `TREND_MA_PERIOD=35`
  - `ADX_THRESHOLD=22`
  - `ATR_SPIKE_THRESHOLD=3.0`
  - `ATR_PERIOD=14`
  - `ADX_PERIOD=14`
  - `DATA_FEED_DEFAULT=ws`
- systemd override 적용:
  - `auto-trade.service` 실행 인자를 `--interval 30`으로 고정
  - `daemon-reload` + `restart`

옵션 예시:
```bash
# 실제 반영 없이 미리보기
tools/deploy_recommended_real_profile.sh --dry-run

# systemd 변경 없이 .env만 반영
tools/deploy_recommended_real_profile.sh --no-systemd
```

적용 후 override 점검:
```bash
cd /home/deploy/KIS-API-Trend-ATR
tools/check_auto_trade_override.sh --sudo --strict
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

### 10-5. `Command 'python' not found`
- 원인: 서버에 `python` 심볼릭 링크가 없고 `python3`만 설치된 환경
- 조치:
  - 실행 명령을 모두 `python3 ...` 형태로 사용
  - 필요 시(시스템 정책 허용 시) `python-is-python3` 패키지 설치

### 10-6. WS 모드에서 `websockets package is required for WS feed`
- 원인: WS 의존 패키지 미설치
- 조치:
```bash
cd /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading
python3 -m pip install -r requirements.txt
```
- 확인:
```bash
python3 -m pip show websockets
```

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
