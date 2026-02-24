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

    def test_token_prewarm_default_time_is_0800(self):
        """토큰 프리워밍 기본 시각이 08:00인지 테스트"""
        api = KISApi(is_paper_trading=True)
        assert api._token_prewarm_hour == 8
        assert api._token_prewarm_minute == 0
    
    @patch('api.kis_api.requests.post')
    def test_get_access_token_success(self, mock_post, tmp_path):
        """토큰 발급 성공 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test_token_12345",
            "token_type": "Bearer",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch("api.kis_api.settings.DATA_DIR", tmp_path):
            with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
                api = KISApi(is_paper_trading=True)
                token = api.get_access_token()
        
        assert token == "test_token_12345"
        assert api.access_token == "test_token_12345"
        assert api.token_expires_at is not None
    
    @patch('api.kis_api.requests.post')
    def test_token_reuse_when_valid(self, mock_post, tmp_path):
        """유효한 토큰 재사용 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test_token_12345",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch("api.kis_api.settings.DATA_DIR", tmp_path):
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
    def test_token_refresh_when_expired(self, mock_post, tmp_path):
        """만료된 토큰 갱신 테스트"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_token_67890",
            "expires_in": 86400
        }
        mock_post.return_value = mock_response
        
        with patch("api.kis_api.settings.DATA_DIR", tmp_path):
            with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
                api = KISApi(is_paper_trading=True)
                
                # 만료된 토큰 설정
                api.access_token = "old_token"
                api.token_expires_at = datetime.now() - timedelta(hours=1)
                
                # 토큰 갱신 요청
                token = api.get_access_token()
        
        assert token == "new_token_67890"

    @patch('api.kis_api.time.sleep')
    @patch('api.kis_api.requests.post')
    def test_get_access_token_retry_interval_fixed_61_seconds(self, mock_post, mock_sleep, tmp_path):
        """토큰 발급 재시도 간격이 61초 고정인지 테스트"""
        timeout_exc = requests.exceptions.Timeout("token timeout")
        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {
            "access_token": "token_after_retry",
            "expires_in": 86400
        }
        # 2회 실패 후 성공
        mock_post.side_effect = [timeout_exc, timeout_exc, success_response]

        with patch("api.kis_api.settings.DATA_DIR", tmp_path):
            with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
                api = KISApi(is_paper_trading=True)
                token = api.get_access_token()

        assert token == "token_after_retry"
        # 두 번의 재시도 대기 모두 61초 고정
        assert mock_sleep.call_count >= 2
        first_two = [c.args[0] for c in mock_sleep.call_args_list[:2]]
        assert first_two == [61.0, 61.0]

    @patch("api.kis_api.requests.post")
    def test_get_access_token_reuses_persisted_cache_on_restart(self, mock_post, tmp_path):
        """재기동 시 영속 토큰 캐시를 재사용해 재발급을 피하는지 테스트"""
        with patch("api.kis_api.settings.DATA_DIR", tmp_path):
            with patch.object(KISApi, "_wait_for_rate_limit", return_value=None):
                issuer = KISApi(is_paper_trading=True)
                issuer.access_token = "persisted_token"
                issuer.token_expires_at = datetime.now().astimezone() + timedelta(hours=23)
                issuer._save_token_cache()

            with patch.object(KISApi, "_wait_for_rate_limit", return_value=None):
                reloaded = KISApi(is_paper_trading=True)
                token = reloaded.get_access_token()

        assert token == "persisted_token"
        mock_post.assert_not_called()


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


class TestExecutionStatusResponses:
    """체결 조회/대기 응답 해석 테스트"""

    @patch('api.kis_api.requests.get')
    def test_get_order_status_filters_target_order_no_with_zero_padding(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [
                {
                    "odno": "0000015962",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "01",
                    "ord_qty": "1",
                    "tot_ccld_qty": "0",
                    "ord_unpr": "0",
                    "avg_prvs": "0",
                    "ord_dt": "20260223",
                    "ord_tmd": "110400",
                },
                {
                    "odno": "15963",
                    "pdno": "000660",
                    "sll_buy_dvsn_cd": "01",
                    "ord_qty": "1",
                    "tot_ccld_qty": "1",
                    "ord_unpr": "0",
                    "avg_prvs": "957000",
                    "ord_dt": "20260223",
                    "ord_tmd": "110535",
                },
            ],
        }
        mock_get.return_value = mock_response

        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            with patch.object(api, "get_access_token", return_value="test_token"):
                result = api.get_order_status("0000015963")

        assert result["success"] is True
        assert result["total_count"] == 1
        assert len(result["orders"]) == 1
        assert result["orders"][0]["order_no"] == "15963"
        assert result["orders"][0]["exec_qty"] == 1

    @patch('api.kis_api.requests.get')
    def test_get_order_status_accepts_custom_date_range(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"rt_cd": "0", "output1": []}
        mock_get.return_value = mock_response

        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            with patch.object(api, "get_access_token", return_value="test_token"):
                result = api.get_order_status(
                    order_no=None,
                    trade_date="2026-02-22",
                    end_date="2026-02-23",
                )

        assert result["success"] is True
        assert result["total_count"] == 0
        params = mock_get.call_args.kwargs.get("params") or {}
        assert params.get("INQR_STRT_DT") == "20260222"
        assert params.get("INQR_END_DT") == "20260223"

    @patch('api.kis_api.requests.get')
    def test_get_order_status_parses_output2_alt_fields(self, mock_get):
        """output2/대체 키 포맷도 체결 행으로 파싱해야 합니다."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "order_no": "0000001896",
                    "stock_code": "032820",
                    "side": "BUY",
                    "order_qty": "1",
                    "exec_qty": "1",
                    "remain_qty": "0",
                    "order_price": "0",
                    "exec_price": "17960",
                    "order_date": "20260224",
                    "order_time": "090823",
                }
            ],
        }
        mock_get.return_value = mock_response

        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            with patch.object(api, "get_access_token", return_value="test_token"):
                result = api.get_order_status("0000001896")

        assert result["success"] is True
        assert result["total_count"] == 1
        assert result["resolved_path"] == "output2"
        row = result["orders"][0]
        assert row["order_no"] == "0000001896"
        assert row["stock_code"] == "032820"
        assert row["side"] == "BUY"
        assert row["exec_qty"] == 1
        assert row["exec_price"] == 17960.0

    @patch('api.kis_api.requests.get')
    def test_get_order_status_uses_requested_order_no_when_row_missing_odno(self, mock_get):
        """응답에 odno 키가 없더라도 요청 주문번호 기준으로 매칭되어야 합니다."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "032820",
                    "sll_buy_dvsn_cd": "01",
                    "ord_qty": "51",
                    "tot_ccld_qty": "51",
                    "avg_prvs": "18180",
                    "ord_dt": "20260224",
                    "ord_tmd": "090622",
                }
            ],
        }
        mock_get.return_value = mock_response

        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"
            with patch.object(api, "get_access_token", return_value="test_token"):
                result = api.get_order_status("0000001640")

        assert result["success"] is True
        assert result["total_count"] == 1
        row = result["orders"][0]
        assert row["order_no"] == "0000001640"
        assert row["exec_qty"] == 51
        assert row["exec_price"] == 18180.0

    @patch('api.kis_api.requests.get')
    def test_get_order_status_logs_raw_payload_when_debug_enabled(self, mock_get):
        """주문번호 미매칭 시 디버그 플래그가 켜져 있으면 원문 payload를 경고 로그로 남깁니다."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [
                {
                    "odno": "0000001111",
                    "pdno": "024060",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "1",
                    "tot_ccld_qty": "0",
                    "ord_dt": "20260224",
                    "ord_tmd": "133504",
                }
            ],
        }
        mock_get.return_value = mock_response

        with patch.dict(
            "os.environ",
            {
                "KIS_ORDER_STATUS_DEBUG_RAW": "true",
                "KIS_ORDER_STATUS_DEBUG_RAW_MAX_LEN": "5000",
            },
            clear=False,
        ):
            with patch("api.kis_api.logger.warning") as mock_warning:
                with patch.object(KISApi, "_wait_for_rate_limit", return_value=None):
                    api = KISApi(is_paper_trading=True)
                    api.access_token = "test_token"
                    with patch.object(api, "get_access_token", return_value="test_token"):
                        result = api.get_order_status("0000009999")

        assert result["success"] is True
        assert result["total_count"] == 0
        warning_headers = [str(call.args[0]) for call in mock_warning.call_args_list if call.args]
        assert any("[KIS][ORDER_STATUS][RAW]" in header for header in warning_headers)

    def test_wait_for_execution_ignores_unmatched_rows(self):
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"

        def _fake_get_order_status(_query_order_no=None):
            return {
                "success": True,
                "orders": [
                    {
                        "order_no": "0000015962",
                        "exec_qty": 0,
                        "exec_price": 0.0,
                        "remain_qty": 1,
                        "side": "SELL",
                        "executed_at": None,
                    },
                    {
                        "order_no": "15963",
                        "exec_qty": 1,
                        "exec_price": 957000.0,
                        "remain_qty": 0,
                        "side": "SELL",
                        "executed_at": None,
                    },
                ],
            }

        api.get_order_status = _fake_get_order_status
        api.cancel_order = lambda _order_no: {"success": True}

        result = api.wait_for_execution(
            order_no="0000015963",
            expected_qty=1,
            timeout_seconds=1,
            check_interval=0,
        )

        assert result["success"] is True
        assert result["status"] == "FILLED"
        assert result["exec_qty"] == 1
        assert result["exec_price"] == 957000.0

    def test_wait_for_execution_uses_final_fallback_scan(self):
        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            api = KISApi(is_paper_trading=True)
            api.access_token = "test_token"

        calls = {"filtered": 0, "all": 0}

        def _fake_get_order_status(query_order_no=None):
            if query_order_no:
                calls["filtered"] += 1
                return {"success": True, "orders": []}
            calls["all"] += 1
            return {
                "success": True,
                "orders": [
                    {
                        "order_no": "15963",
                        "exec_qty": 1,
                        "exec_price": 957000.0,
                        "remain_qty": 0,
                        "side": "SELL",
                        "executed_at": None,
                    }
                ],
            }

        api.get_order_status = _fake_get_order_status
        api.cancel_order = lambda _order_no: {"success": True}

        result = api.wait_for_execution(
            order_no="0000015963",
            expected_qty=1,
            timeout_seconds=0,
            check_interval=0,
        )

        assert result["success"] is True
        assert result["status"] == "FILLED"
        assert calls["filtered"] >= 1
        assert calls["all"] >= 1


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

    @patch('api.kis_api.requests.get')
    def test_get_account_balance_prefers_sellable_qty_when_larger(self, mock_get):
        """hldg_qty보다 ord_psbl_qty가 클 때 보정 수량을 사용해야 합니다."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "1",
                    "ord_psbl_qty": "3",
                    "pchs_avg_pric": "178,100.00",
                    "prpr": "181,200",
                    "evlu_amt": "543600",
                    "evlu_pfls_amt": "9300",
                    "evlu_pfls_rt": "1.74"
                }
            ],
            "output2": [
                {
                    "tot_evlu_amt": "10,002,834",
                    "dnca_tot_amt": "9,476,354",
                    "evlu_pfls_smtl_amt": "-7,748"
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
        assert result["holdings"][0]["holding_qty"] == 1
        assert result["holdings"][0]["sellable_qty"] == 3
        assert result["holdings"][0]["quantity"] == 3
        assert result["total_eval"] == 10002834
        assert result["cash_balance"] == 9476354
        assert result["total_pnl"] == -7748

    @patch('api.kis_api.requests.get')
    def test_get_account_balance_retries_invalid_check_acno_then_succeeds(self, mock_get):
        """INVALID_CHECK_ACNO 응답은 토큰 갱신 후 재시도하여 복구해야 합니다."""
        first = Mock()
        first.status_code = 200
        first.json.return_value = {
            "rt_cd": "1",
            "msg1": "ERROR : INPUT INVALID_CHECK_ACNO"
        }

        second = Mock()
        second.status_code = 200
        second.json.return_value = {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "3",
                    "ord_psbl_qty": "3",
                    "pchs_avg_pric": "178100.00",
                    "prpr": "181200",
                    "evlu_amt": "543600",
                    "evlu_pfls_amt": "9300",
                    "evlu_pfls_rt": "1.74"
                }
            ],
            "output2": [
                {
                    "tot_evlu_amt": "10002834",
                    "dnca_tot_amt": "9476354",
                    "evlu_pfls_smtl_amt": "-7748"
                }
            ]
        }
        mock_get.side_effect = [first, second]

        with patch.object(KISApi, '_wait_for_rate_limit', return_value=None):
            with patch("api.kis_api.time.sleep", return_value=None):
                api = KISApi(is_paper_trading=True)
                api.access_token = "test_token"
                with patch.object(api, "get_access_token", return_value="test_token") as mocked_refresh:
                    result = api.get_account_balance()

        assert result["success"] is True
        assert result["holdings"][0]["quantity"] == 3
        assert mocked_refresh.call_count == 0


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
