# KIS Trend-ATR Trading System 운영 매뉴얼

이 문서는 `kis_trend_atr_trading`의 **운영 기준 문서**입니다.
목적은 다음 4가지입니다.

- 신규 개발자가 구조를 빠르게 이해
- 운영자가 장애 시 원인 추적
- PAPER -> REAL 전환 체크리스트 제공
- Universe 선정/재시작 정책을 코드 기준으로 명확화

> 경고
> - 이 문서는 2026-02-12 기준 `main_multiday.py` 실행 경로를 기준으로 작성됨
> - 코드와 불일치하는 절차를 임의로 추가하지 마십시오

---

## 통합 엔트리포인트 (신규)

- 운영: `python -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed rest`
- 운영(WS): `python -m kis_trend_atr_trading.apps.kr_trade --mode trade --feed ws`
- CBT: `python -m kis_trend_atr_trading.apps.kr_cbt --mode cbt`

호환성 유지:

- 기존 `main.py`, `main_v2.py`, `main_v3.py`, `main_multiday.py`, `main_cbt.py`는
  deprecated thin wrapper로 유지되며 기존 커맨드는 계속 실행 가능합니다.
- 내부 구현은 `deprecated/legacy_main*.py`로 보존됩니다.

---

## 1️⃣ 시스템 개요

### 전략 개요 (Trend + ATR)

- 전략 클래스: `strategy/multiday_trend_atr.py`
- 진입: 추세 + 돌파 + 변동성 조건
- 청산: ATR 손절/익절, 트레일링, 추세 붕괴, 갭 보호
- 멀티데이 보유를 전제로 하며, 시간기반 EOD 강제청산 로직을 사용하지 않음

### 지원 모드 (PAPER / REAL)

- 트레이딩 모드 판별: `env.py` (`get_trading_mode()`)
- 허용값: `PAPER`, `REAL`
- 기본값: `PAPER`
- `TRADING_MODE`가 허용값 외 값이면 시작 실패
- `.env`와 런타임 환경변수 `TRADING_MODE` 불일치 시 시작 실패

### 서버 환경 (GCP e2-micro 기준)

- 기준 사양: vCPU 2 / RAM 1GB
- DB 커넥션 풀 상한: `pool_size <= 5` (`db/mysql.py`)
- 로그 로테이션: 파일당 10MB, 백업 10개 (`utils/logger.py`)
- 단일 인스턴스 락 사용: `data/instance.lock` + stale timeout 기본 3600초 (`engine/order_synchronizer.py`)

---

## 2️⃣ 전체 아키텍처

### 모듈 구조

- `main_multiday.py`: 엔트리포인트, CLI 파싱, 모드 검증, Universe 선정, 실행 시작
- `engine/multiday_executor.py`: 전략 실행 루프, 주문 실행, 재시작 복원, 네트워크 단절 대응
- `engine/order_synchronizer.py`: 주문 동기화, idempotency, `order_state` 복구, 단일 인스턴스 락
- `engine/risk_manager.py`: 손실 한도/킬스위치
- `api/kis_api.py`: KIS API 호출, timeout/retry/backoff, 토큰 관리, 체결 대기
- `universe/universe_selector.py`: 장전 Universe 1회 선정 + 장중 캐시 재사용
- `db/mysql.py`, `db/repository.py`: 트랜잭션/스키마/레코드 저장
- `utils/logger.py`, `utils/telegram_notifier.py`: 로그/알림

### 주문 흐름 다이어그램

```text
[Signal 생성]
   -> [RiskManager 주문 허용 검사]
   -> [OrderSynchronizer idempotency key 생성]
   -> [order_state=PENDING upsert]
   -> [KIS 주문 전송]
   -> [order_state=SUBMITTED upsert]
   -> [wait_for_execution() 체결 대기]
      -> FILLED  : order_state=FILLED, 포지션/거래 반영
      -> PARTIAL : order_state=PARTIAL, 부분체결 수량 반영
      -> TIMEOUT/CANCELLED: order_state=CANCELLED/실패 처리
```

### UniverseSelector 동작 흐름

```text
[select() 호출]
   -> 장중(09:00~15:30 KST)?
      -> Yes: 오늘 cache(date==today) 있으면 재사용
             없으면 fallback 정책 수행
      -> No : selection_method 기반 재선정 후 cache 저장

selection_method:
  fixed      -> stocks[:max_stocks]
  volume_top -> 거래대금 상위 + 안전필터
  atr_filter -> 후보풀(설정) + ATR% 필터
  combined   -> volume_top(max*3) -> ATR 필터 -> max_stocks
```

