"""
KIS Trend-ATR Trading System - 텔레그램 알림 모듈 테스트

텔레그램 알림 기능에 대한 단위 테스트입니다.
실제 API 호출 대신 Mock을 사용합니다.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.telegram_notifier import (
    TelegramNotifier,
    get_telegram_notifier,
    create_notifier_from_settings,
    AlertType,
    MESSAGE_TEMPLATES,
    TELEGRAM_API_BASE_URL
)


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_symbol_resolver():
    """종목 코드 -> 종목명 포맷 Mock"""
    resolver = MagicMock()

    def _format_symbol(code: str) -> str:
        table = {
            "005930": "삼성전자(005930)",
            "000660": "SK하이닉스(000660)",
        }
        key = str(code)
        return table.get(key, f"UNKNOWN({key})")

    resolver.format_symbol.side_effect = _format_symbol
    return resolver


@pytest.fixture
def mock_telegram_notifier(mock_symbol_resolver):
    """환경변수가 설정된 텔레그램 알림기를 생성합니다."""
    with patch.dict('os.environ', {
        'TELEGRAM_BOT_TOKEN': 'test_token_12345',
        'TELEGRAM_CHAT_ID': '123456789',
        'TELEGRAM_ENABLED': 'true'
    }):
        notifier = TelegramNotifier(symbol_resolver=mock_symbol_resolver)
        yield notifier


@pytest.fixture
def disabled_notifier(mock_symbol_resolver):
    """비활성화된 텔레그램 알림기를 생성합니다."""
    with patch.dict('os.environ', {
        'TELEGRAM_BOT_TOKEN': '',
        'TELEGRAM_CHAT_ID': '',
        'TELEGRAM_ENABLED': 'false'
    }):
        notifier = TelegramNotifier(symbol_resolver=mock_symbol_resolver)
        yield notifier


@pytest.fixture
def mock_requests_post():
    """requests.post를 Mock합니다."""
    with patch('utils.telegram_notifier.requests.post') as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_response
        yield mock_post


# ════════════════════════════════════════════════════════════════
# 초기화 테스트
# ════════════════════════════════════════════════════════════════

class TestTelegramNotifierInit:
    """TelegramNotifier 초기화 테스트"""
    
    def test_init_with_env_vars(self):
        """환경변수로 초기화되는지 테스트"""
        with patch.dict('os.environ', {
            'TELEGRAM_BOT_TOKEN': 'test_token',
            'TELEGRAM_CHAT_ID': '12345',
            'TELEGRAM_ENABLED': 'true'
        }):
            notifier = TelegramNotifier()
            assert notifier._bot_token == 'test_token'
            assert notifier._chat_id == '12345'
            assert notifier.enabled is True
    
    def test_init_with_params(self):
        """파라미터로 초기화되는지 테스트"""
        notifier = TelegramNotifier(
            bot_token='param_token',
            chat_id='67890',
            enabled=True
        )
        assert notifier._bot_token == 'param_token'
        assert notifier._chat_id == '67890'

    def test_init_enabled_with_whitespace_env(self):
        """TELEGRAM_ENABLED 공백 포함 값도 활성화로 해석되는지 테스트"""
        with patch.dict('os.environ', {
            'TELEGRAM_BOT_TOKEN': ' test_token ',
            'TELEGRAM_CHAT_ID': ' 12345 ',
            'TELEGRAM_ENABLED': ' TRUE '
        }, clear=True):
            notifier = TelegramNotifier()
            assert notifier.enabled is True
            assert notifier._bot_token == 'test_token'
            assert notifier._chat_id == '12345'
    
    def test_init_disabled_without_token(self):
        """토큰 없이 초기화하면 비활성화되는지 테스트"""
        with patch.dict('os.environ', {
            'TELEGRAM_BOT_TOKEN': '',
            'TELEGRAM_CHAT_ID': '12345',
            'TELEGRAM_ENABLED': 'true'
        }, clear=True):
            notifier = TelegramNotifier(bot_token='', chat_id='12345')
            assert notifier.enabled is False
    
    def test_init_disabled_without_chat_id(self):
        """채팅 ID 없이 초기화하면 비활성화되는지 테스트"""
        with patch.dict('os.environ', {
            'TELEGRAM_BOT_TOKEN': 'test_token',
            'TELEGRAM_CHAT_ID': '',
            'TELEGRAM_ENABLED': 'true'
        }, clear=True):
            notifier = TelegramNotifier(bot_token='test_token', chat_id='')
            assert notifier.enabled is False


# ════════════════════════════════════════════════════════════════
# 메시지 전송 테스트
# ════════════════════════════════════════════════════════════════

class TestSendMessage:
    """메시지 전송 테스트"""
    
    def test_send_message_success(self, mock_telegram_notifier, mock_requests_post):
        """메시지 전송 성공 테스트"""
        result = mock_telegram_notifier.send_message("테스트 메시지")
        
        assert result is True
        mock_requests_post.assert_called_once()
        
        # 전송된 payload 확인
        call_args = mock_requests_post.call_args
        assert call_args[1]['json']['chat_id'] == '123456789'
        assert call_args[1]['json']['text'] == "테스트 메시지"
        assert call_args[1]['json']['parse_mode'] == "Markdown"
    
    def test_send_message_disabled(self, disabled_notifier):
        """비활성화 상태에서 메시지 전송 시 False 반환"""
        result = disabled_notifier.send_message("테스트")
        assert result is False
    
    def test_send_message_truncate_long_text(self, mock_telegram_notifier, mock_requests_post):
        """4096자 초과 시 잘림 테스트"""
        long_text = "A" * 5000
        mock_telegram_notifier.send_message(long_text)
        
        call_args = mock_requests_post.call_args
        sent_text = call_args[1]['json']['text']
        assert len(sent_text) <= 4096
        assert sent_text.endswith("...")
    
    def test_send_message_api_failure(self, mock_telegram_notifier):
        """API 실패 시 재시도 테스트"""
        with patch('utils.telegram_notifier.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_post.return_value = mock_response
            
            # 재시도 대기 시간 최소화
            mock_telegram_notifier._retry_delay = 0.01
            mock_telegram_notifier._max_retries = 2
            
            result = mock_telegram_notifier.send_message("테스트")
            
            assert result is False
            assert mock_post.call_count == 2  # 재시도
    
    def test_send_message_timeout(self, mock_telegram_notifier):
        """타임아웃 발생 시 재시도 테스트"""
        from requests.exceptions import Timeout
        
        with patch('utils.telegram_notifier.requests.post') as mock_post:
            mock_post.side_effect = Timeout("Connection timed out")
            
            # 재시도 대기 시간 최소화
            mock_telegram_notifier._retry_delay = 0.01
            mock_telegram_notifier._max_retries = 2
            
            result = mock_telegram_notifier.send_message("테스트")
            
            assert result is False
            assert mock_post.call_count == 2

    def test_send_message_client_error_stops_retry(self, mock_telegram_notifier):
        """4xx 오류는 즉시 실패 처리(재시도 안 함) 테스트"""
        with patch('utils.telegram_notifier.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.json.return_value = {"description": "Bad Request: chat not found"}
            mock_post.return_value = mock_response

            mock_telegram_notifier._retry_delay = 0.01
            mock_telegram_notifier._max_retries = 3

            result = mock_telegram_notifier.send_message("테스트")

            assert result is False
            assert mock_post.call_count == 1

    def test_send_message_formats_direct_symbol_label(self, mock_telegram_notifier, mock_requests_post):
        """엔진 직접 메시지의 종목 라벨 포맷 보정 테스트"""
        direct_message = "• 종목: `005930`\n• 진입가: 70,000원"
        result = mock_telegram_notifier.send_message(direct_message)

        assert result is True
        sent_text = mock_requests_post.call_args[1]["json"]["text"]
        assert "삼성전자(005930)" in sent_text

    def test_send_message_formats_symbol_label_without_bullet(self, mock_telegram_notifier, mock_requests_post):
        """불릿 없는 `종목:` 라인도 포맷 보정되는지 테스트"""
        direct_message = "청산 보류 해제\n종목: 005930\n사유: exit_reason_changed"
        result = mock_telegram_notifier.send_message(direct_message)

        assert result is True
        sent_text = mock_requests_post.call_args[1]["json"]["text"]
        assert "삼성전자(005930)" in sent_text


# ════════════════════════════════════════════════════════════════
# 거래 알림 테스트
# ════════════════════════════════════════════════════════════════

class TestTradeNotifications:
    """거래 알림 테스트"""
    
    def test_notify_buy_order(self, mock_telegram_notifier, mock_requests_post):
        """매수 주문 알림 테스트"""
        result = mock_telegram_notifier.notify_buy_order(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=67000,
            take_profit=76000
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "매수 주문 체결" in text
        assert "삼성전자(005930)" in text
        assert "005930" in text
        assert "70,000" in text
        assert "10" in text
    
    def test_notify_sell_order(self, mock_telegram_notifier, mock_requests_post):
        """매도 주문 알림 테스트"""
        result = mock_telegram_notifier.notify_sell_order(
            stock_code="005930",
            price=75000,
            quantity=10,
            reason="익절 도달",
            pnl=50000,
            pnl_pct=7.14
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "매도 주문 체결" in text
        assert "삼성전자(005930)" in text
        assert "익절 도달" in text
        assert "+50,000" in text

    def test_notify_sell_order_escapes_reason(self, mock_telegram_notifier, mock_requests_post):
        """매도 사유의 마크다운 특수문자 이스케이프 테스트"""
        result = mock_telegram_notifier.notify_sell_order(
            stock_code="005930",
            price=75000,
            quantity=10,
            reason="TAKE_PROFIT[FAST]",
            pnl=50000,
            pnl_pct=7.14,
        )

        assert result is True
        text = mock_requests_post.call_args[1]['json']['text']
        assert "TAKE\\_PROFIT\\[FAST\\]" in text

    def test_notify_cbt_signal_uses_markdown_safe_header(self, mock_telegram_notifier, mock_requests_post):
        """CBT 시그널 헤더가 Markdown 링크 파싱과 충돌하지 않는지 테스트"""
        result = mock_telegram_notifier.notify_cbt_signal(
            signal_type="BUY",
            stock_code="005930",
            price=70000,
            stop_loss=68000,
            take_profit=74000,
            atr=1200,
            trend="UPTREND",
            reason="test",
        )

        assert result is True
        text = mock_requests_post.call_args[1]['json']['text']
        assert "(CBT) 매매 시그널" in text
    
    def test_notify_stop_loss(self, mock_telegram_notifier, mock_requests_post):
        """손절 청산 알림 테스트"""
        result = mock_telegram_notifier.notify_stop_loss(
            stock_code="005930",
            entry_price=70000,
            exit_price=67000,
            pnl=-30000,
            pnl_pct=-4.29
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "손절 청산" in text
        assert "70,000" in text
        assert "67,000" in text
    
    def test_notify_take_profit(self, mock_telegram_notifier, mock_requests_post):
        """익절 청산 알림 테스트"""
        result = mock_telegram_notifier.notify_take_profit(
            stock_code="005930",
            entry_price=70000,
            exit_price=79000,
            pnl=90000,
            pnl_pct=12.86
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "익절 청산" in text
        assert "목표 수익" in text

    def test_notify_gap_protection_formats_symbol(self, mock_telegram_notifier, mock_requests_post):
        """갭 보호 알림 종목명 포맷 테스트"""
        result = mock_telegram_notifier.notify_gap_protection(
            stock_code="005930",
            open_price=65000,
            stop_loss=66000,
            entry_price=70000,
            gap_loss_pct=2.3,
            raw_gap_pct=2.3456,
            reference_price=66500,
            reference_type="prev_close",
            reason_code="GAP_DOWN",
            pnl=-50000,
            pnl_pct=-7.14,
        )

        assert result is True
        text = mock_requests_post.call_args[1]["json"]["text"]
        assert "삼성전자(005930)" in text


# ════════════════════════════════════════════════════════════════
# 리스크 알림 테스트
# ════════════════════════════════════════════════════════════════

class TestRiskNotifications:
    """리스크 알림 테스트"""
    
    def test_notify_daily_loss_limit(self, mock_telegram_notifier, mock_requests_post):
        """일일 손실 한도 알림 테스트"""
        result = mock_telegram_notifier.notify_daily_loss_limit(
            daily_loss=-300000,
            loss_pct=-3.0,
            max_loss_pct=3.0
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "일일 손실 한도" in text
        assert "신규 주문이 차단" in text
    
    def test_notify_kill_switch(self, mock_telegram_notifier, mock_requests_post):
        """킬 스위치 발동 알림 테스트"""
        result = mock_telegram_notifier.notify_kill_switch("시스템 긴급 정지")
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "킬 스위치" in text
        assert "모든 거래가 즉시 중단" in text


# ════════════════════════════════════════════════════════════════
# 시스템 알림 테스트
# ════════════════════════════════════════════════════════════════

class TestSystemNotifications:
    """시스템 알림 테스트"""
    
    def test_notify_system_start(self, mock_telegram_notifier, mock_requests_post):
        """시스템 시작 알림 테스트"""
        result = mock_telegram_notifier.notify_system_start(
            stock_code="005930",
            order_quantity=10,
            interval=60,
            mode="모의투자"
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "시스템 시작" in text
        assert "005930" in text
        assert "모의투자" in text

    def test_notify_system_start_multi_symbols(self, mock_telegram_notifier, mock_requests_post):
        """시스템 시작 알림에서 다중 종목 포맷 테스트"""
        result = mock_telegram_notifier.notify_system_start(
            stock_code="['005930', '000660']",
            order_quantity=10,
            interval=60,
            mode="모의투자",
        )

        assert result is True
        text = mock_requests_post.call_args[1]["json"]["text"]
        assert "삼성전자(005930)" in text
        assert "SK하이닉스(000660)" in text
    
    def test_notify_system_stop(self, mock_telegram_notifier, mock_requests_post):
        """시스템 종료 알림 테스트"""
        result = mock_telegram_notifier.notify_system_stop(
            reason="사용자 중단",
            total_trades=5,
            daily_pnl=50000
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "시스템 종료" in text
        assert "사용자 중단" in text
        assert "5회" in text
    
    def test_notify_error(self, mock_telegram_notifier, mock_requests_post):
        """에러 알림 테스트"""
        result = mock_telegram_notifier.notify_error(
            error_type="API 오류",
            error_message="Connection refused"
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "오류 발생" in text
        assert "API 오류" in text
    
    def test_notify_warning(self, mock_telegram_notifier, mock_requests_post):
        """경고 알림 테스트"""
        result = mock_telegram_notifier.notify_warning("변동성이 급증하고 있습니다.")
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "경고" in text
        assert "변동성" in text

    def test_notify_warning_formats_inline_symbol_code(self, mock_telegram_notifier, mock_requests_post):
        """warning 본문 내 6자리 종목코드를 name(code)로 변환하는지 테스트"""
        result = mock_telegram_notifier.notify_warning("부분 체결: 005930 1/2주 @ 70,000원")

        assert result is True
        text = mock_requests_post.call_args[1]["json"]["text"]
        assert "삼성전자(005930)" in text
    
    def test_notify_info(self, mock_telegram_notifier, mock_requests_post):
        """정보 알림 테스트"""
        result = mock_telegram_notifier.notify_info("API 토큰이 갱신되었습니다.")
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "정보" in text
        assert "토큰" in text

    def test_notify_info_formats_multiline_symbol_line(self, mock_telegram_notifier, mock_requests_post):
        """info 본문의 종목 라인이 불릿 없이 와도 변환되는지 테스트"""
        result = mock_telegram_notifier.notify_info("청산 보류 해제\n종목: 000660\n사유: retry_key_changed")

        assert result is True
        text = mock_requests_post.call_args[1]["json"]["text"]
        assert "SK하이닉스(000660)" in text
    
    def test_notify_daily_summary(self, mock_telegram_notifier, mock_requests_post):
        """일일 요약 알림 테스트"""
        result = mock_telegram_notifier.notify_daily_summary(
            date="2024-01-15",
            total_trades=10,
            buy_count=5,
            sell_count=5,
            daily_pnl=150000,
            daily_pnl_pct=1.5,
            win_rate=60.0,
            max_profit=80000,
            max_loss=-30000
        )
        
        assert result is True
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "일일 거래 요약" in text
        assert "10회" in text
        assert "60.0%" in text


# ════════════════════════════════════════════════════════════════
# 유틸리티 테스트
# ════════════════════════════════════════════════════════════════

class TestUtilities:
    """유틸리티 함수 테스트"""
    
    def test_enable_disable(self, mock_telegram_notifier):
        """활성화/비활성화 토글 테스트"""
        mock_telegram_notifier.disable()
        assert mock_telegram_notifier.enabled is False
        
        mock_telegram_notifier.enable()
        assert mock_telegram_notifier.enabled is True
    
    def test_escape_markdown(self):
        """마크다운 이스케이프 테스트"""
        text = "Hello *world* [test] (link)"
        escaped = TelegramNotifier._escape_markdown(text)
        
        assert "\\*" in escaped
        assert "\\[" in escaped
        assert "\\]" in escaped
    
    def test_get_timestamp(self):
        """타임스탬프 형식 테스트"""
        timestamp = TelegramNotifier._get_timestamp()
        
        # YYYY-MM-DD HH:MM:SS 형식인지 확인
        assert len(timestamp) == 19
        assert timestamp[4] == '-'
        assert timestamp[7] == '-'
        assert timestamp[10] == ' '


# ════════════════════════════════════════════════════════════════
# 싱글톤 및 팩토리 테스트
# ════════════════════════════════════════════════════════════════

class TestSingletonAndFactory:
    """싱글톤 및 팩토리 함수 테스트"""
    
    def test_get_telegram_notifier_singleton(self):
        """싱글톤 인스턴스 테스트"""
        with patch.dict('os.environ', {
            'TELEGRAM_BOT_TOKEN': 'test_token',
            'TELEGRAM_CHAT_ID': '12345',
            'TELEGRAM_ENABLED': 'true'
        }):
            # 전역 인스턴스 초기화
            import utils.telegram_notifier as telegram_module
            telegram_module._notifier_instance = None
            
            notifier1 = get_telegram_notifier()
            notifier2 = get_telegram_notifier()
            
            assert notifier1 is notifier2


# ════════════════════════════════════════════════════════════════
# AlertType Enum 테스트
# ════════════════════════════════════════════════════════════════

class TestAlertType:
    """AlertType Enum 테스트"""
    
    def test_alert_type_values(self):
        """AlertType 값 확인"""
        assert AlertType.BUY_ORDER.value == "📈 매수 주문"
        assert AlertType.SELL_ORDER.value == "📉 매도 주문"
        assert AlertType.STOP_LOSS.value == "🛑 손절 청산"
        assert AlertType.TAKE_PROFIT.value == "🎯 익절 청산"
        assert AlertType.KILL_SWITCH.value == "🚨 킬 스위치 발동"


# ════════════════════════════════════════════════════════════════
# 메시지 템플릿 테스트
# ════════════════════════════════════════════════════════════════

class TestMessageTemplates:
    """메시지 템플릿 테스트"""
    
    def test_all_templates_exist(self):
        """모든 필수 템플릿 존재 확인"""
        required_templates = [
            "buy_order",
            "sell_order",
            "stop_loss",
            "take_profit",
            "daily_loss_limit",
            "kill_switch",
            "system_start",
            "system_stop",
            "error",
            "warning",
            "info",
            "daily_summary"
        ]
        
        for template_name in required_templates:
            assert template_name in MESSAGE_TEMPLATES, f"Missing template: {template_name}"
    
    def test_templates_contain_placeholders(self):
        """템플릿에 플레이스홀더 포함 확인"""
        assert "{stock_code}" in MESSAGE_TEMPLATES["buy_order"]
        assert "price" in MESSAGE_TEMPLATES["buy_order"]  # {price:,} 형식
        assert "{timestamp}" in MESSAGE_TEMPLATES["buy_order"]


# ════════════════════════════════════════════════════════════════
# 연결 테스트
# ════════════════════════════════════════════════════════════════

class TestConnection:
    """연결 테스트"""
    
    def test_connection_success(self, mock_telegram_notifier, mock_requests_post):
        """연결 테스트 성공"""
        result = mock_telegram_notifier.test_connection()
        
        assert result is True
        mock_requests_post.assert_called_once()
        
        call_args = mock_requests_post.call_args
        text = call_args[1]['json']['text']
        assert "테스트" in text
        assert "연결" in text
    
    def test_connection_disabled(self, disabled_notifier):
        """비활성화 상태에서 연결 테스트"""
        result = disabled_notifier.test_connection()
        assert result is False
