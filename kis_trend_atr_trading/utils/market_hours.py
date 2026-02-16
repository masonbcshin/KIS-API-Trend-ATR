"""
KIS Trend-ATR Trading System - 거래시간 검증 모듈

한국 주식시장 거래시간을 관리하고 검증합니다.

정규장: 09:00 ~ 15:30
- 동시호가: 08:30~09:00, 15:20~15:30
- 안전 마진을 위해 09:00~15:20 사이에만 주문

⚠️ 공휴일은 별도 캘린더 연동이 필요합니다.
"""

from datetime import datetime, time, date, timedelta
from enum import Enum
from typing import Optional, Sequence, Tuple
import logging
import pytz

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 시간대 설정 (KST)
# ════════════════════════════════════════════════════════════════
KST = pytz.timezone('Asia/Seoul')


def _combine_kst(target_date: date, target_time: time) -> datetime:
    """
    pytz에서 tzinfo 직접 주입 대신 localize를 사용해 올바른 KST datetime을 생성합니다.
    """
    return KST.localize(datetime.combine(target_date, target_time))


# ════════════════════════════════════════════════════════════════
# 거래시간 설정
# ════════════════════════════════════════════════════════════════

# 정규장 시작 시간 (동시호가 종료 후)
MARKET_OPEN = time(9, 0, 0)

# 정규장 종료 시간 (동시호가 시작 전, 안전 마진 포함)
MARKET_CLOSE = time(15, 20, 0)

# 상태머신 세션 판정 기준 정규장 종료(15:30)
SESSION_CLOSE = time(15, 30, 0)

# 점심시간 (선택적 거래 제한용)
LUNCH_START = time(11, 30, 0)
LUNCH_END = time(13, 0, 0)

# 2024-2025년 한국 주식시장 휴장일 (수동 관리)
# 실제 운영 시 외부 API 또는 캘린더 서비스 연동 권장
HOLIDAYS_2024_2025 = {
    # 2024년
    date(2024, 1, 1),   # 신정
    date(2024, 2, 9),   # 설날 연휴
    date(2024, 2, 10),  # 설날
    date(2024, 2, 11),  # 설날 연휴
    date(2024, 2, 12),  # 대체휴일
    date(2024, 3, 1),   # 삼일절
    date(2024, 4, 10),  # 국회의원선거
    date(2024, 5, 1),   # 근로자의날
    date(2024, 5, 6),   # 대체휴일
    date(2024, 5, 15),  # 부처님오신날
    date(2024, 6, 6),   # 현충일
    date(2024, 8, 15),  # 광복절
    date(2024, 9, 16),  # 추석 연휴
    date(2024, 9, 17),  # 추석
    date(2024, 9, 18),  # 추석 연휴
    date(2024, 10, 3),  # 개천절
    date(2024, 10, 9),  # 한글날
    date(2024, 12, 25), # 크리스마스
    date(2024, 12, 31), # 연말휴장
    
    # 2025년
    date(2025, 1, 1),   # 신정
    date(2025, 1, 28),  # 설날 연휴
    date(2025, 1, 29),  # 설날
    date(2025, 1, 30),  # 설날 연휴
    date(2025, 3, 1),   # 삼일절
    date(2025, 5, 1),   # 근로자의날
    date(2025, 5, 5),   # 어린이날
    date(2025, 5, 6),   # 대체휴일
    date(2025, 6, 6),   # 현충일
    date(2025, 8, 15),  # 광복절
    date(2025, 10, 3),  # 개천절
    date(2025, 10, 5),  # 추석 연휴
    date(2025, 10, 6),  # 추석
    date(2025, 10, 7),  # 추석 연휴
    date(2025, 10, 8),  # 대체휴일
    date(2025, 10, 9),  # 한글날
    date(2025, 12, 25), # 크리스마스
    date(2025, 12, 31), # 연말휴장
    
    # 2026년 (예상)
    date(2026, 1, 1),   # 신정
    date(2026, 2, 16),  # 설날 연휴
    date(2026, 2, 17),  # 설날
    date(2026, 2, 18),  # 설날 연휴
    date(2026, 3, 1),   # 삼일절
    date(2026, 3, 2),   # 대체휴일
    date(2026, 5, 1),   # 근로자의날
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 부처님오신날
    date(2026, 5, 25),  # 대체휴일
    date(2026, 6, 6),   # 현충일
    date(2026, 8, 15),  # 광복절
    date(2026, 8, 17),  # 대체휴일
    date(2026, 9, 24),  # 추석 연휴
    date(2026, 9, 25),  # 추석
    date(2026, 9, 26),  # 추석 연휴
    date(2026, 10, 3),  # 개천절
    date(2026, 10, 5),  # 대체휴일
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 크리스마스
    date(2026, 12, 31), # 연말휴장
}


