"""
KIS Trend-ATR Trading System - 성과 추적기

═══════════════════════════════════════════════════════════════════════════════
⚠️ 이 모듈은 DRY_RUN 모드에서도 완전하게 동작합니다.
═══════════════════════════════════════════════════════════════════════════════

★ 핵심 기능:
  - 거래 기록 및 저장
  - 실시간 손익 계산
  - 성과 지표 산출 (승률, MDD, Profit Factor 등)
  - 일일/월별 리포트 생성

★ 지원 데이터 소스:
  - JSON 파일 (기본)
  - MySQL 데이터베이스

사용 예시:
    from performance import get_performance_tracker
    
    tracker = get_performance_tracker()
    
    # 매수 기록
    tracker.record_buy(
        symbol="005930",
        price=70000,
        quantity=10,
        atr=1500,
        stop_price=67000,
        take_profit=75000
    )
    
    # 매도 기록
    tracker.record_sell(
        symbol="005930",
        price=72000,
        quantity=10,
        reason="TAKE_PROFIT"
    )
    
    # 성과 요약
    summary = tracker.get_summary()

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

from performance.trade_record import TradeRecord, DailyTradeStats
from performance.position_snapshot import PositionSnapshot, AccountSnapshot
from utils.logger import get_logger

logger = get_logger("performance_tracker")


@dataclass
class PerformanceSummary:
    """성과 요약 데이터 클래스"""
    # 기본 통계
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    
    # 손익
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    
    # 평균
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_holding_days: float = 0.0
    
    # 최대/최소
    max_win: float = 0.0
    max_loss: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    
    # 성과 지표
    profit_factor: float = 0.0
    expectancy: float = 0.0
    
    # 자본금
    initial_capital: float = 0.0
    current_equity: float = 0.0
    total_return_pct: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "total_trades": self.total_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": round(self.win_rate, 2),
            "total_pnl": round(self.total_pnl, 0),
            "realized_pnl": round(self.realized_pnl, 0),
            "unrealized_pnl": round(self.unrealized_pnl, 0),
            "avg_win": round(self.avg_win, 0),
            "avg_loss": round(self.avg_loss, 0),
            "avg_holding_days": round(self.avg_holding_days, 1),
            "max_win": round(self.max_win, 0),
            "max_loss": round(self.max_loss, 0),
            "max_drawdown": round(self.max_drawdown, 0),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 0),
            "initial_capital": round(self.initial_capital, 0),
            "current_equity": round(self.current_equity, 0),
            "total_return_pct": round(self.total_return_pct, 2)
        }


class PerformanceTracker:
    """
    성과 추적기 클래스
    
    ★ DRY_RUN, PAPER, REAL 모든 모드에서 동작
    ★ 가상 체결도 실제 체결과 동일하게 기록
    """
    
    def __init__(
        self,
        data_dir: Path = None,
        initial_capital: float = 10_000_000,
        commission_rate: float = 0.00015
    ):
        """
        Args:
            data_dir: 데이터 저장 디렉토리
            initial_capital: 초기 자본금
            commission_rate: 수수료율
        """
        self.data_dir = data_dir or Path(__file__).parent.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        
        # 파일 경로
        self.trades_file = self.data_dir / "performance_trades.json"
        self.snapshots_file = self.data_dir / "performance_snapshots.json"
        self.equity_file = self.data_dir / "equity_curve.json"
        
        # 메모리 캐시
        self._trades: List[TradeRecord] = []
        self._positions: Dict[str, PositionSnapshot] = {}
        self._equity_curve: List[Dict] = []
        self._realized_pnl: float = 0.0
        
        # 데이터 로드
        self._load_data()
        
        logger.info(
            f"[PERF] 성과 추적기 초기화: "
            f"초기자본 {initial_capital:,.0f}원, "
            f"수수료율 {commission_rate*100:.3f}%"
        )
    
    def _load_data(self) -> None:
        """저장된 데이터 로드"""
        # 거래 기록 로드
        if self.trades_file.exists():
            try:
                data = json.loads(self.trades_file.read_text())
                self._trades = [TradeRecord.from_dict(t) for t in data]
                # 실현 손익 계산
                self._realized_pnl = sum(
                    t.pnl or 0 
                    for t in self._trades 
                    if t.side == "SELL"
                )
                logger.info(f"[PERF] {len(self._trades)}개 거래 기록 로드")
            except Exception as e:
                logger.warning(f"[PERF] 거래 기록 로드 실패: {e}")
        
        # Equity Curve 로드
        if self.equity_file.exists():
            try:
                self._equity_curve = json.loads(self.equity_file.read_text())
            except Exception as e:
                logger.warning(f"[PERF] Equity Curve 로드 실패: {e}")
    
    def _save_trades(self) -> None:
        """거래 기록 저장"""
        try:
            data = [t.to_dict() for t in self._trades]
            self.trades_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str)
            )
        except Exception as e:
            logger.error(f"[PERF] 거래 기록 저장 실패: {e}")
    
    def _save_equity_curve(self) -> None:
        """Equity Curve 저장"""
        try:
            # 최근 1000개만 유지
            if len(self._equity_curve) > 1000:
                self._equity_curve = self._equity_curve[-1000:]
            
            self.equity_file.write_text(
                json.dumps(self._equity_curve, ensure_ascii=False, indent=2, default=str)
            )
        except Exception as e:
            logger.error(f"[PERF] Equity Curve 저장 실패: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 거래 기록 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def record_buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        atr: float = None,
        stop_price: float = None,
        take_profit: float = None,
        is_virtual: bool = True,
        order_no: str = None,
        mode: str = "DRY_RUN"
    ) -> TradeRecord:
        """
        매수 기록
        
        Args:
            symbol: 종목 코드
            price: 체결가
            quantity: 수량
            atr: ATR 값
            stop_price: 손절가
            take_profit: 익절가
            is_virtual: 가상 체결 여부
            order_no: 주문 번호
            mode: 실행 모드
        
        Returns:
            TradeRecord: 거래 기록
        """
        # 수수료 적용
        commission = price * quantity * self.commission_rate
        
        trade = TradeRecord(
            symbol=symbol,
            side="BUY",
            price=price,
            quantity=quantity,
            executed_at=datetime.now(),
            is_virtual=is_virtual,
            order_no=order_no,
            mode=mode,
            atr_at_entry=atr,
            stop_price=stop_price,
            take_profit_price=take_profit
        )
        
        self._trades.append(trade)
        
        # 포지션 추가
        self._positions[symbol] = PositionSnapshot(
            symbol=symbol,
            entry_price=price,
            current_price=price,
            quantity=quantity,
            entry_time=datetime.now(),
            atr_at_entry=atr,
            stop_price=stop_price,
            take_profit_price=take_profit,
            trailing_stop=stop_price,
            highest_price=price
        )
        
        self._save_trades()
        
        logger.info(
            f"[PERF] 매수 기록: {symbol} @ {price:,.0f}원 x {quantity}주 "
            f"({'가상' if is_virtual else '실제'})"
        )
        
        return trade
    
    def record_sell(
        self,
        symbol: str,
        price: float,
        quantity: int,
        reason: str = None,
        is_virtual: bool = True,
        order_no: str = None,
        mode: str = "DRY_RUN"
    ) -> Optional[TradeRecord]:
        """
        매도 기록
        
        Args:
            symbol: 종목 코드
            price: 체결가
            quantity: 수량
            reason: 청산 사유
            is_virtual: 가상 체결 여부
            order_no: 주문 번호
            mode: 실행 모드
        
        Returns:
            TradeRecord: 거래 기록 (포지션 없으면 None)
        """
        position = self._positions.get(symbol)
        if not position:
            logger.warning(f"[PERF] 포지션 없음: {symbol}")
            return None
        
        # 손익 계산
        entry_price = position.entry_price
        pnl = (price - entry_price) * quantity
        pnl_percent = ((price - entry_price) / entry_price) * 100
        
        # 수수료 적용
        commission = price * quantity * self.commission_rate
        pnl -= commission
        
        # 보유 일수
        holding_days = (datetime.now().date() - position.entry_time.date()).days
        
        trade = TradeRecord(
            symbol=symbol,
            side="SELL",
            price=price,
            quantity=quantity,
            executed_at=datetime.now(),
            is_virtual=is_virtual,
            reason=reason,
            entry_price=entry_price,
            pnl=pnl,
            pnl_percent=pnl_percent,
            holding_days=holding_days,
            order_no=order_no,
            mode=mode,
            atr_at_entry=position.atr_at_entry,
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price
        )
        
        self._trades.append(trade)
        self._realized_pnl += pnl
        
        # 포지션 제거
        del self._positions[symbol]
        
        self._save_trades()
        
        logger.info(
            f"[PERF] 매도 기록: {symbol} @ {price:,.0f}원 x {quantity}주 | "
            f"손익: {pnl:+,.0f}원 ({pnl_percent:+.2f}%) | {reason or ''} "
            f"({'가상' if is_virtual else '실제'})"
        )
        
        return trade
    
    def update_position_price(self, symbol: str, current_price: float) -> None:
        """포지션 현재가 업데이트"""
        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.current_price = current_price
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
            pos.unrealized_pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
            pos.snapshot_time = datetime.now()
            
            # 최고가 갱신
            if current_price > (pos.highest_price or 0):
                pos.highest_price = current_price
    
    def record_equity_snapshot(self) -> None:
        """현재 자산 상태 스냅샷 기록"""
        unrealized = sum(
            p.unrealized_pnl 
            for p in self._positions.values()
        )
        
        total_equity = self.initial_capital + self._realized_pnl + unrealized
        
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "total_equity": total_equity,
            "realized_pnl": self._realized_pnl,
            "unrealized_pnl": unrealized,
            "position_count": len(self._positions)
        }
        
        self._equity_curve.append(snapshot)
        self._save_equity_curve()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 성과 조회 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_summary(self) -> PerformanceSummary:
        """전체 성과 요약 반환"""
        summary = PerformanceSummary(initial_capital=self.initial_capital)
        
        # SELL 거래 필터링
        sell_trades = [t for t in self._trades if t.side == "SELL" and t.pnl is not None]
        
        if not sell_trades:
            summary.current_equity = self.initial_capital
            return summary
        
        # 기본 통계
        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl < 0]
        
        summary.total_trades = len(sell_trades)
        summary.win_count = len(wins)
        summary.loss_count = len(losses)
        summary.win_rate = (len(wins) / len(sell_trades)) * 100 if sell_trades else 0
        
        # 손익
        summary.realized_pnl = self._realized_pnl
        summary.unrealized_pnl = sum(p.unrealized_pnl for p in self._positions.values())
        summary.total_pnl = summary.realized_pnl + summary.unrealized_pnl
        
        # 평균
        if wins:
            summary.avg_win = sum(t.pnl for t in wins) / len(wins)
            summary.max_win = max(t.pnl for t in wins)
        
        if losses:
            summary.avg_loss = sum(t.pnl for t in losses) / len(losses)
            summary.max_loss = min(t.pnl for t in losses)
        
        # 평균 보유 일수
        holding_days = [t.holding_days for t in sell_trades if t.holding_days]
        if holding_days:
            summary.avg_holding_days = sum(holding_days) / len(holding_days)
        
        # Profit Factor
        total_wins = sum(t.pnl for t in wins) if wins else 0
        total_losses = abs(sum(t.pnl for t in losses)) if losses else 0
        summary.profit_factor = total_wins / total_losses if total_losses > 0 else 0
        
        # Expectancy
        summary.expectancy = (
            (summary.win_rate / 100 * summary.avg_win) - 
            ((1 - summary.win_rate / 100) * abs(summary.avg_loss))
        )
        
        # MDD 계산
        mdd_info = self.calculate_mdd()
        summary.max_drawdown = mdd_info.get("mdd", 0)
        summary.max_drawdown_pct = mdd_info.get("mdd_percent", 0)
        
        # 현재 자산
        summary.current_equity = self.initial_capital + summary.total_pnl
        summary.total_return_pct = (summary.total_pnl / self.initial_capital) * 100
        
        return summary
    
    def get_daily_stats(self, trade_date: date = None) -> DailyTradeStats:
        """일별 거래 통계"""
        trade_date = trade_date or date.today()
        
        stats = DailyTradeStats(trade_date=trade_date.isoformat())
        
        day_trades = [
            t for t in self._trades 
            if t.executed_at.date() == trade_date
        ]
        
        stats.total_trades = len(day_trades)
        stats.buy_count = sum(1 for t in day_trades if t.side == "BUY")
        stats.sell_count = sum(1 for t in day_trades if t.side == "SELL")
        
        sells = [t for t in day_trades if t.side == "SELL" and t.pnl is not None]
        stats.win_count = sum(1 for t in sells if t.pnl > 0)
        stats.loss_count = sum(1 for t in sells if t.pnl < 0)
        stats.total_pnl = sum(t.pnl for t in sells if t.pnl)
        
        if sells:
            profits = [t.pnl for t in sells if t.pnl and t.pnl > 0]
            losses = [t.pnl for t in sells if t.pnl and t.pnl < 0]
            stats.max_profit = max(profits) if profits else 0
            stats.max_loss = min(losses) if losses else 0
        
        return stats
    
    def get_trades_by_symbol(self, symbol: str) -> List[TradeRecord]:
        """종목별 거래 기록"""
        return [t for t in self._trades if t.symbol == symbol]
    
    def calculate_mdd(self, days: int = None) -> Dict[str, Any]:
        """MDD 계산"""
        if not self._equity_curve:
            return {"mdd": 0.0, "mdd_percent": 0.0}
        
        # 날짜 필터링
        equity_data = self._equity_curve
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            equity_data = [
                e for e in equity_data 
                if datetime.fromisoformat(e["timestamp"]) >= cutoff
            ]
        
        if not equity_data:
            return {"mdd": 0.0, "mdd_percent": 0.0}
        
        # MDD 계산
        peak = 0.0
        mdd = 0.0
        mdd_percent = 0.0
        
        for e in sorted(equity_data, key=lambda x: x["timestamp"]):
            equity = float(e["total_equity"])
            
            if equity > peak:
                peak = equity
            
            if peak > 0:
                drawdown = peak - equity
                drawdown_pct = (drawdown / peak) * 100
                
                if drawdown > mdd:
                    mdd = drawdown
                    mdd_percent = drawdown_pct
        
        return {
            "mdd": mdd,
            "mdd_percent": mdd_percent
        }
    
    def get_equity_curve(self) -> List[Dict]:
        """Equity Curve 데이터 반환"""
        return self._equity_curve
    
    def get_open_positions(self) -> List[PositionSnapshot]:
        """열린 포지션 목록"""
        return list(self._positions.values())
    
    def has_position(self, symbol: str = None) -> bool:
        """포지션 보유 여부"""
        if symbol:
            return symbol in self._positions
        return len(self._positions) > 0
    
    def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        """특정 포지션 조회"""
        return self._positions.get(symbol)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 리포트 생성 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def generate_summary_text(self) -> str:
        """텍스트 요약 생성"""
        summary = self.get_summary()
        today = self.get_daily_stats()
        
        return f"""
