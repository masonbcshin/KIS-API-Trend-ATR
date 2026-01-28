"""
trader/risk_manager.py - 리스크 관리 모듈

손절/익절 관리, 포지션 크기 계산, 중복 진입 방지 등
리스크 관리 기능을 담당합니다.
"""

from datetime import datetime
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field

from config.settings import get_settings
from trader.broker_kis import KISBroker, Position
from trader.notifier import get_notifier


@dataclass
class TradePosition:
    """거래 포지션 데이터 클래스"""
    stock_code: str
    stock_name: str
    entry_price: float
    quantity: int
    stop_loss_price: float
    take_profit_price: float
    entry_time: datetime = field(default_factory=datetime.now)


class RiskManager:
    """
    리스크 관리자 클래스
    
    손절/익절 가격 계산, 포지션 크기 결정,
    중복 진입 방지 등 리스크 관리 기능을 제공합니다.
    """
    
    def __init__(self, broker: KISBroker):
        self.broker = broker
        self.settings = get_settings()
        self.notifier = get_notifier()
        
        # 현재 관리 중인 포지션
        self._positions: Dict[str, TradePosition] = {}
    
    # ═══════════════════════════════════════════════════════════════
    # 손절/익절 계산
    # ═══════════════════════════════════════════════════════════════
    
    def calculate_stop_loss(self, entry_price: float) -> float:
        """
        손절가 계산
        
        Args:
            entry_price: 진입가
        
        Returns:
            float: 손절가
        """
        stop_loss_pct = self.settings.strategy.stop_loss_pct
        return entry_price * (1 + stop_loss_pct / 100)
    
    def calculate_take_profit(self, entry_price: float) -> float:
        """
        익절가 계산
        
        Args:
            entry_price: 진입가
        
        Returns:
            float: 익절가
        """
        take_profit_pct = self.settings.strategy.take_profit_pct
        return entry_price * (1 + take_profit_pct / 100)
    
    # ═══════════════════════════════════════════════════════════════
    # 포지션 크기 계산
    # ═══════════════════════════════════════════════════════════════
    
    def calculate_position_size(
        self,
        entry_price: float,
        total_capital: float
    ) -> int:
        """
        포지션 크기 계산 (리스크 기반)
        
        1회 거래 리스크 = 총 자본 × risk_per_trade
        포지션 크기 = 1회 거래 리스크 / (진입가 × 손절률)
        
        Args:
            entry_price: 진입가
            total_capital: 총 자본금
        
        Returns:
            int: 주문 수량
        """
        risk_per_trade = self.settings.strategy.risk_per_trade
        stop_loss_pct = abs(self.settings.strategy.stop_loss_pct)
        min_order_amount = self.settings.strategy.min_order_amount
        
        # 1회 거래 리스크 금액
        risk_amount = total_capital * risk_per_trade
        
        # 1주당 리스크 금액
        risk_per_share = entry_price * (stop_loss_pct / 100)
        
        if risk_per_share <= 0:
            return 0
        
        # 포지션 크기
        quantity = int(risk_amount / risk_per_share)
        
        # 최소 주문 금액 체크
        order_amount = entry_price * quantity
        if order_amount < min_order_amount:
            quantity = int(min_order_amount / entry_price) + 1
        
        return max(1, quantity)
    
    # ═══════════════════════════════════════════════════════════════
    # 포지션 관리
    # ═══════════════════════════════════════════════════════════════
    
    def add_position(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        quantity: int
    ) -> TradePosition:
        """
        신규 포지션 추가
        
        Args:
            stock_code: 종목코드
            stock_name: 종목명
            entry_price: 진입가
            quantity: 수량
        
        Returns:
            TradePosition: 생성된 포지션
        """
        position = TradePosition(
            stock_code=stock_code,
            stock_name=stock_name,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss_price=self.calculate_stop_loss(entry_price),
            take_profit_price=self.calculate_take_profit(entry_price)
        )
        
        self._positions[stock_code] = position
        return position
    
    def remove_position(self, stock_code: str) -> Optional[TradePosition]:
        """포지션 제거"""
        return self._positions.pop(stock_code, None)
    
    def get_position(self, stock_code: str) -> Optional[TradePosition]:
        """포지션 조회"""
        return self._positions.get(stock_code)
    
    def has_position(self, stock_code: str) -> bool:
        """포지션 보유 여부 확인"""
        return stock_code in self._positions
    
    def get_all_positions(self) -> List[TradePosition]:
        """전체 포지션 목록"""
        return list(self._positions.values())
    
    def get_position_count(self) -> int:
        """보유 포지션 수"""
        return len(self._positions)
    
    # ═══════════════════════════════════════════════════════════════
    # 손절/익절 체크
    # ═══════════════════════════════════════════════════════════════
    
    def check_stop_loss(
        self,
        stock_code: str,
        current_price: float
    ) -> Tuple[bool, Optional[TradePosition]]:
        """
        손절 조건 체크
        
        Args:
            stock_code: 종목코드
            current_price: 현재가
        
        Returns:
            Tuple: (손절 여부, 포지션)
        """
        position = self.get_position(stock_code)
        if not position:
            return False, None
        
        if current_price <= position.stop_loss_price:
            return True, position
        
        return False, position
    
    def check_take_profit(
        self,
        stock_code: str,
        current_price: float
    ) -> Tuple[bool, Optional[TradePosition]]:
        """
        익절 조건 체크
        
        Args:
            stock_code: 종목코드
            current_price: 현재가
        
        Returns:
            Tuple: (익절 여부, 포지션)
        """
        position = self.get_position(stock_code)
        if not position:
            return False, None
        
        if current_price >= position.take_profit_price:
            return True, position
        
        return False, position
    
    def check_exit_conditions(
        self,
        stock_code: str,
        current_price: float
    ) -> Tuple[bool, str, Optional[TradePosition]]:
        """
        청산 조건 종합 체크
        
        Args:
            stock_code: 종목코드
            current_price: 현재가
        
        Returns:
            Tuple: (청산 여부, 청산 사유, 포지션)
        """
        # 손절 체크
        is_stop_loss, position = self.check_stop_loss(stock_code, current_price)
        if is_stop_loss:
            return True, "stop_loss", position
        
        # 익절 체크
        is_take_profit, position = self.check_take_profit(stock_code, current_price)
        if is_take_profit:
            return True, "take_profit", position
        
        return False, "", position
    
    # ═══════════════════════════════════════════════════════════════
    # 리스크 체크
    # ═══════════════════════════════════════════════════════════════
    
    def can_open_new_position(self) -> Tuple[bool, str]:
        """
        신규 포지션 진입 가능 여부 체크
        
        Returns:
            Tuple: (진입 가능 여부, 불가 사유)
        """
        max_positions = self.settings.strategy.max_positions
        
        if self.get_position_count() >= max_positions:
            return False, f"최대 보유 종목 수 초과 ({max_positions}개)"
        
        if not self.broker.can_place_new_order():
            return False, "신규 진입 불가 시간"
        
        return True, ""
    
    def can_enter_stock(self, stock_code: str) -> Tuple[bool, str]:
        """
        특정 종목 진입 가능 여부 체크 (중복 진입 방지)
        
        Args:
            stock_code: 종목코드
        
        Returns:
            Tuple: (진입 가능 여부, 불가 사유)
        """
        if self.has_position(stock_code):
            return False, "이미 해당 종목 포지션 보유 중"
        
        return self.can_open_new_position()
    
    # ═══════════════════════════════════════════════════════════════
    # 청산 실행
    # ═══════════════════════════════════════════════════════════════
    
    def execute_stop_loss(
        self,
        stock_code: str,
        current_price: float
    ) -> bool:
        """
        손절 실행
        
        Args:
            stock_code: 종목코드
            current_price: 현재가
        
        Returns:
            bool: 실행 성공 여부
        """
        position = self.get_position(stock_code)
        if not position:
            return False
        
        # 매도 주문
        result = self.broker.place_sell_order(
            stock_code=stock_code,
            quantity=position.quantity
        )
        
        if result.success:
            loss = (current_price - position.entry_price) * position.quantity
            loss_pct = ((current_price / position.entry_price) - 1) * 100
            
            # 텔레그램 알림
            self.notifier.notify_stop_loss(
                stock_code=stock_code,
                stock_name=position.stock_name,
                entry_price=position.entry_price,
                exit_price=current_price,
                quantity=position.quantity,
                loss=loss,
                loss_pct=loss_pct
            )
            
            self.remove_position(stock_code)
            return True
        
        return False
    
    def execute_take_profit(
        self,
        stock_code: str,
        current_price: float
    ) -> bool:
        """
        익절 실행
        
        Args:
            stock_code: 종목코드
            current_price: 현재가
        
        Returns:
            bool: 실행 성공 여부
        """
        position = self.get_position(stock_code)
        if not position:
            return False
        
        # 매도 주문
        result = self.broker.place_sell_order(
            stock_code=stock_code,
            quantity=position.quantity
        )
        
        if result.success:
            profit = (current_price - position.entry_price) * position.quantity
            profit_pct = ((current_price / position.entry_price) - 1) * 100
            
            # 텔레그램 알림
            self.notifier.notify_take_profit(
                stock_code=stock_code,
                stock_name=position.stock_name,
                entry_price=position.entry_price,
                exit_price=current_price,
                quantity=position.quantity,
                profit=profit,
                profit_pct=profit_pct
            )
            
            self.remove_position(stock_code)
            return True
        
        return False
    
    def sync_positions_from_broker(self) -> None:
        """브로커의 실제 보유 종목과 동기화"""
        broker_positions = self.broker.get_positions()
        
        for pos in broker_positions:
            if pos.stock_code not in self._positions:
                # 브로커에 있지만 관리 중이 아닌 포지션
                # (수동 매수 등으로 발생 가능)
                self.add_position(
                    stock_code=pos.stock_code,
                    stock_name=pos.stock_name,
                    entry_price=pos.avg_price,
                    quantity=pos.quantity
                )
