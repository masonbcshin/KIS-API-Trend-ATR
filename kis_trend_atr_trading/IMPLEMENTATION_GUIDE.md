# 실계좌 투입 수정 가이드 (Implementation Guide)

> 감사 보고서 지적 사항을 해결하기 위한 구체적인 수정 내용

---

## 1️⃣ 즉시 수정하지 않으면 실계좌 투입 불가한 항목 (Blocker)

### Blocker #1: 체결 확인 없는 포지션 상태 갱신

| 항목 | 내용 |
|------|------|
| **(a) 수정 대상** | `api/kis_api.py` - `place_buy_order()`, `place_sell_order()` |
| **(b) 현재 문제** | API 응답 성공만 확인하고 실제 체결 여부 미확인. 포지션 상태를 즉시 갱신. |
| **(c) 수정 후** | `wait_for_execution()` 메서드 추가. 체결 완료 확인 후에만 성공 반환. 미체결 시 자동 취소. |
| **(d) 미수정 시 손실** | 네트워크 지연 시 이중 매수 발생. 10주 주문이 두 번 체결되어 20주 보유. 손절 시 예상 손실의 2배 발생. |
| **(e) 난이도** | **Medium** |

**수정 내용**: `kis_api.py`에 다음 메서드 추가 완료

```python
def wait_for_execution(self, order_no, expected_qty, timeout_seconds=30):
    # 체결 대기 루프
    while time.time() - start < timeout_seconds:
        status = self.get_order_status(order_no)
        if status["exec_qty"] >= expected_qty:
            return {"success": True, "status": "FILLED", ...}
    # 타임아웃 시 취소
    self.cancel_order(order_no)
    return {"success": False, "status": "CANCELLED"}

def cancel_order(self, order_no):
    # 미체결 주문 취소 API 호출
```

---

### Blocker #2: 갭 보호 기본 비활성화

| 항목 | 내용 |
|------|------|
| **(a) 수정 대상** | `config/settings.py` - `ENABLE_GAP_PROTECTION` |
| **(b) 현재 문제** | `ENABLE_GAP_PROTECTION = False` (기본값) |
| **(c) 수정 후** | `ENABLE_GAP_PROTECTION = True`, `MAX_GAP_LOSS_PCT = 2.0` |
| **(d) 미수정 시 손실** | 시가가 손절가 아래에서 시작 시 손절선 무력화. 예: 손절가 66,000원인데 시가 60,000원 → -14% 손실 |
| **(e) 난이도** | **Low** |

**수정 내용**: `settings.py` 변경 완료

```python
# Before
ENABLE_GAP_PROTECTION = False
MAX_GAP_LOSS_PCT = 3.0

# After
ENABLE_GAP_PROTECTION = True
MAX_GAP_LOSS_PCT = 2.0
```

---

### Blocker #3: 단일 인스턴스 보장 부재

| 항목 | 내용 |
|------|------|
| **(a) 수정 대상** | `engine/multiday_executor.py` - `__init__()` |
| **(b) 현재 문제** | 동일 프로그램 중복 실행 가능. 두 인스턴스가 동시에 매수 신호 감지 시 이중 주문. |
| **(c) 수정 후** | 파일 락 기반 단일 인스턴스 강제. 두 번째 실행 시 즉시 종료. |
| **(d) 미수정 시 손실** | cron 오류나 수동 실행 실수로 이중 매수. 의도한 자금의 2배 노출. |
| **(e) 난이도** | **Medium** |

**수정 내용**: `engine/order_synchronizer.py`에 `SingleInstanceLock` 클래스 추가

```python
class SingleInstanceLock:
    def acquire(self):
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        
    def release(self):
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
```

---

### Blocker #4: 장 운영시간 체크 부재

