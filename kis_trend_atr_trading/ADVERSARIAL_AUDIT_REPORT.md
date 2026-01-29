# 적대적 시스템 감사 보고서 (Adversarial Trading Auditor Report)

> **경고**: 이 보고서는 시스템의 "안전함"을 증명하기 위한 것이 아닙니다.
> 실계좌 투입 전 반드시 해결해야 할 위험 요소들을 식별하기 위한 것입니다.

---

## 1️⃣ 자본 보존 관점: 계좌 '0원' 경로 분석

### 1.1 갭 다운 연쇄 실패 시나리오 (심각도: 치명적)

```
[Day 1] 삼성전자 70,000원 매수 → 손절가 66,000원 설정 (ATR 2x)
[Day 2 장전] 악재로 시가 60,000원 갭 다운 (손절가 아래에서 시작)
→ ENABLE_GAP_PROTECTION = False (기본값)이므로 즉시 청산 안 함
→ 손절 신호 발생하지만 이미 -14.3% 손실
→ 시장가 매도 체결까지 60초 대기 (실행 간격)
→ 추가 하락 시 -20% 이상 손실 확정
```

**문제점**: 갭 보호가 기본 비활성화 상태. 멀티데이 전략에서 이는 자살 행위.

### 1.2 API 체결 확인 부재로 인한 포지션 누적 (심각도: 치명적)

```python
# multiday_executor.py 337-357행 분석
result = self.api.place_buy_order(...)
if result["success"]:
    self.strategy.open_position(...)  # ← 체결 확인 없이 포지션 오픈
```

**시나리오**:
1. 매수 주문 API 호출 → 네트워크 지연으로 타임아웃
2. 실제로 주문은 접수되어 체결됨
3. 코드는 실패로 인식 → 재시도 → 동일 종목 이중 매수
4. 반복되면 의도치 않은 레버리지 효과 발생

### 1.3 일일 손실 한도의 무력화 경로

**설정 분석**:
```python
DAILY_MAX_LOSS_PERCENT = 3.0  # 일일 3% 손실 시 거래 중단
MAX_LOSS_PCT = 5.0  # 단일 거래 최대 5% 손실
```

**누적 붕괴 계산**:
- 20 거래일 연속 일일 한도(-3%) 도달 시: 0.97^20 = 0.5438 → **-45.6% 손실**
- 일일 한도는 "당일"만 보호. 장기적 드로다운에 무방비

**추가 허점**:
- `_daily_pnl.starting_capital`이 고정값으로 설정됨
- 전날 손실 반영 없이 매일 동일 자본 기준으로 3% 계산
- 복리 손실 누적을 막지 못함

### 1.4 60초 실행 간격의 치명적 공백

```python
# multiday_executor.py 725-781행
def run(self, interval_seconds: int = 60, ...):
    if interval_seconds < 60:
        interval_seconds = 60  # 최소 60초 강제
```

**극단적 상황**:
- t=0: 현재가 67,000원 (손절가 66,000원, 진입가 70,000원)
- t=30초: 악재 발생 → 가격 65,000원 급락 (손절선 이탈)
- t=60초: 다음 체크 시 가격 60,000원
- **30초 동안 손절 기회를 놓치고 추가 -7.5% 손실 발생**

---

## 2️⃣ 상태 불일치 관점: 5가지 이상 시나리오

### 시나리오 1: API 성공 + DB 저장 실패

```python
# repository.py 251-285행
result = self.db.execute_command(
    "INSERT INTO positions ...",
    ...,
    returning=True
)
if result:
    logger.info(f"[REPO] 포지션 저장: ...")
    return PositionRecord.from_dict(result)
return None  # ← DB 실패 시 None 반환, 하지만 이미 주문은 체결됨
```

**상태 불일치**:
- 실제 계좌: 주식 보유 중
- DB positions 테이블: 레코드 없음
- 메모리 (strategy.position): 포지션 존재

**결과**: 프로그램 재시작 시 포지션 복원 불가 → 청산되지 않은 좀비 포지션

### 시나리오 2: 파일 저장 vs DB 불일치

**저장 경로 분석**:
1. `utils/position_store.py` → `data/positions.json` (JSON 파일)
2. `db/repository.py` → PostgreSQL positions 테이블

```python
# multiday_executor.py 356-357행
self._save_position_on_exit()  # JSON 파일 저장

# 하지만 DB 저장은?
# db/repository.py의 save()가 별도로 호출되어야 함
```

**불일치 발생**:
- JSON 파일: 포지션 존재
- PostgreSQL: 레코드 없거나 상태 불일치
- 어느 것이 진실인가?