---

## 3️⃣ 실행 흐름

### 장 시작 전

1. `TRADING_MODE`/환경 검증 (`validate_environment()`)
2. REAL이면 `--confirm-real-trading` 필수 + 10초 경고 대기
3. API 토큰 발급
4. Universe 선정 1회 (`UniverseSelector.select()`)
5. `MultidayExecutor` 생성
6. 포지션 복원 (`restore_position_on_start()`)

### 장중

- `run_once()` 주기 실행 (최소 60초, 손절 근접 시 15초)
- 시세/일봉 조회 -> 시그널 계산 -> 주문 동기화 실행
- 네트워크 단절이 60초 이상이면 해당 사이클에서 거래 중단
- 장중 재시작 시 Universe 재계산 금지(캐시 재사용)

### 장 종료 후

- 강제청산 없음
- 포지션이 있으면 `data/positions.json`에 저장 유지
- 프로세스 종료 시 일일 요약 로그/알림

### 재시작 시 동작

- 단일 인스턴스 락 확인
- `order_state`에서 `PENDING/SUBMITTED/PARTIAL` 조회
- 포지션 복원:
  - PAPER: 모의계좌 보유 조회 + 저장 데이터 대조 + DB 포지션 동기화
  - REAL: 실계좌 보유 조회 + 저장 데이터 대조 + DB 포지션 동기화
  - 불일치 발생 시 API 계좌 기준 자동복구(저장 보정/정리) 후 텔레그램 알림

---

## 4️⃣ Universe 선정 로직

설정 파일: `config/universe.yaml`

### fixed

- 구현 상태: 사용 중
- 동작: `stocks` 목록에서 순서 유지, `max_stocks`까지만 사용
- 설정 위치: `universe.stocks` 권장, 하위호환으로 루트 `stocks`도 지원(동시 존재 시 `universe.stocks` 우선)
- 회귀 테스트: `tests/test_universe_selector_unittest.py`

### volume_top

- 구현 상태: 사용 중
- 풀 모드:
  - `candidate_pool_mode=yaml`(restricted): `candidate_stocks` 또는 `stocks` 내부에서만 정렬 (`universe.stocks` 우선, 루트 `stocks` 하위호환)
- `candidate_pool_mode=market`: 시장 후보군 스캔(가능하면 API universe, 없으면 KOSPI200 대체)
- `market_scan_size`: market 모드에서 실제 스캔할 후보군 상한 (combined 1차에도 동일 적용)
- 1차 후보: 풀 모드별 후보군
- 거래대금 기준 정렬
- 필터:
  - `min_volume`
  - `min_market_cap` (시총값 존재 시만 적용)
  - 거래정지 제외 (`is_suspended`)
  - 관리종목 제외 (`is_management`, 설정값 반영)
  - 시가 대비 변동률 절대값 28% 이상 제외
- API 호출 최적화:
  - `get_market_snapshot_bulk`가 있으면 bulk 우선
  - 없으면 snapshot 반복 조회 + 주기적 sleep

### atr_filter

- 구현 상태: 사용 중
- 후보풀(`candidate_pool_mode`): `kospi200 | yaml | volume_top | market`
- ATR 비율 계산: `(ATR / 종가) * 100`
- 필터: `min_atr_pct <= ratio <= max_atr_pct`
- 제외 조건:
  - 최근 데이터 20개 미만
  - 종가 `<= 0`

### combined

- 구현 상태: 사용 중
- 단계:
  1. `volume_top(max_stocks * 3)`
  2. ATR 필터 적용
  3. `max_stocks`로 최종 제한
- 주의:
  - `candidate_pool_mode=yaml`이면 `combined`도 restricted 모드(후보군 내부 선별)로 동작
  - 시장 자동선정이 목적이면 `candidate_pool_mode=market`을 사용

### 캐싱 구조

파일: `data/universe_cache.json`

저장 필드:

- `date`
- `stocks`
- `selection_method`

정책:

- 장전: 재계산 후 캐시 갱신
- 장중 재시작: 캐시 재사용
- 장중이라도 `selection_method`가 캐시 메서드와 다르면 캐시 즉시 무효화 후 재선정
- 로그에 `CACHE HIT/MISS`, 사유(reason), `cache_key`, `cache_file` 출력

