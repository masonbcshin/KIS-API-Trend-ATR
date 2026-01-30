"""
KIS Trend-ATR Trading System - 포지션 스냅샷 데이터 클래스

특정 시점의 포지션 상태와 손익을 기록합니다.
"""

from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List


@dataclass
class PositionSnapshot:
    """
    포지션 스냅샷 데이터 클래스
    
    ★ 특정 시점의 포지션 상태를 기록
    ★ 미실현 손익 추적에 사용
    
    Attributes:
        symbol: 종목 코드
        entry_price: 진입가
        current_price: 현재가
        quantity: 보유 수량
        entry_time: 진입 시간
        snapshot_time: 스냅샷 시간
        unrealized_pnl: 미실현 손익
        unrealized_pnl_pct: 미실현 손익률 (%)
        atr_at_entry: 진입 시 ATR
        stop_price: 손절가
        take_profit_price: 익절가
        trailing_stop: 트레일링 스탑
        highest_price: 최고가
    """
    symbol: str
    entry_price: float
    current_price: float
    quantity: int
    entry_time: datetime
    snapshot_time: datetime = field(default_factory=datetime.now)
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    atr_at_entry: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trailing_stop: Optional[float] = None
    highest_price: Optional[float] = None
    
    def __post_init__(self):
        """초기화 후 처리"""
        # 미실현 손익 자동 계산
        if self.unrealized_pnl == 0.0:
            self.unrealized_pnl = (self.current_price - self.entry_price) * self.quantity
            self.unrealized_pnl_pct = ((self.current_price - self.entry_price) / self.entry_price) * 100
    
    def get_market_value(self) -> float:
        """현재 평가금액"""
        return self.current_price * self.quantity
    
    def get_cost_basis(self) -> float:
        """매수 원금"""
        return self.entry_price * self.quantity
    
    def get_stop_loss_distance(self) -> Optional[float]:
        """손절가까지 거리 (%)"""
        if self.stop_price:
            return ((self.current_price - self.stop_price) / self.current_price) * 100
        return None
    
    def get_take_profit_distance(self) -> Optional[float]:
        """익절가까지 거리 (%)"""
        if self.take_profit_price:
            return ((self.take_profit_price - self.current_price) / self.current_price) * 100
        return None
    
    def is_near_stop_loss(self, threshold_pct: float = 80.0) -> bool:
        """손절가에 근접했는지 확인"""
        if not self.stop_price:
            return False
        
        total_distance = self.entry_price - self.stop_price
        if total_distance <= 0:
            return False
        
        current_distance = self.entry_price - self.current_price
        progress = (current_distance / total_distance) * 100
        
        return progress >= threshold_pct
    
    def is_near_take_profit(self, threshold_pct: float = 80.0) -> bool:
        """익절가에 근접했는지 확인"""
        if not self.take_profit_price:
            return False
        
        total_distance = self.take_profit_price - self.entry_price
        if total_distance <= 0:
            return False
        
        current_distance = self.current_price - self.entry_price
        progress = (current_distance / total_distance) * 100
        
        return progress >= threshold_pct
    
    def get_holding_days(self) -> int:
        """보유 일수"""
        return (self.snapshot_time.date() - self.entry_time.date()).days
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        result = asdict(self)
        if isinstance(result.get('entry_time'), datetime):
            result['entry_time'] = result['entry_time'].isoformat()
        if isinstance(result.get('snapshot_time'), datetime):
            result['snapshot_time'] = result['snapshot_time'].isoformat()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PositionSnapshot":
        """딕셔너리에서 생성"""
        entry_time = data.get('entry_time')
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time)
        
        snapshot_time = data.get('snapshot_time')
        if isinstance(snapshot_time, str):
            snapshot_time = datetime.fromisoformat(snapshot_time)
        
        return cls(
            symbol=data['symbol'],
            entry_price=float(data['entry_price']),
            current_price=float(data['current_price']),
            quantity=int(data['quantity']),
            entry_time=entry_time or datetime.now(),
            snapshot_time=snapshot_time or datetime.now(),
            unrealized_pnl=float(data.get('unrealized_pnl', 0)),
            unrealized_pnl_pct=float(data.get('unrealized_pnl_pct', 0)),
            atr_at_entry=float(data['atr_at_entry']) if data.get('atr_at_entry') else None,
            stop_price=float(data['stop_price']) if data.get('stop_price') else None,
            take_profit_price=float(data['take_profit_price']) if data.get('take_profit_price') else None,
            trailing_stop=float(data['trailing_stop']) if data.get('trailing_stop') else None,
            highest_price=float(data['highest_price']) if data.get('highest_price') else None
        )


@dataclass
class AccountSnapshot:
    """
    계좌 스냅샷 데이터 클래스
    
    ★ 특정 시점의 계좌 전체 상태를 기록
    ★ Equity Curve, MDD 계산에 사용
    """
    snapshot_time: datetime = field(default_factory=datetime.now)
    total_equity: float = 0.0
    cash: float = 0.0
    position_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    position_count: int = 0
    positions: List[PositionSnapshot] = field(default_factory=list)
    
    @property
    def total_pnl(self) -> float:
        """총 손익 (실현 + 미실현)"""
        return self.realized_pnl + self.unrealized_pnl
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "snapshot_time": self.snapshot_time.isoformat(),
            "total_equity": self.total_equity,
            "cash": self.cash,
            "position_value": self.position_value,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "position_count": self.position_count,
            "positions": [p.to_dict() for p in self.positions]
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountSnapshot":
        """딕셔너리에서 생성"""
        snapshot_time = data.get('snapshot_time')
        if isinstance(snapshot_time, str):
            snapshot_time = datetime.fromisoformat(snapshot_time)
        
        positions = [
            PositionSnapshot.from_dict(p) 
            for p in data.get('positions', [])
        ]
        
        return cls(
            snapshot_time=snapshot_time or datetime.now(),
            total_equity=float(data.get('total_equity', 0)),
            cash=float(data.get('cash', 0)),
            position_value=float(data.get('position_value', 0)),
            unrealized_pnl=float(data.get('unrealized_pnl', 0)),
            realized_pnl=float(data.get('realized_pnl', 0)),
            position_count=int(data.get('position_count', 0)),
            positions=positions
        )