class MarketSessionState(Enum):
    """런타임 상태머신용 시장 세션 상태."""

    OFF_SESSION = "OFF_SESSION"
    PREOPEN_WARMUP = "PREOPEN_WARMUP"
    IN_SESSION = "IN_SESSION"
    AUCTION_GUARD = "AUCTION_GUARD"
    POSTCLOSE = "POSTCLOSE"


def _ensure_tz(check_time: Optional[datetime], tz: str) -> datetime:
    tzinfo = pytz.timezone(tz)
    if check_time is None:
        return datetime.now(tzinfo)
    if check_time.tzinfo is None:
        return tzinfo.localize(check_time)
    return check_time.astimezone(tzinfo)


def _parse_hhmm(value: str) -> Optional[time]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        hour_str, minute_str = raw.split(":", 1)
        return time(int(hour_str), int(minute_str), 0)
    except Exception:
        return None


def _in_auction_window(now: datetime, windows: Sequence[str]) -> Tuple[bool, str]:
    current_time = now.time()
    for raw_window in windows:
        token = str(raw_window or "").strip()
        if not token:
            continue
        if "-" not in token:
            continue
        start_raw, end_raw = token.split("-", 1)
        start_time = _parse_hhmm(start_raw)
        end_time = _parse_hhmm(end_raw)
        if start_time is None or end_time is None:
            continue
        if start_time <= current_time < end_time:
            return True, token
    return False, ""


def get_market_session_state(
    now: Optional[datetime] = None,
    tz: str = "Asia/Seoul",
    preopen_warmup_min: int = 10,
    postclose_min: int = 10,
    auction_guard_windows: Optional[Sequence[str]] = None,
) -> Tuple[MarketSessionState, str]:
    """
    24/365 런타임용 시장 세션 상태를 반환합니다.

    기본 세션:
      - IN_SESSION: 09:00~15:30 (KST)
      - 주말/휴장: OFF_SESSION
    """
    now_kst = _ensure_tz(now, tz)
    today = now_kst.date()

    if is_weekend(today):
        return MarketSessionState.OFF_SESSION, "weekend_closed"
    if is_holiday(today):
        return MarketSessionState.OFF_SESSION, "holiday_closed"

    open_dt = now_kst.replace(
        hour=MARKET_OPEN.hour,
        minute=MARKET_OPEN.minute,
        second=0,
        microsecond=0,
    )
    close_dt = now_kst.replace(
        hour=SESSION_CLOSE.hour,
        minute=SESSION_CLOSE.minute,
        second=0,
        microsecond=0,
    )
    preopen_dt = open_dt - timedelta(minutes=max(int(preopen_warmup_min), 0))
    postclose_end_dt = close_dt + timedelta(minutes=max(int(postclose_min), 0))

    windows = list(auction_guard_windows or [])
    in_auction, auction_reason = _in_auction_window(now_kst, windows)
    if in_auction:
        return MarketSessionState.AUCTION_GUARD, f"auction_guard:{auction_reason}"

    if now_kst < preopen_dt:
        return MarketSessionState.OFF_SESSION, "off_session_before_prewarm"
    if preopen_dt <= now_kst < open_dt:
        return MarketSessionState.PREOPEN_WARMUP, "preopen_warmup"
    if open_dt <= now_kst < close_dt:
        return MarketSessionState.IN_SESSION, "regular_session_open"
    if close_dt <= now_kst < postclose_end_dt:
        return MarketSessionState.POSTCLOSE, "post_close_cleanup"
    return MarketSessionState.OFF_SESSION, "off_session_after_postclose"


# ════════════════════════════════════════════════════════════════
# 공개 함수
# ════════════════════════════════════════════════════════════════

def get_now() -> datetime:
    
    """현재 시간을 KST 기준으로 반환합니다."""
    return datetime.now(KST)

def get_today() -> date:
    """오늘 날짜를 KST 기준으로 반환합니다."""
    return datetime.now(KST).date()

def is_holiday(check_date: date = None) -> bool:
    """
    주어진 날짜가 휴장일인지 확인합니다.
    
    Args:
        check_date: 확인할 날짜 (None이면 오늘)
    
    Returns:
        bool: 휴장일 여부
    """
    if check_date is None:
        check_date = get_today()
    
    return check_date in HOLIDAYS_2024_2025