### 시나리오 3: 주문 타임아웃 후 실제 체결

```python
# kis_api.py 116-170행
response = requests.post(
    url,
    ...,
    timeout=settings.API_TIMEOUT  # 10초
)
```

**시나리오**:
1. 매수 주문 전송 → 10초 타임아웃 → KISApiError 발생
2. 실제로 KIS 서버에서는 주문 접수되어 체결됨
3. 코드는 실패로 인식 → 재시도 → 이중 매수
4. 또는 포지션 없다고 인식 → 청산 신호 무시

### 시나리오 4: 모드 전환 시 포지션 파일 오염

**위험한 시퀀스**:
1. PAPER 모드로 테스트 → positions.json에 가상 포지션 저장
2. LIVE 모드로 전환하여 실행
3. 프로그램이 positions.json 로드 → 존재하지 않는 포지션 복원 시도
4. 실제 계좌에 해당 종목 없음 → 청산 주문 실패 또는 공매도 발생

```python
# position_store.py 160-165행 - 모드 구분 없이 동일 파일 사용
self.file_path = file_path or POSITION_FILE  # data/positions.json
```

### 시나리오 5: 동시 실행 인스턴스로 인한 레이스 컨디션

```python
# postgres.py 239-243행
self._pool = pool.ThreadedConnectionPool(
    minconn=self.config.min_connections,
    maxconn=self.config.max_connections,
    ...
)
```

**문제**: 동일 프로그램이 실수로 두 번 실행될 경우:
1. 인스턴스 A: 매수 신호 감지 → 주문 전송
2. 인스턴스 B: 동일 매수 신호 감지 → 주문 전송
3. 이중 매수 발생
4. JSON 파일 덮어쓰기로 인한 데이터 손실

**방어 로직 부재**: 락 파일이나 단일 인스턴스 보장 메커니즘 없음

### 시나리오 6 (보너스): API 잔고 조회와 메모리 상태 불일치

```python
# position_store.py 302-319행
def reconcile_position(self, api_client, stored_position):
    ...
    if stored_position and not has_holding:
        # 시나리오 2: 저장O + 보유X
        logger.warning("포지션 불일치...")
        self.clear_position()  # ← 저장된 포지션 삭제
        return None, "포지션 불일치 - 저장 데이터 삭제됨"
```

**위험**: 
- API 일시적 오류로 잔고 조회 실패 시 → 유효한 포지션 데이터 삭제
- 복구 불가능한 데이터 손실 발생

---

## 3️⃣ API 현실성 관점: 오작동 분석

### 3.1 미체결 처리 부재

```python
# kis_api.py 406-439행
def place_buy_order(self, stock_code, quantity, price=0, order_type="01"):
    return self._place_order(..., is_buy=True)
```

**문제점**:
- 시장가 주문(`order_type="01"`)이지만 체결 확인 로직 없음
- 유동성 부족 종목에서 미체결 발생 가능
- 미체결 주문 취소 로직 없음
- 다음 사이클에서 동일 신호 시 중복 주문 가능

### 3.2 부분체결 완전 무시

```python
# executor.py 218-228행
if result["success"]:
    self.strategy.open_position(
        stock_code=self.stock_code,
        entry_price=signal.price,  # ← 신호 가격, 실제 체결가 아님
        quantity=self.order_quantity,  # ← 주문 수량, 실제 체결 수량 아님
        ...
    )
```

**시나리오**:
- 10주 매수 주문 → 3주만 체결 (부분체결)
- 코드는 10주 보유로 인식
- 청산 시 10주 매도 주문 → 7주는 미보유로 에러 또는 공매도

### 3.3 주문 응답 지연 시 중복 주문

```python
# executor.py 165-179행
def _can_execute_order(self, signal: Signal) -> bool:
    ...
    if self._last_signal_type == signal.signal_type:
        if self._last_order_time:
            elapsed = (datetime.now() - self._last_order_time).total_seconds()
            if elapsed < 60:  # 1분 이내 동일 시그널 무시
                return False
```

**허점**:
- API 호출 시작부터 응답까지 10초 걸린다고 가정
- 첫 번째 호출이 타임아웃 발생 → `_last_order_time` 업데이트 안 됨
- 60초 후 재시도 → 이미 체결된 주문 위에 추가 주문

### 3.4 장중 네트워크 단절

**코드에 없는 것**:
- 네트워크 상태 모니터링
- 연결 복구 후 포지션 상태 동기화
- 청산 신호 놓침에 대한 보상 로직

