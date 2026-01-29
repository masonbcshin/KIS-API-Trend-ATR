"""
KIS Trend-ATR Trading System - 트레이딩 상태 머신

멀티데이 포지션 관리를 위한 상태 머신 구현.
전략의 본질에 따라 포지션은 Exit 신호가 발생할 때까지 유지됩니다.

상태:
    - WAIT: 포지션 없음, 진입 조건 감시 중
    - ENTERED: 포지션 보유 중, Exit 조건만 감시
    - EXITED: 청산 완료, 다음 WAIT로 전환 대기

★ 핵심 원칙:
    - EOD(장 마감) 시간 기준 강제 청산 절대 금지
    - Exit는 오직 가격 조건(ATR/Trailing Stop/추세 붕괴)으로만 발생
    - 진입 시 ATR 값은 고정되며 익일에 재계산 금지
"""

from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, Any


class TradingState(Enum):
    """
    트레이딩 상태 열거형
    
    ★ 절대 규칙:
        - 시간(EOD)은 상태 전환 조건이 아님
        - Exit는 오직 가격 조건으로만 발생
    """
    WAIT = "WAIT"         # 포지션 없음, 진입 조건 감시
    ENTERED = "ENTERED"   # 포지션 보유, Exit 조건만 감시
    EXITED = "EXITED"     # 청산 완료, 다음 WAIT 대기


class ExitReason(Enum):
    """
    청산 사유 열거형
    
    ★ 허용된 청산 사유만 정의
    ★ EOD_CLOSE 같은 시간 기반 청산은 절대 추가하지 않음
    """
    ATR_STOP_LOSS = "ATR_STOP_LOSS"           # ATR 기반 손절
    ATR_TAKE_PROFIT = "ATR_TAKE_PROFIT"       # ATR 기반 익절
    TRAILING_STOP = "TRAILING_STOP"           # 트레일링 스탑
    TREND_BROKEN = "TREND_BROKEN"             # 추세 붕괴
    GAP_PROTECTION = "GAP_PROTECTION"         # 갭 보호 (옵션)
    MANUAL_EXIT = "MANUAL_EXIT"               # 수동 청산
    KILL_SWITCH = "KILL_SWITCH"               # 긴급 정지


