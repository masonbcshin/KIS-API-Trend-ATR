"""
KIS Trend-ATR Trading System - 성과 측정 로직 (PostgreSQL 기반)

이 모듈은 PostgreSQL에 저장된 거래 데이터를 기반으로
트레이딩 성과를 측정하고 분석합니다.

★ 핵심 기능:
    1. 종목별 수익률 계산
    2. 종목별 실현/미실현 손익 계산
    3. 전체 계좌 기준 누적 손익
    4. 일별 손익 계산
    5. 승률, MDD, Profit Factor 등 성과 지표

★ 중학생도 이해할 수 있는 설명:
    - "내 전략이 얼마나 잘 먹히고 있는가?"를 숫자로 보여줌
    - "이번 달 얼마 벌었지?" → get_period_pnl()
    - "승률은 몇 %지?" → get_win_rate()
    - "최악의 손실은 얼마였지?" → get_max_drawdown()

사용 예시:
    from report.performance import PerformanceCalculator
    
    calc = PerformanceCalculator()
    
    # 전체 성과 요약
    summary = calc.get_performance_summary()
    
    # 일별 손익
    daily_pnl = calc.get_daily_pnl()
    
    # 종목별 손익
    by_symbol = calc.get_pnl_by_symbol()
"""

from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from decimal import Decimal

from db.postgres import get_db_manager, PostgresManager
from db.repository import (
    TradeRepository,
    AccountSnapshotRepository,
    PositionRepository,
    get_trade_repository,
    get_position_repository,
    get_account_snapshot_repository
)
from utils.logger import get_logger

logger = get_logger("performance")


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PerformanceSummary:
    """
    성과 요약 데이터 클래스
    
    ★ 중학생도 이해할 수 있는 설명:
        - total_trades: 지금까지 몇 번 거래했나
        - win_rate: 100번 중 몇 번 이겼나 (%)
        - total_pnl: 총 얼마 벌었나/잃었나 (원)
        - profit_factor: 번 돈 / 잃은 돈 (1보다 크면 좋음)
        - max_drawdown: 최악의 경우 얼마나 손실 봤나 (%)
    """
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
    sharpe_ratio: float = 0.0
    
    # 기간
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    trading_days: int = 0
    
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
            "start_date": self.start_date,
            "end_date": self.end_date,
            "trading_days": self.trading_days
        }


@dataclass
class DailyPnL:
    """
    일별 손익 데이터 클래스
    """
    trade_date: str
    realized_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    cumulative_pnl: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.trade_date,
            "realized_pnl": round(self.realized_pnl, 0),
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "cumulative_pnl": round(self.cumulative_pnl, 0)
        }


