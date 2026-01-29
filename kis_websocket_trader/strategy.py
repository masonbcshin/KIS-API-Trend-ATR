"""
KIS WebSocket 자동매매 시스템 - ATR 기반 전략 모듈

장 시작 전에 계산된 ATR 값과 진입/손절/익절 가격을 기반으로
실시간 시세를 감시하여 시그널을 생성합니다.

주요 기능:
    - 진입 조건 체크 (현재가 >= entry_price)
    - 손절 조건 체크 (현재가 <= stop_loss)
    - 익절 조건 체크 (현재가 >= take_profit)
    - 상태 관리 (WAIT -> ENTERED -> EXITED)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple
from datetime import datetime

from config import StockState


# 로거 설정
logger = logging.getLogger("strategy")


# ════════════════════════════════════════════════════════════════
# 시그널 유형 열거형
# ════════════════════════════════════════════════════════════════

class SignalType(Enum):
    """
    매매 시그널 유형
    
    - ENTRY: 진입 시그널
    - STOP_LOSS: 손절 시그널
    - TAKE_PROFIT: 익절 시그널
    - HOLD: 대기 (시그널 없음)
    """
    ENTRY = "ENTRY"             # 진입
    STOP_LOSS = "STOP_LOSS"     # 손절
    TAKE_PROFIT = "TAKE_PROFIT" # 익절
    HOLD = "HOLD"               # 대기


# ════════════════════════════════════════════════════════════════
# 데이터 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    """
    매매 시그널 데이터 클래스
    
    Attributes:
        signal_type: 시그널 유형
        stock_code: 종목 코드
        current_price: 현재가
        reason: 시그널 발생 사유
        timestamp: 시그널 발생 시간
    """
    signal_type: SignalType
    stock_code: str
    current_price: float
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class StockPosition:
    """
    종목별 포지션 정보
    
    Attributes:
        stock_code: 종목 코드
        stock_name: 종목명
        entry_price: 진입가 (사전 설정된 돌파 가격)
        stop_loss: 손절가
        take_profit: 익절가
        atr: ATR 값
        state: 현재 상태 (WAIT/ENTERED/EXITED)
        entered_price: 실제 진입가 (진입 시 기록)
        entered_time: 진입 시간
        quantity: 보유 수량 (LIVE 모드용)
    """
    stock_code: str
    stock_name: str
    entry_price: float      # 사전 설정된 진입 돌파가
    stop_loss: float        # 손절가
    take_profit: float      # 익절가
    atr: float              # ATR 값
    state: StockState = StockState.WAIT
    entered_price: float = 0.0
    entered_time: Optional[datetime] = None
    quantity: int = 0
    
    def __post_init__(self):
        """초기화 후 유효성 검증"""
        if self.entry_price <= 0:
            raise ValueError(f"entry_price는 0보다 커야 합니다: {self.entry_price}")
        if self.stop_loss <= 0:
            raise ValueError(f"stop_loss는 0보다 커야 합니다: {self.stop_loss}")
        if self.take_profit <= 0:
            raise ValueError(f"take_profit는 0보다 커야 합니다: {self.take_profit}")
        if self.stop_loss >= self.entry_price:
            logger.warning(
                f"[{self.stock_code}] 손절가({self.stop_loss:,})가 "
                f"진입가({self.entry_price:,}) 이상입니다."
            )
        if self.take_profit <= self.entry_price:
            logger.warning(
                f"[{self.stock_code}] 익절가({self.take_profit:,})가 "
                f"진입가({self.entry_price:,}) 이하입니다."
            )


# ════════════════════════════════════════════════════════════════
# ATR 전략 클래스
# ════════════════════════════════════════════════════════════════

class ATRStrategy:
    """
    ATR 기반 매매 전략 클래스
    
    장 시작 전에 설정된 entry_price, stop_loss, take_profit 값을 기반으로
    실시간 시세를 감시하여 진입/청산 시그널을 생성합니다.
    
    전략 규칙:
        1. WAIT 상태에서 현재가 >= entry_price → 진입 시그널
        2. ENTERED 상태에서 현재가 <= stop_loss → 손절 시그널
        3. ENTERED 상태에서 현재가 >= take_profit → 익절 시그널
        4. EXITED 상태에서는 더 이상 시그널 생성하지 않음
    
    Attributes:
        positions: 종목별 포지션 정보 딕셔너리
        entry_allowed: 신규 진입 허용 여부
    """
    
    def __init__(self):
        """전략 초기화"""
        self.positions: Dict[str, StockPosition] = {}
        self.entry_allowed: bool = True
        
        logger.info("[STRATEGY] ATR 전략 초기화 완료")
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 관리
    # ════════════════════════════════════════════════════════════════
    
    def add_position(self, position: StockPosition) -> None:
        """
        감시할 종목을 추가합니다.
        
        Args:
            position: 종목 포지션 정보
        """
        self.positions[position.stock_code] = position
        
        logger.info(
            f"[STRATEGY] 종목 추가: {position.stock_code} ({position.stock_name}) | "
            f"진입: {position.entry_price:,.0f} | "
            f"손절: {position.stop_loss:,.0f} | "
            f"익절: {position.take_profit:,.0f}"
        )
    
    def get_position(self, stock_code: str) -> Optional[StockPosition]:
        """
        종목의 포지션 정보를 반환합니다.
        
        Args:
            stock_code: 종목 코드
            
        Returns:
            StockPosition: 포지션 정보 (없으면 None)
        """
        return self.positions.get(stock_code)
    
    def remove_position(self, stock_code: str) -> None:
        """
        종목을 감시 목록에서 제거합니다.
        
        Args:
            stock_code: 종목 코드
        """
        if stock_code in self.positions:
            del self.positions[stock_code]
            logger.info(f"[STRATEGY] 종목 제거: {stock_code}")
    
    def get_all_positions(self) -> Dict[str, StockPosition]:
        """
        모든 포지션 정보를 반환합니다.
        
        Returns:
            Dict: 종목코드 -> 포지션 정보
        """
        return self.positions
    
    def get_subscribed_codes(self) -> list:
        """
        WebSocket 구독이 필요한 종목 코드 리스트를 반환합니다.
        EXITED 상태가 아닌 종목만 반환합니다.
        
        Returns:
            list: 종목 코드 리스트
        """
        return [
            code for code, pos in self.positions.items()
            if pos.state != StockState.EXITED
        ]
    
    # ════════════════════════════════════════════════════════════════
    # 진입 허용 제어
    # ════════════════════════════════════════════════════════════════
    
    def enable_entry(self) -> None:
        """신규 진입을 허용합니다."""
        self.entry_allowed = True
        logger.info("[STRATEGY] 신규 진입 허용됨")
    
    def disable_entry(self) -> None:
        """신규 진입을 금지합니다. (시간 외 등)"""
        self.entry_allowed = False
        logger.info("[STRATEGY] 신규 진입 금지됨")
    
    # ════════════════════════════════════════════════════════════════
    # 시그널 생성
    # ════════════════════════════════════════════════════════════════
    
    def check_signal(self, stock_code: str, current_price: float) -> Signal:
        """
        현재가를 기준으로 매매 시그널을 체크합니다.
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
            
        Returns:
            Signal: 매매 시그널
        """
        position = self.positions.get(stock_code)
        
        if position is None:
            return Signal(
                signal_type=SignalType.HOLD,
                stock_code=stock_code,
                current_price=current_price,
                reason="등록되지 않은 종목"
            )
        
        # 이미 청산된 종목은 시그널 생성하지 않음
        if position.state == StockState.EXITED:
            return Signal(
                signal_type=SignalType.HOLD,
                stock_code=stock_code,
                current_price=current_price,
                reason="이미 청산된 종목"
            )
        
        # WAIT 상태: 진입 조건 체크
        if position.state == StockState.WAIT:
            return self._check_entry_signal(position, current_price)
        
        # ENTERED 상태: 손절/익절 조건 체크
        if position.state == StockState.ENTERED:
            return self._check_exit_signal(position, current_price)
        
        # 그 외 (예상치 못한 상태)
        return Signal(
            signal_type=SignalType.HOLD,
            stock_code=stock_code,
            current_price=current_price,
            reason=f"알 수 없는 상태: {position.state}"
        )
    
    def _check_entry_signal(
        self,
        position: StockPosition,
        current_price: float
    ) -> Signal:
        """
        진입 조건을 체크합니다.
        
        진입 조건: 현재가 >= entry_price (돌파 매수)
        
        Args:
            position: 포지션 정보
            current_price: 현재가
            
        Returns:
            Signal: 진입 시그널 또는 HOLD
        """
        # 진입 금지 시간인 경우
        if not self.entry_allowed:
            return Signal(
                signal_type=SignalType.HOLD,
                stock_code=position.stock_code,
                current_price=current_price,
                reason="진입 금지 시간"
            )
        
        # 진입 조건 체크: 현재가 >= 진입가
        if current_price >= position.entry_price:
            logger.info(
                f"[STRATEGY] 진입 시그널! {position.stock_code} | "
                f"현재가: {current_price:,.0f} >= 진입가: {position.entry_price:,.0f}"
            )
            
            return Signal(
                signal_type=SignalType.ENTRY,
                stock_code=position.stock_code,
                current_price=current_price,
                reason=f"진입가 돌파 ({current_price:,.0f} >= {position.entry_price:,.0f})"
            )
        
        # 진입 조건 미충족
        return Signal(
            signal_type=SignalType.HOLD,
            stock_code=position.stock_code,
            current_price=current_price,
            reason=f"진입 대기 ({current_price:,.0f} < {position.entry_price:,.0f})"
        )
    
    def _check_exit_signal(
        self,
        position: StockPosition,
        current_price: float
    ) -> Signal:
        """
        청산(손절/익절) 조건을 체크합니다.
        
        손절 조건: 현재가 <= stop_loss
        익절 조건: 현재가 >= take_profit
        
        Args:
            position: 포지션 정보
            current_price: 현재가
            
        Returns:
            Signal: 손절/익절 시그널 또는 HOLD
        """
        # 손절 조건 체크: 현재가 <= 손절가
        if current_price <= position.stop_loss:
            pnl_pct = self._calculate_pnl_pct(position.entered_price, current_price)
            
            logger.info(
                f"[STRATEGY] 손절 시그널! {position.stock_code} | "
                f"현재가: {current_price:,.0f} <= 손절가: {position.stop_loss:,.0f} | "
                f"손익: {pnl_pct:.2f}%"
            )
            
            return Signal(
                signal_type=SignalType.STOP_LOSS,
                stock_code=position.stock_code,
                current_price=current_price,
                reason=f"손절가 도달 ({current_price:,.0f} <= {position.stop_loss:,.0f})"
            )
        
        # 익절 조건 체크: 현재가 >= 익절가
        if current_price >= position.take_profit:
            pnl_pct = self._calculate_pnl_pct(position.entered_price, current_price)
            
            logger.info(
                f"[STRATEGY] 익절 시그널! {position.stock_code} | "
                f"현재가: {current_price:,.0f} >= 익절가: {position.take_profit:,.0f} | "
                f"손익: +{pnl_pct:.2f}%"
            )
            
            return Signal(
                signal_type=SignalType.TAKE_PROFIT,
                stock_code=position.stock_code,
                current_price=current_price,
                reason=f"익절가 도달 ({current_price:,.0f} >= {position.take_profit:,.0f})"
            )
        
        # 청산 조건 미충족
        return Signal(
            signal_type=SignalType.HOLD,
            stock_code=position.stock_code,
            current_price=current_price,
            reason=f"보유 중 (손절: {position.stop_loss:,.0f} ~ 익절: {position.take_profit:,.0f})"
        )
    
    @staticmethod
    def _calculate_pnl_pct(entry_price: float, current_price: float) -> float:
        """
        손익률을 계산합니다.
        
        Args:
            entry_price: 진입가
            current_price: 현재가
            
        Returns:
            float: 손익률 (%)
        """
        if entry_price <= 0:
            return 0.0
        return ((current_price - entry_price) / entry_price) * 100
    
    # ════════════════════════════════════════════════════════════════
    # 상태 업데이트
    # ════════════════════════════════════════════════════════════════
    
    def update_state_to_entered(
        self,
        stock_code: str,
        entered_price: float,
        quantity: int = 0
    ) -> bool:
        """
        종목 상태를 ENTERED로 변경합니다.
        
        Args:
            stock_code: 종목 코드
            entered_price: 실제 진입가
            quantity: 보유 수량
            
        Returns:
            bool: 업데이트 성공 여부
        """
        position = self.positions.get(stock_code)
        
        if position is None:
            logger.warning(f"[STRATEGY] 상태 업데이트 실패: 등록되지 않은 종목 {stock_code}")
            return False
        
        if position.state != StockState.WAIT:
            logger.warning(
                f"[STRATEGY] 상태 업데이트 실패: {stock_code}는 "
                f"WAIT 상태가 아님 (현재: {position.state.value})"
            )
            return False
        
        position.state = StockState.ENTERED
        position.entered_price = entered_price
        position.entered_time = datetime.now()
        position.quantity = quantity
        
        logger.info(
            f"[STRATEGY] 상태 변경: {stock_code} WAIT → ENTERED | "
            f"진입가: {entered_price:,.0f} | 수량: {quantity}"
        )
        
        return True
    
    def update_state_to_exited(self, stock_code: str) -> bool:
        """
        종목 상태를 EXITED로 변경합니다.
        
        Args:
            stock_code: 종목 코드
            
        Returns:
            bool: 업데이트 성공 여부
        """
        position = self.positions.get(stock_code)
        
        if position is None:
            logger.warning(f"[STRATEGY] 상태 업데이트 실패: 등록되지 않은 종목 {stock_code}")
            return False
        
        if position.state != StockState.ENTERED:
            logger.warning(
                f"[STRATEGY] 상태 업데이트 실패: {stock_code}는 "
                f"ENTERED 상태가 아님 (현재: {position.state.value})"
            )
            return False
        
        old_state = position.state
        position.state = StockState.EXITED
        
        logger.info(
            f"[STRATEGY] 상태 변경: {stock_code} {old_state.value} → EXITED"
        )
        
        return True
    
    # ════════════════════════════════════════════════════════════════
    # 통계
    # ════════════════════════════════════════════════════════════════
    
    def get_statistics(self) -> dict:
        """
        현재 상태별 통계를 반환합니다.
        
        Returns:
            dict: 상태별 종목 수
        """
        stats = {
            "total": len(self.positions),
            "wait": 0,
            "entered": 0,
            "exited": 0
        }
        
        for position in self.positions.values():
            if position.state == StockState.WAIT:
                stats["wait"] += 1
            elif position.state == StockState.ENTERED:
                stats["entered"] += 1
            elif position.state == StockState.EXITED:
                stats["exited"] += 1
        
        return stats
    
    def print_status(self) -> None:
        """현재 모든 종목의 상태를 출력합니다."""
        print("\n" + "=" * 80)
        print("종목별 상태")
        print("=" * 80)
        print(f"{'코드':<10} {'종목명':<15} {'상태':<10} {'진입가':<12} {'손절가':<12} {'익절가':<12}")
        print("-" * 80)
        
        for code, pos in self.positions.items():
            print(
                f"{code:<10} {pos.stock_name:<15} {pos.state.value:<10} "
                f"{pos.entry_price:>10,.0f} {pos.stop_loss:>10,.0f} {pos.take_profit:>10,.0f}"
            )
        
        print("=" * 80)
        stats = self.get_statistics()
        print(f"총 {stats['total']}개 | 대기: {stats['wait']} | 보유: {stats['entered']} | 청산: {stats['exited']}")
        print("=" * 80 + "\n")


# ════════════════════════════════════════════════════════════════
# 유틸리티 함수
# ════════════════════════════════════════════════════════════════

def load_universe_from_json(file_path: str) -> list:
    """
    trade_universe.json 파일에서 종목 정보를 로드합니다.
    
    파일 형식 예시:
    [
        {
            "stock_code": "005930",
            "stock_name": "삼성전자",
            "entry_price": 71000,
            "stop_loss": 69500,
            "take_profit": 73500,
            "atr": 750,
            "quantity": 10
        },
        ...
    ]
    
    Args:
        file_path: JSON 파일 경로
        
    Returns:
        list: StockPosition 객체 리스트
    """
    import json
    from pathlib import Path
    
    path = Path(file_path)
    
    if not path.exists():
        logger.error(f"[STRATEGY] 종목 파일을 찾을 수 없음: {file_path}")
        return []
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        positions = []
        
        for item in data:
            try:
                position = StockPosition(
                    stock_code=str(item["stock_code"]),
                    stock_name=item.get("stock_name", ""),
                    entry_price=float(item["entry_price"]),
                    stop_loss=float(item["stop_loss"]),
                    take_profit=float(item["take_profit"]),
                    atr=float(item.get("atr", 0)),
                    quantity=int(item.get("quantity", 0))
                )
                positions.append(position)
                
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"[STRATEGY] 종목 데이터 파싱 오류: {item} - {e}")
                continue
        
        logger.info(f"[STRATEGY] {len(positions)}개 종목 로드 완료")
        return positions
        
    except json.JSONDecodeError as e:
        logger.error(f"[STRATEGY] JSON 파싱 오류: {e}")
        return []
    except Exception as e:
        logger.error(f"[STRATEGY] 파일 읽기 오류: {e}")
        return []


# ════════════════════════════════════════════════════════════════
# 직접 실행 시 테스트
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    
    # 전략 생성
    strategy = ATRStrategy()
    
    # 테스트 종목 추가
    test_position = StockPosition(
        stock_code="005930",
        stock_name="삼성전자",
        entry_price=71000,
        stop_loss=69500,
        take_profit=73500,
        atr=750
    )
    strategy.add_position(test_position)
    
    # 상태 출력
    strategy.print_status()
    
    # 시그널 테스트
    print("\n=== 시그널 테스트 ===")
    
    # 진입 전 (WAIT 상태)
    print("\n1. 진입 전 - 가격 미달:")
    signal = strategy.check_signal("005930", 70000)
    print(f"   시그널: {signal.signal_type.value}, 사유: {signal.reason}")
    
    # 진입 시그널
    print("\n2. 진입 시그널:")
    signal = strategy.check_signal("005930", 71500)
    print(f"   시그널: {signal.signal_type.value}, 사유: {signal.reason}")
    
    # 상태 변경: ENTERED
    strategy.update_state_to_entered("005930", 71500)
    
    # 보유 중 (ENTERED 상태)
    print("\n3. 보유 중:")
    signal = strategy.check_signal("005930", 72000)
    print(f"   시그널: {signal.signal_type.value}, 사유: {signal.reason}")
    
    # 손절 시그널
    print("\n4. 손절 시그널:")
    signal = strategy.check_signal("005930", 69000)
    print(f"   시그널: {signal.signal_type.value}, 사유: {signal.reason}")
    
    # 상태 복원 후 익절 테스트
    strategy.positions["005930"].state = StockState.ENTERED
    print("\n5. 익절 시그널:")
    signal = strategy.check_signal("005930", 74000)
    print(f"   시그널: {signal.signal_type.value}, 사유: {signal.reason}")
