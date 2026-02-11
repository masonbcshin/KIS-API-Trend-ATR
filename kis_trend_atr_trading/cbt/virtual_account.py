"""
KIS Trend-ATR Trading System - CBT 가상 계좌 관리

이 모듈은 CBT 모드에서 가상 자본금을 관리합니다.

주요 기능:
    - 초기 자본금 설정
    - Realized / Unrealized PnL 분리 관리
    - Equity Curve 저장
    - 가용 현금 관리

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
import threading

from config import settings
from utils.logger import get_logger
from utils.market_hours import KST

logger = get_logger("cbt_account")


@dataclass
class Position:
    """CBT 가상 포지션"""
    stock_code: str
    entry_price: float
    quantity: int
    entry_date: str
    stop_loss: float
    take_profit: float
    atr_at_entry: float
    highest_price: float = 0.0  # 트레일링용
    trailing_stop: float = 0.0


@dataclass
class EquitySnapshot:
    """자산 스냅샷"""
    timestamp: str
    cash: float
    position_value: float
    total_equity: float
    unrealized_pnl: float


@dataclass
class AccountState:
    """계좌 상태 데이터"""
    initial_capital: float
    cash: float
    realized_pnl: float
    unrealized_pnl: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    position: Optional[Dict] = None
    equity_curve: List[Dict] = field(default_factory=list)
    last_updated: str = ""
    
    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "AccountState":
        """딕셔너리에서 생성"""
        return cls(**data)


class VirtualAccount:
    """
    CBT 가상 계좌 클래스
    
    실계좌와 동일한 로직으로 자본금을 관리하되,
    실제 주문은 발생하지 않습니다.
    
    Attributes:
        initial_capital: 초기 자본금
        cash: 가용 현금
        realized_pnl: 실현 손익 (청산된 거래)
        unrealized_pnl: 미실현 손익 (보유 중인 포지션)
        position: 현재 포지션
    
    Usage:
        account = VirtualAccount(initial_capital=10_000_000)
        
        # 매수 체결
        account.execute_buy(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        # 현재가 업데이트 (미실현 손익 계산)
        account.update_position_price(71000)
        
        # 매도 체결
        result = account.execute_sell(price=72000, reason="TAKE_PROFIT")
    """
    
    def __init__(
        self,
        initial_capital: float = None,
        data_dir: Path = None,
        load_existing: bool = True
    ):
        """
        가상 계좌 초기화
        
        Args:
            initial_capital: 초기 자본금 (미입력 시 설정값 사용)
            data_dir: 데이터 저장 디렉토리
            load_existing: 기존 상태 로드 여부
        """
        self.initial_capital = initial_capital or settings.CBT_INITIAL_CAPITAL
        self.data_dir = data_dir or settings.CBT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._state_file = self.data_dir / "account_state.json"
        self._lock = threading.Lock()
        
        # 계좌 상태
        self.cash: float = self.initial_capital
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.position: Optional[Position] = None
        self.equity_curve: List[EquitySnapshot] = []
        
        # 기존 상태 로드
        if load_existing and self._state_file.exists():
            self._load_state()
        
        logger.info(
            f"[CBT] 가상 계좌 초기화: "
            f"초기자본={self.initial_capital:,}원, "
            f"현재현금={self.cash:,}원"
        )
    
    # ════════════════════════════════════════════════════════════════
    # 거래 실행
    # ════════════════════════════════════════════════════════════════
    
    def execute_buy(
        self,
        stock_code: str,
        price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float,
        atr: float,
        entry_date: str = None
    ) -> Dict:
        """
        가상 매수 체결
        
        Args:
            stock_code: 종목 코드
            price: 체결가
            quantity: 수량
            stop_loss: 손절가
            take_profit: 익절가
            atr: 진입 시 ATR
            entry_date: 진입일 (미입력 시 현재 시간)
        
        Returns:
            Dict: 체결 결과
        """
        with self._lock:
            # 이미 포지션 보유 중인 경우
            if self.position is not None:
                logger.warning("[CBT] 매수 실패: 이미 포지션 보유 중")
                return {
                    "success": False,
                    "message": "포지션 보유 중",
                    "order_no": ""
                }
            
            # 수수료 계산
            total_cost = price * quantity
            commission = total_cost * settings.CBT_COMMISSION_RATE
            required_cash = total_cost + commission
            
            # 현금 부족 체크
            if self.cash < required_cash:
                logger.warning(
                    f"[CBT] 매수 실패: 현금 부족 "
                    f"(필요: {required_cash:,.0f}, 보유: {self.cash:,.0f})"
                )
                return {
                    "success": False,
                    "message": "현금 부족",
                    "order_no": ""
                }
            
            # 현금 차감
            self.cash -= required_cash
            
            # 포지션 생성
            entry_date = entry_date or datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            self.position = Position(
                stock_code=stock_code,
                entry_price=price,
                quantity=quantity,
                entry_date=entry_date,
                stop_loss=stop_loss,
                take_profit=take_profit,
                atr_at_entry=atr,
                highest_price=price,
                trailing_stop=stop_loss
            )
            
            # 가상 주문번호 생성
            order_no = f"CBT{datetime.now(KST).strftime('%Y%m%d%H%M%S')}"
            
            logger.info(
                f"[CBT] 가상 매수 체결: {stock_code} @ {price:,.0f}원 x {quantity}주, "
                f"수수료: {commission:,.0f}원"
            )
            
            self._save_state()
            self._record_equity_snapshot(price)
            
            return {
                "success": True,
                "message": "가상 매수 체결",
                "order_no": order_no,
                "commission": commission
            }
    
    def execute_sell(
        self,
        price: float,
        reason: str = ""
    ) -> Dict:
        """
        가상 매도 체결
        
        Args:
            price: 체결가
            reason: 청산 사유 (ATR_STOP, TREND_BROKEN, TAKE_PROFIT 등)
        
        Returns:
            Dict: 체결 결과 (손익 정보 포함)
        """
        with self._lock:
            if self.position is None:
                logger.warning("[CBT] 매도 실패: 보유 포지션 없음")
                return {
                    "success": False,
                    "message": "포지션 없음",
                    "order_no": ""
                }
            
            pos = self.position
            
            # 수수료 계산
            total_proceeds = price * pos.quantity
            commission = total_proceeds * settings.CBT_COMMISSION_RATE
            net_proceeds = total_proceeds - commission
            
            # 손익 계산
            entry_cost = pos.entry_price * pos.quantity
            gross_pnl = total_proceeds - entry_cost
            
            # 매수/매도 수수료 합산
            total_commission = entry_cost * settings.CBT_COMMISSION_RATE + commission
            net_pnl = gross_pnl - total_commission
            return_pct = (net_pnl / entry_cost) * 100
            
            # 보유일수 계산
            entry_dt = datetime.strptime(
                pos.entry_date.split()[0], 
                "%Y-%m-%d"
            )
            exit_dt = datetime.now(KST)
            holding_days = (exit_dt.date() - entry_dt.date()).days + 1
            
            # 현금 복구
            self.cash += net_proceeds
            
            # 실현 손익 누적
            self.realized_pnl += net_pnl
            self.unrealized_pnl = 0.0
            
            # 거래 카운트
            self.total_trades += 1
            if net_pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1
            
            # 가상 주문번호
            order_no = f"CBT{datetime.now(KST).strftime('%Y%m%d%H%M%S')}"
            
            result = {
                "success": True,
                "message": "가상 매도 체결",
                "order_no": order_no,
                "stock_code": pos.stock_code,
                "entry_price": pos.entry_price,
                "exit_price": price,
                "quantity": pos.quantity,
                "gross_pnl": gross_pnl,
                "commission": total_commission,
                "net_pnl": net_pnl,
                "return_pct": return_pct,
                "holding_days": holding_days,
                "exit_reason": reason,
                "entry_date": pos.entry_date,
                "exit_date": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            }
            
            logger.info(
                f"[CBT] 가상 매도 체결: {pos.stock_code} @ {price:,.0f}원 x {pos.quantity}주, "
                f"순손익: {net_pnl:,.0f}원 ({return_pct:+.2f}%), "
                f"사유: {reason}"
            )
            
            # 포지션 청산
            self.position = None
            
            self._save_state()
            self._record_equity_snapshot(price)
            
            return result
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 관리
    # ════════════════════════════════════════════════════════════════
    
    def update_position_price(self, current_price: float) -> None:
        """
        현재가로 포지션 미실현 손익을 업데이트합니다.
        
        Args:
            current_price: 현재가
        """
        with self._lock:
            if self.position is None:
                self.unrealized_pnl = 0.0
                return
            
            pos = self.position
            position_value = current_price * pos.quantity
            entry_cost = pos.entry_price * pos.quantity
            
            # 수수료 추정 (매도 시 발생)
            sell_commission = position_value * settings.CBT_COMMISSION_RATE
            
            self.unrealized_pnl = position_value - entry_cost - sell_commission
            
            # 최고가 갱신 (트레일링용)
            if current_price > pos.highest_price:
                pos.highest_price = current_price
                # 트레일링 스탑 갱신
                if settings.ENABLE_TRAILING_STOP:
                    new_trailing = current_price - (pos.atr_at_entry * settings.TRAILING_STOP_ATR_MULTIPLIER)
                    if new_trailing > pos.trailing_stop:
                        pos.trailing_stop = new_trailing
    
    def has_position(self) -> bool:
        """포지션 보유 여부"""
        return self.position is not None
    
    def get_position_info(self) -> Optional[Dict]:
        """현재 포지션 정보 반환"""
        if self.position is None:
            return None
        
        return {
            "stock_code": self.position.stock_code,
            "entry_price": self.position.entry_price,
            "quantity": self.position.quantity,
            "entry_date": self.position.entry_date,
            "stop_loss": self.position.stop_loss,
            "take_profit": self.position.take_profit,
            "atr_at_entry": self.position.atr_at_entry,
            "highest_price": self.position.highest_price,
            "trailing_stop": self.position.trailing_stop
        }
    
    # ════════════════════════════════════════════════════════════════
    # 계좌 조회
    # ════════════════════════════════════════════════════════════════
    
    def get_total_equity(self, current_price: float = None) -> float:
        """
        총 자산 (현금 + 포지션 평가금액)
        
        Args:
            current_price: 현재가 (포지션 보유 시 필요)
        
        Returns:
            float: 총 자산
        """
        # 락 없이 직접 접근 (호출자가 락 관리)
        if self.position is None:
            return self.cash
        
        if current_price is None:
            # 현재가 미제공 시 진입가 기준
            current_price = self.position.entry_price
        
        position_value = current_price * self.position.quantity
        return self.cash + position_value
    
    def get_account_summary(self, current_price: float = None) -> Dict:
        """
        계좌 요약 정보
        
        Args:
            current_price: 현재가 (포지션 보유 시 필요)
        
        Returns:
            Dict: 계좌 요약
        """
        with self._lock:
            total_equity = self.get_total_equity(current_price)
            total_pnl = total_equity - self.initial_capital
            total_return_pct = (total_pnl / self.initial_capital) * 100
            
            return {
                "initial_capital": self.initial_capital,
                "cash": self.cash,
                "total_equity": total_equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized_pnl,
                "total_pnl": total_pnl,
                "total_return_pct": total_return_pct,
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "win_rate": (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0,
                "has_position": self.position is not None
            }
    
    # ════════════════════════════════════════════════════════════════
    # Equity Curve
    # ════════════════════════════════════════════════════════════════
    
    def _record_equity_snapshot(self, current_price: float = None) -> None:
        """자산 스냅샷 기록"""
        position_value = 0.0
        if self.position and current_price:
            position_value = current_price * self.position.quantity
        
        total_equity = self.cash + position_value
        
        snapshot = EquitySnapshot(
            timestamp=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            cash=self.cash,
            position_value=position_value,
            total_equity=total_equity,
            unrealized_pnl=self.unrealized_pnl
        )
        
        self.equity_curve.append(snapshot)
    
    def get_equity_curve(self) -> List[Dict]:
        """Equity Curve 데이터 반환"""
        return [asdict(s) for s in self.equity_curve]
    
    # ════════════════════════════════════════════════════════════════
    # 상태 저장/로드
    # ════════════════════════════════════════════════════════════════
    
    def _save_state(self) -> None:
        """계좌 상태 저장"""
        state = AccountState(
            initial_capital=self.initial_capital,
            cash=self.cash,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            total_trades=self.total_trades,
            winning_trades=self.winning_trades,
            losing_trades=self.losing_trades,
            position=asdict(self.position) if self.position else None,
            equity_curve=[asdict(s) for s in self.equity_curve[-1000:]],  # 최근 1000개만
            last_updated=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        )
        
        with open(self._state_file, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        
        logger.debug(f"[CBT] 계좌 상태 저장 완료")
    
    def _load_state(self) -> None:
        """계좌 상태 로드"""
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            self.initial_capital = data.get("initial_capital", self.initial_capital)
            self.cash = data.get("cash", self.initial_capital)
            self.realized_pnl = data.get("realized_pnl", 0.0)
            self.unrealized_pnl = data.get("unrealized_pnl", 0.0)
            self.total_trades = data.get("total_trades", 0)
            self.winning_trades = data.get("winning_trades", 0)
            self.losing_trades = data.get("losing_trades", 0)
            
            # 포지션 복원
            pos_data = data.get("position")
            if pos_data:
                self.position = Position(**pos_data)
            
            # Equity Curve 복원
            self.equity_curve = [
                EquitySnapshot(**s) for s in data.get("equity_curve", [])
            ]
            
            logger.info(
                f"[CBT] 계좌 상태 로드 완료: "
                f"현금={self.cash:,.0f}원, "
                f"실현손익={self.realized_pnl:,.0f}원, "
                f"거래수={self.total_trades}"
            )
            
        except Exception as e:
            logger.warning(f"[CBT] 계좌 상태 로드 실패: {e}")
    
    def reset(self) -> None:
        """계좌 초기화"""
        with self._lock:
            self.cash = self.initial_capital
            self.realized_pnl = 0.0
            self.unrealized_pnl = 0.0
            self.total_trades = 0
            self.winning_trades = 0
            self.losing_trades = 0
            self.position = None
            self.equity_curve = []
            
            # 저장 파일 삭제
            if self._state_file.exists():
                self._state_file.unlink()
            
            logger.info(f"[CBT] 계좌 초기화 완료: 자본금={self.initial_capital:,}원")