**실제 발생 가능한 상황**:
1. 포지션 보유 중 네트워크 단절 (30분)
2. 그 사이 손절선 이탈 → 청산 신호 감지 못함
3. 연결 복구 시 가격 추가 하락
4. 원래 손절가보다 훨씬 낮은 가격에 청산

### 3.5 장 종료 직전 주문 실패

```python
# 장 종료 시간 체크 로직이 존재하지 않음
def execute_sell(self, signal: TradingSignal) -> Dict[str, Any]:
    ...
    result = self.api.place_sell_order(...)  # 15:29:50에 호출하면?
```

**문제**:
- 한국 주식시장 동시호가: 15:20~15:30
- 이 시간대 주문 거부 가능
- 손절 신호 발생 → 주문 실패 → 익일까지 포지션 유지 강제
- 갭 다운 리스크에 무방비 노출

---

## 4️⃣ 전략 왜곡 관점

### 4.1 심리적 이유에 의한 왜곡

**코드에 박힌 심리적 편향**:

```python
# settings.py 67-71행
ATR_MULTIPLIER_SL = 2.0  # 손절 배수
ATR_MULTIPLIER_TP = 3.0  # 익절 배수
```

**문제**: 
- 손익비 1:1.5 (손절 2x ATR vs 익절 3x ATR)
- 승률이 40% 이상이어야 손익분기점
- 실제 Trend-ATR 전략의 기대 승률은 30-40%
- **수학적으로 음의 기대값 가능성**

### 4.2 기술적 편의에 의한 왜곡

```python
# multiday_trend_atr.py 584-589행
if self.enable_trailing_stop and current_price > pos.highest_price:
    pos.update_highest_price(current_price)
    new_trailing = self.calculate_trailing_stop(
        pos.highest_price, pos.atr_at_entry
    )
    pos.update_trailing_stop(new_trailing)
```

**편의적 구현의 문제**:
- 트레일링 스탑이 60초마다만 갱신됨
- 일중 고점 후 급락 시 실제 트레일링 가격과 괴리 발생
- 예: 고점 75,000원 → 30초 후 급락 73,000원 → 다음 체크 때 이미 손실 확대

### 4.3 시간 종료(EOD) 왜곡

**명시적으로 EOD 청산을 금지했지만...**

```python
# multiday_trend_atr.py 9-11행 주석
# ★ 절대 금지 사항:
#     - ❌ 장 마감(EOD) 시간 기준 강제 청산 로직
```

**현실적 문제**:
- EOD에 청산하지 않으면 오버나이트 리스크에 노출
- 한국 시장 특성: 미국 시장 영향으로 갭 발생 빈번
- "EOD 청산 금지"가 오히려 리스크 증가 요인

### 4.4 신호 지연으로 인한 전략 왜곡

**원래 전략 의도**:
- 직전 캔들 고가 돌파 시 즉시 진입

**실제 구현**:
- 60초마다 체크 → 돌파 후 최대 60초 지연 진입
- 진입가가 신호 가격보다 높아질 가능성
- 손익비 왜곡

---

## 5️⃣ 성과 측정 신뢰성 관점

### 5.1 승률 계산 불가 이유

```python
# repository.py 844-850행
win_rate = (wins / sells) if sells > 0 else 0
```

**문제점**:
- 매도(청산) 기록만으로 승률 계산
- 현재 보유 중인 미청산 포지션의 잠재 손실 미반영
- 프로그램 재시작 시 일부 거래 기록 누락 가능

### 5.2 기대값(Expectancy) 왜곡

```python
# repository.py 843행
expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
```

**왜곡 원인**:
1. `avg_win`, `avg_loss`가 **신호 가격** 기준 계산 (실제 체결가 아님)
2. 슬리피지 미반영 (시장가 주문의 실제 체결가 차이)
3. 수수료 0.015%만 반영, 증권거래세(0.23%) 미반영
4. **실제 기대값 = 계산된 기대값 - 0.5% 이상**

### 5.3 MDD 측정 오류

```python
# repository.py 1068-1125행 MDD 계산
for r in results:
    equity = float(r["total_equity"])
    if equity > peak:
        peak = equity
        peak_time = r["snapshot_time"]
    if peak > 0:
        drawdown = peak - equity
        ...
```

**측정 오류**:
- 스냅샷 간격에 의존 (account_snapshots 테이블)
- 일중 순간 최대 낙폭 미측정
- 예: 장중 -15% 낙폭 → 장 마감 -5% 회복 → MDD 5%로 기록
- **실제 체감 MDD와 괴리**

### 5.4 연속 손실 계산의 허점

