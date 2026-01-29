"""
KIS Trend-ATR Trading System - CBT Trade Log 저장소

이 모듈은 CBT 모드에서 거래 기록을 JSON 또는 SQLite에 저장합니다.

저장 필드:
    - entry_price: 진입가
    - exit_price: 청산가
    - quantity: 수량
    - pnl: 손익 (원)
    - return_pct: 수익률 (%)
    - holding_days: 보유일수
    - exit_reason: 청산 사유 (ATR_STOP, TREND_BROKEN, TAKE_PROFIT 등)

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field
from enum import Enum
import threading

from config import settings
from utils.logger import get_logger

logger = get_logger("cbt_trade_store")


class ExitReason(Enum):
    """청산 사유 열거형"""
    ATR_STOP = "ATR_STOP"            # ATR 기반 손절
    TRAILING_STOP = "TRAILING_STOP"  # 트레일링 스탑
    TAKE_PROFIT = "TAKE_PROFIT"      # 목표가 익절
    TREND_BROKEN = "TREND_BROKEN"    # 추세 이탈
    GAP_PROTECTION = "GAP_PROTECTION"  # 갭 보호
    MANUAL = "MANUAL"                # 수동 청산
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"  # 일일 손실 한도
    KILL_SWITCH = "KILL_SWITCH"      # 킬 스위치
    OTHER = "OTHER"                  # 기타


@dataclass
class Trade:
    """
    거래 기록 데이터 클래스
    
    Attributes:
        trade_id: 거래 고유 ID
        stock_code: 종목 코드
        entry_date: 진입일시
        exit_date: 청산일시
        entry_price: 진입가
        exit_price: 청산가
        quantity: 수량
        gross_pnl: 총손익 (수수료 전)
        commission: 수수료
        pnl: 순손익
        return_pct: 수익률 (%)
        holding_days: 보유일수
        exit_reason: 청산 사유
        atr_at_entry: 진입 시 ATR
        stop_loss: 손절가
        take_profit: 익절가
        highest_price: 보유 중 최고가
    """
    trade_id: str
    stock_code: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    commission: float
    pnl: float
    return_pct: float
    holding_days: int
    exit_reason: str
    atr_at_entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    highest_price: float = 0.0
    
    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Trade":
        """딕셔너리에서 생성"""
        return cls(**data)
    
    def is_winner(self) -> bool:
        """수익 거래 여부"""
        return self.pnl > 0
    
    def is_loser(self) -> bool:
        """손실 거래 여부"""
        return self.pnl < 0


class TradeStore:
    """
    CBT 거래 기록 저장소
    
    JSON 또는 SQLite 형식으로 거래 기록을 저장/조회합니다.
    
    Usage:
        store = TradeStore()
        
        # 거래 기록 추가
        trade = Trade(
            trade_id="CBT20240101120000",
            stock_code="005930",
            entry_date="2024-01-01 09:30:00",
            exit_date="2024-01-02 15:00:00",
            entry_price=70000,
            exit_price=72000,
            quantity=10,
            gross_pnl=20000,
            commission=30,
            pnl=19970,
            return_pct=2.85,
            holding_days=2,
            exit_reason="TAKE_PROFIT"
        )
        store.add_trade(trade)
        
        # 모든 거래 조회
        trades = store.get_all_trades()
        
        # 기간별 조회
        trades = store.get_trades_by_date("2024-01-01", "2024-01-31")
    """
    
    def __init__(
        self,
        storage_type: str = None,
        data_dir: Path = None
    ):
        """
        거래 저장소 초기화
        
        Args:
            storage_type: 저장 방식 ("json" 또는 "sqlite")
            data_dir: 데이터 저장 디렉토리
        """
        self.storage_type = storage_type or settings.CBT_STORAGE_TYPE
        self.data_dir = data_dir or settings.CBT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._lock = threading.Lock()
        
        if self.storage_type == "sqlite":
            self._db_file = self.data_dir / "cbt_trades.db"
            self._init_sqlite_db()
        else:
            self._json_file = self.data_dir / "cbt_trades.json"
            self._init_json_file()
        
        logger.info(f"[CBT] Trade Store 초기화: {self.storage_type.upper()}")
    
    # ════════════════════════════════════════════════════════════════
    # JSON 저장소
    # ════════════════════════════════════════════════════════════════
    
    def _init_json_file(self) -> None:
        """JSON 파일 초기화"""
        if not self._json_file.exists():
            with open(self._json_file, "w", encoding="utf-8") as f:
                json.dump({"trades": []}, f, ensure_ascii=False, indent=2)
    
    def _load_json(self) -> Dict:
        """JSON 파일 로드"""
        with open(self._json_file, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def _save_json(self, data: Dict) -> None:
        """JSON 파일 저장"""
        with open(self._json_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    # ════════════════════════════════════════════════════════════════
    # SQLite 저장소
    # ════════════════════════════════════════════════════════════════
    
    def _init_sqlite_db(self) -> None:
        """SQLite DB 초기화"""
        conn = sqlite3.connect(self._db_file)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                stock_code TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                exit_date TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                gross_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                pnl REAL NOT NULL,
                return_pct REAL NOT NULL,
                holding_days INTEGER NOT NULL,
                exit_reason TEXT NOT NULL,
                atr_at_entry REAL DEFAULT 0,
                stop_loss REAL DEFAULT 0,
                take_profit REAL DEFAULT 0,
                highest_price REAL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 인덱스 생성
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_date ON trades(exit_date)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock_code ON trades(stock_code)
        """)
        
        conn.commit()
        conn.close()
    
    def _get_sqlite_connection(self) -> sqlite3.Connection:
        """SQLite 연결 반환"""
        conn = sqlite3.connect(self._db_file)
        conn.row_factory = sqlite3.Row
        return conn
    
    # ════════════════════════════════════════════════════════════════
    # 거래 기록 추가
    # ════════════════════════════════════════════════════════════════
    
    def add_trade(self, trade: Trade) -> bool:
        """
        거래 기록 추가
        
        Args:
            trade: Trade 객체
        
        Returns:
            bool: 성공 여부
        """
        with self._lock:
            try:
                if self.storage_type == "sqlite":
                    return self._add_trade_sqlite(trade)
                else:
                    return self._add_trade_json(trade)
            except Exception as e:
                logger.error(f"[CBT] 거래 기록 추가 실패: {e}")
                return False
    
    def _add_trade_json(self, trade: Trade) -> bool:
        """JSON에 거래 추가"""
        data = self._load_json()
        data["trades"].append(trade.to_dict())
        self._save_json(data)
        logger.debug(f"[CBT] 거래 기록 추가 (JSON): {trade.trade_id}")
        return True
    
    def _add_trade_sqlite(self, trade: Trade) -> bool:
        """SQLite에 거래 추가"""
        conn = self._get_sqlite_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO trades (
                trade_id, stock_code, entry_date, exit_date,
                entry_price, exit_price, quantity,
                gross_pnl, commission, pnl, return_pct,
                holding_days, exit_reason,
                atr_at_entry, stop_loss, take_profit, highest_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.trade_id, trade.stock_code, trade.entry_date, trade.exit_date,
            trade.entry_price, trade.exit_price, trade.quantity,
            trade.gross_pnl, trade.commission, trade.pnl, trade.return_pct,
            trade.holding_days, trade.exit_reason,
            trade.atr_at_entry, trade.stop_loss, trade.take_profit, trade.highest_price
        ))
        
        conn.commit()
        conn.close()
        
        logger.debug(f"[CBT] 거래 기록 추가 (SQLite): {trade.trade_id}")
        return True
    
    def add_trade_from_result(self, result: Dict) -> Optional[Trade]:
        """
        execute_sell 결과로 거래 기록 생성 및 추가
        
        Args:
            result: VirtualAccount.execute_sell() 반환값
        
        Returns:
            Trade: 생성된 Trade 객체 (실패 시 None)
        """
        if not result.get("success"):
            return None
        
        trade = Trade(
            trade_id=result.get("order_no", f"CBT{datetime.now().strftime('%Y%m%d%H%M%S')}"),
            stock_code=result.get("stock_code", ""),
            entry_date=result.get("entry_date", ""),
            exit_date=result.get("exit_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            entry_price=result.get("entry_price", 0),
            exit_price=result.get("exit_price", 0),
            quantity=result.get("quantity", 0),
            gross_pnl=result.get("gross_pnl", 0),
            commission=result.get("commission", 0),
            pnl=result.get("net_pnl", 0),
            return_pct=result.get("return_pct", 0),
            holding_days=result.get("holding_days", 0),
            exit_reason=result.get("exit_reason", "OTHER"),
            atr_at_entry=result.get("atr_at_entry", 0),
            stop_loss=result.get("stop_loss", 0),
            take_profit=result.get("take_profit", 0),
            highest_price=result.get("highest_price", 0)
        )
        
        self.add_trade(trade)
        return trade
    
    # ════════════════════════════════════════════════════════════════
    # 거래 기록 조회
    # ════════════════════════════════════════════════════════════════
    
    def get_all_trades(self) -> List[Trade]:
        """모든 거래 기록 조회"""
        with self._lock:
            if self.storage_type == "sqlite":
                return self._get_all_trades_sqlite()
            else:
                return self._get_all_trades_json()
    
    def _get_all_trades_json(self) -> List[Trade]:
        """JSON에서 모든 거래 조회"""
        data = self._load_json()
        return [Trade.from_dict(t) for t in data.get("trades", [])]
    
    def _get_all_trades_sqlite(self) -> List[Trade]:
        """SQLite에서 모든 거래 조회"""
        conn = self._get_sqlite_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM trades ORDER BY exit_date DESC
        """)
        
        rows = cursor.fetchall()
        conn.close()
        
        return [self._row_to_trade(row) for row in rows]
    
    def get_trades_by_date(
        self,
        start_date: str,
        end_date: str = None
    ) -> List[Trade]:
        """
        기간별 거래 조회
        
        Args:
            start_date: 시작일 (YYYY-MM-DD)
            end_date: 종료일 (YYYY-MM-DD, 미입력 시 오늘)
        
        Returns:
            List[Trade]: 거래 목록
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        
        # 시간 범위 확장
        start_dt = f"{start_date} 00:00:00"
        end_dt = f"{end_date} 23:59:59"
        
        with self._lock:
            if self.storage_type == "sqlite":
                return self._get_trades_by_date_sqlite(start_dt, end_dt)
            else:
                return self._get_trades_by_date_json(start_dt, end_dt)
    
    def _get_trades_by_date_json(self, start_dt: str, end_dt: str) -> List[Trade]:
        """JSON에서 기간별 거래 조회"""
        all_trades = self._get_all_trades_json()
        return [
            t for t in all_trades
            if start_dt <= t.exit_date <= end_dt
        ]
    
    def _get_trades_by_date_sqlite(self, start_dt: str, end_dt: str) -> List[Trade]:
        """SQLite에서 기간별 거래 조회"""
        conn = self._get_sqlite_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM trades 
            WHERE exit_date >= ? AND exit_date <= ?
            ORDER BY exit_date DESC
        """, (start_dt, end_dt))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [self._row_to_trade(row) for row in rows]
    
    def get_trades_by_stock(self, stock_code: str) -> List[Trade]:
        """종목별 거래 조회"""
        with self._lock:
            if self.storage_type == "sqlite":
                conn = self._get_sqlite_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM trades WHERE stock_code = ? ORDER BY exit_date DESC",
                    (stock_code,)
                )
                rows = cursor.fetchall()
                conn.close()
                return [self._row_to_trade(row) for row in rows]
            else:
                all_trades = self._get_all_trades_json()
                return [t for t in all_trades if t.stock_code == stock_code]
    
    def get_recent_trades(self, count: int = 10) -> List[Trade]:
        """최근 거래 조회"""
        with self._lock:
            if self.storage_type == "sqlite":
                conn = self._get_sqlite_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM trades ORDER BY exit_date DESC LIMIT ?",
                    (count,)
                )
                rows = cursor.fetchall()
                conn.close()
                return [self._row_to_trade(row) for row in rows]
            else:
                all_trades = self._get_all_trades_json()
                sorted_trades = sorted(
                    all_trades, 
                    key=lambda t: t.exit_date, 
                    reverse=True
                )
                return sorted_trades[:count]
    
    def _row_to_trade(self, row: sqlite3.Row) -> Trade:
        """SQLite Row를 Trade 객체로 변환"""
        return Trade(
            trade_id=row["trade_id"],
            stock_code=row["stock_code"],
            entry_date=row["entry_date"],
            exit_date=row["exit_date"],
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            quantity=row["quantity"],
            gross_pnl=row["gross_pnl"],
            commission=row["commission"],
            pnl=row["pnl"],
            return_pct=row["return_pct"],
            holding_days=row["holding_days"],
            exit_reason=row["exit_reason"],
            atr_at_entry=row["atr_at_entry"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            highest_price=row["highest_price"]
        )
    
    # ════════════════════════════════════════════════════════════════
    # 통계 조회
    # ════════════════════════════════════════════════════════════════
    
    def get_trade_count(self) -> int:
        """총 거래 횟수"""
        trades = self.get_all_trades()
        return len(trades)
    
    def get_summary_stats(self) -> Dict:
        """
        거래 요약 통계
        
        Returns:
            Dict: 통계 요약
        """
        trades = self.get_all_trades()
        
        if not trades:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0,
                "total_commission": 0,
                "win_rate": 0,
                "avg_pnl": 0,
                "avg_return_pct": 0,
                "max_pnl": 0,
                "min_pnl": 0,
                "avg_holding_days": 0
            }
        
        winning = [t for t in trades if t.is_winner()]
        losing = [t for t in trades if t.is_loser()]
        
        total_pnl = sum(t.pnl for t in trades)
        total_commission = sum(t.commission for t in trades)
        
        return {
            "total_trades": len(trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "total_pnl": total_pnl,
            "total_commission": total_commission,
            "win_rate": len(winning) / len(trades) * 100 if trades else 0,
            "avg_pnl": total_pnl / len(trades) if trades else 0,
            "avg_return_pct": sum(t.return_pct for t in trades) / len(trades) if trades else 0,
            "max_pnl": max(t.pnl for t in trades) if trades else 0,
            "min_pnl": min(t.pnl for t in trades) if trades else 0,
            "avg_holding_days": sum(t.holding_days for t in trades) / len(trades) if trades else 0
        }
    
    # ════════════════════════════════════════════════════════════════
    # 데이터 관리
    # ════════════════════════════════════════════════════════════════
    
    def clear_all_trades(self) -> bool:
        """모든 거래 기록 삭제 (주의!)"""
        with self._lock:
            try:
                if self.storage_type == "sqlite":
                    conn = self._get_sqlite_connection()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM trades")
                    conn.commit()
                    conn.close()
                else:
                    self._save_json({"trades": []})
                
                logger.warning("[CBT] 모든 거래 기록 삭제됨")
                return True
            except Exception as e:
                logger.error(f"[CBT] 거래 기록 삭제 실패: {e}")
                return False
    
    def export_to_csv(self, filepath: Path = None) -> str:
        """
        거래 기록을 CSV로 내보내기
        
        Args:
            filepath: 저장 경로 (미입력 시 자동 생성)
        
        Returns:
            str: 저장된 파일 경로
        """
        import csv
        
        if filepath is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.data_dir / f"cbt_trades_export_{timestamp}.csv"
        
        trades = self.get_all_trades()
        
        if not trades:
            logger.warning("[CBT] 내보낼 거래 기록이 없습니다.")
            return ""
        
        fieldnames = [
            "trade_id", "stock_code", "entry_date", "exit_date",
            "entry_price", "exit_price", "quantity",
            "gross_pnl", "commission", "pnl", "return_pct",
            "holding_days", "exit_reason",
            "atr_at_entry", "stop_loss", "take_profit", "highest_price"
        ]
        
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for trade in trades:
                writer.writerow(trade.to_dict())
        
        logger.info(f"[CBT] 거래 기록 CSV 내보내기 완료: {filepath}")
        return str(filepath)
