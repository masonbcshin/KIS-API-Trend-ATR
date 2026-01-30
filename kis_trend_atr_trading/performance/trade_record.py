"""
KIS Trend-ATR Trading System - 거래 기록 데이터 클래스

거래 기록을 표준화된 형식으로 저장하고 관리합니다.
"""

from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any
from enum import Enum


class TradeSide(Enum):
    """거래 방향"""
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(Enum):
    """청산 사유"""
    ATR_STOP = "ATR_STOP"         # ATR 기반 손절
    TAKE_PROFIT = "TAKE_PROFIT"   # 익절
    TRAILING_STOP = "TRAILING_STOP"  # 트레일링 스탑
    GAP_PROTECTION = "GAP_PROTECTION"  # 갭 보호
    MANUAL = "MANUAL"             # 수동 청산
    KILL_SWITCH = "KILL_SWITCH"   # 킬 스위치
    SIGNAL_ONLY = "SIGNAL_ONLY"   # 신호만 (가상 체결)


@dataclass
class TradeRecord:
    """
    거래 기록 데이터 클래스
    
    ★ 모든 거래(실제/가상)를 동일한 형식으로 기록
    
    Attributes:
        symbol: 종목 코드
        side: 거래 방향 (BUY/SELL)
        price: 체결가
        quantity: 수량
        executed_at: 체결 시간
        is_virtual: 가상 체결 여부 (DRY_RUN)
        reason: 청산 사유 (SELL인 경우)
        entry_price: 진입가 (SELL인 경우)
        pnl: 손익 금액
        pnl_percent: 손익률 (%)
        holding_days: 보유 일수
        order_no: 주문 번호 (실제 주문인 경우)
        mode: 실행 모드 (DRY_RUN/PAPER/REAL)
    """
    symbol: str
    side: str
    price: float
    quantity: int
    executed_at: datetime = field(default_factory=datetime.now)
    is_virtual: bool = False
    reason: Optional[str] = None
    entry_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    holding_days: Optional[int] = None
    order_no: Optional[str] = None
    mode: str = "DRY_RUN"
    atr_at_entry: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    
    def __post_init__(self):
        """초기화 후 처리"""
        # 손익 자동 계산
        if self.side == "SELL" and self.entry_price and self.pnl is None:
            self.pnl = (self.price - self.entry_price) * self.quantity
            self.pnl_percent = ((self.price - self.entry_price) / self.entry_price) * 100
    
    def is_win(self) -> bool:
        """수익 거래인지 확인"""
        return self.pnl is not None and self.pnl > 0
    
    def is_loss(self) -> bool:
        """손실 거래인지 확인"""
        return self.pnl is not None and self.pnl < 0
    
    def get_amount(self) -> float:
        """거래 금액"""
        return self.price * self.quantity
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        result = asdict(self)
        if isinstance(result.get('executed_at'), datetime):
            result['executed_at'] = result['executed_at'].isoformat()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeRecord":
        """딕셔너리에서 생성"""
        executed_at = data.get('executed_at')
        if isinstance(executed_at, str):
            executed_at = datetime.fromisoformat(executed_at)
        
        return cls(
            symbol=data['symbol'],
            side=data['side'],
            price=float(data['price']),
            quantity=int(data['quantity']),
            executed_at=executed_at or datetime.now(),
            is_virtual=data.get('is_virtual', False),
            reason=data.get('reason'),
            entry_price=float(data['entry_price']) if data.get('entry_price') else None,
            pnl=float(data['pnl']) if data.get('pnl') else None,
            pnl_percent=float(data['pnl_percent']) if data.get('pnl_percent') else None,
            holding_days=data.get('holding_days'),
            order_no=data.get('order_no'),
            mode=data.get('mode', 'DRY_RUN'),
            atr_at_entry=float(data['atr_at_entry']) if data.get('atr_at_entry') else None,
            stop_price=float(data['stop_price']) if data.get('stop_price') else None,
            take_profit_price=float(data['take_profit_price']) if data.get('take_profit_price') else None
        )
    
    def get_summary_text(self) -> str:
        """요약 텍스트 생성"""
        if self.side == "BUY":
            return (
                f"📈 {self.symbol} 매수 @ {self.price:,.0f}원 x {self.quantity}주 "
                f"({'가상' if self.is_virtual else '실제'})"
            )
        else:
            pnl_str = f"{self.pnl:+,.0f}원 ({self.pnl_percent:+.2f}%)" if self.pnl else "N/A"
            return (
                f"📉 {self.symbol} 매도 @ {self.price:,.0f}원 x {self.quantity}주 "
                f"| 손익: {pnl_str} | {self.reason or ''} "
                f"({'가상' if self.is_virtual else '실제'})"
            )


@dataclass
class DailyTradeStats:
    """일별 거래 통계"""
    trade_date: str
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    max_profit: float = 0.0
    max_loss: float = 0.0
    
    @property
    def win_rate(self) -> float:
        """승률"""
        if self.sell_count == 0:
            return 0.0
        return (self.win_count / self.sell_count) * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "date": self.trade_date,
            "total_trades": self.total_trades,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_pnl": self.total_pnl,
            "win_rate": self.win_rate,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss
        }
