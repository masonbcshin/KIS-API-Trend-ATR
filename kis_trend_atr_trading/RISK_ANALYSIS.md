# 자동매매 시스템 위험 시나리오 분석 보고서

> 실계좌 운영 시 발생 가능한 최악의 시나리오 5가지와 안전장치 수정 포인트

---

## 목차
1. [시나리오 1: API 통신 장애 시 손절 실패](#시나리오-1-api-통신-장애-시-손절-실패)
2. [시나리오 2: 프로그램 재시작 시 포지션 동기화 불가](#시나리오-2-프로그램-재시작-시-포지션-동기화-불가)
3. [시나리오 3: 주문 체결 미확인으로 인한 포지션 불일치](#시나리오-3-주문-체결-미확인으로-인한-포지션-불일치)
4. [시나리오 4: 일일 손실 한도 미설정](#시나리오-4-일일-손실-한도-미설정)
5. [시나리오 5: 거래시간 검증 없이 주문 실행](#시나리오-5-거래시간-검증-없이-주문-실행)
6. [수정 우선순위 요약](#수정-우선순위-요약)

---

## 시나리오 1: API 통신 장애 시 손절 실패

### 위험도: 🔴 치명적

### 발생 조건 (코드 기준)

**파일:** `engine/executor.py`

```python
# 144-159행: 중복 주문 방지 로직
def _can_execute_order(self, signal: Signal) -> bool:
    if self._last_signal_type == signal.signal_type:
        if self._last_order_time:
            elapsed = (datetime.now() - self._last_order_time).total_seconds()
            # 1분 이내 동일 시그널 무시
            if elapsed < 60:
                return False

# 280-286행: 매도 실패 시 단순 반환
except KISApiError as e:
    trade_logger.log_error("매도 주문", str(e))
    return {"success": False, "message": str(e)}  # ← 재시도 없이 종료
```

### 상황 설명

1. 포지션 보유 중 손절가 도달 → SELL 시그널 발생
2. API 서버 장애/네트워크 불안정/타임아웃 발생
3. `execute_sell_order()`가 실패 반환하고 종료
4. 다음 사이클(60초 후)에 "동일 시그널 1분 이내" 로직에 의해 무시 가능
5. **시장이 급락하는 동안 손절 주문이 실행되지 않음**

### 손실 예시

| 항목 | 값 |
|------|-----|
| 보유가 | 100,000원 |
| 손절가 | 95,000원 |
| API 장애 시간 | 30분 |
| 장애 중 최저가 | 80,000원 |
| 예상 손실 | -5% |
| **실제 손실** | **-20%** |

### 수정 포인트

```python
# engine/executor.py 수정 필요

# 1. 긴급 손절 재시도 설정 추가
EMERGENCY_SELL_MAX_RETRIES = 10
EMERGENCY_SELL_RETRY_INTERVAL = 5  # 초

# 2. execute_sell_order() 수정
def execute_sell_order(self, signal: Signal, is_emergency: bool = False) -> Dict:
    max_retries = EMERGENCY_SELL_MAX_RETRIES if is_emergency else 3
    
    for attempt in range(max_retries):
        try:
            result = self.api.place_sell_order(...)
            if result["success"]:
                return result
        except KISApiError as e:
            if attempt < max_retries - 1:
                time.sleep(EMERGENCY_SELL_RETRY_INTERVAL)
                continue
            # 최종 실패 시 긴급 알림 발송
            self._send_emergency_alert(f"손절 실패: {e}")
    
    return {"success": False, "critical": True}

# 3. 손절 실패 시 프로그램 정지 및 수동 개입 요청
```

---

## 시나리오 2: 프로그램 재시작 시 포지션 동기화 불가

### 위험도: 🔴 높음

### 발생 조건 (코드 기준)

**파일:** `strategy/trend_atr.py`

```python
# 130-131행: 포지션이 메모리에만 저장됨
class TrendATRStrategy:
    def __init__(self, ...):
        # 현재 포지션 (None = 포지션 없음)
        self.position: Optional[Position] = None  # ← 영속화 안 됨
```

**파일:** `engine/executor.py`

```python
# 176-179행: 포지션 체크 후 매수
if self.strategy.has_position():
    logger.warning("매수 주문 취소: 포지션 이미 보유 중")
    return {"success": False, "message": "포지션 보유 중"}
```

### 상황 설명

**시나리오 A - 중복 매수:**
1. 프로그램 실행 중 삼성전자 100주 매수 완료
2. 서버 재부팅/프로그램 크래시 발생
3. 프로그램 재시작 → `self.position = None`
4. **실제로는 100주 보유 중인데 시스템은 "포지션 없음"으로 인식**
5. 매수 조건 충족 시 추가 100주 매수 → 총 200주 보유

**시나리오 B - 손절 관리 불가:**
1. 실제 포지션 보유 중인데 시스템이 인식 못함
2. 주가 급락해도 손절 시그널 생성 안 됨
3. **손절가 없이 무한 보유 → 큰 손실**

### 수정 포인트

```python
# 1. 프로그램 시작 시 계좌 잔고에서 포지션 동기화
# engine/executor.py 수정

def __init__(self, ...):
    # ... 기존 코드
    self._sync_position_from_account()  # 추가

def _sync_position_from_account(self):
    """계좌 잔고에서 실제 포지션을 동기화합니다."""
    try:
        balance = self.api.get_account_balance()
        for holding in balance["holdings"]:
            if holding["stock_code"] == self.stock_code:
                # 포지션 복구 (손절가는 현재가 기준 재계산)
                current_price = holding["current_price"]
                atr = self._get_current_atr()
                
                self.strategy.position = Position(
                    stock_code=holding["stock_code"],
                    entry_price=holding["avg_price"],
                    quantity=holding["quantity"],
                    stop_loss=current_price - (atr * 2.0),
                    take_profit=current_price + (atr * 3.0),
                    entry_date="RECOVERED",
                    atr_at_entry=atr
                )
                logger.warning(f"포지션 복구: {holding}")
                break
    except Exception as e:
        logger.error(f"포지션 동기화 실패: {e}")

# 2. 포지션 정보 파일 영속화 (선택적)
# utils/position_store.py 신규 생성
```

---

## 시나리오 3: 주문 체결 미확인으로 인한 포지션 불일치

### 위험도: 🔴 높음

### 발생 조건 (코드 기준)

**파일:** `engine/executor.py`

```python
# 181-213행: 주문 접수만 확인하고 바로 포지션 오픈
result = self.api.place_buy_order(
    stock_code=self.stock_code,
    quantity=self.order_quantity,
    price=0,  # 시장가
    order_type="01"
)

if result["success"]:  # ← "주문 접수 성공"이지 "체결 완료"가 아님
    # 포지션 오픈
    self.strategy.open_position(
        entry_price=signal.price,  # ← 시그널 가격, 실제 체결가 아님
        quantity=self.order_quantity,
        ...
    )
```

**파일:** `api/kis_api.py`

```python
# 521-541행: success는 주문 접수 성공만 의미
success = data.get("rt_cd") == "0"  # API 응답 코드 0 = 주문 접수 성공
return {
    "success": success,  # ← 체결 완료가 아님!
    "order_no": order_no,
    ...
}
```

### 상황 설명

1. 시장가 매수 주문 발생
2. API 응답: `{"success": True, "order_no": "12345"}` (주문 접수됨)
3. **실제 체결은 별개 프로세스** (미체결/부분체결 가능)
4. 시스템: 바로 `open_position()` 호출
5. 결과:
   - 미체결인데 포지션이 있는 것으로 처리
   - 또는 60,000원 시그널 → 61,000원에 체결 (체결가 다름)
   - 손절가 계산이 잘못됨

### 손실 예시

| 항목 | 시스템 인식 | 실제 |
|------|-----------|------|
| 진입가 | 60,000원 | 61,000원 |
| 손절가 | 58,000원 | 58,000원 (잘못됨) |
| 현재가 | 59,000원 | 59,000원 |
| 손절 발동 | ❌ 안 함 | ✅ 해야 함 (-3.3%) |

### 수정 포인트

```python
# engine/executor.py 수정

def execute_buy_order(self, signal: Signal) -> Dict:
    result = self.api.place_buy_order(...)
    
    if result["success"]:
        # 체결 확인 대기 (최대 30초)
        executed = self._wait_for_execution(
            order_no=result["order_no"],
            timeout=30,
            check_interval=2
        )
        
        if executed and executed["status"] == "체결":
            # 실제 체결가로 포지션 오픈
            self.strategy.open_position(
                entry_price=executed["exec_price"],  # 실제 체결가
                quantity=executed["exec_qty"],       # 실제 체결 수량
                ...
            )
            return {"success": True, "executed": True}
        else:
            # 미체결 처리
            logger.warning(f"주문 미체결: {result['order_no']}")
            # 선택: 주문 취소 또는 대기
            return {"success": False, "reason": "미체결"}

def _wait_for_execution(self, order_no: str, timeout: int, check_interval: int) -> dict:
    """주문 체결을 대기합니다."""
    start = time.time()
    while time.time() - start < timeout:
        status = self.api.get_order_status(order_no)
        for order in status["orders"]:
            if order["order_no"] == order_no:
                if order["exec_qty"] > 0:
                    return order
        time.sleep(check_interval)
    return None
```

---

## 시나리오 4: 일일 손실 한도 미설정

### 위험도: 🔴 높음

### 발생 조건 (코드 기준)

**파일:** `config/settings.py`

```python
# 71-74행: 1회 거래당 최대 손실만 제한
MAX_LOSS_PCT = 5.0  # ← 1회 거래 기준, 일일 누적 아님
```

**파일:** `engine/executor.py`

```python
# 399-440행: 무한 반복, 일일 손실 한도 체크 없음
def run(self, interval_seconds: int = 60, max_iterations: int = None):
    while self.is_running:
        iteration += 1
        self.run_once()  # ← 일일 손실 체크 없이 계속 실행
        time.sleep(interval_seconds)
```

### 상황 설명

- `MAX_LOSS_PCT = 5.0`은 **1회 거래당** 최대 손실만 제한
- **일일 누적 손실 한도가 없음**
- 하루 동안 거래 횟수 제한 없음

### 손실 누적 시뮬레이션

| 거래 | 손실률 | 잔여 자본 |
|-----|-------|---------|
| 1회차 | -5% | 95.0% |
| 2회차 | -5% | 90.25% |
| 3회차 | -5% | 85.74% |
| 4회차 | -5% | 81.45% |
| 5회차 | -5% | 77.38% |
| **합계** | **-22.6%** | **하루 손실** |

### 수정 포인트

```python
# 1. config/settings.py에 일일 한도 추가
DAILY_MAX_LOSS_PCT = 10.0  # 일일 최대 손실 10%
DAILY_MAX_TRADES = 5       # 일일 최대 거래 횟수

# 2. engine/executor.py 수정
def run_once(self) -> Dict:
    # 일일 손실 한도 체크
    daily_pnl = self._calculate_daily_pnl()
    if daily_pnl <= -settings.DAILY_MAX_LOSS_PCT:
        logger.critical(f"일일 손실 한도 도달: {daily_pnl:.2f}%")
        self.stop()
        self._send_alert("일일 손실 한도 도달! 자동매매 중지")
        return {"error": "일일 손실 한도 초과", "daily_pnl": daily_pnl}
    
    # 일일 거래 횟수 체크
    if len(self._daily_trades) >= settings.DAILY_MAX_TRADES:
        logger.warning(f"일일 거래 횟수 한도 도달: {len(self._daily_trades)}")
        return {"error": "일일 거래 횟수 초과"}
    
    # ... 기존 로직

def _calculate_daily_pnl(self) -> float:
    """당일 누적 손익률을 계산합니다."""
    if not self._daily_trades:
        return 0.0
    return sum(t.get("pnl_pct", 0) for t in self._daily_trades)
```

---

## 시나리오 5: 거래시간 검증 없이 주문 실행

### 위험도: 🟡 중간

### 발생 조건 (코드 기준)

**파일:** `engine/executor.py`

```python
# 292-364행: 거래시간 체크 없이 실행
def run_once(self) -> Dict:
    """전략을 1회 실행합니다."""
    # 거래시간 체크 로직이 없음
    
    # 1. 시장 데이터 조회
    df = self.fetch_market_data()
    
    # 2. 현재가 조회  
    current_price = self.fetch_current_price()
    
    # 3. 전략 시그널 생성
    signal = self.strategy.generate_signal(...)
    
    # 4. 시그널에 따른 주문 실행  ← 장 외 시간에도 실행됨
    if signal.signal_type == SignalType.BUY:
        order_result = self.execute_buy_order(signal)
```

### 상황 설명

- 정규장: 09:00~15:30
- 시간외 단일가: 07:30~08:30, 15:40~16:00
- **프로그램이 24시간 실행 중이면 장 외 시간에도 주문 시도**

### 위험 시나리오

**1. 장 마감 직후 (15:35)**
- 시스템이 매수 시그널 생성
- 시장가 주문 → 시간외 단일가 체결 시도
- 유동성 부족으로 예상과 다른 가격에 체결

**2. 익일 장 시작 전 (08:00)**
- 전일 대비 -5% 갭 하락 예정
- 시스템이 손절 시그널 생성
- 시장가 매도 → 시간외에 안 되고 09:00 시초가에 체결
- **시초가가 손절가보다 훨씬 낮아서 추가 손실**

### 수정 포인트

```python
# 1. utils/market_hours.py 신규 생성
from datetime import datetime, time

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 20)  # 동시호가 제외, 안전 마진

def is_market_open() -> bool:
    """정규장 운영 시간인지 확인합니다."""
    now = datetime.now()
    current_time = now.time()
    
    # 주말 체크
    if now.weekday() >= 5:  # 토(5), 일(6)
        return False
    
    # 공휴일 체크 (별도 캘린더 연동 권장)
    # if is_holiday(now.date()):
    #     return False
    
    return MARKET_OPEN <= current_time <= MARKET_CLOSE

# 2. engine/executor.py 수정
from utils.market_hours import is_market_open

def run_once(self) -> Dict:
    # 거래시간 체크
    if not is_market_open():
        logger.info("장 운영 시간이 아닙니다. 주문을 건너뜁니다.")
        return {"skipped": True, "reason": "장 외 시간"}
    
    # ... 기존 로직
```

---

## 수정 우선순위 요약

| 우선순위 | 시나리오 | 위험도 | 수정 복잡도 | 수정 파일 |
|:-------:|---------|:------:|:---------:|----------|
| **1** | API 장애 시 손절 실패 | 🔴 치명적 | 중 | `executor.py` |
| **2** | 포지션 동기화 불가 | 🔴 높음 | 높음 | `executor.py`, `trend_atr.py` |
| **3** | 체결 미확인 | 🔴 높음 | 중 | `executor.py` |
| **4** | 일일 손실 한도 없음 | 🔴 높음 | 낮음 | `executor.py`, `settings.py` |
| **5** | 거래시간 미검증 | 🟡 중간 | 낮음 | `executor.py`, 신규 파일 |

---

## 권장 조치 순서

1. **즉시 적용 (Low-hanging fruit)**
   - 일일 손실 한도 설정 (시나리오 4)
   - 거래시간 검증 추가 (시나리오 5)

2. **단기 적용 (1주일 내)**
   - 손절 주문 재시도 로직 강화 (시나리오 1)
   - 주문 체결 확인 로직 추가 (시나리오 3)

3. **중기 적용 (1개월 내)**
   - 포지션 영속화 및 동기화 (시나리오 2)
   - 긴급 알림 시스템 구축 (SMS/텔레그램)

---

*문서 작성일: 2026-01-27*
*분석 대상 버전: 1.0.0*
