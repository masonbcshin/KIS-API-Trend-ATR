"""
KIS Trend-ATR Trading System - 데이터베이스 ORM 모델
"""
from sqlalchemy import (
    Column, Integer, String, DECIMAL, DateTime, Date, Enum as SAEnum, func
)
from sqlalchemy.orm import declarative_base

# position_manager와 enum을 공유하기 위해 절대경로 사용 시도
# (단, 순환참조 문제가 발생할 수 있어 주의 필요)
try:
    from engine.position_manager import PositionState, ExitReason
except ImportError:
    # 대체 Enum 정의 (순환참조 방지용)
    import enum
    class PositionState(enum.Enum):
        PENDING = "PENDING"
        ENTERED = "ENTERED"
        PARTIAL_EXIT = "PARTIAL_EXIT"
        EXITED = "EXITED"
    class ExitReason(enum.Enum):
        ATR_STOP = "ATR_STOP"
        TAKE_PROFIT = "TAKE_PROFIT"
        TRAILING_STOP = "TRAILING_STOP"
        TREND_BROKEN = "TREND_BROKEN"
        GAP_PROTECTION = "GAP_PROTECTION"
        MANUAL = "MANUAL"
        KILL_SWITCH = "KILL_SWITCH"
        OTHER = "OTHER"

Base = declarative_base()

class Position(Base):
    """
    `positions` 테이블에 매핑되는 SQLAlchemy ORM 모델
    
    [타임존 관리]
    모든 DateTime 필드는 `timezone=True` 옵션을 사용하여, 타임존 정보가 포함된
    TIMESTAMP WITH TIME ZONE 타입으로 데이터베이스에 저장됩니다.
    """
    __tablename__ = 'positions'

    position_id = Column(String(64), primary_key=True, comment="포지션 고유 ID")
    stock_code = Column(String(20), nullable=False, index=True, comment="종목 코드")
    stock_name = Column(String(100), comment="종목명")
    state = Column(SAEnum(PositionState), nullable=False, index=True, comment="포지션 상태")
    side = Column(String(10), nullable=False, default='LONG', comment="포지션 방향")
    
    entry_price = Column(DECIMAL(15, 2), nullable=False, comment="매수 평균가")
    quantity = Column(Integer, nullable=False, comment="보유 수량")
    entry_date = Column(Date, nullable=False, comment="최초 매수 날짜")
    entry_time = Column(DateTime(timezone=True), nullable=False, comment="최초 매수 시간 (KST)")
    entry_order_no = Column(String(50), comment="진입 주문 번호")
    
    atr_at_entry = Column(DECIMAL(15, 2), nullable=False, comment="진입 시점 ATR")
    stop_loss = Column(DECIMAL(15, 2), nullable=False, comment="기본 손절가")
    take_profit = Column(DECIMAL(15, 2), nullable=False, comment="기본 익절가")
    
    trailing_stop = Column(DECIMAL(15, 2), comment="트레일링 스탑 가격")
    highest_price = Column(DECIMAL(15, 2), comment="보유 중 최고가")
    
    current_price = Column(DECIMAL(15, 2), comment="현재가")
    unrealized_pnl = Column(DECIMAL(15, 2), comment="미실현 손익")
    unrealized_pnl_pct = Column(DECIMAL(10, 4), comment="미실현 손익률")
    
    exit_price = Column(DECIMAL(15, 2), comment="매도 평균가")
    exit_date = Column(Date, comment="청산 날짜")
    exit_time = Column(DateTime(timezone=True), comment="청산 시간 (KST)")
    exit_reason = Column(SAEnum(ExitReason), comment="청산 사유")
    exit_order_no = Column(String(50), comment="청산 주문 번호")
    
    realized_pnl = Column(DECIMAL(15, 2), comment="실현 손익")
    realized_pnl_pct = Column(DECIMAL(10, 4), comment="실현 손익률")
    commission = Column(DECIMAL(15, 2), comment="수수료")
    holding_days = Column(Integer, comment="보유 기간(일)")
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="생성 시간 (UTC)")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(), comment="수정 시간 (UTC)")

    def __repr__(self):
        return (f"<Position(id='{self.position_id}', stock='{self.stock_code}', "
                f"state='{self.state.value}', qty='{self.quantity}')>")