```python
# position_store.py 384-389행
if pnl < 0:
    data[today]["consecutive_losses"] += 1
else:
    data[today]["consecutive_losses"] = 0
```

**문제**:
- `today` 기준으로만 계산 → 일 바뀌면 리셋
- 장기 연속 손실 패턴 감지 불가
- 예: 5일 연속 -2% 손실 → 매일 consecutive_losses = 1로 기록

---

## 6️⃣ 실계좌 투입 시 최악 시나리오 (순서도)

```
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:00] 시스템 시작                       │
│      - positions.json에서 전일 포지션 복원 시도                     │
│      - 삼성전자 70,000원 매수 포지션 (손절가 66,000원)               │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:01] 갭 다운 발생                      │
│      - 미국 증시 급락 영향으로 삼성전자 시가 64,000원               │
│      - ENABLE_GAP_PROTECTION = False (기본값)                     │
│      - 갭 보호 미발동 → 포지션 유지                                 │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:01:30] 손절 신호 발생                  │
│      - current_price(64,000) < stop_loss(66,000)                 │
│      - ATR_STOP_LOSS 조건 충족                                    │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:02] 매도 주문 시도                     │
│      - api.place_sell_order() 호출                               │
│      - 하지만 아직 60초 대기 중 (interval_seconds=60)              │
│      - 실제 주문은 09:02에나 발생                                   │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:02] 네트워크 지연                      │
│      - API 타임아웃 (10초)                                        │
│      - 주문 실패로 인식                                            │
│      - 실제로는 KIS 서버에 주문 접수됨 (체결 대기)                   │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:02:10] 첫 번째 체결                    │
│      - 체결가 63,500원 (호가 스프레드로 슬리피지 발생)               │
│      - 시스템은 이 체결을 인식하지 못함                             │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:03] 재시도 주문                        │
│      - 코드는 여전히 포지션 보유 중으로 인식                        │
│      - 재시도 매도 주문 전송                                       │
│      - 실제 계좌에는 이미 주식 없음 → 에러 또는 공매도               │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:03:30] 상태 불일치                     │
│      - 메모리: 포지션 보유 중 (청산 실패로 인식)                     │
│      - 실제 계좌: 주식 없음 + 공매도 주문 체결                       │
│      - JSON 파일: 포지션 존재                                      │
│      - DB: 불확실한 상태                                           │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 09:04~] 혼돈 상태                         │
│      - 공매도 포지션 발생 (의도치 않음)                             │
│      - 주가 반등 시 공매도 손실 확대                                │
│      - 프로그램은 롱 포지션 청산 시도 계속                          │
│      - 에러 무한 반복                                              │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    [D-Day 장 마감] 결산                             │
│      - 원래 예상 손실: -8.5% (70,000→64,000)                       │
│      - 실제 손실: -15% 이상 (슬리피지 + 공매도 손실)                 │
│      - 단 하루에 계좌 심각 훼손                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 출력 형식 - 고정

### 1. 치명적 위험 (반드시 수정) - 3개

| # | 위험 항목 | 위치 | 영향 |
|---|----------|------|------|
| 1 | **체결 확인 없는 포지션 상태 갱신** | `multiday_executor.py:337-357` | 이중 매수, 포지션 불일치, 좀비 포지션 |
| 2 | **갭 보호 기본 비활성화** | `settings.py:112` `ENABLE_GAP_PROTECTION = False` | 오버나이트 갭 다운 시 손절선 무력화 |
| 3 | **부분체결/미체결 처리 부재** | `kis_api.py:476-551` | 포지션 수량 불일치, 공매도 발생 가능 |

### 2. 구조적 취약점 (장기 손실 유발) - 3개

| # | 취약점 | 위치 | 누적 효과 |
|---|--------|------|----------|
| 1 | **일일 손실 한도의 복리 미적용** | `risk_manager.py:249-258` | 매일 동일 자본 기준 3% → 장기적으로 -45% 이상 가능 |
| 2 | **JSON과 DB 이중 저장의 불일치 가능성** | `position_store.py` vs `repository.py` | 복원 실패, 데이터 손실 |
| 3 | **60초 실행 간격의 신호 지연** | `multiday_executor.py:725` | 손절 타이밍 최대 60초 지연, 추가 손실 누적 |

### 3. 통계 왜곡 가능성 - 3개

| # | 왜곡 항목 | 원인 | 실제 오차 |
|---|----------|------|----------|
| 1 | **승률** | 미청산 포지션 미반영, 시그널가 vs 체결가 차이 | ±10% |
| 2 | **MDD** | 스냅샷 간격 의존, 일중 낙폭 미측정 | 과소 측정 50% 이상 |
| 3 | **기대값** | 수수료 과소 반영, 슬리피지 미반영 | 실제보다 +0.5% 이상 과대평가 |

### 4. 실계좌 투입 전 반드시 추가해야 할 로직

```python
# 1. 체결 확인 루프 (필수)
def wait_for_execution(self, order_no: str, timeout: int = 30) -> Dict:
    """주문 체결 확인 - 미체결 시 취소"""
    start = time.time()
    while time.time() - start < timeout:
        status = self.api.get_order_status(order_no)
        if status["exec_qty"] == status["order_qty"]:
            return {"success": True, "exec_price": status["exec_price"]}
        if status["exec_qty"] > 0:
            # 부분체결 처리
            return {"success": True, "partial": True, ...}
        time.sleep(2)
    # 타임아웃 시 미체결 주문 취소
    self.api.cancel_order(order_no)
    return {"success": False, "reason": "timeout"}