📊 *성과 리포트*
━━━━━━━━━━━━━━━━━━

💰 *오늘 ({today.trade_date})*
• 거래: {today.total_trades}회 (매수 {today.buy_count} / 매도 {today.sell_count})
• 손익: {today.total_pnl:+,.0f}원
• 승률: {today.win_rate:.1f}%

📈 *전체 성과*
• 총 거래: {summary.total_trades}회
• 승률: {summary.win_rate:.1f}% ({summary.win_count}승 / {summary.loss_count}패)
• 총 손익: {summary.total_pnl:+,.0f}원
• 수익률: {summary.total_return_pct:+.2f}%

📊 *성과 지표*
• Profit Factor: {summary.profit_factor:.2f}
• Expectancy: {summary.expectancy:+,.0f}원
• MDD: {summary.max_drawdown_pct:.2f}%
• 평균 보유: {summary.avg_holding_days:.1f}일

💵 *자본금*
• 초기: {summary.initial_capital:,.0f}원
• 현재: {summary.current_equity:,.0f}원
• 변화: {summary.total_pnl:+,.0f}원 ({summary.total_return_pct:+.2f}%)
"""
    
    def print_summary(self) -> None:
        """요약 출력"""
        print(self.generate_summary_text())


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_tracker_instance: Optional[PerformanceTracker] = None


def get_performance_tracker() -> PerformanceTracker:
    """싱글톤 PerformanceTracker 인스턴스"""
    global _tracker_instance
    
    if _tracker_instance is None:
        # 설정에서 값 로드
        try:
            from config import settings
            initial_capital = getattr(settings, "INITIAL_CAPITAL", 10_000_000)
            commission_rate = getattr(settings, "COMMISSION_RATE", 0.00015)
        except ImportError:
            initial_capital = 10_000_000
            commission_rate = 0.00015
        
        _tracker_instance = PerformanceTracker(
            initial_capital=initial_capital,
            commission_rate=commission_rate
        )
    
    return _tracker_instance


def reset_tracker() -> None:
    """트래커 리셋 (테스트용)"""
    global _tracker_instance
    _tracker_instance = None
