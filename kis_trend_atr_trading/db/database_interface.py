"""
KIS Trend-ATR Trading System - 데이터베이스 추상화 인터페이스

═══════════════════════════════════════════════════════════════════════════════
⚠️ 이 모듈은 PostgreSQL과 MySQL을 추상화하여 동일한 인터페이스로 사용합니다.
═══════════════════════════════════════════════════════════════════════════════

★ 목적:
  - DB 종류에 관계없이 동일한 코드로 작동
  - SQL Dialect 차이를 내부에서 처리
  - DB 없이도 JSON 파일로 기본 동작

★ 지원 데이터베이스:
  - PostgreSQL
  - MySQL
  - JSON (파일 기반, DB 없이 동작)

★ 핵심 메서드:
  - save_trade()
  - load_positions()
  - save_performance_snapshot()

사용 예시:
    from db.database_interface import get_database
    
    db = get_database()
    
    # 거래 저장
    db.save_trade(
        symbol="005930",
        side="BUY",
        price=70000,
        quantity=10
    )
    
    # 포지션 조회
    positions = db.load_positions()

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

import os
import json
from abc import ABC, abstractmethod
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict

from utils.logger import get_logger

logger = get_logger("database_interface")


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeData:
    """거래 데이터"""
    symbol: str
    side: str  # BUY / SELL
    price: float
    quantity: int
    executed_at: datetime
    reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    entry_price: Optional[float] = None
    holding_days: Optional[int] = None
    order_no: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        result = asdict(self)
        if isinstance(result.get('executed_at'), datetime):
            result['executed_at'] = result['executed_at'].isoformat()
        return result


@dataclass
class PositionData:
    """포지션 데이터"""
    symbol: str
    entry_price: float
    quantity: int
    entry_time: datetime
    atr_at_entry: float
    stop_price: float
    take_profit_price: Optional[float] = None
    trailing_stop: Optional[float] = None
    highest_price: Optional[float] = None
    status: str = "OPEN"
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        result = asdict(self)
        if isinstance(result.get('entry_time'), datetime):
            result['entry_time'] = result['entry_time'].isoformat()
        return result


@dataclass
class PerformanceSnapshot:
    """성과 스냅샷 데이터"""
    snapshot_time: datetime
    total_equity: float
    cash: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    position_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        result = asdict(self)
        if isinstance(result.get('snapshot_time'), datetime):
            result['snapshot_time'] = result['snapshot_time'].isoformat()
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# 추상 인터페이스
# ═══════════════════════════════════════════════════════════════════════════════

class DatabaseInterface(ABC):
    """
    데이터베이스 추상 인터페이스
    
    ★ 모든 DB 구현체는 이 인터페이스를 따릅니다.
    ★ DB 없이도 JSON으로 동작하는 기본 구현 제공
    """
    
    @abstractmethod
    def save_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        executed_at: datetime = None,
        reason: str = None,
        pnl: float = None,
        pnl_percent: float = None,
        entry_price: float = None,
        holding_days: int = None,
        order_no: str = None
    ) -> bool:
        """
        거래 기록을 저장합니다.
        
        Args:
            symbol: 종목 코드
            side: BUY / SELL
            price: 체결가
            quantity: 수량
            executed_at: 체결 시간
            reason: 청산 사유
            pnl: 손익 금액
            pnl_percent: 손익률
            entry_price: 진입가
            holding_days: 보유일수
            order_no: 주문번호
        
        Returns:
            bool: 저장 성공 여부
        """
        pass
    
    @abstractmethod
    def load_positions(self) -> List[PositionData]:
        """
        열린 포지션 목록을 로드합니다.
        
        Returns:
            List[PositionData]: 포지션 목록
        """
        pass
    
    @abstractmethod
    def save_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        entry_time: datetime,
        atr_at_entry: float,
        stop_price: float,
        take_profit_price: float = None,
        trailing_stop: float = None,
        highest_price: float = None
    ) -> bool:
        """
        포지션을 저장합니다.
        
        Returns:
            bool: 저장 성공 여부
        """
        pass
    
    @abstractmethod
    def close_position(self, symbol: str) -> bool:
        """
        포지션을 청산 상태로 변경합니다.
        
        Args:
            symbol: 종목 코드
        
        Returns:
            bool: 청산 성공 여부
        """
        pass
    
    @abstractmethod
    def update_trailing_stop(
        self,
        symbol: str,
        trailing_stop: float,
        highest_price: float
    ) -> bool:
        """
        트레일링 스탑을 업데이트합니다.
        
        Returns:
            bool: 업데이트 성공 여부
        """
        pass
    
    @abstractmethod
    def save_performance_snapshot(
        self,
        total_equity: float,
        cash: float,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        position_count: int = 0,
        snapshot_time: datetime = None
    ) -> bool:
        """
        성과 스냅샷을 저장합니다.
        
        Returns:
            bool: 저장 성공 여부
        """
        pass
    
    @abstractmethod
    def get_trades_by_date(self, trade_date: date) -> List[TradeData]:
        """
        특정 날짜의 거래 기록을 조회합니다.
        
        Args:
            trade_date: 조회 날짜
        
        Returns:
            List[TradeData]: 거래 기록 목록
        """
        pass
    
    @abstractmethod
    def get_performance_stats(self) -> Dict[str, Any]:
        """
        전체 성과 통계를 반환합니다.
        
        Returns:
            Dict: 성과 지표
        """
        pass
    
    @abstractmethod
    def calculate_mdd(self, days: int = None) -> Dict[str, Any]:
        """
        최대 낙폭(MDD)을 계산합니다.
        
        Args:
            days: 계산 기간 (None이면 전체)
        
        Returns:
            Dict: MDD 정보
        """
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """
        DB 연결 상태를 확인합니다.
        
        Returns:
            bool: 연결 상태
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# JSON 파일 기반 구현 (DB 없이 동작)
# ═══════════════════════════════════════════════════════════════════════════════