| 항목 | 내용 |
|------|------|
| **(a) 수정 대상** | `engine/multiday_executor.py` - `execute_buy()`, `execute_sell()` |
| **(b) 현재 문제** | 동시호가(15:20~15:30) 시간대 주문 시 거부 가능. 손절 신호 발생해도 주문 실패. |
| **(c) 수정 후** | 정규장(09:00~15:20)에서만 신규 진입 허용. 청산은 동시호가에서도 시도. |
| **(d) 미수정 시 손실** | 15:25에 손절 신호 발생 → 주문 거부 → 익일까지 포지션 강제 보유 → 갭 리스크 노출 |
| **(e) 난이도** | **Medium** |

**수정 내용**: `engine/order_synchronizer.py`에 `MarketHoursChecker` 클래스 추가

```python
class MarketHoursChecker:
    def is_tradeable(self) -> Tuple[bool, str]:
        status = self.get_market_status()
        if status == MarketStatus.OPEN:
            return True, "정규장"
        elif status == MarketStatus.SIMULTANEOUS_QUOTE:
            return False, "동시호가 - 주문 차단"
        ...
```

---

### Blocker #5: 동적 실행 간격 부재

| 항목 | 내용 |
|------|------|
| **(a) 수정 대상** | `engine/multiday_executor.py` - `run()` |
| **(b) 현재 문제** | 고정 60초 간격. 손절선 근접해도 동일 간격 유지. 급락 시 최대 60초 손절 지연. |
| **(c) 수정 후** | 손절선 70% 도달 시 15초 간격으로 전환. 급락장 대응력 4배 향상. |
| **(d) 미수정 시 손실** | 손절가 66,000원, 현재가 66,500원 상태에서 60초 내 급락 시 손절 기회 상실 |
| **(e) 난이도** | **Low** |

**수정 내용**: `multiday_executor.py`에 동적 간격 로직 추가

```python
def _calculate_dynamic_interval(self) -> int:
    near_sl_pct = pos.get_distance_to_stop_loss(current_price)
    if near_sl_pct >= self._near_sl_threshold:  # 70%
        return self._near_sl_interval  # 15초
    return self._current_interval  # 60초
```

---

## 2️⃣ 설계 결정 자체를 뒤집어야 하는 항목 (Design Reversal)

### 설계 변경 #1: 체결 확인 방식

| 구분 | 기존 설계 A | 변경 설계 B |
|------|------------|------------|
| **방식** | API 응답 성공 → 즉시 포지션 상태 갱신 | 주문 → 체결 대기 → 확인 후 상태 갱신 |
| **흐름** | `place_order()` → `open_position()` | `place_order()` → `wait_for_execution()` → `open_position()` |
| **실패 처리** | 없음 (응답 실패만 처리) | 미체결 시 자동 취소, 부분체결 별도 처리 |

**B가 아니면 해결 불가 이유**: 
네트워크 지연, 부분체결, 미체결 상황에서 시스템 상태와 실제 계좌가 불일치. 이 불일치 위에서 모든 리스크 관리 로직이 작동하므로 전체 시스템이 무의미해짐.

---

### 설계 변경 #2: 상태 저장 단일화 전략

| 구분 | 기존 설계 A | 변경 설계 B |
|------|------------|------------|
| **저장소** | JSON 파일 + PostgreSQL (이중) | **API를 진실의 원천(Source of Truth)으로 사용** |
| **복원 시** | JSON 로드 → API 검증 | API 조회 → JSON/DB와 비교 → 불일치 시 API 기준 |
| **불일치 처리** | 경고만 | 자동 조정 또는 킬 스위치 |

**B가 아니면 해결 불가 이유**:
JSON과 DB 사이 불일치 발생 시 어느 것이 진실인지 판단 불가. 실제 계좌(API)만이 유일한 진실. 모든 복원은 API 기준으로 수행해야 함.

---

### 설계 변경 #3: 실행 주기(60초) 구조

| 구분 | 기존 설계 A | 변경 설계 B |
|------|------------|------------|
| **간격** | 고정 60초 | 동적 (15~60초) |
| **결정 기준** | 없음 | 손절선 근접도 기반 |
| **손절선 70% 도달 시** | 60초 유지 | 15초로 단축 |

