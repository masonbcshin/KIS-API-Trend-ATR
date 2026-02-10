"""
KIS Trend-ATR Trading System - 거래시간 검증 모듈

한국 주식시장 거래시간을 관리하고 검증합니다.

정규장: 09:00 ~ 15:30
- 동시호가: 08:30~09:00, 15:20~15:30
- 안전 마진을 위해 09:00~15:20 사이에만 주문

✅ `holidays` 라이브러리를 사용하여 공휴일을 동적으로 처리합니다.
"""

from datetime import datetime, time, date, timedelta
from typing import Tuple
import logging
from zoneinfo import ZoneInfo
import holidays  # 휴장일 관리를 위해 추가

logger = logging.getLogger(__name__)

# KST (Korea Standard Time) 타임존 객체 생성
KST = ZoneInfo("Asia/Seoul")

#  대한민국 공휴일 객체 생성
KR_HOLIDAYS = holidays.KR()

# ════════════════════════════════════════════════════════════════
# 거래시간 설정
# ════════════════════════════════════════════════════════════════
MARKET_OPEN = time(9, 0, 0)
MARKET_CLOSE = time(15, 20, 0)  # 동시호가 회피
LUNCH_START = time(11, 30, 0)
LUNCH_END = time(13, 0, 0)

def get_kst_now() -> datetime:
    """현재 한국 시간을 반환합니다."""
    return datetime.now(KST)

def get_kst_today() -> date:
    """현재 한국 날짜를 반환합니다."""
    return datetime.now(KST).date()

# ════════════════════════════════════════════════════════════════
# 공개 함수
# ════════════════════════════════════════════════════════════════

def is_holiday(check_date: date = None) -> bool:
    """
    주어진 날짜가 휴장일인지 확인합니다. (대한민국 기준)
    
    Args:
        check_date: 확인할 날짜 (None이면 오늘)
    
    Returns:
        bool: 휴장일 여부
    """
    if check_date is None:
        check_date = get_kst_today()
    
    return check_date in KR_HOLIDAYS

def is_weekend(check_date: date = None) -> bool:
    """
    주어진 날짜가 주말인지 확인합니다.
    
    Args:
        check_date: 확인할 날짜 (None이면 오늘)
    
    Returns:
        bool: 주말 여부
    """
    if check_date is None:
        check_date = get_kst_today()
    
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
        check_time = get_kst_now()
    
    check_date = check_time.date()
    current_time = check_time.time()
    
    # 주말 또는 휴장일이면 거래 불가
    if is_weekend(check_date) or is_holiday(check_date):
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
        check_time = get_kst_now()
    
    check_date = check_time.date()
    current_time = check_time.time()
    
    # 휴장일 체크 (주말 포함)
    if is_weekend(check_date):
        return False, "주말 휴장"
    if is_holiday(check_date):
        # f-string으로 휴일 이름 표시
        return False, f"{KR_HOLIDAYS.get(check_date)} 휴장"
    
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
    now = get_kst_now()
    
    if is_market_open(now):
        return 0
    
    # 오늘 장 시작 시간
    today_open = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
    
    if now < today_open:
        # 오늘 장 시작 전
        return int((today_open - now).total_seconds())
    else:
        # 오늘 장 마감 후 - 다음 영업일 계산
        next_day = now.date() + timedelta(days=1)
        
        while is_weekend(next_day) or is_holiday(next_day):
            next_day += timedelta(days=1)
        
        next_open = datetime.combine(next_day, MARKET_OPEN, tzinfo=KST)
        return int((next_open - now).total_seconds())

def should_skip_trading(check_time: datetime = None) -> Tuple[bool, str]:
    """
    거래를 건너뛰어야 하는지 확인합니다.
    
    Args:
        check_time: 확인할 시간 (None이면 현재)
    
    Returns:
        Tuple[bool, str]: (건너뛰기 여부, 사유)
    """
    if check_time is None:
        check_time = get_kst_now()

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
    if from_date is None:
        from_date = get_kst_today()
    
    next_day = from_date + timedelta(days=1)
    
    while is_weekend(next_day) or is_holiday(next_day):
        next_day += timedelta(days=1)
    
    return next_day
