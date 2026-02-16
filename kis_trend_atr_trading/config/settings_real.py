"""
KIS Trend-ATR Trading System - 실계좌 전용 설정

═══════════════════════════════════════════════════════════════════════════════
⚠️⚠️⚠️ 경고: 이 파일은 실계좌 설정입니다! ⚠️⚠️⚠️
═══════════════════════════════════════════════════════════════════════════════

★ 이 파일의 설정은 실제 돈에 영향을 미칩니다!

★ 실계좌 활성화 조건 (모두 충족 필요):
  1. EXECUTION_MODE=REAL 환경변수 설정
  2. ENABLE_REAL_TRADING=true 환경변수 설정
  3. 아래 REAL_TRADING_CONFIRMED = True 설정
  
★ 세 가지 중 하나라도 미충족 시 DRY_RUN으로 자동 전환

★ 강제 규칙:
  - 손절 기준 완화 금지
  - 레버리지/과도한 비중 금지
  - ENABLE_GAP_PROTECTION = True 강제
  - 모든 안전장치 활성화 강제

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

from kis_trend_atr_trading.config.settings_base import *

# ═══════════════════════════════════════════════════════════════════════════════
# ⚠️⚠️⚠️ 실계좌 활성화 확인 플래그 ⚠️⚠️⚠️
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 이 값을 True로 변경해야만 실계좌 주문이 가능합니다.
# ★ 변경 전 반드시 모든 설정을 검토하세요!
# ★ DRY_RUN → PAPER → REAL 순서로 충분히 테스트 후 변경하세요!

REAL_TRADING_CONFIRMED = False  # ⚠️ True로 변경 시 실계좌 주문 활성화


# ═══════════════════════════════════════════════════════════════════════════════
# 실계좌 전용 설정 (매우 보수적)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 실계좌 API URL (REAL_TRADING_CONFIRMED=True일 때만 사용됨)
# ★ 그 외에는 모의투자 URL 사용
if REAL_TRADING_CONFIRMED and ENABLE_REAL_TRADING:
    KIS_BASE_URL = REAL_API_URL
    EXECUTION_MODE = "REAL"
else:
    KIS_BASE_URL = PAPER_API_URL
    EXECUTION_MODE = "DRY_RUN"  # 안전을 위해 DRY_RUN으로 폴백


# ═══════════════════════════════════════════════════════════════════════════════
# 리스크 관리 (실계좌용 - 매우 보수적, 완화 금지!)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 일일 최대 손실 비율 (%) - 실계좌는 2% 이하 유지
# ★ 이 값을 높이면 하루에 계좌가 크게 손실될 수 있습니다!
DAILY_MAX_LOSS_PERCENT = 2.0  # 절대 이 값 이상으로 올리지 마세요!

# ★ 일일 최대 거래 횟수 - 실계좌는 3회 이하 유지
# ★ 과매매 방지
DAILY_MAX_TRADES = 3  # 절대 이 값 이상으로 올리지 마세요!

# ★ 연속 손실 허용 횟수 - 실계좌는 2회 이하 유지
MAX_CONSECUTIVE_LOSSES = 2  # 절대 이 값 이상으로 올리지 마세요!

# ★ 누적 드로다운 한도 (%) - 실계좌는 10% 이하 유지
# ★ 이 비율 도달 시 Kill Switch 자동 발동
MAX_CUMULATIVE_DRAWDOWN_PCT = 10.0  # 절대 이 값 이상으로 올리지 마세요!

# ★ 누적 드로다운 경고 비율 (%) - 7%에서 경고
CUMULATIVE_DRAWDOWN_WARNING_PCT = 7.0


# ═══════════════════════════════════════════════════════════════════════════════
# 갭 보호 (실계좌 필수 - 변경 금지!)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 갭 보호 강제 활성화 - 이 값은 절대 False로 변경하지 마세요!
# ★ 멀티데이 전략에서 갭 보호 OFF는 자살 행위입니다!
ENABLE_GAP_PROTECTION = True

# ★ 최대 갭 손실 허용 비율 (%) - 실계좌는 1.5% 이하 유지
MAX_GAP_LOSS_PCT = 1.5
GAP_THRESHOLD_PCT = 1.5
GAP_EPSILON_PCT = 0.001
GAP_REFERENCE = "entry"


# ═══════════════════════════════════════════════════════════════════════════════
# 손절 설정 (실계좌용 - 완화 금지!)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 최대 손실 비율 (진입가 대비, %) - 실계좌는 3% 이하 유지
MAX_LOSS_PCT = 3.0  # 절대 이 값 이상으로 올리지 마세요!

# ★ 손절 배수 (ATR 기준) - 실계좌는 2.0 이상 유지
# ★ 이 값을 낮추면 너무 빨리 손절되어 손해볼 수 있습니다
ATR_MULTIPLIER_SL = 2.0  # 1.5 미만으로 낮추지 마세요!

# ★ 익절 배수 (ATR 기준) - 손익비 1.5:1 이상 유지
ATR_MULTIPLIER_TP = 3.0


# ═══════════════════════════════════════════════════════════════════════════════
# 주문 설정 (실계좌용 - 보수적)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 1회 주문 수량 - 소량으로 시작 권장
# ★ 처음에는 1주로 시작하여 전략 검증 후 점진적으로 증가
ORDER_QUANTITY = 1

# ★ 최대 포지션 수 - 실계좌는 1개로 제한 권장
MAX_POSITIONS = 1


# ═══════════════════════════════════════════════════════════════════════════════
# 안전장치 (실계좌 필수 - 변경 금지!)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 트레일링 스탑 강제 활성화
ENABLE_TRAILING_STOP = True

# ★ 동기화 주문 강제 활성화
ENABLE_SYNCHRONIZED_ORDERS = True

# ★ 단일 인스턴스 강제 - 중복 실행 방지
ENFORCE_SINGLE_INSTANCE = True

# ★ 장 운영시간 강제 - 장외 주문 방지
ENFORCE_MARKET_HOURS = True


# ═══════════════════════════════════════════════════════════════════════════════
# 검증 함수
# ═══════════════════════════════════════════════════════════════════════════════

def validate_real_settings() -> tuple[bool, list[str]]:
    """
    실계좌 설정 검증
    
    ★ 모든 안전 설정이 올바른지 확인합니다.
    ★ 하나라도 위반되면 실계좌 주문이 차단됩니다.
    """
    errors = []
    warnings = []
    
    # 필수 안전 설정 검증
    if not ENABLE_GAP_PROTECTION:
        errors.append("❌ ENABLE_GAP_PROTECTION이 False입니다. 실계좌에서 필수!")
    
    if not ENABLE_TRAILING_STOP:
        errors.append("❌ ENABLE_TRAILING_STOP이 False입니다. 실계좌에서 필수!")
    
    if not ENABLE_SYNCHRONIZED_ORDERS:
        errors.append("❌ ENABLE_SYNCHRONIZED_ORDERS가 False입니다. 실계좌에서 필수!")
    
    if not ENFORCE_SINGLE_INSTANCE:
        errors.append("❌ ENFORCE_SINGLE_INSTANCE가 False입니다. 실계좌에서 필수!")
    
    if not ENFORCE_MARKET_HOURS:
        errors.append("❌ ENFORCE_MARKET_HOURS가 False입니다. 실계좌에서 필수!")
    
    # 리스크 한도 검증
    if DAILY_MAX_LOSS_PERCENT > 3.0:
        errors.append(f"❌ 일일 손실 한도가 {DAILY_MAX_LOSS_PERCENT}%로 너무 높습니다. 3% 이하로 설정하세요.")
    
    if MAX_CUMULATIVE_DRAWDOWN_PCT > 15.0:
        errors.append(f"❌ 누적 드로다운 한도가 {MAX_CUMULATIVE_DRAWDOWN_PCT}%로 너무 높습니다. 15% 이하로 설정하세요.")
    
    if DAILY_MAX_TRADES > 5:
        errors.append(f"❌ 일일 최대 거래가 {DAILY_MAX_TRADES}회로 너무 많습니다. 5회 이하로 설정하세요.")
    
    if MAX_GAP_LOSS_PCT > 3.0:
        errors.append(f"❌ 갭 손실 한도가 {MAX_GAP_LOSS_PCT}%로 너무 높습니다. 3% 이하로 설정하세요.")
    
    # 손절 설정 검증
    if ATR_MULTIPLIER_SL < 1.5:
        errors.append(f"❌ 손절 배수가 {ATR_MULTIPLIER_SL}로 너무 작습니다. 1.5 이상으로 설정하세요.")
    
    if MAX_LOSS_PCT > 5.0:
        errors.append(f"❌ 최대 손실 비율이 {MAX_LOSS_PCT}%로 너무 높습니다. 5% 이하로 설정하세요.")
    
    # 경고
    if ORDER_QUANTITY > 10:
        warnings.append(f"⚠️ 주문 수량이 {ORDER_QUANTITY}주로 많습니다. 신중하게 검토하세요.")
    
    if MAX_POSITIONS > 3:
        warnings.append(f"⚠️ 최대 포지션 수가 {MAX_POSITIONS}개로 많습니다. 신중하게 검토하세요.")
    
    return (len(errors) == 0, errors + warnings)


def can_activate_real_trading() -> tuple[bool, str]:
    """
    실계좌 거래 활성화 가능 여부 확인
    
    Returns:
        tuple: (활성화 가능 여부, 사유)
    """
    # 1. 설정 파일 확인
    if not REAL_TRADING_CONFIRMED:
        return (False, "REAL_TRADING_CONFIRMED가 False입니다. settings_real.py에서 True로 변경하세요.")
    
    # 2. 환경변수 확인
    import os
    if os.getenv("ENABLE_REAL_TRADING", "false").lower() not in ("true", "1", "yes"):
        return (False, "ENABLE_REAL_TRADING 환경변수가 'true'로 설정되지 않았습니다.")
    
    if os.getenv("EXECUTION_MODE", "") != "REAL":
        return (False, "EXECUTION_MODE 환경변수가 'REAL'로 설정되지 않았습니다.")
    
    # 3. 설정 검증
    valid, errors = validate_real_settings()
    if not valid:
        return (False, f"설정 검증 실패: {errors[0]}")
    
    return (True, "모든 조건 충족")


def print_real_settings_summary() -> str:
    """실계좌 설정 요약"""
    can_trade, reason = can_activate_real_trading()
    
    return f"""