**B가 아니면 해결 불가 이유**:
급락장에서 60초는 영겁. 손절 신호 발생 후 실제 체결까지 최악의 경우 60초 지연. 이 시간 동안 추가 -5% 이상 손실 가능. 위험 구간에서만 빈도를 높여 대응력 확보.

---

## 3️⃣ 실제 코드 수정 가이드

### 3.1 주문 → 체결 확인 → 상태 반영 동기화 흐름

**파일**: `engine/multiday_executor.py`

**수정 순서**:

```
1. execute_buy() 진입
   │
   ├─ 2. 리스크 체크 (check_order_allowed)
   │
   ├─ 3. 장 운영시간 체크 (market_checker.is_tradeable)
   │      └─ 동시호가/폐장 시 → 즉시 반환 (주문 불가)
   │
   ├─ 4. 동기화 주문 실행 (order_synchronizer.execute_buy_order)
   │      │
   │      ├─ 4a. api.place_buy_order() 호출
   │      │
   │      ├─ 4b. api.wait_for_execution() 호출 (최대 45초 대기)
   │      │      │
   │      │      ├─ 완전 체결 → success=True, status="FILLED"
   │      │      ├─ 부분 체결 → success=False, status="PARTIAL"
   │      │      └─ 미체결 → api.cancel_order() 호출 → status="CANCELLED"
   │      │
   │      └─ 4c. SynchronizedOrderResult 반환
   │
   ├─ 5. 결과에 따른 분기
   │      │
   │      ├─ FILLED → strategy.open_position(실제_체결가)
   │      │           position_store.save_position()
   │      │           telegram.notify_buy_order()
   │      │
   │      ├─ PARTIAL → 체결된 수량만큼 position 생성
   │      │            telegram.notify_warning("부분체결")
   │      │
   │      └─ FAILED/CANCELLED → 포지션 상태 변경 없음
   │
   └─ 6. 결과 반환
```

---

### 3.2 프로그램 재시작 시 포지션 복구 절차

**파일**: `engine/multiday_executor.py` - `restore_position_on_start()`

**복구 순서**:

```
1. API 토큰 발급 (api.get_access_token)
   │
2. 실제 보유 조회 (api.get_account_balance)
   │
3. JSON 저장 데이터 로드 (position_store.load_position)
   │
4. 동기화 비교 (position_resync.synchronize_on_startup)
   │
   ├─ 케이스 1: 저장 없음 + 보유 없음
   │      └─ 정상 - 포지션 없음으로 시작
   │
   ├─ 케이스 2: 저장 없음 + 보유 있음
   │      └─ 미기록 보유 발견 - 경고 + 수동 확인 요청
   │
   ├─ 케이스 3: 저장 있음 + 보유 없음
   │      └─ 저장 데이터 무효 - 삭제 + 포지션 없음으로 시작
   │
   ├─ 케이스 4: 저장 있음 + 보유 있음 (종목 일치)
   │      │
   │      ├─ 수량 일치 → 정상 복원
   │      └─ 수량 불일치 → API 기준 수량 조정 후 복원
   │
   └─ 케이스 5: 저장 있음 + 다른 종목 보유
          └─ 심각한 불일치 - 킬 스위치 권장
```

---

### 3.3 API 오류 후 재동기화 시퀀스

**파일**: `engine/order_synchronizer.py` - `PositionResynchronizer`

**재동기화 시퀀스**:

```
1. API 오류 발생 (예: 네트워크 단절)
   │
2. 재연결 시도 (최대 3회, 지수 백오프)
   │
3. 연결 복구 후
   │
   ├─ 3a. 실제 보유 조회 (force_sync_from_api)
   │
   ├─ 3b. 현재 메모리 상태와 비교
   │
   └─ 3c. 불일치 시
          │
          ├─ 메모리에 포지션 있음 + API에 보유 없음
          │      └─ 청산 완료로 간주 → 메모리 클리어
          │
          ├─ 메모리에 포지션 없음 + API에 보유 있음
          │      └─ 미기록 보유 → 경고 + 수동 확인
          │
          └─ 수량 불일치
                 └─ API 기준 수량으로 조정
```

