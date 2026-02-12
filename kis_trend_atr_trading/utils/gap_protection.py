from typing import Optional, Tuple, Any

GAP_REASON_TRIGGERED = "GAP_PROTECTION_TRIGGERED"
GAP_REASON_DISABLED = "GAP_PROTECTION_DISABLED"
GAP_REASON_OTHER = "OTHER_EXIT_LOGIC"
GAP_REASON_FALLBACK = "EXIT_FALLBACK_AFTER_ORDER_FAIL"


def should_trigger_gap_protection(
    position: Any,
    open_price: Optional[float],
    reference_price: Optional[float],
    threshold_pct: Optional[float],
    epsilon_pct: float,
) -> Tuple[bool, str, float]:
    """
    갭 보호 발동 판단 (롱 포지션 손실 갭 전용).

    Returns:
        (trigger, reason_code, raw_gap_pct)
    """
    _ = position  # 향후 포지션 타입 확장을 위한 시그니처 유지
    try:
        op = float(open_price) if open_price is not None else 0.0
        ref = float(reference_price) if reference_price is not None else 0.0
    except (TypeError, ValueError):
        return False, GAP_REASON_DISABLED, 0.0

    if threshold_pct is None:
        return False, GAP_REASON_DISABLED, 0.0
    try:
        threshold = float(threshold_pct)
    except (TypeError, ValueError):
        return False, GAP_REASON_DISABLED, 0.0
    if threshold <= 0:
        return False, GAP_REASON_DISABLED, 0.0

    if op <= 0 or ref <= 0:
        return False, GAP_REASON_DISABLED, 0.0

    # 기준: (open - reference) / reference * 100
    raw_gap_pct = ((op - ref) / ref) * 100.0

    # 이익 갭은 절대 발동 금지
    if raw_gap_pct > 0:
        return False, GAP_REASON_OTHER, raw_gap_pct

    # threshold + epsilon 초과 손실(음수)만 발동
    epsilon = max(float(epsilon_pct), 0.0)
    if raw_gap_pct <= -(threshold + epsilon):
        return True, GAP_REASON_TRIGGERED, raw_gap_pct

    return False, GAP_REASON_OTHER, raw_gap_pct