### fallback 정책

- 자동선정 실패 시 `fixed`로 fallback 가능 (`fallback_to_fixed=true`)
- REAL에서 fallback 발생 시 콘솔 경고 + 10초 대기
- `halt_on_fallback_in_real=true`면 REAL에서 fallback 즉시 거래 중단

### 강제 제약

- 중복 종목 제거
- 종목코드 6자리 숫자 검증
- `max_stocks` 초과 금지
- 최종 0종목이면 예외로 거래 중단

---

## 5️⃣ 안전 설계

### PAPER 모드 이중 안전장치

- `TRADING_MODE` 기본값은 PAPER (`env.py`)
- PAPER 경로에서 `assert_not_real_mode()` 실행
- REAL 실행은 `--confirm-real-trading` 없으면 즉시 종료
- PAPER 모드에서 실계좌 전용 키(`REAL_KIS_*`)가 감지되면 시작 종료

### RISK MANAGER STATUS 해석

- `당일 실현 손익`: 당일 청산된 거래의 누적 손익(리스크 한도 판정 기준)
- `계좌 평가손익`: KIS 잔고조회(`get_account_balance`) 기준 평가손익
- 두 값은 목적이 다르므로 동일하지 않을 수 있음
- 멀티종목 실행에서는 공용 `RiskManager` 인스턴스를 공유하여 계좌 단위 한도로 관리

### idempotency key

- 생성 위치: `engine/order_synchronizer.py`
- 규칙: `mode|side|stock_code|quantity|signal_id` SHA-256
- 저장: `order_state.idempotency_key` unique key
- 거래 기록: `trades.idempotency_key` unique index

### DB 트랜잭션

- `autocommit=False`
- 트랜잭션 컨텍스트에서 commit/rollback 명시
- 세션 시작 시 `READ COMMITTED` 설정

### Race condition 방지

- 현재 적용:
  - 단일 인스턴스 파일락
  - idempotency unique 제약
- 현재 미적용:
  - `SELECT ... FOR UPDATE`
  - optimistic locking version column
- 운영 권고:
  - 실계좌 전에는 단일 프로세스/단일 서비스로만 운용

### API retry 정책

- 일반 API: 최대 3회, exponential backoff (`RETRY_DELAY * 2^attempt`)
- 주문 API: 중복 주문 위험 때문에 재시도 0회 강제

### Gap Protection 발동 정책

- 기준 수식: `raw_gap_pct = ((open_price - reference_price) / reference_price) * 100`
- 발동 조건(롱 전용): `raw_gap_pct <= -(gap_threshold_pct + gap_epsilon_pct)`
- 비발동 조건:
  - 이익 갭 (`raw_gap_pct > 0`)
  - 본전/미세 노이즈 (`0%` 근처)
  - `gap_threshold_pct` 누락 또는 `<= 0` (정책상 비활성화)
- `abs()` 비교는 사용하지 않음
- 로그/텔레그램에 `raw_gap_pct`와 표시값(`display`)을 함께 출력
- 텔레그램 `시가(open_price)`와 `기준가(reference_price)`는 갭 계산에 실제 사용된 값과 동일하게 출력
- 갭 청산 주문 직전 로그:
  - `symbol, open, base_label, base_price, gap_pct, threshold, triggered, reason`

---

## 6️⃣ 예외 처리

### API 타임아웃

- HTTP timeout: `API_TIMEOUT=15`초
- 일반 API는 최대 3회 재시도
- 주문은 1회 호출 후 체결 대기로 상태 확인

### 네트워크 단절

- 요청 실패 시 단절 시작 시각 기록
- 60초 이상 단절이면 실행 사이클에서 거래 중단 에러 반환
- 네트워크 복구 감지 시 단절 시간 로그

### 장종료/주문불가 반복 방지

- 동일 종목 + 동일 청산사유(`exit_reason/reason_code`)에서 주문 실패가 장종료/주문불가로 판정되면 `pending_exit`로 전환
- `PENDING_EXIT_BACKOFF_MINUTES` 동안 동일 주문 재시도 차단
- 장중/주문 가능 시점 도달 시 1회 재시도 후 성공하면 `pending_exit` 해제
- 상태 변경 시에만 알림:
  - pending 전환
  - pending 해제(주문 성공/사유 변경)

### 토큰 재발급

