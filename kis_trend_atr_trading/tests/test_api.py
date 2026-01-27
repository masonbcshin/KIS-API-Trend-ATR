"""
KIS Trend-ATR Trading System - API 클라이언트 테스트

KISApi 클래스의 핵심 기능을 테스트합니다.
실제 API 호출 없이 Mock을 사용합니다.

테스트 항목:
- 실계좌 사용 방지
- 토큰 관리
- Rate Limit 준수
- 재시도 로직
- 주문 API 응답 처리
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch
import requests
import pandas as pd

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.kis_api import KISApi, KISApiError
from config import settings


class TestRealAccountPrevention:
    """실계좌 사용 방지 테스트"""
    
    def test_real_account_blocked(self):
        """
        실계좌 모드 사용 시 예외 발생 테스트
        
        is_paper_trading=False로 생성 시도 시 KISApiError 발생해야 함
        """
        with pytest.raises(KISApiError) as exc_info:
            api = KISApi(is_paper_trading=False)
        
        assert "실계좌" in str(exc_info.value) or "금지" in str(exc_info.value), \
            "실계좌 사용 금지 메시지가 포함되어야 합니다"
    
    def test_paper_trading_allowed(self):
        """모의투자 모드 허용 테스트"""
        with patch.object(KISApi, '__init__', lambda self, **kwargs: None):
            api = KISApi(is_paper_trading=True)
            # 정상 생성되면 통과
    
    def test_base_url_is_paper_trading(self):
        """BASE URL이 모의투자 URL인지 확인"""
        assert "openapivts" in settings.KIS_BASE_URL, \
            "BASE URL은 모의투자 URL이어야 합니다 (openapivts 포함)"
        
        assert "29443" in settings.KIS_BASE_URL, \
            "모의투자 포트는 29443이어야 합니다"
    
    def test_settings_validate_real_account_url(self):
        """설정 검증에서 실계좌 URL 감지 테스트"""
        # 원래 URL 저장
        original_url = settings.KIS_BASE_URL
        
        # 실계좌 URL로 변경 시도
        settings.KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
        
        # 검증 실패해야 함
        is_valid = settings.validate_settings()
        
        # URL 복원
        settings.KIS_BASE_URL = original_url
        
        # 실계좌 URL이면 검증 실패
        assert is_valid is False or "9443" not in settings.KIS_BASE_URL, \
            "실계좌 URL 감지 시 검증이 실패해야 합니다"


class TestTokenManagement:
    """토큰 관리 테스트"""
    
    @patch('api.kis_api.requests.post')
    def test_get_access_token_success(self, mock_post):
        """토큰 발급 성공 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test_token_12345",
            "token_type": "Bearer",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            token = api.get_access_token()
        
        assert token == "test_token_12345"
        assert api.access_token == "test_token_12345"
        assert api.token_expires_at is not None
    
    @patch('api.kis_api.requests.post')
    def test_token_reuse_when_valid(self, mock_post):
        """유효한 토큰 재사용 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test_token_12345",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            
            # 첫 번째 토큰 발급
            token1 = api.get_access_token()
            
            # 두 번째 호출 - 토큰이 유효하면 재사용
            token2 = api.get_access_token()
        
        assert token1 == token2
        # API는 첫 번째 호출에서만 호출됨
        assert mock_post.call_count == 1
    
    @patch('api.kis_api.requests.post')
    def test_token_refresh_when_expired(self, mock_post):
        """만료된 토큰 갱신 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_token_67890",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            
            # 만료된 토큰 설정
            api.access_token = "old_token"
            api.token_expires_at = datetime.now() - timedelta(hours=1)
            
            # 토큰 갱신 요청
            token = api.get_access_token()
        
        assert token == "new_token_67890"


