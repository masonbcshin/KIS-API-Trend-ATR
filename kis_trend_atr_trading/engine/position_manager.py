"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 통합 포지션 관리자
═══════════════════════════════════════════════════════════════════════════════

멀티데이 포지션을 통합 관리합니다.

★ 핵심 기능:
    1. 포지션 상태 영속화 (JSON)
    2. 프로그램 재시작 시 포지션 복구
    3. API를 통한 실제 보유 확인 및 정합성 검증
    4. 포지션별 손절/익절/추세이탈 자동 청산 판단
    5. 트레일링 스탑 관리

★ 안전장치:
    - 포지션 불일치 시 경고 및 자동 동기화
    - 최대 포지션 수 제한
    - 익일 갭 손실 보호
"""

import json
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum
import threading

from utils.logger import get_logger
from utils.telegram_notifier import get_telegram_notifier

logger = get_logger("position_manager")


# ═══════════════════════════════════════════════════════════════════════════════
# 열거형 및 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class PositionState(Enum):
    """포지션 상태"""
    PENDING = "PENDING"           # 진입 대기
    ENTERED = "ENTERED"           # 진입 완료
    PARTIAL_EXIT = "PARTIAL_EXIT" # 부분 청산
    EXITED = "EXITED"             # 청산 완료


class ExitReason(Enum):
    """청산 사유"""
    ATR_STOP = "ATR_STOP"             # ATR 기반 손절
    TAKE_PROFIT = "TAKE_PROFIT"       # 익절 도달
    TRAILING_STOP = "TRAILING_STOP"   # 트레일링 스탑
    TREND_BROKEN = "TREND_BROKEN"     # 추세 이탈
    GAP_PROTECTION = "GAP_PROTECTION" # 갭 보호
    MANUAL = "MANUAL"                 # 수동 청산
    KILL_SWITCH = "KILL_SWITCH"       # 긴급 청산
    OTHER = "OTHER"


@dataclass
class ManagedPosition:
    """
    관리 포지션 데이터 클래스
    
    ★ 필수 저장 필드:
        - atr_at_entry: 진입 시 ATR (고정, 재계산 금지)
        - stop_loss: 손절가 (진입 시 설정)
        - trailing_stop: 현재 트레일링 스탑 가격
        - highest_price: 보유 중 최고가
    """
    # 기본 정보
    position_id: str
    stock_code: str
    stock_name: str = ""
    
    # 포지션 상태
    state: PositionState = PositionState.ENTERED
    side: str = "LONG"  # LONG only (현재)
    
    # 진입 정보
    entry_price: float = 0.0
    quantity: int = 0
    entry_date: str = ""
    entry_time: str = ""
    entry_order_no: str = ""
    
    # Exit 관리 (★ 핵심)
    atr_at_entry: float = 0.0      # 진입 시 ATR (고정!)
    stop_loss: float = 0.0         # 손절가
    take_profit: float = 0.0       # 익절가
    trailing_stop: float = 0.0     # 트레일링 스탑
    highest_price: float = 0.0     # 최고가 (트레일링용)
    
    # 현재 상태
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    
    # 청산 정보 (청산 완료 시)
    exit_price: float = 0.0
    exit_date: str = ""
    exit_time: str = ""
    exit_reason: ExitReason = ExitReason.OTHER
    exit_order_no: str = ""
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    commission: float = 0.0
    
    # 메타 정보
    created_at: str = ""
    updated_at: str = ""
    holding_days: int = 0
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if not self.entry_date:
            self.entry_date = datetime.now().strftime("%Y-%m-%d")
        if not self.entry_time:
            self.entry_time = datetime.now().strftime("%H:%M:%S")
        
        if self.highest_price == 0.0 and self.entry_price > 0:
            self.highest_price = self.entry_price
        if self.trailing_stop == 0.0 and self.stop_loss > 0:
            self.trailing_stop = self.stop_loss
    
    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        d = asdict(self)
        d["state"] = self.state.value
        d["exit_reason"] = self.exit_reason.value
        return d
    
    @classmethod
    def from_dict(cls, data: Dict) -> "ManagedPosition":
        """딕셔너리에서 생성"""
        data["state"] = PositionState(data.get("state", "ENTERED"))
        data["exit_reason"] = ExitReason(data.get("exit_reason", "OTHER"))
        return cls(**data)
    
    def update_unrealized(self, current_price: float) -> None:
        """미실현 손익 업데이트"""
        self.current_price = current_price
        
        if self.entry_price > 0 and self.quantity > 0:
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
            self.unrealized_pnl_pct = (
                (current_price - self.entry_price) / self.entry_price * 100
            )
        
        # 최고가 갱신
        if current_price > self.highest_price:
            self.highest_price = current_price
        
        # 보유일수 계산
        try:
            entry_dt = datetime.strptime(self.entry_date, "%Y-%m-%d")
            self.holding_days = (datetime.now() - entry_dt).days + 1
        except:
            pass
        
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════════
# 포지션 매니저 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class PositionManager:
    """
    통합 포지션 관리자
    
    모든 포지션의 생명주기를 관리합니다.
    
    Usage:
        manager = PositionManager()
        
        # 포지션 오픈
        position = manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=74000,
            atr=1500
        )
        
        # Exit 조건 체크
        exit_signal = manager.check_exit_conditions("005930", current_price=67000)
        if exit_signal:
            manager.close_position("005930", exit_price=67000, reason=exit_signal)
        
        # 포지션 복구 (프로그램 재시작 시)
        manager.restore_from_api(api_client)
    """
    
    def __init__(
        self,
        data_dir: Path = None,
        max_positions: int = 10,
        enable_trailing: bool = True,
        trailing_atr_multiplier: float = 2.0,
        trailing_activation_pct: float = 1.0,
        enable_gap_protection: bool = True,
        max_gap_loss_pct: float = 3.0
    ):
        """
        포지션 매니저 초기화
        
        Args:
            data_dir: 데이터 저장 경로
            max_positions: 최대 동시 포지션 수
            enable_trailing: 트레일링 스탑 활성화
            trailing_atr_multiplier: 트레일링 ATR 배수
            trailing_activation_pct: 트레일링 활성화 수익률 (%)
            enable_gap_protection: 갭 보호 활성화
            max_gap_loss_pct: 최대 갭 손실 허용 (%)
        """
        self.data_dir = data_dir or Path(__file__).parent.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._positions_file = self.data_dir / "managed_positions.json"
        self._lock = threading.Lock()
        
        # 설정
        self.max_positions = max_positions
        self.enable_trailing = enable_trailing
        self.trailing_atr_multiplier = trailing_atr_multiplier
        self.trailing_activation_pct = trailing_activation_pct
        self.enable_gap_protection = enable_gap_protection
        self.max_gap_loss_pct = max_gap_loss_pct
        
        # 포지션 저장소 (stock_code -> ManagedPosition)
        self._positions: Dict[str, ManagedPosition] = {}
        
        # 청산 완료 기록 (stock_code -> List[ManagedPosition])
        self._closed_positions: Dict[str, List[ManagedPosition]] = {}
        
        # 텔레그램
        self._telegram = get_telegram_notifier()
        
        # 기존 데이터 로드
        self._load_positions()
        
        logger.info(
            f"[POSITION] 매니저 초기화: "
            f"최대포지션={max_positions}, "
            f"트레일링={'ON' if enable_trailing else 'OFF'}, "
            f"현재포지션={len(self._positions)}개"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 포지션 생명주기 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def open_position(
        self,
        stock_code: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float,
        atr: float,
        stock_name: str = "",
        order_no: str = ""
    ) -> Optional[ManagedPosition]:
        """
        새 포지션을 오픈합니다.
        
        Args:
            stock_code: 종목 코드
            entry_price: 진입가
            quantity: 수량
            stop_loss: 손절가
            take_profit: 익절가
            atr: 진입 시 ATR
            stock_name: 종목명
            order_no: 주문번호
            
        Returns:
            Optional[ManagedPosition]: 생성된 포지션 (실패 시 None)
        """
        with self._lock:
            # 이미 해당 종목 포지션 보유 중인지 확인
            if stock_code in self._positions:
                logger.warning(
                    f"[POSITION] 이미 포지션 보유 중: {stock_code}"
                )
                return None
            
            # 최대 포지션 수 체크
            if len(self._positions) >= self.max_positions:
                logger.warning(
                    f"[POSITION] 최대 포지션 수 도달: {self.max_positions}"
                )
                return None
            
            # 포지션 생성
            position_id = f"P{datetime.now().strftime('%Y%m%d%H%M%S')}_{stock_code}"
            
            position = ManagedPosition(
                position_id=position_id,
                stock_code=stock_code,
                stock_name=stock_name,
                state=PositionState.ENTERED,
                entry_price=entry_price,
                quantity=quantity,
                atr_at_entry=atr,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop=stop_loss,  # 초기에는 손절가와 동일
                highest_price=entry_price,
                entry_order_no=order_no
            )
            
            self._positions[stock_code] = position
            self._save_positions()
            
            logger.info(
                f"[POSITION] 포지션 오픈: {stock_code} @ {entry_price:,.0f}원 x {quantity}주, "
                f"손절={stop_loss:,.0f}, 익절={take_profit:,.0f}"
            )
            
            return position
    
    def close_position(
        self,
        stock_code: str,
        exit_price: float,
        reason: ExitReason,
        order_no: str = "",
        commission: float = 0.0
    ) -> Optional[ManagedPosition]:
        """
        포지션을 청산합니다.
        
        Args:
            stock_code: 종목 코드
            exit_price: 청산가
            reason: 청산 사유
            order_no: 주문번호
            commission: 수수료
            
        Returns:
            Optional[ManagedPosition]: 청산된 포지션
        """
        with self._lock:
            if stock_code not in self._positions:
                logger.warning(f"[POSITION] 포지션 없음: {stock_code}")
                return None
            
            position = self._positions[stock_code]
            
            # 청산 정보 업데이트
            position.state = PositionState.EXITED
            position.exit_price = exit_price
            position.exit_date = datetime.now().strftime("%Y-%m-%d")
            position.exit_time = datetime.now().strftime("%H:%M:%S")
            position.exit_reason = reason
            position.exit_order_no = order_no
            position.commission = commission
            
            # 실현 손익 계산
            gross_pnl = (exit_price - position.entry_price) * position.quantity
            position.realized_pnl = gross_pnl - commission
            position.realized_pnl_pct = (
                (exit_price - position.entry_price) / position.entry_price * 100
            )
            
            # 보유일수
            try:
                entry_dt = datetime.strptime(position.entry_date, "%Y-%m-%d")
                position.holding_days = (datetime.now() - entry_dt).days + 1
            except:
                pass
            
            # 청산 기록으로 이동
            if stock_code not in self._closed_positions:
                self._closed_positions[stock_code] = []
            self._closed_positions[stock_code].append(position)
            
            # 현재 포지션에서 제거
            del self._positions[stock_code]
            
            self._save_positions()
            
            logger.info(
                f"[POSITION] 포지션 청산: {stock_code} @ {exit_price:,.0f}원, "
                f"손익={position.realized_pnl:+,.0f}원 ({position.realized_pnl_pct:+.2f}%), "
                f"사유={reason.value}"
            )
            
            return position
    
    def update_position(
        self,
        stock_code: str,
        current_price: float
    ) -> Optional[ManagedPosition]:
        """
        포지션 상태를 업데이트합니다.
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
            
        Returns:
            Optional[ManagedPosition]: 업데이트된 포지션
        """
        with self._lock:
            if stock_code not in self._positions:
                return None
            
            position = self._positions[stock_code]
            position.update_unrealized(current_price)
            
            # 트레일링 스탑 갱신
            if self.enable_trailing:
                self._update_trailing_stop(position, current_price)
            
            self._save_positions()
            return position
    
    def _update_trailing_stop(
        self,
        position: ManagedPosition,
        current_price: float
    ) -> None:
        """트레일링 스탑을 업데이트합니다."""
        # 수익률이 활성화 기준 이상인지 확인
        profit_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        
        if profit_pct < self.trailing_activation_pct:
            return
        
        # 새 트레일링 스탑 계산
        new_trailing = current_price - (position.atr_at_entry * self.trailing_atr_multiplier)
        
        # 현재 트레일링보다 높을 때만 갱신 (손절선만 올림)
        if new_trailing > position.trailing_stop:
            old_trailing = position.trailing_stop
            position.trailing_stop = new_trailing
            
            logger.info(
                f"[POSITION] 트레일링 갱신: {position.stock_code} "
                f"{old_trailing:,.0f} → {new_trailing:,.0f}"
            )
            
            # 텔레그램 알림
            self._telegram.notify_trailing_stop_updated(
                stock_code=position.stock_code,
                highest_price=position.highest_price,
                trailing_stop=new_trailing,
                entry_price=position.entry_price,
                pnl=position.unrealized_pnl,
                pnl_pct=position.unrealized_pnl_pct
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Exit 조건 체크
    # ═══════════════════════════════════════════════════════════════════════════
    
    def check_exit_conditions(
        self,
        stock_code: str,
        current_price: float,
        current_trend_bullish: bool = True,
        is_market_open: bool = True
    ) -> Optional[ExitReason]:
        """
        Exit 조건을 체크합니다.
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
            current_trend_bullish: 현재 상승 추세 여부
            is_market_open: 시장 개장 여부 (장시작 시 갭 체크용)
            
        Returns:
            Optional[ExitReason]: 청산 사유 (None이면 보유 유지)
        """
        with self._lock:
            if stock_code not in self._positions:
                return None
            
            position = self._positions[stock_code]
            
            # 1. 손절 체크 (ATR Stop)
            if current_price <= position.stop_loss:
                return ExitReason.ATR_STOP
            
            # 2. 트레일링 스탑 체크
            if self.enable_trailing and current_price <= position.trailing_stop:
                return ExitReason.TRAILING_STOP
            
            # 3. 익절 체크
            if position.take_profit > 0 and current_price >= position.take_profit:
                return ExitReason.TAKE_PROFIT
            
            # 4. 추세 이탈 체크
            if not current_trend_bullish:
                return ExitReason.TREND_BROKEN
            
            # 5. 갭 보호 체크 (장 시작 시)
            if self.enable_gap_protection and is_market_open:
                gap_loss_pct = (
                    (position.stop_loss - current_price) / position.entry_price * 100
                )
                if current_price < position.stop_loss and gap_loss_pct > self.max_gap_loss_pct:
                    return ExitReason.GAP_PROTECTION
            
            return None
    
    def get_exit_reason_for_telegram(self, reason: ExitReason) -> str:
        """Exit 사유를 텔레그램용 문자열로 변환합니다."""
        reason_map = {
            ExitReason.ATR_STOP: "🛑 ATR 손절",
            ExitReason.TAKE_PROFIT: "🎯 익절 도달",
            ExitReason.TRAILING_STOP: "📈 트레일링 스탑",
            ExitReason.TREND_BROKEN: "📉 추세 이탈",
            ExitReason.GAP_PROTECTION: "🛡️ 갭 보호",
            ExitReason.MANUAL: "👤 수동 청산",
            ExitReason.KILL_SWITCH: "🚨 긴급 청산",
            ExitReason.OTHER: "기타"
        }
        return reason_map.get(reason, reason.value)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 포지션 조회
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_position(self, stock_code: str) -> Optional[ManagedPosition]:
        """특정 종목의 포지션을 반환합니다."""
        return self._positions.get(stock_code)
    
    def get_all_positions(self) -> Dict[str, ManagedPosition]:
        """모든 포지션을 반환합니다."""
        return self._positions.copy()
    
    def get_position_codes(self) -> List[str]:
        """보유 종목 코드 목록을 반환합니다."""
        return list(self._positions.keys())
    
    def has_position(self, stock_code: str = None) -> bool:
        """
        포지션 보유 여부를 반환합니다.
        
        Args:
            stock_code: 종목 코드 (None이면 전체 확인)
            
        Returns:
            bool: 포지션 보유 여부
        """
        if stock_code:
            return stock_code in self._positions
        return len(self._positions) > 0
    
    def count_positions(self) -> int:
        """현재 포지션 수를 반환합니다."""
        return len(self._positions)
    
    def get_total_unrealized_pnl(self) -> Tuple[float, float]:
        """
        전체 미실현 손익을 반환합니다.
        
        Returns:
            Tuple[float, float]: (총 미실현 손익, 평균 수익률)
        """
        if not self._positions:
            return 0.0, 0.0
        
        total_pnl = sum(p.unrealized_pnl for p in self._positions.values())
        avg_pct = sum(p.unrealized_pnl_pct for p in self._positions.values()) / len(self._positions)
        
        return total_pnl, avg_pct
    
    # ═══════════════════════════════════════════════════════════════════════════
    # API 연동 및 정합성 검증
    # ═══════════════════════════════════════════════════════════════════════════
    
    def restore_from_api(
        self,
        api_client,
        auto_sync: bool = True
    ) -> Tuple[List[str], List[str]]:
        """
        API로부터 실제 보유 종목을 조회하여 포지션을 복구합니다.
        
        Args:
            api_client: KIS API 클라이언트
            auto_sync: 자동 동기화 여부
            
        Returns:
            Tuple[List[str], List[str]]: (복구된 종목, 불일치 종목)
        """
        restored = []
        mismatched = []
        
        try:
            # API로 실제 보유 조회
            balance = api_client.get_account_balance()
            
            if not balance.get("success"):
                logger.error("[POSITION] 계좌 잔고 조회 실패")
                return restored, mismatched
            
            holdings = balance.get("holdings", [])
            api_stocks = {h["stock_code"]: h for h in holdings if h.get("quantity", 0) > 0}
            
            # 1. 저장된 포지션 vs API 보유 비교
            for code, position in list(self._positions.items()):
                if code in api_stocks:
                    # 보유 중 - 수량 확인
                    api_qty = api_stocks[code]["quantity"]
                    if api_qty != position.quantity:
                        logger.warning(
                            f"[POSITION] 수량 불일치: {code} "
                            f"(저장={position.quantity}, 실제={api_qty})"
                        )
                        if auto_sync:
                            position.quantity = api_qty
                        mismatched.append(code)
                    restored.append(code)
                else:
                    # 저장됨 + 보유 없음 = 불일치
                    logger.warning(
                        f"[POSITION] 포지션 불일치: {code} "
                        f"(저장됨이지만 실제 보유 없음)"
                    )
                    if auto_sync:
                        del self._positions[code]
                    mismatched.append(code)
            
            # 2. API 보유 중 저장 안 된 종목
            for code, holding in api_stocks.items():
                if code not in self._positions:
                    logger.warning(
                        f"[POSITION] 미기록 보유 발견: {code} {holding['quantity']}주"
                    )
                    mismatched.append(code)
            
            self._save_positions()
            
            logger.info(
                f"[POSITION] API 동기화 완료: "
                f"복구={len(restored)}, 불일치={len(mismatched)}"
            )
            
            # 텔레그램 알림
            for code in restored:
                pos = self._positions.get(code)
                if pos:
                    self._telegram.notify_position_restored(
                        stock_code=code,
                        entry_price=pos.entry_price,
                        quantity=pos.quantity,
                        entry_date=pos.entry_date,
                        holding_days=pos.holding_days,
                        stop_loss=pos.stop_loss,
                        take_profit=pos.take_profit,
                        trailing_stop=pos.trailing_stop,
                        atr_at_entry=pos.atr_at_entry
                    )
            
            return restored, mismatched
            
        except Exception as e:
            logger.error(f"[POSITION] API 동기화 실패: {e}")
            return restored, mismatched
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 데이터 저장/로드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _save_positions(self) -> None:
        """포지션을 파일에 저장합니다."""
        try:
            data = {
                "positions": {
                    code: pos.to_dict() 
                    for code, pos in self._positions.items()
                },
                "closed_positions": {
                    code: [p.to_dict() for p in positions]
                    for code, positions in self._closed_positions.items()
                },
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(self._positions_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.debug("[POSITION] 포지션 저장 완료")
            
        except Exception as e:
            logger.error(f"[POSITION] 포지션 저장 실패: {e}")
    
    def _load_positions(self) -> None:
        """저장된 포지션을 로드합니다."""
        if not self._positions_file.exists():
            return
        
        try:
            with open(self._positions_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 현재 포지션 로드
            positions_data = data.get("positions", {})
            for code, pos_dict in positions_data.items():
                self._positions[code] = ManagedPosition.from_dict(pos_dict)
            
            # 청산 기록 로드
            closed_data = data.get("closed_positions", {})
            for code, pos_list in closed_data.items():
                self._closed_positions[code] = [
                    ManagedPosition.from_dict(p) for p in pos_list
                ]
            
            logger.info(
                f"[POSITION] 포지션 로드 완료: "
                f"현재={len(self._positions)}개, "
                f"청산={sum(len(v) for v in self._closed_positions.values())}건"
            )
            
        except Exception as e:
            logger.warning(f"[POSITION] 포지션 로드 실패: {e}")
    
    def clear_all(self) -> None:
        """모든 포지션 데이터를 초기화합니다."""
        with self._lock:
            self._positions.clear()
            self._closed_positions.clear()
            
            if self._positions_file.exists():
                self._positions_file.unlink()
            
            logger.info("[POSITION] 모든 포지션 초기화됨")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════════════
    
    def print_positions(self) -> None:
        """현재 포지션을 출력합니다."""
        print("\n" + "═" * 70)
        print("                    [CURRENT POSITIONS]")
        print("═" * 70)
        
        if not self._positions:
            print("  보유 포지션 없음")
        else:
            for code, pos in self._positions.items():
                print(f"\n  📊 {code} ({pos.stock_name or 'N/A'})")
                print(f"     진입가: {pos.entry_price:,.0f}원 x {pos.quantity}주")
                print(f"     손절가: {pos.stop_loss:,.0f}원")
                print(f"     익절가: {pos.take_profit:,.0f}원")
                print(f"     트레일링: {pos.trailing_stop:,.0f}원")
                print(f"     현재가: {pos.current_price:,.0f}원")
                print(f"     손익: {pos.unrealized_pnl:+,.0f}원 ({pos.unrealized_pnl_pct:+.2f}%)")
                print(f"     보유일: {pos.holding_days}일")
        
        print("\n" + "═" * 70)


# ═══════════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════════

_position_manager: Optional[PositionManager] = None


def get_position_manager(**kwargs) -> PositionManager:
    """
    싱글톤 PositionManager를 반환합니다.
    
    Returns:
        PositionManager: 포지션 매니저
    """
    global _position_manager
    
    if _position_manager is None:
        _position_manager = PositionManager(**kwargs)
    
    return _position_manager