@dataclass
class SymbolPnL:
    """
    종목별 손익 데이터 클래스
    """
    symbol: str
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_trades": self.total_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": round(self.win_rate, 2),
            "realized_pnl": round(self.realized_pnl, 0),
            "unrealized_pnl": round(self.unrealized_pnl, 0),
            "total_pnl": round(self.total_pnl, 0),
            "avg_pnl": round(self.avg_pnl, 0)
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 성과 계산기 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class PerformanceCalculator:
    """
    PostgreSQL 기반 성과 계산기
    
    ★ 이 클래스가 하는 일:
        - DB에서 거래 기록을 읽어옴
        - 다양한 성과 지표를 계산함
        - 일별, 월별, 종목별 등 다양한 기준으로 분석
    
    사용 예시:
        calc = PerformanceCalculator()
        
        # 전체 성과
        summary = calc.get_performance_summary()
        print(f"승률: {summary.win_rate}%")
        print(f"총 손익: {summary.total_pnl:,}원")
        
        # 일별 손익
        daily = calc.get_daily_pnl()
        for day in daily:
            print(f"{day.trade_date}: {day.realized_pnl:+,}원")
    """
    
    def __init__(
        self,
        db: PostgresManager = None,
        trade_repo: TradeRepository = None,
        position_repo: PositionRepository = None,
        snapshot_repo: AccountSnapshotRepository = None
    ):
        """
        성과 계산기 초기화
        
        Args:
            db: PostgresManager 인스턴스
            trade_repo: 거래 기록 Repository
            position_repo: 포지션 Repository
            snapshot_repo: 스냅샷 Repository
        """
        self.db = db or get_db_manager()
        self.trade_repo = trade_repo or get_trade_repository()
        self.position_repo = position_repo or get_position_repository()
        self.snapshot_repo = snapshot_repo or get_account_snapshot_repository()
        
        logger.info("[PERF] 성과 계산기 초기화 완료")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 전체 성과 요약
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_performance_summary(
        self,
        start_date: date = None,
        end_date: date = None
    ) -> PerformanceSummary:
        """
        전체 성과 요약을 반환합니다.
        
        ★ 가장 많이 쓰는 함수!
        ★ "내 전략 어떻게 되고 있어?" 한눈에 보여줌
        
        Args:
            start_date: 시작일 (None이면 전체)
            end_date: 종료일 (None이면 오늘)
        
        Returns:
            PerformanceSummary: 성과 요약
        """
        summary = PerformanceSummary()
        
        try:
            # 기본 통계 조회
            stats = self.trade_repo.get_performance_stats()
            
            summary.total_trades = stats.get("total_trades", 0)
            summary.win_count = stats.get("wins", 0)
            summary.loss_count = stats.get("losses", 0)
            summary.win_rate = stats.get("win_rate", 0.0)
            summary.realized_pnl = stats.get("total_pnl", 0.0)
            summary.avg_win = stats.get("avg_win", 0.0)
            summary.avg_loss = stats.get("avg_loss", 0.0)
            summary.max_win = stats.get("max_win", 0.0)
            summary.max_loss = stats.get("max_loss", 0.0)
            summary.profit_factor = stats.get("profit_factor", 0.0)
            summary.expectancy = stats.get("expectancy", 0.0)
            summary.avg_holding_days = stats.get("avg_holding_days", 0.0)
            
            # 미실현 손익 계산 (열린 포지션)
            summary.unrealized_pnl = self._calculate_unrealized_pnl()
            
            # 총 손익 = 실현 + 미실현
            summary.total_pnl = summary.realized_pnl + summary.unrealized_pnl
            
            # MDD 계산
            mdd_info = self.snapshot_repo.calculate_mdd()
            summary.max_drawdown = mdd_info.get("mdd", 0.0)
            summary.max_drawdown_pct = mdd_info.get("mdd_percent", 0.0)
            
            # 기간 정보
            date_range = self._get_trading_date_range()
            summary.start_date = date_range.get("start_date")
            summary.end_date = date_range.get("end_date")
            summary.trading_days = date_range.get("trading_days", 0)
            
            logger.info(
                f"[PERF] 성과 요약: {summary.total_trades}거래, "
                f"승률 {summary.win_rate:.1f}%, 손익 {summary.total_pnl:+,.0f}원"
            )
            
        except Exception as e:
            logger.error(f"[PERF] 성과 요약 계산 오류: {e}")
        
        return summary
    
    def _calculate_unrealized_pnl(self) -> float:
        """
        미실현 손익을 계산합니다.
        
        ★ 아직 팔지 않은 주식의 현재 손익
        """
        total_unrealized = 0.0
        
        try:
            positions = self.position_repo.get_open_positions()
            
            for pos in positions:
                # 현재가 조회가 필요하지만, 여기서는 DB만 사용
                # 실제 구현에서는 API로 현재가를 가져와야 함
                # 임시로 진입가 기준 0으로 처리
                pass
            
        except Exception as e:
            logger.warning(f"[PERF] 미실현 손익 계산 오류: {e}")
        
        return total_unrealized
    
    def _get_trading_date_range(self) -> Dict[str, Any]:
        """거래 기간 정보를 반환합니다."""
        result = self.db.execute_query(
            """
            SELECT 
                MIN(DATE(executed_at)) as start_date,
                MAX(DATE(executed_at)) as end_date,
                COUNT(DISTINCT DATE(executed_at)) as trading_days
            FROM trades
            """,
            fetch_one=True
        )
        
        if result:
            return {
                "start_date": str(result["start_date"]) if result["start_date"] else None,
                "end_date": str(result["end_date"]) if result["end_date"] else None,
                "trading_days": result.get("trading_days", 0) or 0
            }
        
        return {"start_date": None, "end_date": None, "trading_days": 0}
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 일별 손익
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_daily_pnl(
        self,
        days: int = 30,
        end_date: date = None
    ) -> List[DailyPnL]:
        """
        일별 손익을 반환합니다.
        
        ★ "오늘/어제/그제 얼마 벌었지?" 보여줌
        ★ 누적 손익도 함께 계산
        
        Args:
            days: 조회 일수
            end_date: 종료일 (None이면 오늘)
        
        Returns:
            List[DailyPnL]: 일별 손익 목록
        """
        end_date = end_date or date.today()
        start_date = end_date - timedelta(days=days)
        
        results = self.db.execute_query(
            """
            SELECT 
                DATE(executed_at) as trade_date,
                COALESCE(SUM(pnl), 0) as realized_pnl,
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_count
            FROM trades
            WHERE side = 'SELL' 
              AND DATE(executed_at) BETWEEN %s AND %s
              AND reason != 'SIGNAL_ONLY'
            GROUP BY DATE(executed_at)
            ORDER BY trade_date
            """,
            (start_date, end_date)
        )
        
        daily_list = []
        cumulative = 0.0
        
        for r in results:
            pnl = float(r.get("realized_pnl", 0) or 0)
            cumulative += pnl
            
            daily_list.append(DailyPnL(
                trade_date=str(r["trade_date"]),
                realized_pnl=pnl,
                trade_count=r.get("trade_count", 0) or 0,
                win_count=r.get("win_count", 0) or 0,
                loss_count=r.get("loss_count", 0) or 0,
                cumulative_pnl=cumulative
            ))
        
        return daily_list
    
    def get_today_pnl(self) -> DailyPnL:
        """오늘의 손익을 반환합니다."""
        today_list = self.get_daily_pnl(days=1, end_date=date.today())
        
        if today_list:
            return today_list[-1]
        
        return DailyPnL(trade_date=date.today().isoformat())
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 종목별 손익
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_pnl_by_symbol(self) -> List[SymbolPnL]:
        """
        종목별 손익을 반환합니다.
        
        ★ "삼성전자는 얼마 벌었고, SK하이닉스는 얼마 잃었지?" 보여줌
        
        Returns:
            List[SymbolPnL]: 종목별 손익 목록 (손익 높은 순)
        """
        results = self.db.execute_query(
            """
            SELECT 
                symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_count,
                COALESCE(SUM(pnl), 0) as realized_pnl,
                COALESCE(AVG(pnl), 0) as avg_pnl
            FROM trades
            WHERE side = 'SELL' AND reason != 'SIGNAL_ONLY'
            GROUP BY symbol
            ORDER BY realized_pnl DESC
            """
        )
        
        symbol_list = []
        
        for r in results:
            total = r.get("total_trades", 0) or 0
            wins = r.get("win_count", 0) or 0
            
            symbol_list.append(SymbolPnL(
                symbol=r["symbol"],
                total_trades=total,
                win_count=wins,
                loss_count=r.get("loss_count", 0) or 0,
                win_rate=(wins / total * 100) if total > 0 else 0.0,
                realized_pnl=float(r.get("realized_pnl", 0) or 0),
                unrealized_pnl=0.0,  # 현재가 조회 필요
                total_pnl=float(r.get("realized_pnl", 0) or 0),
                avg_pnl=float(r.get("avg_pnl", 0) or 0)
            ))
        
        return symbol_list
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 청산 사유별 분석
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_pnl_by_exit_reason(self) -> List[Dict[str, Any]]:
        """
        청산 사유별 손익을 반환합니다.
        
        ★ "손절이 얼마나 도움이 됐나? 익절은?" 분석
        
        Returns:
            List[Dict]: 사유별 통계
        """
        return self.trade_repo.get_pnl_by_reason()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 월별 손익
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_monthly_pnl(self, months: int = 12) -> List[Dict[str, Any]]:
        """
        월별 손익을 반환합니다.
        
        Args:
            months: 조회 개월 수
        
        Returns:
            List[Dict]: 월별 손익
        """
        results = self.db.execute_query(
            """
            SELECT 
                DATE_TRUNC('month', executed_at) as month,
                COALESCE(SUM(pnl), 0) as realized_pnl,
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_count
            FROM trades
            WHERE side = 'SELL' 
              AND reason != 'SIGNAL_ONLY'
              AND executed_at >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '%s months')
            GROUP BY DATE_TRUNC('month', executed_at)
            ORDER BY month DESC
            """,
            (months,)
        )
        
        monthly_list = []
        
        for r in results:
            total = r.get("trade_count", 0) or 0
            wins = r.get("win_count", 0) or 0
            
            monthly_list.append({
                "month": str(r["month"])[:7],  # YYYY-MM
                "realized_pnl": float(r.get("realized_pnl", 0) or 0),
                "trade_count": total,
                "win_count": wins,
                "loss_count": r.get("loss_count", 0) or 0,
                "win_rate": (wins / total * 100) if total > 0 else 0.0
            })
        
        return monthly_list
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 성과 리포트 생성
    # ═══════════════════════════════════════════════════════════════════════════
    
    def generate_report_text(self) -> str:
        """
        텔레그램용 성과 리포트 텍스트를 생성합니다.
        
        ★ 한눈에 볼 수 있는 요약 문자열 생성
        
        Returns:
            str: 리포트 텍스트
        """
        summary = self.get_performance_summary()
        today = self.get_today_pnl()
        
        report = f"""
📊 *성과 리포트*
━━━━━━━━━━━━━━━━━━

💰 *오늘 손익*
• 실현 손익: {today.realized_pnl:+,.0f}원
• 거래 횟수: {today.trade_count}회
• 승률: {(today.win_count / today.trade_count * 100) if today.trade_count > 0 else 0:.1f}%

📈 *전체 성과*
• 총 거래: {summary.total_trades}회
• 승률: {summary.win_rate:.1f}%
• 총 손익: {summary.total_pnl:+,.0f}원
• Profit Factor: {summary.profit_factor:.2f}

📉 *리스크 지표*
• Max Drawdown: {summary.max_drawdown_pct:.2f}%
• 평균 수익: {summary.avg_win:+,.0f}원
• 평균 손실: {summary.avg_loss:,.0f}원

📅 *기간*
• 시작: {summary.start_date or 'N/A'}
• 거래일: {summary.trading_days}일
"""
        return report
    
    def generate_daily_report_text(self, trade_date: date = None) -> str:
        """
        일일 리포트 텍스트를 생성합니다.
        
        Args:
            trade_date: 리포트 날짜 (None이면 오늘)
        
        Returns:
            str: 일일 리포트 텍스트
        """
        trade_date = trade_date or date.today()
        
        daily_summary = self.trade_repo.get_daily_summary(trade_date)
        
        report = f"""
📊 *일일 거래 요약*
━━━━━━━━━━━━━━━━━━
📅 날짜: {trade_date.isoformat()}

💰 *손익*
• 당일 손익: {daily_summary['total_pnl']:+,.0f}원

📈 *거래 통계*
• 총 거래: {daily_summary['total_trades']}회
• 매수: {daily_summary['buy_count']}회
• 매도: {daily_summary['sell_count']}회
• 승률: {daily_summary['win_rate']:.1f}%

📊 *상세*
• 최대 수익: {daily_summary['max_profit']:+,.0f}원
• 최대 손실: {daily_summary['max_loss']:+,.0f}원
"""
        return report


