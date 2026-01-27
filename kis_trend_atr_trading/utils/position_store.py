"""
KIS Trend-ATR Trading System - 포지션 영속화 모듈

포지션 정보를 파일에 저장하고 복구합니다.
프로그램 재시작 시 포지션 손실을 방지합니다.

저장 위치: data/positions.json
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# 데이터 저장 경로
DATA_DIR = Path(__file__).parent.parent / "data"
POSITION_FILE = DATA_DIR / "positions.json"


@dataclass
class StoredPosition:
    """
    저장되는 포지션 정보
    
    Attributes:
        stock_code: 종목 코드
        entry_price: 진입가
        quantity: 수량
        stop_loss: 손절가
        take_profit: 익절가
        entry_date: 진입일
        atr_at_entry: 진입 시 ATR
        saved_at: 저장 시간
    """
    stock_code: str
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float
    entry_date: str
    atr_at_entry: float
    saved_at: str = ""
    
    def __post_init__(self):
        if not self.saved_at:
            self.saved_at = datetime.now().isoformat()


class PositionStore:
    """
    포지션 영속화 클래스
    
    포지션 정보를 JSON 파일에 저장하고 복구합니다.
    """
    
    def __init__(self, file_path: Path = None):
        """
        PositionStore 초기화
        
        Args:
            file_path: 저장 파일 경로 (None이면 기본 경로)
        """
        self.file_path = file_path or POSITION_FILE
        self._ensure_data_dir()
    
    def _ensure_data_dir(self) -> None:
        """데이터 디렉토리를 생성합니다."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
    
    def save_position(self, position: StoredPosition) -> bool:
        """
        포지션을 파일에 저장합니다.
        
        Args:
            position: 저장할 포지션
        
        Returns:
            bool: 저장 성공 여부
        """
        try:
            data = {
                "position": asdict(position),
                "version": "1.0",
                "updated_at": datetime.now().isoformat()
            }
            
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"포지션 저장 완료: {position.stock_code}")
            return True
            
        except Exception as e:
            logger.error(f"포지션 저장 실패: {e}")
            return False
    
    def load_position(self) -> Optional[StoredPosition]:
        """
        저장된 포지션을 불러옵니다.
        
        Returns:
            Optional[StoredPosition]: 저장된 포지션 (없으면 None)
        """
        if not self.file_path.exists():
            logger.debug("저장된 포지션 없음")
            return None
        
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            position_data = data.get("position")
            if not position_data:
                return None
            
            position = StoredPosition(**position_data)
            logger.info(f"포지션 로드 완료: {position.stock_code}")
            return position
            
        except Exception as e:
            logger.error(f"포지션 로드 실패: {e}")
            return None
    
    def clear_position(self) -> bool:
        """
        저장된 포지션을 삭제합니다.
        
        Returns:
            bool: 삭제 성공 여부
        """
        try:
            if self.file_path.exists():
                # 완전 삭제 대신 빈 데이터로 덮어쓰기 (히스토리 보존)
                data = {
                    "position": None,
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "cleared_at": datetime.now().isoformat()
                }
                
                with open(self.file_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                logger.info("포지션 정보 삭제 완료")
            return True
            
        except Exception as e:
            logger.error(f"포지션 삭제 실패: {e}")
            return False
    
    def has_position(self) -> bool:
        """
        저장된 포지션이 있는지 확인합니다.
        
        Returns:
            bool: 포지션 존재 여부
        """
        position = self.load_position()
        return position is not None


class DailyTradeStore:
    """
    일일 거래 기록 저장 클래스
    
    일일 손실 한도 체크를 위해 당일 거래 기록을 저장합니다.
    """
    
    def __init__(self, file_path: Path = None):
        """
        DailyTradeStore 초기화
        
        Args:
            file_path: 저장 파일 경로
        """
        self.file_path = file_path or (DATA_DIR / "daily_trades.json")
        self._ensure_data_dir()
    
    def _ensure_data_dir(self) -> None:
        """데이터 디렉토리를 생성합니다."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _get_today_str(self) -> str:
        """오늘 날짜 문자열을 반환합니다."""
        return datetime.now().strftime("%Y-%m-%d")
    
    def save_trade(self, trade: Dict[str, Any]) -> bool:
        """
        거래 기록을 저장합니다.
        
        Args:
            trade: 거래 정보
        
        Returns:
            bool: 저장 성공 여부
        """
        try:
            data = self._load_data()
            today = self._get_today_str()
            
            if today not in data:
                data[today] = {
                    "trades": [],
                    "total_pnl": 0.0,
                    "consecutive_losses": 0
                }
            
            data[today]["trades"].append(trade)
            
            # 손익 업데이트
            pnl = trade.get("pnl", 0)
            data[today]["total_pnl"] += pnl
            
            # 연속 손실 카운트
            if pnl < 0:
                data[today]["consecutive_losses"] += 1
            else:
                data[today]["consecutive_losses"] = 0
            
            self._save_data(data)
            return True
            
        except Exception as e:
            logger.error(f"거래 기록 저장 실패: {e}")
            return False
    
    def get_daily_stats(self) -> Dict[str, Any]:
        """
        당일 거래 통계를 반환합니다.
        
        Returns:
            Dict: 거래 통계
        """
        data = self._load_data()
        today = self._get_today_str()
        
        if today not in data:
            return {
                "trade_count": 0,
                "total_pnl": 0.0,
                "total_pnl_pct": 0.0,
                "consecutive_losses": 0,
                "trades": []
            }
        
        today_data = data[today]
        trades = today_data.get("trades", [])
        
        # 손익률 계산 (진입금액 기준)
        total_entry_value = sum(
            t.get("entry_price", 0) * t.get("quantity", 0) 
            for t in trades 
            if t.get("type") == "BUY"
        )
        
        total_pnl_pct = 0.0
        if total_entry_value > 0:
            total_pnl_pct = (today_data.get("total_pnl", 0) / total_entry_value) * 100
        
        return {
            "trade_count": len(trades),
            "total_pnl": today_data.get("total_pnl", 0),
            "total_pnl_pct": total_pnl_pct,
            "consecutive_losses": today_data.get("consecutive_losses", 0),
            "trades": trades
        }
    
    def is_daily_limit_reached(
        self, 
        max_loss_pct: float, 
        max_trades: int,
        max_consecutive_losses: int
    ) -> tuple[bool, str]:
        """
        일일 한도에 도달했는지 확인합니다.
        
        Args:
            max_loss_pct: 최대 손실률 (%)
            max_trades: 최대 거래 횟수
            max_consecutive_losses: 최대 연속 손실 횟수
        
        Returns:
            tuple[bool, str]: (한도 도달 여부, 사유)
        """
        stats = self.get_daily_stats()
        
        # 손실 한도 체크
        if stats["total_pnl_pct"] <= -max_loss_pct:
            return True, f"일일 손실 한도 도달 ({stats['total_pnl_pct']:.2f}% <= -{max_loss_pct}%)"
        
        # 거래 횟수 체크
        if stats["trade_count"] >= max_trades:
            return True, f"일일 거래 횟수 한도 도달 ({stats['trade_count']} >= {max_trades})"
        
        # 연속 손실 체크
        if stats["consecutive_losses"] >= max_consecutive_losses:
            return True, f"연속 손실 한도 도달 ({stats['consecutive_losses']} >= {max_consecutive_losses})"
        
        return False, ""
    
    def clear_today(self) -> bool:
        """당일 기록을 초기화합니다."""
        try:
            data = self._load_data()
            today = self._get_today_str()
            
            if today in data:
                del data[today]
                self._save_data(data)
            
            return True
        except Exception as e:
            logger.error(f"당일 기록 초기화 실패: {e}")
            return False
    
    def _load_data(self) -> Dict:
        """데이터 파일을 로드합니다."""
        if not self.file_path.exists():
            return {}
        
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    
    def _save_data(self, data: Dict) -> None:
        """데이터를 파일에 저장합니다."""
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════════
# 편의 함수
# ════════════════════════════════════════════════════════════════

_position_store: Optional[PositionStore] = None
_daily_trade_store: Optional[DailyTradeStore] = None


def get_position_store() -> PositionStore:
    """싱글톤 PositionStore를 반환합니다."""
    global _position_store
    if _position_store is None:
        _position_store = PositionStore()
    return _position_store


def get_daily_trade_store() -> DailyTradeStore:
    """싱글톤 DailyTradeStore를 반환합니다."""
    global _daily_trade_store
    if _daily_trade_store is None:
        _daily_trade_store = DailyTradeStore()
    return _daily_trade_store