═══════════════════════════════════════════════════════════════
🔴 KIS Trend-ATR Trading System - 실계좌 설정
═══════════════════════════════════════════════════════════════
[활성화 상태]
  REAL_TRADING_CONFIRMED: {'✅ True' if REAL_TRADING_CONFIRMED else '❌ False'}
  ENABLE_REAL_TRADING: {'✅ True' if ENABLE_REAL_TRADING else '❌ False'}
  실계좌 주문 가능: {'✅ 가능' if can_trade else '❌ 불가'}
  사유: {reason}

[API 설정]
  API URL: {KIS_BASE_URL}
  모드: {EXECUTION_MODE}

[리스크 관리 (보수적)]
  일일 손실 한도: {DAILY_MAX_LOSS_PERCENT}%
  누적 드로다운 한도: {MAX_CUMULATIVE_DRAWDOWN_PCT}%
  일일 최대 거래: {DAILY_MAX_TRADES}회
  최대 손실 비율: {MAX_LOSS_PCT}%
  갭 손실 한도: {MAX_GAP_LOSS_PCT}%

[안전 장치]
  갭 보호: {'✅ ON' if ENABLE_GAP_PROTECTION else '❌ OFF'}
  트레일링 스탑: {'✅ ON' if ENABLE_TRAILING_STOP else '❌ OFF'}
  동기화 주문: {'✅ ON' if ENABLE_SYNCHRONIZED_ORDERS else '❌ OFF'}
  단일 인스턴스 강제: {'✅ ON' if ENFORCE_SINGLE_INSTANCE else '❌ OFF'}
  장 운영시간 강제: {'✅ ON' if ENFORCE_MARKET_HOURS else '❌ OFF'}