# ═══════════════════════════════════════════════════════════════════════════════
# 유틸리티 함수
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_sharpe_ratio(
    returns: List[float],
    risk_free_rate: float = 0.02
) -> float:
    """
    샤프 비율을 계산합니다.
    
    ★ 중학생도 이해할 수 있는 설명:
        - "얼마나 효율적으로 돈을 벌었나"
        - 높을수록 좋음 (1 이상이면 괜찮음)
    
    Args:
        returns: 수익률 리스트
        risk_free_rate: 무위험 이자율 (연 2%)
    
    Returns:
        float: 샤프 비율
    """
    if not returns or len(returns) < 2:
        return 0.0
    
    import statistics
    
    avg_return = statistics.mean(returns)
    std_return = statistics.stdev(returns)
    
    if std_return == 0:
        return 0.0
    
    # 일일 무위험 이자율
    daily_rf = risk_free_rate / 252
    
    sharpe = (avg_return - daily_rf) / std_return
    
    # 연환산
    return sharpe * (252 ** 0.5)


def calculate_sortino_ratio(
    returns: List[float],
    risk_free_rate: float = 0.02
) -> float:
    """
    소르티노 비율을 계산합니다.
    
    ★ 샤프 비율과 비슷하지만 "하락"만 위험으로 봄
    ★ 높을수록 좋음
    
    Args:
        returns: 수익률 리스트
        risk_free_rate: 무위험 이자율
    
    Returns:
        float: 소르티노 비율
    """
    if not returns or len(returns) < 2:
        return 0.0
    
    import statistics
    
    avg_return = statistics.mean(returns)
    
    # 음의 수익률만 추출
    negative_returns = [r for r in returns if r < 0]
    
    if not negative_returns:
        return float('inf')  # 손실 없음
    
    downside_std = statistics.stdev(negative_returns) if len(negative_returns) > 1 else 0
    
    if downside_std == 0:
        return float('inf')
    
    daily_rf = risk_free_rate / 252
    
    sortino = (avg_return - daily_rf) / downside_std
    
    return sortino * (252 ** 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_performance_calculator: Optional[PerformanceCalculator] = None


def get_performance_calculator() -> PerformanceCalculator:
    """싱글톤 PerformanceCalculator 인스턴스"""
    global _performance_calculator
    
    if _performance_calculator is None:
        _performance_calculator = PerformanceCalculator()
    
    return _performance_calculator