---

## 4️⃣ 기본값(Default)로 바뀌어야 할 설정 목록

| 설정 | 현재 값 | 권장 값 | 변경 근거 |
|------|--------|--------|----------|
| `ENABLE_GAP_PROTECTION` | `False` | **`True`** | 갭 다운 시나리오 차단 |
| `MAX_GAP_LOSS_PCT` | `3.0` | **`2.0`** | 갭 손실 허용 범위 축소 |
| `DAILY_MAX_LOSS_PERCENT` | `3.0` | **`2.0`** | 일일 손실 한도 강화 |
| `DAILY_MAX_LOSS_PCT` | `10.0` | **`5.0`** | position_store 한도 강화 |
| `DAILY_MAX_TRADES` | `5` | **`3`** | 과잉 거래 방지 |
| `MAX_CONSECUTIVE_LOSSES` | `3` | **`2`** | 연속 손실 시 조기 중단 |
| `ORDER_EXECUTION_TIMEOUT` | `30` | **`45`** | 유동성 낮은 종목 대비 |
| `API_TIMEOUT` | `10` | **`15`** | 네트워크 지연 대비 |
| `DEFAULT_EXECUTION_INTERVAL` | `60` | **`60`** (유지) | 기본 간격 |
| `NEAR_STOPLOSS_EXECUTION_INTERVAL` | (없음) | **`15`** (신규) | 손절 근접 시 빠른 대응 |
| `ENFORCE_SINGLE_INSTANCE` | (없음) | **`True`** (신규) | 중복 실행 방지 |
| `MAX_CUMULATIVE_DRAWDOWN_PCT` | (없음) | **`15.0`** (신규) | 누적 낙폭 제어 |

---

## 5️⃣ 실계좌 투입 가능 여부 판단

### ⚠️ 조건부 실계좌 투입 가능

다음 조건을 **모두 충족**해야 실계좌 투입 가능:

#### 필수 체크리스트

- [ ] **체결 동기화**: `ENABLE_SYNCHRONIZED_ORDERS = True` 설정 확인
- [ ] **갭 보호**: `ENABLE_GAP_PROTECTION = True` 설정 확인
- [ ] **단일 인스턴스**: `ENFORCE_SINGLE_INSTANCE = True` 설정 확인
- [ ] **일일 손실 한도**: `DAILY_MAX_LOSS_PERCENT <= 2.0` 확인
- [ ] **누적 드로다운 한도**: `MAX_CUMULATIVE_DRAWDOWN_PCT <= 15.0` 확인
- [ ] **테스트 완료**: 모의투자(PAPER) 모드에서 최소 5거래일 테스트
- [ ] **포지션 동기화 테스트**: 프로그램 재시작 후 포지션 복원 정상 동작 확인
- [ ] **장 운영시간 테스트**: 동시호가 시간대(15:20~15:30) 주문 차단 확인
- [ ] **텔레그램 알림**: 모든 알림 정상 수신 확인
- [ ] **수동 킬 스위치 테스트**: `data/KILL_SWITCH` 파일 생성 시 즉시 중단 확인

#### 조건 위반 시 발생하는 실패 시나리오

| 위반 조건 | 실패 시나리오 |
|----------|--------------|
| 체결 동기화 OFF | 네트워크 지연 시 이중 매수 → 손실 2배 |
| 갭 보호 OFF | 갭 다운 시 손절선 무력화 → 예상 외 대형 손실 |
| 단일 인스턴스 OFF | cron 오류로 중복 실행 → 이중 포지션 |
| 일일 손실 한도 초과 | 연속 손실 누적 → 계좌 급격히 훼손 |
| 포지션 복원 미테스트 | 재시작 후 좀비 포지션 또는 미기록 보유 |