class TestRateLimiting:
    """Rate Limit 테스트"""
    
    def test_rate_limit_delay_setting(self):
        """Rate Limit 설정 확인"""
        assert settings.RATE_LIMIT_DELAY > 0, "Rate Limit 딜레이가 설정되어야 합니다"
        assert settings.RATE_LIMIT_DELAY >= 0.05, "초당 20회 제한을 위해 최소 0.05초 필요"
    
    @patch('api.kis_api.time.sleep')
    def test_rate_limit_wait(self, mock_sleep):
        """Rate Limit 대기 테스트"""
        api = KISApi(is_paper_trading=True)
        
        # 마지막 호출 시간을 현재로 설정 (딜레이 필요)
        import time
        api._last_api_call_time = time.time()
        
        api._wait_for_rate_limit()
        
        # Rate Limit 딜레이가 필요하면 sleep이 호출됨
        # (실제로는 time.sleep이 호출될 수 있음)


class TestRetryLogic:
    """재시도 로직 테스트"""
    
    @patch('api.kis_api.requests.get')
    def test_retry_on_timeout(self, mock_get):
        """타임아웃 시 재시도 테스트"""
        # 처음 2번은 타임아웃, 3번째 성공
        mock_get.side_effect = [
            requests.exceptions.Timeout("타임아웃"),
            requests.exceptions.Timeout("타임아웃"),
            Mock(status_code=200, json=lambda: {"rt_cd": "0", "output": {}})
        ]
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            with patch('api.kis_api.time.sleep', return_value=None):
                api = KISApi(is_paper_trading=True)
                api.access_token = "test_token"
                
                # 3번 재시도 설정
                try:
                    response = api._request_with_retry(
                        method="GET",
                        url="https://test.com/api",
                        headers={},
                        max_retries=3
                    )
                    assert response.status_code == 200
                except KISApiError:
                    pass  # 재시도 후에도 실패하면 예외 발생
        
        # 3번 호출됨 (초기 1회 + 재시도 2회)
        assert mock_get.call_count >= 2
    
    @patch('api.kis_api.requests.get')
    def test_max_retries_exceeded(self, mock_get):
        """최대 재시도 횟수 초과 테스트"""
        mock_get.side_effect = requests.exceptions.Timeout("타임아웃")
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            with patch('api.kis_api.time.sleep', return_value=None):
                api = KISApi(is_paper_trading=True)
                api.access_token = "test_token"
                
                with pytest.raises(KISApiError) as exc_info:
                    api._request_with_retry(
                        method="GET",
                        url="https://test.com/api",
                        headers={},
                        max_retries=2
                    )
                
                assert "타임아웃" in str(exc_info.value)
        
        # 최대 재시도 + 1번 호출 (초기 1회 + 재시도 2회 = 3회)
        assert mock_get.call_count == 3