class JsonDatabase(DatabaseInterface):
    """
    JSON 파일 기반 데이터베이스
    
    ★ DB 서버 없이도 동작하는 간단한 구현
    ★ 소규모 테스트 및 DRY_RUN 모드에 적합
    ★ 대용량 데이터에는 적합하지 않음
    """
    
    def __init__(self, data_dir: Path = None):
        """
        Args:
            data_dir: 데이터 저장 디렉토리
        """
        self.data_dir = data_dir or Path(__file__).parent.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 파일 경로
        self.trades_file = self.data_dir / "trades.json"
        self.positions_file = self.data_dir / "positions.json"
        self.snapshots_file = self.data_dir / "snapshots.json"
        
        # 파일 초기화
        self._init_files()
        
        logger.info(f"[JSON_DB] JSON 데이터베이스 초기화: {self.data_dir}")
    
    def _init_files(self) -> None:
        """파일 초기화"""
        for file in [self.trades_file, self.positions_file, self.snapshots_file]:
            if not file.exists():
                file.write_text("[]")
    
    def _load_json(self, file: Path) -> List[Dict]:
        """JSON 파일 로드"""
        try:
            content = file.read_text()
            return json.loads(content) if content else []
        except Exception as e:
            logger.error(f"[JSON_DB] JSON 로드 실패: {file}, {e}")
            return []
    
    def _save_json(self, file: Path, data: List[Dict]) -> bool:
        """JSON 파일 저장"""
        try:
            file.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            return True
        except Exception as e:
            logger.error(f"[JSON_DB] JSON 저장 실패: {file}, {e}")
            return False
    
    def save_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        executed_at: datetime = None,
        reason: str = None,
        pnl: float = None,
        pnl_percent: float = None,
        entry_price: float = None,
        holding_days: int = None,
        order_no: str = None
    ) -> bool:
        """거래 기록 저장"""
        executed_at = executed_at or datetime.now()
        
        trade = TradeData(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            executed_at=executed_at,
            reason=reason,
            pnl=pnl,
            pnl_percent=pnl_percent,
            entry_price=entry_price,
            holding_days=holding_days,
            order_no=order_no
        )
        
        trades = self._load_json(self.trades_file)
        trades.append(trade.to_dict())
        
        success = self._save_json(self.trades_file, trades)
        if success:
            logger.info(f"[JSON_DB] 거래 저장: {side} {symbol} @ {price:,.0f}")
        
        return success
    
    def load_positions(self) -> List[PositionData]:
        """열린 포지션 로드"""
        data = self._load_json(self.positions_file)
        positions = []
        
        for item in data:
            if item.get("status") == "OPEN":
                try:
                    # entry_time 파싱
                    entry_time = item.get("entry_time")
                    if isinstance(entry_time, str):
                        entry_time = datetime.fromisoformat(entry_time)
                    
                    positions.append(PositionData(
                        symbol=item["symbol"],
                        entry_price=float(item["entry_price"]),
                        quantity=int(item["quantity"]),
                        entry_time=entry_time,
                        atr_at_entry=float(item["atr_at_entry"]),
                        stop_price=float(item["stop_price"]),
                        take_profit_price=float(item["take_profit_price"]) if item.get("take_profit_price") else None,
                        trailing_stop=float(item["trailing_stop"]) if item.get("trailing_stop") else None,
                        highest_price=float(item["highest_price"]) if item.get("highest_price") else None,
                        status=item.get("status", "OPEN")
                    ))
                except Exception as e:
                    logger.error(f"[JSON_DB] 포지션 파싱 오류: {e}")
        
        return positions
    
    def save_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        entry_time: datetime,
        atr_at_entry: float,
        stop_price: float,
        take_profit_price: float = None,
        trailing_stop: float = None,
        highest_price: float = None
    ) -> bool:
        """포지션 저장"""
        positions = self._load_json(self.positions_file)
        
        # 기존 포지션 확인
        for i, pos in enumerate(positions):
            if pos.get("symbol") == symbol:
                # 업데이트
                positions[i] = PositionData(
                    symbol=symbol,
                    entry_price=entry_price,
                    quantity=quantity,
                    entry_time=entry_time,
                    atr_at_entry=atr_at_entry,
                    stop_price=stop_price,
                    take_profit_price=take_profit_price,
                    trailing_stop=trailing_stop or stop_price,
                    highest_price=highest_price or entry_price,
                    status="OPEN"
                ).to_dict()
                
                success = self._save_json(self.positions_file, positions)
                if success:
                    logger.info(f"[JSON_DB] 포지션 업데이트: {symbol}")
                return success
        
        # 신규 추가
        position = PositionData(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=entry_time,
            atr_at_entry=atr_at_entry,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            trailing_stop=trailing_stop or stop_price,
            highest_price=highest_price or entry_price,
            status="OPEN"
        )
        positions.append(position.to_dict())
        
        success = self._save_json(self.positions_file, positions)
        if success:
            logger.info(f"[JSON_DB] 포지션 저장: {symbol} @ {entry_price:,.0f}")
        
        return success
    
    def close_position(self, symbol: str) -> bool:
        """포지션 청산"""
        positions = self._load_json(self.positions_file)
        
        for pos in positions:
            if pos.get("symbol") == symbol and pos.get("status") == "OPEN":
                pos["status"] = "CLOSED"
                
                success = self._save_json(self.positions_file, positions)
                if success:
                    logger.info(f"[JSON_DB] 포지션 청산: {symbol}")
                return success
        
        return False
    
    def update_trailing_stop(
        self,
        symbol: str,
        trailing_stop: float,
        highest_price: float
    ) -> bool:
        """트레일링 스탑 업데이트"""
        positions = self._load_json(self.positions_file)
        
        for pos in positions:
            if pos.get("symbol") == symbol and pos.get("status") == "OPEN":
                pos["trailing_stop"] = trailing_stop
                pos["highest_price"] = highest_price
                
                success = self._save_json(self.positions_file, positions)
                if success:
                    logger.debug(f"[JSON_DB] 트레일링 업데이트: {symbol} → {trailing_stop:,.0f}")
                return success
        
        return False
    
    def save_performance_snapshot(
        self,
        total_equity: float,
        cash: float,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        position_count: int = 0,
        snapshot_time: datetime = None
    ) -> bool:
        """성과 스냅샷 저장"""
        snapshot_time = snapshot_time or datetime.now()
        
        snapshot = PerformanceSnapshot(
            snapshot_time=snapshot_time,
            total_equity=total_equity,
            cash=cash,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            position_count=position_count
        )
        
        snapshots = self._load_json(self.snapshots_file)
        snapshots.append(snapshot.to_dict())
        
        # 최근 1000개만 유지
        if len(snapshots) > 1000:
            snapshots = snapshots[-1000:]
        
        return self._save_json(self.snapshots_file, snapshots)
    
    def get_trades_by_date(self, trade_date: date) -> List[TradeData]:
        """날짜별 거래 조회"""
        trades = self._load_json(self.trades_file)
        result = []
        
        for t in trades:
            try:
                executed = t.get("executed_at", "")
                if isinstance(executed, str):
                    executed_date = datetime.fromisoformat(executed).date()
                else:
                    executed_date = executed.date()
                
                if executed_date == trade_date:
                    result.append(TradeData(
                        symbol=t["symbol"],
                        side=t["side"],
                        price=float(t["price"]),
                        quantity=int(t["quantity"]),
                        executed_at=datetime.fromisoformat(t["executed_at"]) if isinstance(t["executed_at"], str) else t["executed_at"],
                        reason=t.get("reason"),
                        pnl=float(t["pnl"]) if t.get("pnl") else None,
                        pnl_percent=float(t["pnl_percent"]) if t.get("pnl_percent") else None,
                        entry_price=float(t["entry_price"]) if t.get("entry_price") else None,
                        holding_days=t.get("holding_days"),
                        order_no=t.get("order_no")
                    ))
            except Exception as e:
                logger.warning(f"[JSON_DB] 거래 파싱 오류: {e}")
        
        return result
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """전체 성과 통계"""
        trades = self._load_json(self.trades_file)
        
        # SELL 거래만 필터링 (청산된 거래)
        sell_trades = [t for t in trades if t.get("side") == "SELL" and t.get("pnl") is not None]
        
        if not sell_trades:
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
                "expectancy": 0.0
            }
        
        # 통계 계산
        wins = [t for t in sell_trades if float(t.get("pnl", 0)) > 0]
        losses = [t for t in sell_trades if float(t.get("pnl", 0)) < 0]
        
        total_pnl = sum(float(t.get("pnl", 0)) for t in sell_trades)
        total_wins = sum(float(t.get("pnl", 0)) for t in wins)
        total_losses = abs(sum(float(t.get("pnl", 0)) for t in losses))
        
        avg_win = total_wins / len(wins) if wins else 0
        avg_loss = -total_losses / len(losses) if losses else 0
        
        win_rate = (len(wins) / len(sell_trades)) * 100 if sell_trades else 0
        profit_factor = total_wins / total_losses if total_losses > 0 else 0
        expectancy = (win_rate/100 * avg_win) - ((1 - win_rate/100) * abs(avg_loss))
        
        return {
            "total_trades": len(sell_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "max_win": max((float(t.get("pnl", 0)) for t in wins), default=0),
            "max_loss": min((float(t.get("pnl", 0)) for t in losses), default=0),
            "profit_factor": profit_factor,
            "expectancy": expectancy
        }
    
    def calculate_mdd(self, days: int = None) -> Dict[str, Any]:
        """MDD 계산"""
        snapshots = self._load_json(self.snapshots_file)
        
        if not snapshots:
            return {"mdd": 0.0, "mdd_percent": 0.0, "peak_time": None, "trough_time": None}
        
        # 날짜 필터링
        if days:
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(days=days)
            snapshots = [
                s for s in snapshots 
                if datetime.fromisoformat(s["snapshot_time"]) >= cutoff
            ]
        
        if not snapshots:
            return {"mdd": 0.0, "mdd_percent": 0.0, "peak_time": None, "trough_time": None}
        
        # MDD 계산
        peak = 0.0
        mdd = 0.0
        mdd_percent = 0.0
        peak_time = None
        trough_time = None
        
        for s in sorted(snapshots, key=lambda x: x["snapshot_time"]):
            equity = float(s["total_equity"])
            
            if equity > peak:
                peak = equity
                peak_time = s["snapshot_time"]
            
            if peak > 0:
                drawdown = peak - equity
                drawdown_pct = (drawdown / peak) * 100
                
                if drawdown > mdd:
                    mdd = drawdown
                    mdd_percent = drawdown_pct
                    trough_time = s["snapshot_time"]
        
        return {
            "mdd": mdd,
            "mdd_percent": mdd_percent,
            "peak_time": peak_time,
            "trough_time": trough_time
        }
    
    def is_connected(self) -> bool:
        """JSON DB는 항상 연결 가능"""
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# MySQL 래퍼 구현
# ═══════════════════════════════════════════════════════════════════════════════

class MySQLDatabase(DatabaseInterface):
    """
    MySQL 데이터베이스 래퍼
    
    기존 db/mysql.py와 db/repository.py를 래핑합니다.
    """
    
    def __init__(self):
        try:
            from db.mysql import get_db_manager
            from db.repository import (
                get_trade_repository,
                get_position_repository,
                get_account_snapshot_repository
            )
            
            self.db = get_db_manager()
            self.trade_repo = get_trade_repository()
            self.position_repo = get_position_repository()
            self.snapshot_repo = get_account_snapshot_repository()
            
            logger.info("[MySQL_DB] MySQL 데이터베이스 연결됨")
        except Exception as e:
            logger.error(f"[MySQL_DB] MySQL 연결 실패: {e}")
            raise
    
    def save_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        executed_at: datetime = None,
        reason: str = None,
        pnl: float = None,
        pnl_percent: float = None,
        entry_price: float = None,
        holding_days: int = None,
        order_no: str = None
    ) -> bool:
        if side == "BUY":
            result = self.trade_repo.save_buy(
                symbol=symbol,
                price=price,
                quantity=quantity,
                executed_at=executed_at,
                order_no=order_no
            )
        else:
            result = self.trade_repo.save_sell(
                symbol=symbol,
                price=price,
                quantity=quantity,
                entry_price=entry_price,
                reason=reason,
                holding_days=holding_days,
                executed_at=executed_at,
                order_no=order_no
            )
        
        return result is not None
    
    def load_positions(self) -> List[PositionData]:
        positions = self.position_repo.get_open_positions()
        return [
            PositionData(
                symbol=p.symbol,
                entry_price=p.entry_price,
                quantity=p.quantity,
                entry_time=p.entry_time,
                atr_at_entry=p.atr_at_entry,
                stop_price=p.stop_price,
                take_profit_price=p.take_profit_price,
                trailing_stop=p.trailing_stop,
                highest_price=p.highest_price,
                status=p.status
            )
            for p in positions
        ]
    
    def save_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        entry_time: datetime,
        atr_at_entry: float,
        stop_price: float,
        take_profit_price: float = None,
        trailing_stop: float = None,
        highest_price: float = None
    ) -> bool:
        result = self.position_repo.save(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=entry_time,
            atr_at_entry=atr_at_entry,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            trailing_stop=trailing_stop,
            highest_price=highest_price
        )
        return result is not None
    
    def close_position(self, symbol: str) -> bool:
        return self.position_repo.close_position(symbol)
    
    def update_trailing_stop(
        self,
        symbol: str,
        trailing_stop: float,
        highest_price: float
    ) -> bool:
        return self.position_repo.update_trailing_stop(symbol, trailing_stop, highest_price)
    
    def save_performance_snapshot(
        self,
        total_equity: float,
        cash: float,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        position_count: int = 0,
        snapshot_time: datetime = None
    ) -> bool:
        result = self.snapshot_repo.save(
            total_equity=total_equity,
            cash=cash,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            position_count=position_count,
            snapshot_time=snapshot_time
        )
        return result is not None
    
    def get_trades_by_date(self, trade_date: date) -> List[TradeData]:
        trades = self.trade_repo.get_trades_by_date(trade_date)
        return [
            TradeData(
                symbol=t.symbol,
                side=t.side,
                price=t.price,
                quantity=t.quantity,
                executed_at=t.executed_at,
                reason=t.reason,
                pnl=t.pnl,
                pnl_percent=t.pnl_percent,
                entry_price=t.entry_price,
                holding_days=t.holding_days,
                order_no=t.order_no
            )
            for t in trades
        ]
    
    def get_performance_stats(self) -> Dict[str, Any]:
        return self.trade_repo.get_performance_stats()
    
    def calculate_mdd(self, days: int = None) -> Dict[str, Any]:
        return self.snapshot_repo.calculate_mdd(days)
    
    def is_connected(self) -> bool:
        try:
            return self.db.check_connection()
        except:
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# 팩토리 함수
# ═══════════════════════════════════════════════════════════════════════════════