# 2. 단일 인스턴스 보장 (필수)
import fcntl
def acquire_lock():
    lock_file = open("/tmp/kis_trading.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except IOError:
        raise RuntimeError("Another instance is already running")

# 3. 장 운영시간 체크 (필수)
def is_market_tradeable(self) -> bool:
    """동시호가/폐장 시간 체크"""
    now = datetime.now()
    if now.hour < 9 or now.hour >= 16:
        return False
    if now.hour == 15 and now.minute >= 20:
        return False  # 동시호가 시간
    return True

# 4. 복리 기반 일일 손실 한도 (권장)
def check_cumulative_drawdown(self, max_cumulative_loss_pct: float = 20.0):
    """누적 낙폭 체크"""
    initial_capital = self.get_initial_capital()  # 최초 시작 자본
    current_equity = self.get_current_equity()
    cumulative_loss = (initial_capital - current_equity) / initial_capital * 100
    if cumulative_loss >= max_cumulative_loss_pct:
        self.enable_kill_switch("누적 낙폭 한도 초과")
```

### 5. "이 상태로 실계좌 투입하면 후회할 가능성이 높은 이유"

1. **체결 확인 없이 상태 갱신**: 네트워크 지연/타임아웃 시 실제 계좌와 시스템 상태가 불일치. 이중 주문, 좀비 포지션, 의도치 않은 공매도 발생 가능.

2. **갭 리스크에 무방비**: 멀티데이 전략인데 갭 보호가 기본 OFF. 악재로 시가가 손절선 아래에서 시작하면 손절선이 무의미해짐.

3. **60초라는 반응 속도**: 실시간 시장에서 60초는 영겁. 급락장에서 손절 신호 감지 지연으로 예상보다 훨씬 큰 손실 발생.

4. **통계 수치의 환상**: 백테스트/CBT 결과는 신호가 기준. 실제 체결가, 슬리피지, 미체결, 부분체결을 반영하면 수익률 수%p 이상 하락 예상.

5. **복리 손실 누적 방치**: 매일 -3% 한도를 20일 맞으면 -45%. 누적 드로다운 제어 로직 없음.

---

## 최종 강제 질문에 대한 답변

> **만약 이 시스템이 실패한다면, 그 책임은 "시장"이 아니라 "설계 단계의 어떤 결정" 때문인가?**

### 핵심 원인: **"주문 체결을 동기적으로 확인하지 않고 비동기적 희망에 의존한 결정"**

```python
# 문제의 코드 (multiday_executor.py:337-357)
result = self.api.place_buy_order(...)
if result["success"]:  # ← API 응답만 체크, 실제 체결 확인 안 함
    self.strategy.open_position(...)  # ← 체결되었다고 "가정"하고 상태 변경
```

**이 결정의 파급 효과**:
1. 포지션 상태가 실제 계좌와 동기화되지 않음
2. 이중 주문, 공매도 등 치명적 오류 가능
3. 모든 리스크 관리 로직이 잘못된 상태 위에서 동작
4. 백테스트와 실매매의 근본적 괴리 발생

**이것이 핵심인 이유**:
- 전략 로직이 아무리 우수해도
- ATR 계산이 아무리 정확해도
- 리스크 매니저가 아무리 정교해도

**시스템이 인식하는 포지션과 실제 포지션이 다르면 모든 것이 무너진다.**

이것은 "시장 탓"이 아니다. 이것은 "설계자가 금융 시스템의 본질적 불확실성(네트워크 지연, 부분체결, 미체결)을 API 응답 성공으로 대체할 수 있다고 착각한 것"의 결과다.

---

> **보고서 작성 완료: 2026-01-29**
> **감사자: Adversarial Trading Auditor**