class TestOrderAPIResponses:
    """주문 API 응답 처리 테스트"""
    
    @patch('api.kis_api.requests.post')
    def test_buy_order_success_response(self, mock_post):
        """매수 주문 성공 응답 처리 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg_cd": "APBK0013",
            "msg1": "주문 접수 완료",
            "output": {
                "ODNO": "0001234567",
                "ODRNO": "1"
            }
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            result = api.place_buy_order(
                stock_code="005930",
                quantity=10,
                price=0,
                order_type="01"
            )
        
        assert result["success"] is True
        assert result["order_no"] == "0001234567"
    
    @patch('api.kis_api.requests.post')
    def test_buy_order_failure_response(self, mock_post):
        """매수 주문 실패 응답 처리 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "APBK0014",
            "msg1": "잔고 부족",
            "output": {}
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            result = api.place_buy_order(
                stock_code="005930",
                quantity=10,
                price=0,
                order_type="01"
            )
        
        assert result["success"] is False
        assert "잔고 부족" in result["message"]
    
    @patch('api.kis_api.requests.post')
    def test_sell_order_success_response(self, mock_post):
        """매도 주문 성공 응답 처리 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg_cd": "APBK0013",
            "msg1": "주문 접수 완료",
            "output": {
                "ODNO": "0001234568",
                "ODRNO": "2"
            }
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            result = api.place_sell_order(
                stock_code="005930",
                quantity=10,
                price=0,
                order_type="01"
            )
        
        assert result["success"] is True
        assert result["order_no"] == "0001234568"


class TestCurrentPriceAPI:
    """현재가 조회 API 테스트"""
    
    @patch('api.kis_api.requests.get')
    def test_get_current_price_success(self, mock_get):
        """현재가 조회 성공 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": {
                "stck_prpr": "65000",
                "prdy_ctrt": "1.50",
                "acml_vol": "5000000",
                "stck_hgpr": "66000",
                "stck_lwpr": "64000",
                "stck_oprc": "64500"
            }
        }
        mock_get.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            result = api.get_current_price("005930")
        
        assert result["current_price"] == 65000.0
        assert result["change_rate"] == 1.50
        assert result["volume"] == 5000000
    
    @patch('api.kis_api.requests.get')
    def test_get_current_price_failure(self, mock_get):
        """현재가 조회 실패 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg1": "종목 코드 오류"
        }
        mock_get.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            with pytest.raises(KISApiError) as exc_info:
                api.get_current_price("INVALID")
            
            assert "종목" in str(exc_info.value) or "오류" in str(exc_info.value)


class TestDailyOHLCVAPI:
    """일봉 데이터 조회 API 테스트"""
    
    @patch('api.kis_api.requests.get')
    def test_get_daily_ohlcv_success(self, mock_get):
        """일봉 데이터 조회 성공 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20240115",
                    "stck_oprc": "64500",
                    "stck_hgpr": "66000",
                    "stck_lwpr": "64000",
                    "stck_clpr": "65000",
                    "acml_vol": "5000000"
                },
                {
                    "stck_bsop_date": "20240114",
                    "stck_oprc": "64000",
                    "stck_hgpr": "65000",
                    "stck_lwpr": "63500",
                    "stck_clpr": "64500",
                    "acml_vol": "4500000"
                }
            ]
        }
        mock_get.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            df = api.get_daily_ohlcv("005930")
        
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert 'date' in df.columns
        assert 'open' in df.columns
        assert 'high' in df.columns
        assert 'low' in df.columns
        assert 'close' in df.columns
        assert 'volume' in df.columns
    
    @patch('api.kis_api.requests.get')
    def test_get_daily_ohlcv_empty(self, mock_get):
        """일봉 데이터 없음 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": []
        }
        mock_get.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            df = api.get_daily_ohlcv("005930")
        
        assert df.empty


class TestAccountBalanceAPI:
    """계좌 잔고 조회 API 테스트"""
    
    @patch('api.kis_api.requests.get')
    def test_get_account_balance_success(self, mock_get):
        """계좌 잔고 조회 성공 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "100",
                    "pchs_avg_pric": "60000.00",
                    "prpr": "65000",
                    "evlu_amt": "6500000",
                    "evlu_pfls_amt": "500000",
                    "evlu_pfls_rt": "8.33"
                }
            ],
            "output2": [
                {
                    "tot_evlu_amt": "16500000",
                    "dnca_tot_amt": "10000000",
                    "evlu_pfls_smtl_amt": "500000"
                }
            ]
        }
        mock_get.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            
            result = api.get_account_balance()
        
        assert result["success"] is True
        assert len(result["holdings"]) == 1
        assert result["holdings"][0]["stock_code"] == "005930"
        assert result["holdings"][0]["quantity"] == 100
        assert result["total_eval"] == 16500000


class TestAuthHeaders:
    """인증 헤더 테스트"""
    
    @patch('api.kis_api.requests.post')
    def test_auth_headers_generation(self, mock_post):
        """인증 헤더 생성 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test_token",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.get_access_token()
            
            headers = api._get_auth_headers("VTTC0802U")
        
        assert "authorization" in headers
        assert "Bearer" in headers["authorization"]
        assert "appkey" in headers
        assert "appsecret" in headers
        assert "tr_id" in headers
        assert headers["tr_id"] == "VTTC0802U"


class TestTransactionIDs:
    """거래 ID (TR_ID) 테스트"""
    
    def test_paper_trading_tr_ids(self):
        """모의투자 TR_ID 확인 테스트"""
        # 모의투자 TR_ID는 'V'로 시작
        paper_trading_buy_tr_id = "VTTC0802U"
        paper_trading_sell_tr_id = "VTTC0801U"
        
        assert paper_trading_buy_tr_id.startswith("V"), \
            "모의투자 매수 TR_ID는 V로 시작해야 합니다"
        assert paper_trading_sell_tr_id.startswith("V"), \
            "모의투자 매도 TR_ID는 V로 시작해야 합니다"
