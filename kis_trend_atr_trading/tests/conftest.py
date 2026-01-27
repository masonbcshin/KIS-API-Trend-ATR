"""
KIS Trend-ATR Trading System - pytest 공통 Fixtures

테스트에서 공통으로 사용되는 fixture들을 정의합니다.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch
import pytest
import pandas as pd
import numpy as np

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ════════════════════════════════════════════════════════════════
# 테스트 환경 설정 (time.sleep 비활성화)
# ════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def mock_time_sleep():
    """time.sleep을 mock하여 테스트 속도 향상"""
    with patch('time.sleep', return_value=None):
        yield


@pytest.fixture(autouse=True)
def mock_executor_sleep():
    """executor 모듈의 time.sleep을 mock"""
    with patch('engine.executor.time.sleep', return_value=None):
        yield


@pytest.fixture(autouse=True)
def fast_test_settings():
    """테스트용 빠른 설정"""
    from config import settings
    
    # 원래 값 저장
    original_timeout = settings.ORDER_EXECUTION_TIMEOUT
    original_interval = settings.ORDER_CHECK_INTERVAL
    original_daily_max_trades = settings.DAILY_MAX_TRADES
    
    # 테스트용 빠른 값 설정
    settings.ORDER_EXECUTION_TIMEOUT = 1
    settings.ORDER_CHECK_INTERVAL = 0.1
    settings.DAILY_MAX_TRADES = 100  # 테스트에서 거래 횟수 제한 완화
    
    yield
    
    # 원래 값 복원
    settings.ORDER_EXECUTION_TIMEOUT = original_timeout
    settings.ORDER_CHECK_INTERVAL = original_interval
    settings.DAILY_MAX_TRADES = original_daily_max_trades


@pytest.fixture(autouse=True)
def clear_daily_trades():
    """테스트 전후 일일 거래 기록 초기화"""
    from utils.position_store import get_daily_trade_store
    store = get_daily_trade_store()
    store.clear_today()
    yield
    store.clear_today()


# ════════════════════════════════════════════════════════════════
# 샘플 데이터 Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_uptrend_df():
    """
    상승 추세 OHLCV 데이터를 생성합니다.
    - 100일치 데이터
    - 지속적으로 상승하는 추세
    - 종가 > 50일 MA
    """
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=100, freq='D')
    
    # 기본 상승 추세 (50000 → 70000)
    base_price = np.linspace(50000, 70000, 100)
    noise = np.random.normal(0, 500, 100)
    
    close = base_price + noise
    high = close + np.random.uniform(500, 1500, 100)
    low = close - np.random.uniform(500, 1500, 100)
    open_price = close + np.random.uniform(-500, 500, 100)
    volume = np.random.randint(100000, 1000000, 100)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


@pytest.fixture
def sample_downtrend_df():
    """
    하락 추세 OHLCV 데이터를 생성합니다.
    - 100일치 데이터
    - 지속적으로 하락하는 추세
    - 종가 < 50일 MA
    """
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=100, freq='D')
    
    # 기본 하락 추세 (70000 → 50000)
    base_price = np.linspace(70000, 50000, 100)
    noise = np.random.normal(0, 500, 100)
    
    close = base_price + noise
    high = close + np.random.uniform(500, 1500, 100)
    low = close - np.random.uniform(500, 1500, 100)
    open_price = close + np.random.uniform(-500, 500, 100)
    volume = np.random.randint(100000, 1000000, 100)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


@pytest.fixture
def sample_sideways_df():
    """
    횡보장 OHLCV 데이터를 생성합니다.
    - 100일치 데이터
    - ADX < 25 (추세 강도 약함)
    """
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=100, freq='D')
    
    # 횡보 (60000 근처에서 작은 변동)
    base_price = 60000 + np.random.normal(0, 200, 100)
    
    close = base_price
    high = close + np.random.uniform(100, 300, 100)
    low = close - np.random.uniform(100, 300, 100)
    open_price = close + np.random.uniform(-100, 100, 100)
    volume = np.random.randint(100000, 1000000, 100)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


@pytest.fixture
def sample_df_with_nan_atr():
    """
    ATR이 NaN인 데이터를 생성합니다.
    - ATR 계산에 필요한 데이터 부족 (10일치만)
    """
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=10, freq='D')
    
    close = np.array([60000 + i * 100 for i in range(10)])
    high = close + 500
    low = close - 500
    open_price = close
    volume = np.random.randint(100000, 1000000, 10)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


@pytest.fixture
def sample_df_with_zero_atr():
    """
    ATR이 0에 가까운 데이터를 생성합니다.
    - 가격 변동이 거의 없는 데이터
    """
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=100, freq='D')
    
    # 거의 변동 없는 가격
    close = np.full(100, 60000.0)
    high = close + 0.01
    low = close - 0.01
    open_price = close.copy()
    volume = np.random.randint(100000, 1000000, 100)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


@pytest.fixture
def sample_atr_spike_df():
    """
    ATR 급등 상황의 데이터를 생성합니다.
    - 마지막 날 변동성이 급격히 증가
    """
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=100, freq='D')
    
    # 평소 변동폭 1000원
    base_price = np.linspace(50000, 60000, 100)
    high = base_price + 500
    low = base_price - 500
    
    # 마지막 10일: 변동폭 10000원 (급등)
    high[-10:] = base_price[-10:] + 5000
    low[-10:] = base_price[-10:] - 5000
    
    close = (high + low) / 2
    open_price = close + np.random.uniform(-200, 200, 100)
    volume = np.random.randint(100000, 1000000, 100)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


# ════════════════════════════════════════════════════════════════
# Mock API Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_kis_api():
    """Mock KIS API 클라이언트를 생성합니다."""
    mock_api = Mock()
    
    # 기본 응답 설정
    mock_api.get_access_token.return_value = "mock_access_token"
    
    mock_api.get_current_price.return_value = {
        "stock_code": "005930",
        "current_price": 65000.0,
        "change_rate": 1.5,
        "volume": 5000000,
        "high_price": 66000.0,
        "low_price": 64000.0,
        "open_price": 64500.0
    }
    
    mock_api.place_buy_order.return_value = {
        "success": True,
        "order_no": "0001234567",
        "message": "주문 접수 완료"
    }
    
    mock_api.place_sell_order.return_value = {
        "success": True,
        "order_no": "0001234568",
        "message": "주문 접수 완료"
    }
    
    # get_order_status - 체결 확인용
    def mock_get_order_status(order_no=None):
        return {
            "success": True,
            "orders": [
                {
                    "order_no": order_no or "0001234567",
                    "stock_code": "005930",
                    "order_type": "매수",
                    "order_qty": 10,
                    "exec_qty": 10,
                    "order_price": 0,
                    "exec_price": 65000,
                    "status": "체결"
                }
            ],
            "total_count": 1
        }
    mock_api.get_order_status.side_effect = mock_get_order_status
    
    mock_api.get_account_balance.return_value = {
        "success": True,
        "holdings": [],
        "total_eval": 10000000,
        "cash_balance": 10000000,
        "total_pnl": 0
    }
    
    mock_api.get_daily_ohlcv.return_value = pd.DataFrame()
    
    return mock_api


@pytest.fixture
def mock_kis_api_with_holdings():
    """보유 종목이 있는 Mock KIS API를 생성합니다."""
    with patch('api.kis_api.KISApi') as MockApi:
        mock_api = MockApi.return_value
        
        mock_api.get_access_token.return_value = "mock_access_token"
        
        mock_api.get_account_balance.return_value = {
            "success": True,
            "holdings": [
                {
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "quantity": 100,
                    "avg_price": 60000.0,
                    "current_price": 65000.0,
                    "eval_amount": 6500000,
                    "pnl_amount": 500000,
                    "pnl_rate": 8.33
                }
            ],
            "total_eval": 16500000,
            "cash_balance": 10000000,
            "total_pnl": 500000
        }
        
        mock_api.get_current_price.return_value = {
            "stock_code": "005930",
            "current_price": 65000.0,
            "change_rate": 1.5,
            "volume": 5000000,
            "high_price": 66000.0,
            "low_price": 64000.0,
            "open_price": 64500.0
        }
        
        yield mock_api


@pytest.fixture
def mock_kis_api_order_failure():
    """주문 실패하는 Mock KIS API를 생성합니다."""
    with patch('api.kis_api.KISApi') as MockApi:
        mock_api = MockApi.return_value
        
        mock_api.get_access_token.return_value = "mock_access_token"
        
        mock_api.place_buy_order.return_value = {
            "success": False,
            "order_no": "",
            "message": "잔고 부족"
        }
        
        mock_api.place_sell_order.return_value = {
            "success": False,
            "order_no": "",
            "message": "보유 수량 부족"
        }
        
        yield mock_api


# ════════════════════════════════════════════════════════════════
# Strategy Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def strategy():
    """기본 전략 인스턴스를 생성합니다."""
    from strategy.trend_atr import TrendATRStrategy
    return TrendATRStrategy()


@pytest.fixture
def strategy_with_position():
    """포지션이 있는 전략 인스턴스를 생성합니다."""
    from strategy.trend_atr import TrendATRStrategy, Position
    
    strategy = TrendATRStrategy()
    strategy.position = Position(
        stock_code="005930",
        entry_price=60000.0,
        quantity=100,
        stop_loss=57000.0,
        take_profit=69000.0,
        entry_date="2024-01-15",
        atr_at_entry=1500.0
    )
    
    return strategy


# ════════════════════════════════════════════════════════════════
# Executor Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_executor(mock_kis_api, strategy):
    """Mock API를 사용하는 Executor를 생성합니다."""
    from engine.executor import TradingExecutor
    
    executor = TradingExecutor(
        api=mock_kis_api,
        strategy=strategy,
        stock_code="005930",
        order_quantity=10,
        auto_sync=False  # 테스트에서는 자동 동기화 비활성화
    )
    
    return executor


# ════════════════════════════════════════════════════════════════
# Helper Functions
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def make_ohlcv_df():
    """커스텀 OHLCV DataFrame을 생성하는 팩토리 함수를 반환합니다."""
    def _make_df(
        days: int = 100,
        start_price: float = 50000,
        end_price: float = 70000,
        volatility: float = 500
    ):
        np.random.seed(42)
        dates = pd.date_range(end=datetime.now(), periods=days, freq='D')
        
        base_price = np.linspace(start_price, end_price, days)
        noise = np.random.normal(0, volatility, days)
        
        close = base_price + noise
        high = close + np.random.uniform(volatility, volatility * 3, days)
        low = close - np.random.uniform(volatility, volatility * 3, days)
        open_price = close + np.random.uniform(-volatility, volatility, days)
        volume = np.random.randint(100000, 1000000, days)
        
        return pd.DataFrame({
            'date': dates,
            'open': open_price,
            'high': high,
            'low': low,
            'close': close,
            'volume': volume
        })
    
    return _make_df