_database_instance: Optional[DatabaseInterface] = None


def get_database() -> DatabaseInterface:
    """
    데이터베이스 인스턴스를 반환합니다.
    
    환경 설정에 따라 적절한 구현체를 반환합니다.
    DB 연결 실패 시 JSON 파일 기반으로 폴백합니다.
    
    Returns:
        DatabaseInterface: 데이터베이스 인스턴스
    """
    global _database_instance
    
    if _database_instance is not None:
        return _database_instance
    
    # 환경 설정 확인
    db_enabled = os.getenv("DB_ENABLED", "false").lower() in ("true", "1", "yes")
    db_type = os.getenv("DB_TYPE", "json").lower()
    
    if not db_enabled:
        logger.info("[DB] DB 비활성화 → JSON 파일 사용")
        _database_instance = JsonDatabase()
        return _database_instance
    
    # DB 유형에 따른 인스턴스 생성
    if db_type == "mysql":
        try:
            _database_instance = MySQLDatabase()
            logger.info("[DB] MySQL 데이터베이스 사용")
        except Exception as e:
            logger.warning(f"[DB] MySQL 연결 실패, JSON 폴백: {e}")
            _database_instance = JsonDatabase()
    
    elif db_type == "postgres":
        # PostgreSQL 구현 (필요시 추가)
        logger.warning("[DB] PostgreSQL 미구현, JSON 폴백")
        _database_instance = JsonDatabase()
    
    else:
        logger.info(f"[DB] 알 수 없는 DB 유형 '{db_type}' → JSON 파일 사용")
        _database_instance = JsonDatabase()
    
    return _database_instance


def reset_database() -> None:
    """데이터베이스 인스턴스 리셋 (테스트용)"""
    global _database_instance
    _database_instance = None
