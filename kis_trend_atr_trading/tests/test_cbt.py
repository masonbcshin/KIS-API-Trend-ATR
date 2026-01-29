"""
KIS Trend-ATR Trading System - CBT 모듈 테스트

이 모듈은 CBT(Closed Beta Test) 관련 기능을 테스트합니다.
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime

from cbt.virtual_account import VirtualAccount, Position
from cbt.trade_store import TradeStore, Trade, ExitReason
from cbt.metrics import CBTMetrics, PerformanceReport


class TestVirtualAccount:
    """VirtualAccount 클래스 테스트"""
    
    @pytest.fixture
    def temp_dir(self):
        """테스트용 임시 디렉토리"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    @pytest.fixture
    def account(self, temp_dir):
        """테스트용 가상 계좌"""
        return VirtualAccount(
            initial_capital=10_000_000,
            data_dir=temp_dir,
            load_existing=False
        )
    
    def test_initial_state(self, account):
        """초기 상태 테스트"""
        assert account.initial_capital == 10_000_000
        assert account.cash == 10_000_000
        assert account.realized_pnl == 0
        assert account.unrealized_pnl == 0
        assert account.total_trades == 0
        assert account.position is None
    
    def test_execute_buy(self, account):
        """가상 매수 테스트"""
        result = account.execute_buy(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        assert result["success"] is True
        assert account.has_position() is True
        assert account.position.stock_code == "005930"
        assert account.position.entry_price == 70000
        assert account.position.quantity == 10
        assert account.cash < 10_000_000  # 현금 차감됨
    
    def test_execute_buy_insufficient_funds(self, account):
        """현금 부족 시 매수 실패 테스트"""
        # 큰 금액으로 매수 시도
        result = account.execute_buy(
            stock_code="005930",
            price=1_000_000,
            quantity=100,  # 1억원 필요
            stop_loss=900_000,
            take_profit=1_100_000,
            atr=50000
        )
        
        assert result["success"] is False
        assert "현금 부족" in result["message"]
    
    def test_execute_sell(self, account):
        """가상 매도 테스트"""
        # 먼저 매수
        account.execute_buy(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        # 매도 실행 (수익)
        result = account.execute_sell(
            price=72000,
            reason="TAKE_PROFIT"
        )
        
        assert result["success"] is True
        assert result["net_pnl"] > 0  # 수익 발생
        assert account.has_position() is False
        assert account.total_trades == 1
        assert account.winning_trades == 1
    
    def test_execute_sell_loss(self, account):
        """손실 매도 테스트"""
        account.execute_buy(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        result = account.execute_sell(
            price=68000,
            reason="ATR_STOP"
        )
        
        assert result["success"] is True
        assert result["net_pnl"] < 0  # 손실 발생
        assert account.losing_trades == 1
    
    def test_update_position_price(self, account):
        """미실현 손익 업데이트 테스트"""
        account.execute_buy(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        # 가격 상승
        account.update_position_price(72000)
        assert account.unrealized_pnl > 0
        
        # 가격 하락
        account.update_position_price(68000)
        assert account.unrealized_pnl < 0
    
    def test_get_account_summary(self, account):
        """계좌 요약 테스트"""
        summary = account.get_account_summary()
        
        assert "initial_capital" in summary
        assert "cash" in summary
        assert "total_equity" in summary
        assert "win_rate" in summary


class TestTradeStore:
    """TradeStore 클래스 테스트"""
    
    @pytest.fixture
    def temp_dir(self):
        """테스트용 임시 디렉토리"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    @pytest.fixture
    def store_json(self, temp_dir):
        """JSON 저장소"""
        return TradeStore(storage_type="json", data_dir=temp_dir)
    
    @pytest.fixture
    def store_sqlite(self, temp_dir):
        """SQLite 저장소"""
        return TradeStore(storage_type="sqlite", data_dir=temp_dir)
    
    @pytest.fixture
    def sample_trade(self):
        """샘플 거래"""
        return Trade(
            trade_id="TEST001",
            stock_code="005930",
            entry_date="2024-01-01 10:00:00",
            exit_date="2024-01-02 14:00:00",
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
    
    def test_add_trade_json(self, store_json, sample_trade):
        """JSON 거래 추가 테스트"""
        result = store_json.add_trade(sample_trade)
        assert result is True
        
        trades = store_json.get_all_trades()
        assert len(trades) == 1
        assert trades[0].trade_id == "TEST001"
    
    def test_add_trade_sqlite(self, store_sqlite, sample_trade):
        """SQLite 거래 추가 테스트"""
        result = store_sqlite.add_trade(sample_trade)
        assert result is True
        
        trades = store_sqlite.get_all_trades()
        assert len(trades) == 1
        assert trades[0].trade_id == "TEST001"
    
    def test_get_trades_by_date(self, store_json, sample_trade):
        """기간별 거래 조회 테스트"""
        store_json.add_trade(sample_trade)
        
        trades = store_json.get_trades_by_date("2024-01-01", "2024-01-31")
        assert len(trades) == 1
        
        trades = store_json.get_trades_by_date("2024-02-01", "2024-02-28")
        assert len(trades) == 0
    
    def test_trade_is_winner(self, sample_trade):
        """수익 거래 판별 테스트"""
        assert sample_trade.is_winner() is True
        assert sample_trade.is_loser() is False
    
    def test_get_summary_stats(self, store_json, sample_trade):
        """통계 요약 테스트"""
        store_json.add_trade(sample_trade)
        
        stats = store_json.get_summary_stats()
        
        assert stats["total_trades"] == 1
        assert stats["winning_trades"] == 1
        assert stats["losing_trades"] == 0
        assert stats["win_rate"] == 100.0


class TestCBTMetrics:
    """CBTMetrics 클래스 테스트"""
    
    @pytest.fixture
    def temp_dir(self):
        """테스트용 임시 디렉토리"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    @pytest.fixture
    def setup_components(self, temp_dir):
        """테스트용 컴포넌트 설정"""
        account = VirtualAccount(
            initial_capital=10_000_000,
            data_dir=temp_dir,
            load_existing=False
        )
        trade_store = TradeStore(storage_type="json", data_dir=temp_dir)
        metrics = CBTMetrics(account, trade_store)
        
        return account, trade_store, metrics
    
    def test_calculate_win_rate_empty(self, setup_components):
        """빈 데이터 승률 테스트"""
        _, _, metrics = setup_components
        
        win_rate = metrics.calculate_win_rate()
        assert win_rate == 0.0
    
    def test_calculate_win_rate(self, setup_components):
        """승률 계산 테스트"""
        account, trade_store, metrics = setup_components
        
        # 승리 거래 추가
        trade_store.add_trade(Trade(
            trade_id="WIN001",
            stock_code="005930",
            entry_date="2024-01-01 10:00:00",
            exit_date="2024-01-02 14:00:00",
            entry_price=70000,
            exit_price=72000,
            quantity=10,
            gross_pnl=20000,
            commission=30,
            pnl=19970,
            return_pct=2.85,
            holding_days=2,
            exit_reason="TAKE_PROFIT"
        ))
        
        # 패배 거래 추가
        trade_store.add_trade(Trade(
            trade_id="LOSE001",
            stock_code="005930",
            entry_date="2024-01-03 10:00:00",
            exit_date="2024-01-04 14:00:00",
            entry_price=70000,
            exit_price=68000,
            quantity=10,
            gross_pnl=-20000,
            commission=30,
            pnl=-20030,
            return_pct=-2.86,
            holding_days=2,
            exit_reason="ATR_STOP"
        ))
        
        win_rate = metrics.calculate_win_rate()
        assert win_rate == 50.0
    
    def test_calculate_expectancy(self, setup_components):
        """Expectancy 계산 테스트"""
        account, trade_store, metrics = setup_components
        
        # 승리 거래 (평균 수익 20000)
        trade_store.add_trade(Trade(
            trade_id="WIN001",
            stock_code="005930",
            entry_date="2024-01-01 10:00:00",
            exit_date="2024-01-02 14:00:00",
            entry_price=70000,
            exit_price=72000,
            quantity=10,
            gross_pnl=20000,
            commission=30,
            pnl=20000,
            return_pct=2.85,
            holding_days=2,
            exit_reason="TAKE_PROFIT"
        ))
        
        # 패배 거래 (평균 손실 10000)
        trade_store.add_trade(Trade(
            trade_id="LOSE001",
            stock_code="005930",
            entry_date="2024-01-03 10:00:00",
            exit_date="2024-01-04 14:00:00",
            entry_price=70000,
            exit_price=69000,
            quantity=10,
            gross_pnl=-10000,
            commission=30,
            pnl=-10000,
            return_pct=-1.43,
            holding_days=2,
            exit_reason="ATR_STOP"
        ))
        
        expectancy, expectancy_pct = metrics.calculate_expectancy()
        
        # 기대값 = (0.5 * 20000) - (0.5 * 10000) = 5000
        assert expectancy == 5000
    
    def test_calculate_profit_factor(self, setup_components):
        """Profit Factor 계산 테스트"""
        account, trade_store, metrics = setup_components
        
        # 수익 거래
        trade_store.add_trade(Trade(
            trade_id="WIN001",
            stock_code="005930",
            entry_date="2024-01-01 10:00:00",
            exit_date="2024-01-02 14:00:00",
            entry_price=70000,
            exit_price=72000,
            quantity=10,
            gross_pnl=20000,
            commission=30,
            pnl=20000,
            return_pct=2.85,
            holding_days=2,
            exit_reason="TAKE_PROFIT"
        ))
        
        # 손실 거래
        trade_store.add_trade(Trade(
            trade_id="LOSE001",
            stock_code="005930",
            entry_date="2024-01-03 10:00:00",
            exit_date="2024-01-04 14:00:00",
            entry_price=70000,
            exit_price=69000,
            quantity=10,
            gross_pnl=-10000,
            commission=30,
            pnl=-10000,
            return_pct=-1.43,
            holding_days=2,
            exit_reason="ATR_STOP"
        ))
        
        pf = metrics.calculate_profit_factor()
        
        # Profit Factor = 20000 / 10000 = 2.0
        assert pf == 2.0
    
    def test_generate_report(self, setup_components):
        """성과 리포트 생성 테스트"""
        account, trade_store, metrics = setup_components
        
        report = metrics.generate_report()
        
        assert isinstance(report, PerformanceReport)
        assert report.initial_capital == 10_000_000
        assert report.total_trades == 0


class TestExitReason:
    """ExitReason 열거형 테스트"""
    
    def test_exit_reasons(self):
        """청산 사유 열거형 테스트"""
        assert ExitReason.ATR_STOP.value == "ATR_STOP"
        assert ExitReason.TAKE_PROFIT.value == "TAKE_PROFIT"
        assert ExitReason.TRAILING_STOP.value == "TRAILING_STOP"
        assert ExitReason.TREND_BROKEN.value == "TREND_BROKEN"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