- 토큰 락(`threading.Lock`)으로 동시 갱신 충돌 방지
- 당일(KST 기준 date) 재사용/재발급 관리
- 만료 10분 전 또는 날짜 변경 시 재발급
- 재발급 실패 시 API 예외로 상위 로직에 전파

### 데이터 이상

- 현재가 `<= 0`이면 주문 로직 진행 안 함
- Universe snapshot에서 `price==0`은 후보 제외
- ATR 계산 시 종가 `<=0` 또는 데이터 부족이면 제외

---

## 7️⃣ 재시작 복구 전략

### open_orders / pending / partial 복구

- `order_state` 테이블에서 `PENDING/SUBMITTED/PARTIAL` 조회
- 프로그램 시작 직후 복구 대상 건수 로그 출력
- `PENDING_ORDER_STALE_MINUTES`(기본 240분) 이상 갱신 없는 미종결 건은 시작 시 `CANCELLED`로 자동 정리
- `PENDING_NO_ORDER_STALE_MINUTES`(기본 15분): 주문번호 없는 `PENDING` 건 조기 정리

### partial fill 복구

- 체결 대기 타임아웃 후 부분체결이면 미체결 취소 시도
- `order_state`에 `PARTIAL` 상태와 잔여수량 저장

### 중복 주문 방지

- 동일 signal_id 기반 idempotency key 재생성
- 기존 상태가 `PENDING/SUBMITTED/PARTIAL/FILLED`면 신규 주문 차단

### 포지션 정합성

- 저장소: `data/positions.json`
- REAL 모드: 실계좌 보유와 저장 포지션 비교
  - 불일치 시 경고/정리/중단 액션 분기
  - DB `positions`를 실계좌 기준으로 upsert/close 동기화

---

## 8️⃣ 로그 및 감사 추적

### 필수 로그 항목

- 전략 시그널 (`BUY/SELL/HOLD`, 사유)
- 주문 시도/성공/실패
- 체결 대기/부분체결/취소
- Universe 단계별 후보 수와 최종 종목
- 시작 시점 git commit hash

### 로그 레벨 정책

- PAPER: INFO
- REAL: INFO
- 파일 로그: `~/auto-trade/logs` (기본, `AUTO_TRADE_LOG_DIR`로 변경 가능)
- 로테이션: 10MB x 10개

### 에러 알림

- ERROR 경로에서 Telegram 알림 전송 (`notify_error`)
- 예외: 시작 시 `positions` 계좌→DB 재동기화 중 개별 upsert 실패는 소프트 실패로 처리되어 로그 경고(`포지션 저장 실패/보류`)만 남고 Telegram ERROR는 전송하지 않음
- Slack 연동은 현재 코드 경로에 없음

### 종목명 해석/캐시 (Telegram)

- 대상: 종목코드가 포함되는 알림 메시지
- 출력 형식: 항상 `종목명(종목코드)` (예: `삼성전자(005930)`)
- 해석 순서:
  - 메모리 캐시 (`utils/symbol_resolver.py`)
  - SSOT DB 캐시 (`symbol_cache` 테이블)
  - KIS API 재조회 (`KISApi.get_account_balance()`의 `holdings[].stock_name`, 원본 키: `output1[].prdt_name`)
  - 실패 폴백: 기존 캐시값 유지, 없으면 `UNKNOWN(코드)`
- TTL 정책:
  - `updated_at` 기준 30일 이내는 캐시값 즉시 사용
  - 30일 초과 시 다음 요청에서 갱신 시도 (실패해도 알림/거래 흐름 중단 없음)
- 캐시 확인:
  - `SELECT stock_code, stock_name, updated_at FROM symbol_cache ORDER BY updated_at DESC LIMIT 20;`

### 감사 추적

- `order_state`에 `order_no`, `fill_id`, `status` 저장
- `trades`, `account_snapshots` 테이블 존재
- 보조 감사 로그: `logs/audit/audit_YYYYMMDD.json`

---

## 9️⃣ 5거래일 검증 절차

아래는 운영 전 리허설 기준입니다.

### Day1 정상 매수/매도

- 목표: 기본 주문 사이클 확인
- 자동 테스트: 부분 가능
  - `python -m pytest kis_trend_atr_trading/tests/test_integration.py -q`

### Day2 손절 트리거

- 목표: 손절 시그널과 청산 흐름 확인
- 자동 테스트: 부분 가능
  - `python -m pytest kis_trend_atr_trading/tests/test_integration.py::TestCompleteTradingCycle::test_stop_loss_cycle -q`