@dataclass
class MultidayPosition:
    """
    멀티데이 포지션 데이터 클래스
    
    ★ 핵심 필드:
        - atr_at_entry: 진입 시 ATR (고정, 절대 재계산 금지)
        - stop_loss: 손절가 (진입 시 설정, ATR 재계산으로 변경 금지)
        - trailing_stop: 트레일링 스탑 가격 (고가 갱신 시만 상향 조정)
        - highest_price: 포지션 보유 중 최고가 (트레일링 스탑용)
    
    Attributes:
        symbol: 종목 코드
        position: 포지션 방향 (LONG)
        entry_price: 진입가
        quantity: 보유 수량
        atr_at_entry: 진입 시 ATR (★ 고정값, 절대 재계산 금지)
        stop_loss: 손절가
        take_profit: 익절가 (None이면 트레일링만 사용)
        trailing_stop: 현재 트레일링 스탑 가격
        highest_price: 보유 중 최고가
        entry_date: 진입일 (YYYY-MM-DD)
        entry_time: 진입 시간 (HH:MM:SS)
        state: 현재 트레이딩 상태
    """
    symbol: str
    position: str = "LONG"
    entry_price: float = 0.0
    quantity: int = 0
    atr_at_entry: float = 0.0  # ★ 진입 시 고정, 재계산 금지
    stop_loss: float = 0.0
    take_profit: Optional[float] = None  # None이면 트레일링만 사용
    trailing_stop: float = 0.0
    highest_price: float = 0.0
    entry_date: str = ""
    entry_time: str = ""
    state: TradingState = TradingState.WAIT
    
    def __post_init__(self):
        if not self.entry_date:
            self.entry_date = datetime.now().strftime("%Y-%m-%d")
        if not self.entry_time:
            self.entry_time = datetime.now().strftime("%H:%M:%S")
        if self.highest_price == 0.0 and self.entry_price > 0:
            self.highest_price = self.entry_price
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환 (JSON 저장용)"""
        data = asdict(self)
        data["state"] = self.state.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MultidayPosition":
        """딕셔너리에서 생성 (JSON 로드용)"""
        if "state" in data:
            data["state"] = TradingState(data["state"])
        return cls(**data)
    
    def update_highest_price(self, current_price: float) -> bool:
        """
        최고가 갱신
        
        Args:
            current_price: 현재가
        
        Returns:
            bool: 최고가 갱신 여부
        """
        if current_price > self.highest_price:
            self.highest_price = current_price
            return True
        return False
    
    def update_trailing_stop(self, new_stop: float) -> bool:
        """
        트레일링 스탑 갱신 (상향만 허용)
        
        ★ 트레일링 스탑은 절대 하향 조정되지 않음
        
        Args:
            new_stop: 새로운 트레일링 스탑 가격
        
        Returns:
            bool: 갱신 여부
        """
        if new_stop > self.trailing_stop:
            self.trailing_stop = new_stop
            return True
        return False
    
    def get_pnl(self, current_price: float) -> tuple:
        """
        현재 손익 계산
        
        Args:
            current_price: 현재가
        
        Returns:
            tuple: (손익금액, 손익률)
        """
        if self.entry_price <= 0:
            return 0.0, 0.0
        
        pnl = (current_price - self.entry_price) * self.quantity
        pnl_pct = (current_price - self.entry_price) / self.entry_price * 100
        
        return pnl, pnl_pct
    
    def get_distance_to_stop_loss(self, current_price: float) -> float:
        """
        손절선까지의 거리 비율 (%)
        
        100%면 손절선 도달, 0%면 진입가
        
        Args:
            current_price: 현재가
        
        Returns:
            float: 손절선까지의 진행률 (%)
        """
        if self.entry_price <= 0 or self.stop_loss <= 0:
            return 0.0
        
        total_distance = self.entry_price - self.stop_loss
        if total_distance <= 0:
            return 100.0
        
        current_distance = self.entry_price - current_price
        return (current_distance / total_distance) * 100
    
    def get_distance_to_take_profit(self, current_price: float) -> float:
        """
        익절선까지의 거리 비율 (%)
        
        100%면 익절선 도달, 0%면 진입가
        
        Args:
            current_price: 현재가
        
        Returns:
            float: 익절선까지의 진행률 (%)
        """
        if self.entry_price <= 0 or self.take_profit is None:
            return 0.0
        
        total_distance = self.take_profit - self.entry_price
        if total_distance <= 0:
            return 100.0
        
        current_distance = current_price - self.entry_price
        return (current_distance / total_distance) * 100


class TradingStateMachine:
    """
    트레이딩 상태 머신
    
    ★ 핵심 원칙:
        1. EOD 청산 로직 없음
        2. Exit는 오직 가격 조건으로만 발생
        3. ATR은 진입 시 고정, 익일 재계산 금지
    """
    
    def __init__(self):
        """상태 머신 초기화"""
        self._position: Optional[MultidayPosition] = None
        self._state: TradingState = TradingState.WAIT
        self._last_exit_reason: Optional[ExitReason] = None
    
    @property
    def state(self) -> TradingState:
        """현재 상태"""
        return self._state
    
    @property
    def position(self) -> Optional[MultidayPosition]:
        """현재 포지션"""
        return self._position
    
    @property
    def has_position(self) -> bool:
        """포지션 보유 여부"""
        return self._position is not None and self._state == TradingState.ENTERED
    
    @property
    def last_exit_reason(self) -> Optional[ExitReason]:
        """마지막 청산 사유"""
        return self._last_exit_reason
    
    def enter_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        atr_at_entry: float,
        stop_loss: float,
        take_profit: Optional[float] = None,
        trailing_stop: float = 0.0
    ) -> MultidayPosition:
        """
        포지션 진입
        
        ★ 진입 시 ATR은 고정됨
        ★ 이후 ATR 재계산으로 stop_loss 변경 금지
        
        Args:
            symbol: 종목 코드
            entry_price: 진입가
            quantity: 수량
            atr_at_entry: 진입 시 ATR (고정)
            stop_loss: 손절가
            take_profit: 익절가 (None이면 트레일링만)
            trailing_stop: 초기 트레일링 스탑
        
        Returns:
            MultidayPosition: 생성된 포지션
        """
        if self._state == TradingState.ENTERED:
            raise ValueError("이미 포지션을 보유 중입니다.")
        
        self._position = MultidayPosition(
            symbol=symbol,
            position="LONG",
            entry_price=entry_price,
            quantity=quantity,
            atr_at_entry=atr_at_entry,  # ★ 고정값
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing_stop if trailing_stop > 0 else stop_loss,
            highest_price=entry_price,
            state=TradingState.ENTERED
        )
        
        self._state = TradingState.ENTERED
        self._last_exit_reason = None
        
        return self._position
    
    def exit_position(self, reason: ExitReason, exit_price: float) -> Dict[str, Any]:
        """
        포지션 청산
        
        ★ 허용된 청산 사유만 사용 가능
        ★ EOD 청산은 절대 불가
        
        Args:
            reason: 청산 사유 (ExitReason)
            exit_price: 청산가
        
        Returns:
            Dict: 청산 결과
        """
        if self._state != TradingState.ENTERED or self._position is None:
            raise ValueError("청산할 포지션이 없습니다.")
        
        pnl, pnl_pct = self._position.get_pnl(exit_price)
        
        result = {
            "symbol": self._position.symbol,
            "entry_price": self._position.entry_price,
            "exit_price": exit_price,
            "quantity": self._position.quantity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "entry_date": self._position.entry_date,
            "exit_date": datetime.now().strftime("%Y-%m-%d"),
            "exit_reason": reason.value,
            "atr_at_entry": self._position.atr_at_entry,
            "holding_days": self._calculate_holding_days()
        }
        
        self._state = TradingState.EXITED
        self._last_exit_reason = reason
        self._position = None
        
        return result
    
    def _calculate_holding_days(self) -> int:
        """보유 일수 계산"""
        if self._position is None or not self._position.entry_date:
            return 0
        
        try:
            entry = datetime.strptime(self._position.entry_date, "%Y-%m-%d")
            today = datetime.now()
            return (today - entry).days + 1
        except ValueError:
            return 0
    
    def reset_to_wait(self) -> None:
        """WAIT 상태로 리셋"""
        self._state = TradingState.WAIT
        self._position = None
    
    def restore_position(self, position: MultidayPosition) -> None:
        """
        포지션 복원 (프로그램 재시작 시)
        
        ★ 저장된 포지션을 로드하여 복원
        ★ ATR 재계산 없이 저장된 값 그대로 사용
        
        Args:
            position: 복원할 포지션
        """
        self._position = position
        self._state = TradingState.ENTERED
        self._position.state = TradingState.ENTERED
    
    def get_state_summary(self) -> Dict[str, Any]:
        """상태 요약 반환"""
        summary = {
            "state": self._state.value,
            "has_position": self.has_position,
            "last_exit_reason": self._last_exit_reason.value if self._last_exit_reason else None
        }
        
        if self._position:
            summary["position"] = self._position.to_dict()
        
        return summary
