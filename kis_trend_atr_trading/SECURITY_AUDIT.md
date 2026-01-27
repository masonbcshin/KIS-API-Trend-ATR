# 🔴 KIS Trend-ATR 자동매매 시스템 코드 감사 보고서

**감사일:** 2026-01-27  
**감사자:** 자동매매 시스템 전문 코드 감사자  
**대상:** KIS Trend-ATR Trading System v1.0.0

---

## 분석 요약

| 등급 | 건수 | 설명 |
|------|------|------|
| **치명적(Critical)** | 4건 | 계좌 전손 또는 통제 불능 가능 |
| **중간(Major)** | 4건 | 심각한 손실 또는 오동작 가능 |
| **경미(Minor)** | 3건 | 운영상 불편 또는 잠재적 문제 |

---

## 🔴 치명적(Critical) - 4건

### CRITICAL-001: 프로그램 재시작 시 포지션 상태 소실

**위험도:** 계좌 전손 가능

**문제:**  
포지션 정보가 메모리(`self.position`)에만 저장되어 재시작 시 실제 계좌와 시스템 상태가 불일치합니다.

**코드 위치:** `strategy/trend_atr.py` Line 131
```python
self.position: Optional[Position] = None
```

**공격 시나리오:**
1. 프로그램이 매수 후 포지션 보유 중
2. 서버 재시작/크래시 발생
3. 재시작 후 `self.position = None`으로 초기화
4. 시스템은 "포지션 없음"으로 인식
5. **동일 종목 중복 매수** 발생 가능
6. 기존 보유 포지션의 **손절/익절 관리 불가** → 무한 손실

**해결 방안:**
- 포지션 상태를 파일/DB에 영속화
- 프로그램 시작 시 `get_account_balance()` API로 실제 잔고 동기화
- 불일치 발견 시 알림 및 수동 개입 요청

---

### CRITICAL-002: 주문 체결 미확인 상태에서 포지션 업데이트

**위험도:** 가상 포지션만 존재하는 유령 거래

**문제:**  
API 주문 응답("주문 접수 성공")만 보고 바로 포지션을 업데이트하며, 실제 체결 여부를 확인하지 않습니다.

**코드 위치:** `engine/executor.py` Line 190-200
```python
if result["success"]:
    # 포지션 오픈 - 체결 확인 없이 바로 실행
    self.strategy.open_position(
        stock_code=self.stock_code,
        entry_price=signal.price,  # 실제 체결가가 아닌 시그널 가격 사용
        quantity=self.order_quantity,
        ...
    )
```

**공격 시나리오:**
1. 시장가 매수 주문 전송 → API는 "주문 접수 성공" 반환
2. 시스템은 포지션 보유로 기록
3. 그러나 실제로는 **미체결** (유동성 부족, 거래 정지 등)
4. 시스템은 매수됐다고 믿고 손절/익절 모니터링
5. 손절 조건 도달 → **없는 주식을 매도 시도**

**해결 방안:**
- 주문 후 `get_order_status()` API로 체결 확인 루프 구현
- 체결 완료 후에만 포지션 업데이트
- 미체결 시 주문 취소 및 재시도 로직 추가

---

### CRITICAL-003: 손절/익절 슬리피지 무방비

**위험도:** ATR 2배 손절이 10배 손실로 확대 가능

**문제:**  
손절가 도달 확인 후 시장가 주문을 사용하는데, 급락장에서는 손절가보다 훨씬 낮게 체결됩니다.

**코드 위치:** `strategy/trend_atr.py` Line 353-357, `engine/executor.py` Line 249-254
```python
# 손절 확인 (현재가 기준)
if current_price <= self.position.stop_loss:
    return True, "손절 도달"

# 매도 실행 (시장가)
result = self.api.place_sell_order(
    stock_code=self.stock_code,
    quantity=position.quantity,
    price=0,  # 시장가 - 슬리피지 무방비
    order_type="01"
)
```

**공격 시나리오:**
1. 진입가 100,000원, ATR 2,000원, 손절가 96,000원 (2배 ATR)
2. 시장이 갑자기 급락, 현재가 95,000원 확인
3. 손절 조건 충족 → 시장가 매도 주문
4. 급락 지속 중 실제 체결가 **80,000원**
5. 예상 손실 -4% → **실제 손실 -20%**

**해결 방안:**
- 손절가 근처 지정가 주문 사용 (손절가 - 슬리피지 허용폭)
- 슬리피지 한도 초과 시 알림 발송
- 극단적 슬리피지 시 전략 일시 중단

---

### CRITICAL-004: 실제 계좌 잔고와 시스템 포지션 불일치

**위험도:** 보유 수량 불일치로 매도 실패 또는 초과 매도

**문제:**  
매도 시 `position.quantity`를 사용하지만, 외부 거래나 부분 체결 시 실제 잔고와 다를 수 있습니다.

**코드 위치:** `engine/executor.py` Line 246-251
```python
position = self.strategy.position

result = self.api.place_sell_order(
    stock_code=self.stock_code,
    quantity=position.quantity,  # 실제 잔고 확인 없음
    ...
)
```