---

## 최종 강제 질문 답변

> **시간이 없다는 이유로 가장 많이 생략될 가능성이 높은 항목**

### "체결 확인 동기화 로직" (`wait_for_execution()`)

**이유**: 
- 구현 복잡도가 가장 높음
- 기존 코드 흐름 전체 변경 필요
- "API 응답만 확인해도 대부분 문제없다"는 착각
- 테스트가 어려움 (실제 네트워크 지연 재현 필요)

**생략 시 동일한 실패가 반복되는 이유 (단 하나의 문장)**:

> **시스템이 인식하는 포지션과 실제 계좌가 다르면, 그 위에서 작동하는 모든 손절/익절/리스크 관리 로직은 잘못된 데이터를 기반으로 잘못된 결정을 내리게 되고, 이것이 바로 "시장 탓"이 아닌 "설계 실패"로 귀결되는 원인이다.**

---

## 수정 파일 요약

| 파일 | 변경 유형 | 주요 내용 |
|------|----------|----------|
| `api/kis_api.py` | 수정 | `wait_for_execution()`, `cancel_order()` 메서드 추가 |
| `engine/order_synchronizer.py` | **신규** | `SingleInstanceLock`, `MarketHoursChecker`, `OrderSynchronizer`, `PositionResynchronizer` 클래스 |
| `engine/multiday_executor.py` | 수정 | 체결 동기화 통합, 동적 실행 간격, 포지션 재동기화 |
| `config/settings.py` | 수정 | 기본값 변경, 신규 설정 추가 |

---

> **문서 작성 완료: 2026-01-29**

---

## 6️⃣ Daily Universe + Holdings 운영 정책 (2026-02-12)

### 핵심 운영 원칙

- `holdings_symbols`: 현재 OPEN 포지션 종목 전체 (항상 관리/청산 대상)
- `todays_universe`: 당일 신규 진입 후보 리스트
- `entry_candidates = todays_universe - holdings_symbols`
- `max_positions`: 동시 보유 상한 (신규 진입에만 적용)
- `universe_size`: 당일 유니버스 크기

### 실행 흐름

1. 시작 시 `UniverseService.load_holdings_symbols()`로 보유종목 로드
2. `UniverseService.get_or_create_todays_universe(trade_date)` 실행
3. 오늘 레코드가 있으면 재생성 없이 재사용
4. `compute_entry_candidates()`로 신규 진입 후보 계산
5. 루프마다:
   - 보유 종목 전체는 무조건 실행(Exit/Stop/Trailing 유지)
   - 신규 진입은 `entry_candidates`만 허용
   - `holdings_count >= max_positions`면 신규 진입 차단

### 장애/폴백 정책

- 유니버스 갱신 실패 시:
  1. 오늘 캐시 사용
  2. 없으면 `fixed.stocks` 사용
  3. 없으면 빈 유니버스(신규 진입 중단)
- 어떤 경우에도 보유 종목 관리/청산은 중단하지 않음

### 운영 시나리오

#### 시나리오 A: 보유 종목이 TopN에서 제외됨
- 상태: `holdings_symbols=["005930"]`, `todays_universe=["000660","035720",...]`
- 동작: `005930`은 계속 Exit/Stop/Trailing 감시
- 신규 진입은 `["000660","035720",...]`에서만 탐색

#### 시나리오 B: 보유 수가 상한 도달
- 상태: `holdings_count=10`, `max_positions=10`
- 동작: 신규 진입 전부 차단
- 보유 포지션의 청산/손절 처리는 계속 수행

#### 시나리오 C: 재시작
- 같은 거래일 재시작: 오늘 유니버스 재사용(재생성 금지)
- 다음 거래일 진입: 신규 거래일 유니버스 1회 생성 후 저장