[주문 설정]
  종목: {DEFAULT_STOCK_CODE}
  주문 수량: {ORDER_QUANTITY}주
  최대 포지션: {MAX_POSITIONS}개
═══════════════════════════════════════════════════════════════

{"⚠️⚠️⚠️ 실계좌 거래가 활성화되었습니다! 실제 돈이 거래됩니다! ⚠️⚠️⚠️" if can_trade else "✅ 실계좌 거래가 비활성화되어 있습니다. 안전합니다."}

═══════════════════════════════════════════════════════════════
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 실계좌 설정 체크리스트 (문서용)
# ═══════════════════════════════════════════════════════════════════════════════

REAL_TRADING_CHECKLIST = """
═══════════════════════════════════════════════════════════════════════════════
              실계좌 투입 전 필수 체크리스트
═══════════════════════════════════════════════════════════════════════════════

□ 1. DRY_RUN 모드에서 최소 2주 이상 전략 논리 검증 완료
□ 2. PAPER 모드에서 최소 1주 이상 모의투자 테스트 완료
□ 3. 모의투자에서 승률 50% 이상, Profit Factor 1.0 이상 확인
□ 4. 모든 안전장치(갭 보호, 트레일링 스탑 등) 작동 확인
□ 5. 텔레그램 알림 정상 수신 확인
□ 6. Kill Switch 작동 테스트 완료
□ 7. 일일 손실 한도 작동 테스트 완료
□ 8. 장 운영시간 외 주문 차단 확인
□ 9. 긴급 손절 로직 작동 확인
□ 10. 모든 API 에러 상황 대응 확인

위 체크리스트를 모두 완료한 후에만 실계좌 투입을 고려하세요.

★ 절대 하면 안 되는 행동:
  - DRY_RUN/PAPER 단계 건너뛰기
  - 손실 한도 완화
  - 갭 보호 비활성화
  - 단일 인스턴스 강제 비활성화
  - 대량 자금으로 바로 시작

★ 권장 순서:
  1. 1주 소량(1주)으로 시작
  2. 2주차에 문제 없으면 2-3주로 증가
  3. 한 달 후 검토 후 점진적 증가
  
═══════════════════════════════════════════════════════════════════════════════
"""


def print_checklist() -> None:
    """실계좌 체크리스트 출력"""
    print(REAL_TRADING_CHECKLIST)