**공격 시나리오:**
1. 시스템: 10주 보유 기록
2. 사용자가 MTS로 5주 수동 매도
3. 실제 잔고: 5주
4. 손절 조건 도달 → 시스템이 10주 매도 시도
5. **5주는 공매도 상태** 또는 주문 거부

**해결 방안:**
- 매도 전 `get_account_balance()` API로 실제 잔고 확인
- 불일치 발견 시 경고 및 실제 잔고 기준 매도
- 외부 거래 감지 로직 추가

---

## 🟡 중간(Major) - 4건

### MAJOR-001: API 장애 시 손절 주문 실패 방치

**문제:**  
3회 재시도 실패 후 에러만 로깅하고 다음 루프(60초 후)까지 대기합니다.

**코드 위치:** `api/kis_api.py` Line 164-170
```python
if attempt < max_retries:
    wait_time = settings.RETRY_DELAY * (2 ** attempt)
    time.sleep(wait_time)

raise last_exception  # 호출부에서 로깅만 하고 무시
```

**결과:** API 장애 60초 동안 손절 불가 → 급락장에서 치명적 손실

**해결 방안:**
- 손절 주문 실패 시 무한 재시도 옵션
- 실패 지속 시 긴급 알림 발송
- 대체 API 엔드포인트 사용

---

### MAJOR-002: 중복 주문 방지 로직 불충분

**문제:**  
단일 프로세스 내 1분 딜레이만 체크하며, 다중 인스턴스나 레이스 컨디션 방지가 없습니다.

**코드 위치:** `engine/executor.py` Line 147-159
```python
if self._last_signal_type == signal.signal_type:
    if self._last_order_time:
        elapsed = (datetime.now() - self._last_order_time).total_seconds()
        if elapsed < 60:
            return False
```

**해결 방안:**
- 분산 락(Redis, 파일 락) 사용
- 주문 ID 기반 멱등성 보장
- 실행 전 미체결 주문 조회

---

### MAJOR-003: 백테스트 100% 풀베팅 로직

**문제:**  
백테스트가 자본금 100%를 단일 종목에 투자합니다.

**코드 위치:** `backtest/backtester.py` Line 126-146
```python
def _calculate_position_size(self, price: float, capital: float) -> int:
    """자본금의 100%를 사용하는 단순한 포지션 사이징"""
    available = capital / (1 + self.commission_rate)
    quantity = int(available // price)
    return max(0, quantity)
```

**해결 방안:**
- 포지션 사이징 옵션 추가 (고정 비율, Kelly 기준 등)
- 최대 포지션 한도 설정

---

### MAJOR-004: 실계좌 URL 하드코딩 존재

**문제:**  
설정 파일 주석에 실계좌 URL이 명시되어 복붙 실수 위험이 있습니다.

**코드 위치:** `config/settings.py` Line 23-26
```python
# 실전: https://openapi.koreainvestment.com:9443  # ← 위험한 주석
KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"
```

**해결 방안:**
- 실계좌 URL 주석 제거
- 환경변수로 URL 관리
- 실계좌 URL 패턴 감지 시 시작 거부

---

## 🟢 경미(Minor) - 3건

### MINOR-001: 토큰 갱신 실패 처리 부재

**코드 위치:** `api/kis_api.py` Line 211-214

토큰 만료 10분 전 갱신 시도는 하지만, 갱신 실패 시 별도 처리 없음.

---

### MINOR-002: 로그 파일 로테이션 없음

**코드 위치:** `utils/logger.py`

로그 로테이션이 없어 장기 운영 시 디스크 풀 가능성.

---

### MINOR-003: 전략 실행 중 예외 일부 미처리

**코드 위치:** `engine/executor.py` `run_once()` 메서드

일부 예외 발생 시 에러 로깅 후 다음 루프까지 대기만 함.

---

## 📋 실계좌 투입 전 필수 조치사항

| 우선순위 | 항목 | 현재 상태 | 필요 조치 |
|---------|------|----------|----------|
| **1** | 포지션 영속화 | ❌ 메모리만 | 파일/DB 저장 + 시작 시 동기화 |
| **2** | 체결 확인 | ❌ 미확인 | 주문 후 체결 조회 루프 추가 |
| **3** | 잔고 검증 | ❌ 미검증 | 매매 전 실제 잔고 조회 |
| **4** | API 장애 대응 | ❌ 방치 | 긴급 손절 시 무한 재시도 또는 알림 |
| **5** | 슬리피지 방어 | ❌ 없음 | 지정가 또는 슬리피지 한도 설정 |

---

## 결론

**⚠️ 현재 상태로 실계좌 투입 시 "망할 수 있습니다."**

주요 위험 요인:
1. **재시작 시 중복 매수** → 의도치 않은 레버리지 효과
2. **손절 실패** → 급락장에서 무방비 상태
3. **가상 포지션** → 없는 주식 매도 시도
4. **API 장애** → 60초간 통제 불능

이 시스템은 **백테스트 및 학습용으로만 사용**해야 하며, 실계좌 투입 전 위 Critical 항목들의 수정이 필수입니다.

---

**권장 사항:**
1. 모든 Critical 항목 수정 완료
2. 모의투자 환경에서 최소 1개월 실시간 테스트
3. 각 Major 항목 순차적 개선
4. 실계좌 투입 시 소액으로 시작