def is_weekend(check_date: date = None) -> bool:
    """
    주어진 날짜가 주말인지 확인합니다.
    
    Args:
        check_date: 확인할 날짜 (None이면 오늘)
    
    Returns:
        bool: 주말 여부
    """
    if check_date is None:
        check_date = get_today()
    
    # 5: 토요일, 6: 일요일
    return check_date.weekday() >= 5

def is_market_open(check_time: datetime = None) -> bool:
    """
    현재 시장이 열려있는지 확인합니다.
    
    정규장 시간: 09:00 ~ 15:20 (동시호가 제외)
    
    Args:
        check_time: 확인할 시간 (None이면 현재)
    
    Returns:
        bool: 시장 오픈 여부
    """
    if check_time is None:
        check_time = get_now()
    
    check_date = check_time.date()
    current_time = check_time.time()
    
    # 주말 체크
    if is_weekend(check_date):
        return False
    
    # 휴장일 체크
    if is_holiday(check_date):
        return False
    
    # 거래시간 체크
    return MARKET_OPEN <= current_time <= MARKET_CLOSE

def get_market_status(check_time: datetime = None) -> Tuple[bool, str]:
    """
    시장 상태를 상세히 반환합니다.
    
    Args:
        check_time: 확인할 시간 (None이면 현재)
    
    Returns:
        Tuple[bool, str]: (시장 오픈 여부, 상태 설명)
    """
    if check_time is None:
        check_time = get_now()
    
    check_date = check_time.date()
    current_time = check_time.time()
    
    # 주말 체크
    if is_weekend(check_date):
        return False, "주말 휴장"
    
    # 휴장일 체크
    if is_holiday(check_date):
        return False, "공휴일 휴장"
    
    # 장 시작 전
    if current_time < MARKET_OPEN:
        return False, f"장 시작 전 (시작: {MARKET_OPEN.strftime('%H:%M')})"
    
    # 장 마감 후
    if current_time > MARKET_CLOSE:
        return False, f"장 마감 후 (마감: {MARKET_CLOSE.strftime('%H:%M')})"
    
    # 점심시간 (정보성, 거래는 가능)
    if LUNCH_START <= current_time <= LUNCH_END:
        return True, "점심시간 (거래 가능)"
    
    return True, "정규장 운영 중"

def get_time_to_market_open() -> int:
    """
    장 시작까지 남은 시간을 초 단위로 반환합니다.
    
    Returns:
        int: 남은 시간 (초), 이미 열려있으면 0
    """
    now = get_now()
    
    if is_market_open(now):
        return 0
    
    # 오늘 장 시작 시간
    today_open = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)

    if now < today_open:
        # 오늘 장 시작 전
        return int((today_open - now).total_seconds())
    else:
        # 오늘 장 마감 후 - 다음 영업일 계산
        from datetime import timedelta
        next_day = now.date() + timedelta(days=1)
        
        while is_weekend(next_day) or is_holiday(next_day):
            next_day += timedelta(days=1)
        
        next_open = _combine_kst(next_day, MARKET_OPEN)
        return int((next_open - now).total_seconds())

def should_skip_trading(check_time: datetime = None) -> Tuple[bool, str]:
    """
    거래를 건너뛰어야 하는지 확인합니다.
    
    Args:
        check_time: 확인할 시간 (None이면 현재)
    
    Returns:
        Tuple[bool, str]: (건너뛰기 여부, 사유)
    """
    is_open, reason = get_market_status(check_time)
    
    if not is_open:
        return True, reason
    
    return False, ""


# ════════════════════════════════════════════════════════════════
# 유틸리티 함수
# ════════════════════════════════════════════════════════════════

def format_market_hours() -> str:
    """거래시간 정보를 문자열로 반환합니다."""
    return f"정규장: {MARKET_OPEN.strftime('%H:%M')} ~ {MARKET_CLOSE.strftime('%H:%M')}"

def get_next_trading_day(from_date: date = None) -> date:
    """
    다음 거래일을 반환합니다.
    
    Args:
        from_date: 기준 날짜 (None이면 오늘)
    
    Returns:
        date: 다음 거래일
    """
    from datetime import timedelta
    
    if from_date is None:
        from_date = get_today()
    
    next_day = from_date + timedelta(days=1)
    
    while is_weekend(next_day) or is_holiday(next_day):
        next_day += timedelta(days=1)
    
    return next_day