### Day3 장중 재시작

- 목표: 재시작 후 포지션/미종결 주문 정합성 확인
- 자동 테스트: 부분 가능
  - `python -m pytest kis_trend_atr_trading/tests/test_executor.py::TestPositionRecognitionAfterRestart::test_position_lost_after_restart_simulation -q`
- 운영 점검(수동):
  - 장중 프로세스 재시작 후 Universe가 캐시 재사용되는지
  - `order_state` 복구 로그가 출력되는지

### Day4 메모리 압박

- 목표: e2-micro에서 장시간 실행 안정성 확인
- 자동 테스트: 없음 (수동 필요)
- 운영 점검(수동):
  - RSS 메모리, 로그 파일 증가 속도, DB 커넥션 수
  - 장중 6시간 연속 실행 후 재시작 복구

### Day5 API timeout

- 목표: timeout/retry/backoff 및 거래중단 플래그 확인
- 자동 테스트: 가능
  - `python -m pytest kis_trend_atr_trading/tests/test_api.py::TestRetryLogic::test_retry_on_timeout -q`

---

## 🔟 실계좌 전환 체크리스트

아래 항목이 모두 충족되기 전에는 REAL 운용을 시작하지 마십시오.

### 1) 환경변수 점검

- `TRADING_MODE=REAL`
- `.env`와 런타임 `TRADING_MODE` 일치
- PAPER 전용/REAL 전용 키 혼재 없음

### 2) 실행 인자 점검

- `--confirm-real-trading` 필수
- 첫 주문 제한 비율 확인 (`--real-first-order-percent`, 기본 10)
- 첫날 종목수 제한 활성 확인 (`--real-limit-symbols-first-day`, 기본 활성)

### 3) Universe 설정 점검

- `config/universe.yaml`의 `selection_method` 확인
- `fallback_to_fixed`, `halt_on_fallback_in_real` 의도대로 설정
- `data/universe_cache.json` 권한/경로 확인

### 4) DB 점검

- `initialize_schema()` 수행 또는 테이블 존재 확인
- 특히 `order_state` 존재 확인
- MySQL 연결/권한/타임존(KST)/격리수준(READ COMMITTED) 확인

### 5) 로그/알림 점검

- `~/auto-trade/logs` 기록 확인
- Telegram ERROR 알림 수신 확인
- 시작 로그에 git commit hash 출력 확인

### 6) 첫날 제한 운용 권장

- 종목 1개만 운용
- 주문 수량은 최대치의 10% 이내
- 장중 재시작 1회 리허설 후 지속 운용

---

## 실행 명령 예시

### 거래 실행 (PAPER)

```bash
cd /home/user/KIS-API-Trend-ATR/kis_trend_atr_trading
TRADING_MODE=PAPER python main_multiday.py --mode trade --interval 60
```

### 거래 실행 (REAL)

```bash
cd /home/user/KIS-API-Trend-ATR/kis_trend_atr_trading
TRADING_MODE=REAL python main_multiday.py --mode trade \
  --confirm-real-trading \
  --real-first-order-percent 10 \
  --real-limit-symbols-first-day
```

### 핵심 테스트

```bash
python -m unittest kis_trend_atr_trading.tests.test_universe_selector_unittest -v
python -m unittest kis_trend_atr_trading.tests.test_gap_protection_unittest -v
python -m unittest kis_trend_atr_trading.tests.test_gap_notification_alignment_unittest -v
python -m unittest kis_trend_atr_trading.tests.test_main_multiday_multi_symbols_unittest -v
python -m unittest kis_trend_atr_trading.tests.test_pending_exit_unittest -v
python -m pytest kis_trend_atr_trading/tests/test_api.py -q
python -m pytest kis_trend_atr_trading/tests/test_executor.py::TestPositionRecognitionAfterRestart::test_position_lost_after_restart_simulation -q
```

---

## 운영자가 먼저 확인할 파일

- 실행/안전 가드: `main_multiday.py`, `env.py`
- 주문 동기화/복구: `engine/order_synchronizer.py`
- 멀티데이 루프: `engine/multiday_executor.py`
- Universe 정책: `universe/universe_selector.py`, `config/universe.yaml`
- DB/스키마: `db/mysql.py`, `db/schema_mysql.sql`
- 로그/알림: `utils/logger.py`, `utils/telegram_notifier.py`
