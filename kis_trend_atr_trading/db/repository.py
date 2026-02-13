"""
KIS Trend-ATR Trading System - 데이터 접근 계층 (Repository)

이 모듈은 테이블별 데이터 접근을 담당합니다.

★ 역할:
    - SQL을 직접 쓰지 않고 함수만 호출하면 되도록 함
    - 비즈니스 로직과 데이터베이스 로직을 분리
    - 실수로 잘못된 쿼리를 작성하는 것을 방지

★ 구성:
    1. PositionRepository: 포지션 관리
    2. TradeRepository: 거래 기록 관리
    3. AccountSnapshotRepository: 계좌 스냅샷 관리

★ 변경 사항 (PostgreSQL → MySQL):
    - ON CONFLICT → INSERT ... ON DUPLICATE KEY UPDATE
    - RETURNING * → execute_insert() + SELECT
    - PostgreSQL 전용 함수 제거 (array_agg 등)

사용 예시:
    from db.repository import get_position_repository
    
    pos_repo = get_position_repository()
    
    # 포지션 저장
    pos_repo.save(
        symbol="005930",
        entry_price=70000,
        quantity=10,
        ...
    )
    
    # 열린 포지션 조회
    open_positions = pos_repo.get_open_positions()
"""

from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import threading

from db.mysql import MySQLManager, get_db_manager, QueryError
from utils.logger import get_logger
from utils.market_hours import KST, get_today
from env import get_trading_mode

logger = get_logger("repository")


def _get_namespace_mode() -> str:
    """DB 네임스페이스용 모드를 반환합니다 (PAPER/REAL)."""
    try:
        return get_trading_mode()
    except Exception:
        return "PAPER"


def _build_idempotency_key(parts: Tuple[Any, ...]) -> str:
    """결정적 idempotency key를 생성합니다."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionRecord:
    """
    포지션 데이터 클래스
    
    ★ DB의 positions 테이블 한 행을 표현
    """
    symbol: str
    entry_price: float
    quantity: int
    entry_time: datetime
    atr_at_entry: float
    stop_price: float
    take_profit_price: Optional[float]
    trailing_stop: Optional[float]
    highest_price: Optional[float]
    mode: str = "PAPER"
    status: str = "OPEN"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "symbol": self.symbol,
            "entry_price": float(self.entry_price),
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat() if isinstance(self.entry_time, datetime) else self.entry_time,
            "atr_at_entry": float(self.atr_at_entry),
            "stop_price": float(self.stop_price),
            "take_profit_price": float(self.take_profit_price) if self.take_profit_price else None,
            "trailing_stop": float(self.trailing_stop) if self.trailing_stop else None,
            "highest_price": float(self.highest_price) if self.highest_price else None,
            "mode": self.mode,
            "status": self.status
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PositionRecord":
        """딕셔너리에서 생성"""
        return cls(
            symbol=data["symbol"],
            entry_price=float(data["entry_price"]),
            quantity=int(data["quantity"]),
            entry_time=data["entry_time"],
            atr_at_entry=float(data["atr_at_entry"]),
            stop_price=float(data["stop_price"]),
            take_profit_price=float(data["take_profit_price"]) if data.get("take_profit_price") else None,
            trailing_stop=float(data["trailing_stop"]) if data.get("trailing_stop") else None,
            highest_price=float(data["highest_price"]) if data.get("highest_price") else None,
            mode=data.get("mode", "PAPER"),
            status=data.get("status", "OPEN"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at")
        )


@dataclass
class TradeRecord:
    """
    거래 기록 데이터 클래스
    
    ★ DB의 trades 테이블 한 행을 표현
    """
    symbol: str
    side: str  # BUY / SELL
    price: float
    quantity: int
    executed_at: datetime
    reason: Optional[str] = None  # 청산 사유
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    entry_price: Optional[float] = None
    holding_days: Optional[int] = None
    order_no: Optional[str] = None
    idempotency_key: Optional[str] = None
    mode: str = "PAPER"
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "price": float(self.price),
            "quantity": self.quantity,
            "executed_at": self.executed_at.isoformat() if isinstance(self.executed_at, datetime) else self.executed_at,
            "reason": self.reason,
            "pnl": float(self.pnl) if self.pnl is not None else None,
            "pnl_percent": float(self.pnl_percent) if self.pnl_percent is not None else None,
            "entry_price": float(self.entry_price) if self.entry_price else None,
            "holding_days": self.holding_days,
            "order_no": self.order_no,
            "idempotency_key": self.idempotency_key,
            "mode": self.mode
        }


@dataclass
class AccountSnapshotRecord:
    """
    계좌 스냅샷 데이터 클래스
    
    ★ DB의 account_snapshots 테이블 한 행을 표현
    """
    snapshot_time: datetime
    total_equity: float
    cash: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    mode: str = "PAPER"
    position_count: int = 0
    created_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "snapshot_time": self.snapshot_time.isoformat() if isinstance(self.snapshot_time, datetime) else self.snapshot_time,
            "total_equity": float(self.total_equity),
            "cash": float(self.cash),
            "unrealized_pnl": float(self.unrealized_pnl),
            "realized_pnl": float(self.realized_pnl),
            "mode": self.mode,
            "position_count": self.position_count
        }


@dataclass
class SymbolCacheRecord:
    """종목명 캐시 데이터 클래스"""
    stock_code: str
    stock_name: str
    updated_at: datetime

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SymbolCacheRecord":
        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            try:
                # MySQL DATETIME 문자열 파싱
                updated_at = datetime.fromisoformat(updated_at)
            except ValueError:
                updated_at = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        return cls(
            stock_code=str(data.get("stock_code", "")),
            stock_name=str(data.get("stock_name", "")),
            updated_at=updated_at or datetime.now(KST),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 포지션 Repository
# ═══════════════════════════════════════════════════════════════════════════════

class PositionRepository:
    """
    포지션 데이터 접근 클래스
    
    ★ 중학생도 이해할 수 있는 설명:
        - positions 테이블에 데이터를 넣고 빼는 역할
        - "삼성전자 70000원에 10주 샀어" → save()
        - "지금 뭐 들고 있어?" → get_open_positions()
        - "삼성전자 다 팔았어" → close_position()
    
    사용 예시:
        repo = PositionRepository()
        
        # 포지션 저장
        repo.save(
            symbol="005930",
            entry_price=70000,
            quantity=10,
            atr_at_entry=1500,
            stop_price=67000,
            take_profit_price=75000
        )
        
        # 열린 포지션 조회
        positions = repo.get_open_positions()
        
        # 포지션 청산
        repo.close_position("005930")
    """
    
    def __init__(self, db: MySQLManager = None):
        """
        Args:
            db: MySQLManager 인스턴스 (미입력 시 싱글톤 사용)
        """
        self.db = db or get_db_manager()
        self.mode = _get_namespace_mode()
    
    def save(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        atr_at_entry: float,
        stop_price: float,
        take_profit_price: float = None,
        trailing_stop: float = None,
        highest_price: float = None,
        entry_time: datetime = None
    ) -> Optional[PositionRecord]:
        """
        새 포지션을 저장합니다.
        
        ★ 동일 종목에 이미 OPEN 포지션이 있으면 업데이트
        ★ MySQL의 INSERT ... ON DUPLICATE KEY UPDATE 사용
        
        Args:
            symbol: 종목 코드
            entry_price: 진입가
            quantity: 수량
            atr_at_entry: 진입 시 ATR (★ 고정값)
            stop_price: 손절가
            take_profit_price: 익절가
            trailing_stop: 트레일링 스탑
            highest_price: 최고가
            entry_time: 진입 시간
        
        Returns:
            PositionRecord: 저장된 포지션 (실패 시 None)
        """
        entry_time = entry_time or datetime.now(KST)
        highest_price = highest_price or entry_price
        trailing_stop = trailing_stop or stop_price
        
        try:
            # 이미 열린 포지션이 있는지 확인
            existing = self.get_by_symbol(symbol)
            if existing and existing.status == "OPEN":
                logger.warning(f"[REPO] 이미 열린 포지션 존재: {symbol}")
                return None
            
            # MySQL INSERT ... ON DUPLICATE KEY UPDATE
            # ★ PostgreSQL의 ON CONFLICT 대체
            self.db.execute_command(
                """
                INSERT INTO positions (
                    symbol, entry_price, quantity, entry_time,
                    atr_at_entry, stop_price, take_profit_price,
                    trailing_stop, highest_price, mode, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                ON DUPLICATE KEY UPDATE
                    entry_price = VALUES(entry_price),
                    quantity = VALUES(quantity),
                    entry_time = VALUES(entry_time),
                    atr_at_entry = VALUES(atr_at_entry),
                    stop_price = VALUES(stop_price),
                    take_profit_price = VALUES(take_profit_price),
                    trailing_stop = VALUES(trailing_stop),
                    highest_price = VALUES(highest_price),
                    mode = VALUES(mode),
                    status = 'OPEN'
                """,
                (
                    symbol, entry_price, quantity, entry_time,
                    atr_at_entry, stop_price, take_profit_price,
                    trailing_stop, highest_price, self.mode
                )
            )
            
            # 저장된 데이터 조회 (RETURNING 대체)
            result = self.get_by_symbol(symbol)
            
            if result:
                logger.info(f"[REPO] 포지션 저장: {symbol} @ {entry_price:,.0f}원 x {quantity}주")
                return result
            return None
            
        except QueryError as e:
            logger.error(f"[REPO] 포지션 저장 실패: {e}")
            return None

    def upsert_from_account_holding(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        atr_at_entry: float,
        stop_price: float,
        take_profit_price: float = None,
        trailing_stop: float = None,
        highest_price: float = None,
        entry_time: datetime = None
    ) -> Optional[PositionRecord]:
        """
        실계좌 보유를 기준으로 포지션을 강제 upsert합니다.

        ★ synchronize 용 메서드:
            - 기존 OPEN 포지션이 있어도 수량/평균단가를 업데이트
            - DB 상태를 실계좌 기준으로 맞출 때 사용
        """
        entry_time = entry_time or datetime.now(KST)
        highest_price = highest_price or entry_price
        trailing_stop = trailing_stop or stop_price

        try:
            self.db.execute_command(
                """
                INSERT INTO positions (
                    symbol, entry_price, quantity, entry_time,
                    atr_at_entry, stop_price, take_profit_price,
                    trailing_stop, highest_price, mode, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                ON DUPLICATE KEY UPDATE
                    entry_price = VALUES(entry_price),
                    quantity = VALUES(quantity),
                    entry_time = VALUES(entry_time),
                    atr_at_entry = VALUES(atr_at_entry),
                    stop_price = VALUES(stop_price),
                    take_profit_price = VALUES(take_profit_price),
                    trailing_stop = VALUES(trailing_stop),
                    highest_price = VALUES(highest_price),
                    mode = VALUES(mode),
                    status = 'OPEN'
                """,
                (
                    symbol, entry_price, quantity, entry_time,
                    atr_at_entry, stop_price, take_profit_price,
                    trailing_stop, highest_price, self.mode
                )
            )
            return self.get_by_symbol(symbol)
        except QueryError as e:
            logger.error(f"[REPO] 실계좌 기준 upsert 실패: {e}")
            return None
    
    def get_by_symbol(self, symbol: str) -> Optional[PositionRecord]:
        """
        종목 코드로 포지션을 조회합니다.
        
        Args:
            symbol: 종목 코드
        
        Returns:
            PositionRecord: 포지션 (없으면 None)
        """
        result = self.db.execute_query(
            "SELECT * FROM positions WHERE symbol = %s AND mode = %s",
            (symbol, self.mode),
            fetch_one=True
        )
        
        if result:
            return PositionRecord.from_dict(result)
        return None
    
    def get_open_positions(self) -> List[PositionRecord]:
        """
        열린(OPEN) 포지션 목록을 조회합니다.
        
        Returns:
            List[PositionRecord]: 열린 포지션 목록
        """
        results = self.db.execute_query(
            "SELECT * FROM positions WHERE status = 'OPEN' AND mode = %s ORDER BY entry_time",
            (self.mode,)
        )
        
        return [PositionRecord.from_dict(r) for r in results]
    
    def get_all_positions(self) -> List[PositionRecord]:
        """모든 포지션(OPEN + CLOSED) 조회"""
        results = self.db.execute_query(
            "SELECT * FROM positions WHERE mode = %s ORDER BY entry_time DESC",
            (self.mode,)
        )
        return [PositionRecord.from_dict(r) for r in results]
    
    def has_open_position(self, symbol: str = None) -> bool:
        """
        열린 포지션이 있는지 확인합니다.
        
        Args:
            symbol: 종목 코드 (None이면 전체 확인)
        
        Returns:
            bool: 열린 포지션 존재 여부
        """
        if symbol:
            result = self.db.execute_query(
                "SELECT COUNT(*) as cnt FROM positions WHERE symbol = %s AND status = 'OPEN' AND mode = %s",
                (symbol, self.mode),
                fetch_one=True
            )
        else:
            result = self.db.execute_query(
                "SELECT COUNT(*) as cnt FROM positions WHERE status = 'OPEN' AND mode = %s",
                (self.mode,),
                fetch_one=True
            )
        
        return result and result.get("cnt", 0) > 0
    
    def update_trailing_stop(
        self,
        symbol: str,
        trailing_stop: float,
        highest_price: float
    ) -> bool:
        """
        트레일링 스탑과 최고가를 업데이트합니다.
        
        Args:
            symbol: 종목 코드
            trailing_stop: 새 트레일링 스탑
            highest_price: 새 최고가
        
        Returns:
            bool: 업데이트 성공 여부
        """
        try:
            affected = self.db.execute_command(
                """
                UPDATE positions 
                SET trailing_stop = %s, highest_price = %s
                WHERE symbol = %s AND status = 'OPEN' AND mode = %s
                """,
                (trailing_stop, highest_price, symbol, self.mode)
            )
            
            if affected > 0:
                logger.debug(f"[REPO] 트레일링 업데이트: {symbol} → {trailing_stop:,.0f}")
                return True
            return False
            
        except QueryError as e:
            logger.error(f"[REPO] 트레일링 업데이트 실패: {e}")
            return False
    
    def close_position(self, symbol: str) -> bool:
        """
        포지션을 청산(CLOSED) 상태로 변경합니다.
        
        ★ 실제 매도가 아니라 DB 상태만 변경
        ★ trades 테이블에 매도 기록은 별도로 해야 함
        
        Args:
            symbol: 종목 코드
        
        Returns:
            bool: 청산 성공 여부
        """
        try:
            affected = self.db.execute_command(
                """
                UPDATE positions 
                SET status = 'CLOSED'
                WHERE symbol = %s AND status = 'OPEN' AND mode = %s
                """,
                (symbol, self.mode)
            )
            
            if affected > 0:
                logger.info(f"[REPO] 포지션 청산: {symbol}")
                return True
            return False
            
        except QueryError as e:
            logger.error(f"[REPO] 포지션 청산 실패: {e}")
            return False
    
    def delete_position(self, symbol: str) -> bool:
        """
        포지션을 완전히 삭제합니다.
        
        ★ 주의: 히스토리가 사라짐. close_position() 권장
        
        Args:
            symbol: 종목 코드
        
        Returns:
            bool: 삭제 성공 여부
        """
        try:
            affected = self.db.execute_command(
                "DELETE FROM positions WHERE symbol = %s AND mode = %s",
                (symbol, self.mode)
            )
            return affected > 0
        except QueryError as e:
            logger.error(f"[REPO] 포지션 삭제 실패: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# 거래 기록 Repository
# ═══════════════════════════════════════════════════════════════════════════════

class TradeRepository:
    """
    거래 기록 데이터 접근 클래스
    
    ★ 역할:
        - 모든 매수/매도 기록을 저장하고 조회
        - 성과 분석용 데이터 제공
    
    사용 예시:
        repo = TradeRepository()
        
        # 매수 기록
        repo.save_buy("005930", 70000, 10)
        
        # 매도 기록 (손익 포함)
        repo.save_sell("005930", 72000, 10, entry_price=70000, reason="TAKE_PROFIT")
        
        # 일별 거래 조회
        trades = repo.get_trades_by_date(date.today())
    """
    
    def __init__(self, db: MySQLManager = None):
        self.db = db or get_db_manager()
        self.mode = _get_namespace_mode()
    
    def save_buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        executed_at: datetime = None,
        order_no: str = None,
        idempotency_key: str = None
    ) -> Optional[TradeRecord]:
        """
        매수 기록을 저장합니다.
        
        Args:
            symbol: 종목 코드
            price: 체결가
            quantity: 수량
            executed_at: 체결 시간
            order_no: 주문번호
        
        Returns:
            TradeRecord: 저장된 거래 기록
        """
        executed_at = executed_at or datetime.now(KST)
        idempotency_key = idempotency_key or _build_idempotency_key(
            ("BUY", symbol, quantity, f"{price:.4f}", order_no or "", executed_at.isoformat(), self.mode)
        )
        
        try:
            # INSERT 실행 후 LAST_INSERT_ID 반환
            trade_id = self.db.execute_insert(
                """
                INSERT INTO trades (symbol, side, price, quantity, executed_at, order_no, mode, idempotency_key)
                VALUES (%s, 'BUY', %s, %s, %s, %s, %s, %s)
                """,
                (symbol, price, quantity, executed_at, order_no, self.mode, idempotency_key)
            )
            
            if trade_id:
                logger.info(f"[REPO] 매수 기록: {symbol} @ {price:,.0f}원 x {quantity}주")
                return TradeRecord(
                    id=trade_id,
                    symbol=symbol,
                    side="BUY",
                    price=price,
                    quantity=quantity,
                    executed_at=executed_at,
                    order_no=order_no,
                    idempotency_key=idempotency_key,
                    mode=self.mode
                )
            return None
            
        except QueryError as e:
            logger.error(f"[REPO] 매수 기록 실패: {e}")
            return None
    
    def save_sell(
        self,
        symbol: str,
        price: float,
        quantity: int,
        entry_price: float = None,
        reason: str = None,
        holding_days: int = None,
        executed_at: datetime = None,
        order_no: str = None,
        idempotency_key: str = None
    ) -> Optional[TradeRecord]:
        """
        매도 기록을 저장합니다.
        
        ★ 손익은 자동 계산됩니다.
        
        Args:
            symbol: 종목 코드
            price: 체결가
            quantity: 수량
            entry_price: 진입가 (손익 계산용)
            reason: 청산 사유 (ATR_STOP, TAKE_PROFIT, TRAILING_STOP, ...)
            holding_days: 보유 일수
            executed_at: 체결 시간
            order_no: 주문번호
        
        Returns:
            TradeRecord: 저장된 거래 기록
        """
        executed_at = executed_at or datetime.now(KST)
        
        # 손익 계산
        pnl = None
        pnl_percent = None
        if entry_price and entry_price > 0:
            pnl = (price - entry_price) * quantity
            pnl_percent = ((price - entry_price) / entry_price) * 100
        idempotency_key = idempotency_key or _build_idempotency_key(
            ("SELL", symbol, quantity, f"{price:.4f}", order_no or "", reason or "", executed_at.isoformat(), self.mode)
        )
        
        try:
            trade_id = self.db.execute_insert(
                """
                INSERT INTO trades (
                    symbol, side, price, quantity, executed_at,
                    reason, pnl, pnl_percent, entry_price, holding_days, order_no, mode, idempotency_key
                )
                VALUES (%s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    symbol, price, quantity, executed_at,
                    reason, pnl, pnl_percent, entry_price, holding_days, order_no, self.mode, idempotency_key
                )
            )
            
            if trade_id:
                pnl_str = f"{pnl:+,.0f}원 ({pnl_percent:+.2f}%)" if pnl else "N/A"
                logger.info(f"[REPO] 매도 기록: {symbol} @ {price:,.0f}원 x {quantity}주, 손익={pnl_str}")
                
                return TradeRecord(
                    id=trade_id,
                    symbol=symbol,
                    side="SELL",
                    price=price,
                    quantity=quantity,
                    executed_at=executed_at,
                    reason=reason,
                    pnl=pnl,
                    pnl_percent=pnl_percent,
                    entry_price=entry_price,
                    holding_days=holding_days,
                    order_no=order_no,
                    idempotency_key=idempotency_key,
                    mode=self.mode
                )
            return None
            
        except QueryError as e:
            logger.error(f"[REPO] 매도 기록 실패: {e}")
            return None
    
    def save_signal_only(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        reason: str = None,
        entry_price: float = None,
        executed_at: datetime = None,
        idempotency_key: str = None
    ) -> Optional[TradeRecord]:
        """
        신호만 기록합니다 (실매매 없음).
        
        ★ SIGNAL_ONLY 모드에서 사용
        
        Args:
            symbol: 종목 코드
            side: BUY / SELL
            price: 가격
            quantity: 수량
            reason: 사유
            entry_price: 진입가 (SELL 시)
            executed_at: 시간
        
        Returns:
            TradeRecord: 기록된 신호
        """
        executed_at = executed_at or datetime.now(KST)
        
        # 손익 계산 (SELL인 경우)
        pnl = None
        pnl_percent = None
        if side == "SELL" and entry_price and entry_price > 0:
            pnl = (price - entry_price) * quantity
            pnl_percent = ((price - entry_price) / entry_price) * 100
        idempotency_key = idempotency_key or _build_idempotency_key(
            ("SIGNAL_ONLY", side, symbol, quantity, f"{price:.4f}", executed_at.isoformat(), self.mode)
        )
        
        try:
            trade_id = self.db.execute_insert(
                """
                INSERT INTO trades (
                    symbol, side, price, quantity, executed_at,
                    reason, pnl, pnl_percent, entry_price, mode, idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, 'SIGNAL_ONLY', %s, %s, %s, %s, %s)
                """,
                (symbol, side, price, quantity, executed_at, pnl, pnl_percent, entry_price, self.mode, idempotency_key)
            )
            
            if trade_id:
                logger.info(f"[REPO] 신호 기록: {side} {symbol} @ {price:,.0f}원")
                return TradeRecord(
                    id=trade_id,
                    symbol=symbol,
                    side=side,
                    price=price,
                    quantity=quantity,
                    executed_at=executed_at,
                    reason="SIGNAL_ONLY",
                    pnl=pnl,
                    pnl_percent=pnl_percent,
                    entry_price=entry_price,
                    idempotency_key=idempotency_key,
                    mode=self.mode
                )
            return None
            
        except QueryError as e:
            logger.error(f"[REPO] 신호 기록 실패: {e}")
            return None
    
    def get_trades_by_date(
        self,
        trade_date: date,
        symbol: str = None
    ) -> List[TradeRecord]:
        """
        특정 날짜의 거래 기록을 조회합니다.
        
        Args:
            trade_date: 조회 날짜
            symbol: 종목 코드 (None이면 전체)
        
        Returns:
            List[TradeRecord]: 거래 기록 목록
        """
        if symbol:
            results = self.db.execute_query(
                """
                SELECT * FROM trades 
                WHERE DATE(executed_at) = %s AND symbol = %s AND mode = %s
                ORDER BY executed_at
                """,
                (trade_date, symbol, self.mode)
            )
        else:
            results = self.db.execute_query(
                """
                SELECT * FROM trades 
                WHERE DATE(executed_at) = %s AND mode = %s
                ORDER BY executed_at
                """,
                (trade_date, self.mode)
            )
        
        return [self._to_record(r) for r in results]
    
    def get_trades_by_symbol(
        self,
        symbol: str,
        limit: int = 100
    ) -> List[TradeRecord]:
        """
        종목별 거래 기록을 조회합니다.
        
        Args:
            symbol: 종목 코드
            limit: 최대 조회 수
        
        Returns:
            List[TradeRecord]: 거래 기록 목록
        """
        results = self.db.execute_query(
            """
            SELECT * FROM trades 
            WHERE symbol = %s AND mode = %s
            ORDER BY executed_at DESC
            LIMIT %s
            """,
            (symbol, self.mode, limit)
        )
        
        return [self._to_record(r) for r in results]
    
    def get_recent_trades(self, limit: int = 50) -> List[TradeRecord]:
        """최근 거래 기록 조회"""
        results = self.db.execute_query(
            "SELECT * FROM trades WHERE mode = %s ORDER BY executed_at DESC LIMIT %s",
            (self.mode, limit)
        )
        return [self._to_record(r) for r in results]
    
    def get_daily_summary(self, trade_date: date = None) -> Dict[str, Any]:
        """
        일별 거래 요약을 반환합니다.
        
        Args:
            trade_date: 조회 날짜 (None이면 오늘)
        
        Returns:
            Dict: 요약 정보
        """
        trade_date = trade_date or get_today()
        
        result = self.db.execute_query(
            """
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) as buy_count,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) as sell_count,
                COALESCE(SUM(pnl), 0) as total_pnl,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_count,
                MAX(pnl) as max_profit,
                MIN(pnl) as max_loss
            FROM trades
            WHERE DATE(executed_at) = %s AND mode = %s
            """,
            (trade_date, self.mode),
            fetch_one=True
        )
        
        if result:
            total = result.get("total_trades", 0)
            sells = result.get("sell_count", 0)
            wins = result.get("win_count", 0)
            
            return {
                "date": trade_date.isoformat(),
                "total_trades": total or 0,
                "buy_count": result.get("buy_count", 0) or 0,
                "sell_count": sells or 0,
                "total_pnl": float(result.get("total_pnl", 0) or 0),
                "win_count": wins or 0,
                "loss_count": result.get("loss_count", 0) or 0,
                "win_rate": (wins / sells * 100) if sells > 0 else 0.0,
                "max_profit": float(result.get("max_profit", 0) or 0),
                "max_loss": float(result.get("max_loss", 0) or 0)
            }
        
        return {
            "date": trade_date.isoformat(),
            "total_trades": 0,
            "buy_count": 0,
            "sell_count": 0,
            "total_pnl": 0.0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0
        }
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """
        전체 성과 통계를 반환합니다.
        
        Returns:
            Dict: 성과 지표
        """
        result = self.db.execute_query(
            """
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) as total_sells,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl), 0) as total_pnl,
                AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss,
                MAX(pnl) as max_win,
                MIN(pnl) as max_loss,
                AVG(holding_days) as avg_holding_days
            FROM trades
            WHERE side = 'SELL' AND reason != 'SIGNAL_ONLY' AND mode = %s
            """,
            (self.mode,),
            fetch_one=True
        )
        
        if result:
            sells = result.get("total_sells", 0) or 0
            wins = result.get("wins", 0) or 0
            losses = result.get("losses", 0) or 0
            avg_win = float(result.get("avg_win") or 0)
            avg_loss = abs(float(result.get("avg_loss") or 0))
            
            # Profit Factor = 총 수익 / 총 손실
            profit_factor = (avg_win * wins) / (avg_loss * losses) if losses > 0 and avg_loss > 0 else 0
            
            # Expectancy = (승률 × 평균수익) - (패률 × 평균손실)
            win_rate = (wins / sells) if sells > 0 else 0
            expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
            
            return {
                "total_trades": sells,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate * 100,
                "total_pnl": float(result.get("total_pnl") or 0),
                "avg_win": avg_win,
                "avg_loss": -avg_loss,  # 음수로 표시
                "max_win": float(result.get("max_win") or 0),
                "max_loss": float(result.get("max_loss") or 0),
                "profit_factor": profit_factor,
                "expectancy": expectancy,
                "avg_holding_days": float(result.get("avg_holding_days") or 0)
            }
        
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "avg_holding_days": 0.0
        }
    
    def get_pnl_by_reason(self) -> List[Dict[str, Any]]:
        """
        청산 사유별 손익 통계를 반환합니다.
        
        Returns:
            List[Dict]: 사유별 통계
        """
        results = self.db.execute_query(
            """
            SELECT 
                reason,
                COUNT(*) as count,
                SUM(pnl) as total_pnl,
                AVG(pnl) as avg_pnl,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM trades
            WHERE side = 'SELL' AND reason IS NOT NULL AND mode = %s
            GROUP BY reason
            ORDER BY count DESC
            """,
            (self.mode,)
        )
        
        return [
            {
                "reason": r["reason"],
                "count": r["count"],
                "total_pnl": float(r["total_pnl"] or 0),
                "avg_pnl": float(r["avg_pnl"] or 0),
                "win_rate": (r["wins"] / r["count"] * 100) if r["count"] > 0 else 0
            }
            for r in results
        ]
    
    def _to_record(self, row: Dict) -> TradeRecord:
        """DB 행을 TradeRecord로 변환"""
        return TradeRecord(
            id=row.get("id"),
            symbol=row["symbol"],
            side=row["side"],
            price=float(row["price"]),
            quantity=int(row["quantity"]),
            executed_at=row["executed_at"],
            reason=row.get("reason"),
            pnl=float(row["pnl"]) if row.get("pnl") else None,
            pnl_percent=float(row["pnl_percent"]) if row.get("pnl_percent") else None,
            entry_price=float(row["entry_price"]) if row.get("entry_price") else None,
            holding_days=row.get("holding_days"),
            order_no=row.get("order_no"),
            idempotency_key=row.get("idempotency_key"),
            mode=row.get("mode", "PAPER"),
            created_at=row.get("created_at")
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 계좌 스냅샷 Repository
# ═══════════════════════════════════════════════════════════════════════════════

class AccountSnapshotRepository:
    """
    계좌 스냅샷 데이터 접근 클래스
    
    ★ 역할:
        - 특정 시점의 계좌 상태를 저장
        - 자산 변화 추적
        - MDD (최대 낙폭) 계산
    """
    
    def __init__(self, db: MySQLManager = None):
        self.db = db or get_db_manager()
        self.mode = _get_namespace_mode()
    
    def save(
        self,
        total_equity: float,
        cash: float,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        position_count: int = 0,
        snapshot_time: datetime = None
    ) -> Optional[AccountSnapshotRecord]:
        """
        계좌 스냅샷을 저장합니다.
        
        Args:
            total_equity: 총 평가금액
            cash: 현금
            unrealized_pnl: 미실현 손익
            realized_pnl: 실현 손익 (누적)
            position_count: 보유 포지션 수
            snapshot_time: 스냅샷 시간
        
        Returns:
            AccountSnapshotRecord: 저장된 스냅샷
        """
        snapshot_time = snapshot_time or datetime.now(KST)
        
        try:
            # MySQL INSERT ... ON DUPLICATE KEY UPDATE
            self.db.execute_command(
                """
                INSERT INTO account_snapshots (
                    snapshot_time, total_equity, cash, 
                    unrealized_pnl, realized_pnl, mode, position_count
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_equity = VALUES(total_equity),
                    cash = VALUES(cash),
                    unrealized_pnl = VALUES(unrealized_pnl),
                    realized_pnl = VALUES(realized_pnl),
                    mode = VALUES(mode),
                    position_count = VALUES(position_count)
                """,
                (
                    snapshot_time, total_equity, cash,
                    unrealized_pnl, realized_pnl, self.mode, position_count
                )
            )
            
            logger.debug(f"[REPO] 계좌 스냅샷: {total_equity:,.0f}원")
            return AccountSnapshotRecord(
                snapshot_time=snapshot_time,
                total_equity=total_equity,
                cash=cash,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                mode=self.mode,
                position_count=position_count
            )
            
        except QueryError as e:
            logger.error(f"[REPO] 스냅샷 저장 실패: {e}")
            return None
    
    def get_latest(self) -> Optional[AccountSnapshotRecord]:
        """최신 스냅샷 조회"""
        result = self.db.execute_query(
            "SELECT * FROM account_snapshots WHERE mode = %s ORDER BY snapshot_time DESC LIMIT 1",
            (self.mode,),
            fetch_one=True
        )
        
        if result:
            return self._to_record(result)
        return None
    
    def get_by_date(self, snapshot_date: date) -> List[AccountSnapshotRecord]:
        """특정 날짜의 스냅샷 조회"""
        results = self.db.execute_query(
            """
            SELECT * FROM account_snapshots 
            WHERE DATE(snapshot_time) = %s AND mode = %s
            ORDER BY snapshot_time
            """,
            (snapshot_date, self.mode)
        )
        
        return [self._to_record(r) for r in results]
    
    def get_daily_equity(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        일별 평가금액 추이를 반환합니다.
        
        ★ MySQL 호환: array_agg 대신 서브쿼리 사용
        
        Args:
            days: 조회 일수
        
        Returns:
            List[Dict]: 일별 평가금액
        """
        # MySQL에서는 array_agg가 없으므로 다른 방법 사용
        cutoff = datetime.now(KST) - timedelta(days=days)

        results = self.db.execute_query(
            """
            SELECT 
                DATE(snapshot_time) as trade_date,
                MIN(total_equity) as min_equity,
                MAX(total_equity) as max_equity,
                (SELECT total_equity FROM account_snapshots a2 
                 WHERE DATE(a2.snapshot_time) = DATE(a1.snapshot_time)
                 ORDER BY a2.snapshot_time DESC LIMIT 1) as end_equity
            FROM account_snapshots a1
            WHERE snapshot_time >= %s AND mode = %s
            GROUP BY DATE(snapshot_time)
            ORDER BY trade_date
            """,
            (cutoff, self.mode)
        )
        
        return [
            {
                "date": str(r["trade_date"]),
                "min_equity": float(r["min_equity"] or 0),
                "max_equity": float(r["max_equity"] or 0),
                "end_equity": float(r["end_equity"] or 0)
            }
            for r in results
        ]
    
    def calculate_mdd(self, days: int = None) -> Dict[str, Any]:
        """
        최대 낙폭(MDD)을 계산합니다.
        
        Args:
            days: 계산 기간 (None이면 전체)
        
        Returns:
            Dict: MDD 정보
        """
        if days:
            cutoff = datetime.now(KST) - timedelta(days=days)
            query = """
                SELECT snapshot_time, total_equity
                FROM account_snapshots
                WHERE snapshot_time >= %s
                ORDER BY snapshot_time
            """
            results = self.db.execute_query(query, (cutoff,))
        else:
            query = """
                SELECT snapshot_time, total_equity
                FROM account_snapshots
                ORDER BY snapshot_time
            """
            results = self.db.execute_query(query)
        
        if not results:
            return {"mdd": 0.0, "mdd_percent": 0.0, "peak_time": None, "trough_time": None}
        
        # MDD 계산
        peak = 0.0
        mdd = 0.0
        mdd_percent = 0.0
        peak_time = None
        trough_time = None
        
        for r in results:
            equity = float(r["total_equity"])
            
            if equity > peak:
                peak = equity
                peak_time = r["snapshot_time"]
            
            if peak > 0:
                drawdown = peak - equity
                drawdown_pct = (drawdown / peak) * 100
                
                if drawdown > mdd:
                    mdd = drawdown
                    mdd_percent = drawdown_pct
                    trough_time = r["snapshot_time"]
        
        return {
            "mdd": mdd,
            "mdd_percent": mdd_percent,
            "peak_time": peak_time,
            "trough_time": trough_time
        }
    
    def _to_record(self, row: Dict) -> AccountSnapshotRecord:
        """DB 행을 Record로 변환"""
        return AccountSnapshotRecord(
            snapshot_time=row["snapshot_time"],
            total_equity=float(row["total_equity"]),
            cash=float(row["cash"]),
            unrealized_pnl=float(row.get("unrealized_pnl") or 0),
            realized_pnl=float(row.get("realized_pnl") or 0),
            mode=row.get("mode", "PAPER"),
            position_count=row.get("position_count", 0),
            created_at=row.get("created_at")
        )


class SymbolCacheRepository:
    """
    종목명 캐시 데이터 접근 클래스

    ★ SSOT DB(positions/trades와 동일한 MySQL)에 종목명 캐시를 저장합니다.
    """

    def __init__(self, db: MySQLManager = None):
        self.db = db or get_db_manager()
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._ensure_table()

    def _is_missing_table_error(self, error: Exception) -> bool:
        msg = str(error)
        return "1146" in msg and "symbol_cache" in msg

    def _ensure_table(self) -> None:
        if self._schema_ready:
            return

        with self._schema_lock:
            if self._schema_ready:
                return
            try:
                self.db.execute_command(
                    """
                    CREATE TABLE IF NOT EXISTS symbol_cache (
                        stock_code VARCHAR(20) NOT NULL PRIMARY KEY,
                        stock_name VARCHAR(100) NOT NULL,
                        updated_at DATETIME NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """
                )
                try:
                    self.db.execute_command(
                        "CREATE INDEX idx_symbol_cache_updated_at ON symbol_cache(updated_at)"
                    )
                except Exception:
                    # 이미 존재하는 인덱스 오류는 무시
                    pass
                self._schema_ready = True
            except Exception as e:
                logger.warning(f"[REPO] symbol_cache 스키마 보장 실패: {e}")

    def get(self, stock_code: str) -> Optional[SymbolCacheRecord]:
        """종목코드로 캐시를 조회합니다."""
        self._ensure_table()
        try:
            result = self.db.execute_query(
                """
                SELECT stock_code, stock_name, updated_at
                FROM symbol_cache
                WHERE stock_code = %s
                """,
                (stock_code,),
                fetch_one=True,
            )
            if not result:
                return None
            return SymbolCacheRecord.from_dict(result)
        except Exception as e:
            if self._is_missing_table_error(e):
                self._ensure_table()
                try:
                    result = self.db.execute_query(
                        """
                        SELECT stock_code, stock_name, updated_at
                        FROM symbol_cache
                        WHERE stock_code = %s
                        """,
                        (stock_code,),
                        fetch_one=True,
                    )
                    if result:
                        return SymbolCacheRecord.from_dict(result)
                    return None
                except Exception as retry_err:
                    logger.warning(f"[REPO] symbol_cache 재조회 실패: {stock_code}, {retry_err}")
            logger.warning(f"[REPO] symbol_cache 조회 실패: {stock_code}, {e}")
            return None

    def upsert(
        self,
        stock_code: str,
        stock_name: str,
        updated_at: datetime = None,
    ) -> bool:
        """종목명 캐시를 upsert 합니다."""
        self._ensure_table()
        ts = updated_at or datetime.now(KST)
        try:
            self.db.execute_command(
                """
                INSERT INTO symbol_cache (stock_code, stock_name, updated_at)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    stock_name = VALUES(stock_name),
                    updated_at = VALUES(updated_at)
                """,
                (stock_code, stock_name, ts),
            )
            return True
        except Exception as e:
            if self._is_missing_table_error(e):
                self._ensure_table()
                try:
                    self.db.execute_command(
                        """
                        INSERT INTO symbol_cache (stock_code, stock_name, updated_at)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            stock_name = VALUES(stock_name),
                            updated_at = VALUES(updated_at)
                        """,
                        (stock_code, stock_name, ts),
                    )
                    return True
                except Exception as retry_err:
                    logger.warning(f"[REPO] symbol_cache 재upsert 실패: {stock_code}, {retry_err}")
            logger.warning(f"[REPO] symbol_cache upsert 실패: {stock_code}, {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_position_repo: Optional[PositionRepository] = None
_trade_repo: Optional[TradeRepository] = None
_snapshot_repo: Optional[AccountSnapshotRepository] = None
_symbol_cache_repo: Optional[SymbolCacheRepository] = None


def get_position_repository() -> PositionRepository:
    """싱글톤 PositionRepository 인스턴스"""
    global _position_repo
    if _position_repo is None:
        _position_repo = PositionRepository()
    return _position_repo


def get_trade_repository() -> TradeRepository:
    """싱글톤 TradeRepository 인스턴스"""
    global _trade_repo
    if _trade_repo is None:
        _trade_repo = TradeRepository()
    return _trade_repo


def get_account_snapshot_repository() -> AccountSnapshotRepository:
    """싱글톤 AccountSnapshotRepository 인스턴스"""
    global _snapshot_repo
    if _snapshot_repo is None:
        _snapshot_repo = AccountSnapshotRepository()
    return _snapshot_repo


def get_symbol_cache_repository() -> SymbolCacheRepository:
    """싱글톤 SymbolCacheRepository 인스턴스"""
    global _symbol_cache_repo
    if _symbol_cache_repo is None:
        _symbol_cache_repo = SymbolCacheRepository()
    return _symbol_cache_repo
