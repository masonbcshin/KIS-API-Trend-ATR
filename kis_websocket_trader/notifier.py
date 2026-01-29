"""
KIS WebSocket 자동매매 시스템 - 텔레그램 알림 모듈

CBT 모드에서는 주문 대신 텔레그램 알림을 전송합니다.
LIVE 모드에서도 거래 알림을 전송합니다.

주요 기능:
    - 진입 시그널 알림
    - 손절 시그널 알림
    - 익절 시그널 알림
    - 시스템 상태 알림 (시작/종료/오류)
"""

import time
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

import requests
from requests.exceptions import RequestException, Timeout

from config import TelegramConfig, get_telegram_config, StockState


# 로거 설정
logger = logging.getLogger("notifier")


# ════════════════════════════════════════════════════════════════
# 상수
# ════════════════════════════════════════════════════════════════

TELEGRAM_API_BASE_URL = "https://api.telegram.org/bot"
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 3


# ════════════════════════════════════════════════════════════════
# 메시지 템플릿
# ════════════════════════════════════════════════════════════════

MESSAGE_TEMPLATES = {
    # 진입 시그널 (CBT 모드)
    "entry_signal_cbt": """
📈 *[CBT] 진입 시그널 발생*
━━━━━━━━━━━━━━━━━━
• 종목코드: `{stock_code}`
• 종목명: {stock_name}
• 현재가: {current_price:,}원
• 진입가: {entry_price:,}원
• 손절가: {stop_loss:,}원 ({sl_pct:.2f}%)
• 익절가: {take_profit:,}원 (+{tp_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
🔔 CBT 모드: 실주문 없음
⏰ {timestamp}
""",

    # 진입 시그널 (LIVE 모드)
    "entry_signal_live": """
📈 *[LIVE] 진입 주문 실행*
━━━━━━━━━━━━━━━━━━
• 종목코드: `{stock_code}`
• 종목명: {stock_name}
• 진입가: {entry_price:,}원
• 수량: {quantity}주
• 손절가: {stop_loss:,}원 ({sl_pct:.2f}%)
• 익절가: {take_profit:,}원 (+{tp_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
💰 주문금액: {order_amount:,}원
⏰ {timestamp}
""",

    # 손절 시그널 (CBT 모드)
    "stop_loss_cbt": """
🛑 *[CBT] 손절 시그널 발생*
━━━━━━━━━━━━━━━━━━
• 종목코드: `{stock_code}`
• 종목명: {stock_name}
• 진입가: {entry_price:,}원
• 현재가: {current_price:,}원
• 손절가: {stop_loss:,}원
• 손실률: {pnl_pct:.2f}%
━━━━━━━━━━━━━━━━━━
🔔 CBT 모드: 실주문 없음
⏰ {timestamp}
""",

    # 손절 시그널 (LIVE 모드)
    "stop_loss_live": """
🛑 *[LIVE] 손절 주문 실행*
━━━━━━━━━━━━━━━━━━
• 종목코드: `{stock_code}`
• 종목명: {stock_name}
• 진입가: {entry_price:,}원
• 청산가: {exit_price:,}원
• 손실: {pnl:,}원 ({pnl_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
💸 손실금액 확정
⏰ {timestamp}
""",

    # 익절 시그널 (CBT 모드)
    "take_profit_cbt": """
🎯 *[CBT] 익절 시그널 발생*
━━━━━━━━━━━━━━━━━━
• 종목코드: `{stock_code}`
• 종목명: {stock_name}
• 진입가: {entry_price:,}원
• 현재가: {current_price:,}원
• 익절가: {take_profit:,}원
• 수익률: +{pnl_pct:.2f}%
━━━━━━━━━━━━━━━━━━
🔔 CBT 모드: 실주문 없음
⏰ {timestamp}
""",

    # 익절 시그널 (LIVE 모드)
    "take_profit_live": """
🎯 *[LIVE] 익절 주문 실행*
━━━━━━━━━━━━━━━━━━
• 종목코드: `{stock_code}`
• 종목명: {stock_name}
• 진입가: {entry_price:,}원
• 청산가: {exit_price:,}원
• 수익: +{pnl:,}원 (+{pnl_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
🎉 수익 확정!
⏰ {timestamp}
""",

    # 시스템 시작
    "system_start": """
🚀 *자동매매 시스템 시작*
━━━━━━━━━━━━━━━━━━
• 모드: {mode}
• 감시 종목: {stock_count}개
• 진입 허용: {entry_start} ~ {entry_end}
• 종료 예정: {close_time}
━━━━━━━━━━━━━━━━━━
{stock_list}
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",

    # 시스템 종료
    "system_stop": """
⏹️ *자동매매 시스템 종료*
━━━━━━━━━━━━━━━━━━
• 종료 사유: {reason}
• 실행 시간: {duration}
━━━━━━━━━━━━━━━━━━
📊 *거래 요약*
• 진입: {entry_count}건
• 손절: {stop_loss_count}건
• 익절: {take_profit_count}건
• 대기중: {waiting_count}건
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",

    # 오류 발생
    "error": """
❌ *시스템 오류 발생*
━━━━━━━━━━━━━━━━━━
• 오류 유형: {error_type}
• 상세 내용:
```
{error_message}
```
━━━━━━━━━━━━━━━━━━
🔧 확인이 필요합니다.
⏰ {timestamp}
""",

    # WebSocket 재연결
    "ws_reconnect": """
🔄 *WebSocket 재연결*
━━━━━━━━━━━━━━━━━━
• 시도: {attempt}/{max_attempts}
• 사유: {reason}
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",

    # 가격 업데이트 (디버그용)
    "price_update": """
📊 *실시간 가격 업데이트*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 현재가: {current_price:,}원
• 상태: {state}
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",
}


# ════════════════════════════════════════════════════════════════
# 텔레그램 알림 클래스
# ════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    텔레그램 알림 클래스
    
    자동매매 시스템의 이벤트를 텔레그램으로 알림합니다.
    재시도 로직과 에러 핸들링을 포함합니다.
    
    Attributes:
        config: 텔레그램 설정
        _api_url: 텔레그램 API URL
    """
    
    def __init__(self, config: Optional[TelegramConfig] = None):
        """
        텔레그램 알림기 초기화
        
        Args:
            config: 텔레그램 설정 (None이면 환경변수에서 로드)
        """
        self.config = config or get_telegram_config()
        self._api_url = f"{TELEGRAM_API_BASE_URL}{self.config.bot_token}"
        
        if self.config.enabled:
            logger.info("[TELEGRAM] 텔레그램 알림 모듈 초기화 완료")
        else:
            logger.warning("[TELEGRAM] 텔레그램 알림이 비활성화 상태입니다.")
    
    @property
    def enabled(self) -> bool:
        """알림 활성화 여부"""
        return self.config.enabled
    
    # ════════════════════════════════════════════════════════════════
    # 기본 전송 메서드
    # ════════════════════════════════════════════════════════════════
    
    def send_message(
        self,
        text: str,
        parse_mode: str = "Markdown",
        disable_notification: bool = False
    ) -> bool:
        """
        텔레그램 메시지를 전송합니다.
        
        Args:
            text: 전송할 메시지
            parse_mode: 파싱 모드 (Markdown, HTML)
            disable_notification: 무음 알림 여부
            
        Returns:
            bool: 전송 성공 여부
        """
        if not self.config.enabled:
            logger.debug("[TELEGRAM] 알림 비활성화 - 전송 건너뜀")
            return False
        
        # 메시지 길이 제한 (텔레그램 최대 4096자)
        if len(text) > 4096:
            text = text[:4090] + "\n..."
            logger.warning("[TELEGRAM] 메시지가 4096자를 초과하여 잘림")
        
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification
        }
        
        return self._send_request("sendMessage", payload)
    
    def _send_request(self, method: str, payload: dict) -> bool:
        """
        텔레그램 API 요청을 전송합니다. (재시도 로직 포함)
        
        Args:
            method: API 메서드명
            payload: 요청 데이터
            
        Returns:
            bool: 요청 성공 여부
        """
        url = f"{self._api_url}/{method}"
        
        for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=DEFAULT_TIMEOUT
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        logger.debug("[TELEGRAM] 메시지 전송 성공")
                        return True
                    else:
                        logger.error(f"[TELEGRAM] API 오류: {result.get('description')}")
                else:
                    logger.error(f"[TELEGRAM] HTTP 오류: {response.status_code}")
                    
            except Timeout:
                logger.warning(f"[TELEGRAM] 타임아웃 (시도 {attempt}/{DEFAULT_MAX_RETRIES})")
            except RequestException as e:
                logger.error(f"[TELEGRAM] 요청 실패 (시도 {attempt}/{DEFAULT_MAX_RETRIES}): {e}")
            
            # 재시도 전 대기 (지수 백오프)
            if attempt < DEFAULT_MAX_RETRIES:
                delay = 1.0 * (2 ** (attempt - 1))
                time.sleep(delay)
        
        logger.error(f"[TELEGRAM] 최대 재시도 횟수 초과")
        return False
    
    @staticmethod
    def _get_timestamp() -> str:
        """현재 시간 문자열 반환"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # ════════════════════════════════════════════════════════════════
    # 거래 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_entry_signal(
        self,
        stock_code: str,
        stock_name: str,
        current_price: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        is_cbt_mode: bool = True,
        quantity: int = 0
    ) -> bool:
        """
        진입 시그널 알림을 전송합니다.
        
        Args:
            stock_code: 종목 코드
            stock_name: 종목명
            current_price: 현재가
            entry_price: 진입가
            stop_loss: 손절가
            take_profit: 익절가
            is_cbt_mode: CBT 모드 여부
            quantity: 주문 수량 (LIVE 모드용)
            
        Returns:
            bool: 전송 성공 여부
        """
        # 손절/익절 퍼센트 계산
        sl_pct = ((stop_loss - entry_price) / entry_price) * 100
        tp_pct = ((take_profit - entry_price) / entry_price) * 100
        
        if is_cbt_mode:
            template = MESSAGE_TEMPLATES["entry_signal_cbt"]
            message = template.format(
                stock_code=stock_code,
                stock_name=stock_name,
                current_price=int(current_price),
                entry_price=int(entry_price),
                stop_loss=int(stop_loss),
                sl_pct=sl_pct,
                take_profit=int(take_profit),
                tp_pct=tp_pct,
                timestamp=self._get_timestamp()
            )
        else:
            template = MESSAGE_TEMPLATES["entry_signal_live"]
            order_amount = int(entry_price * quantity)
            message = template.format(
                stock_code=stock_code,
                stock_name=stock_name,
                entry_price=int(entry_price),
                quantity=quantity,
                stop_loss=int(stop_loss),
                sl_pct=sl_pct,
                take_profit=int(take_profit),
                tp_pct=tp_pct,
                order_amount=order_amount,
                timestamp=self._get_timestamp()
            )
        
        return self.send_message(message)
    
    def notify_stop_loss(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        current_price: float,
        stop_loss: float,
        is_cbt_mode: bool = True,
        exit_price: float = 0,
        pnl: float = 0
    ) -> bool:
        """
        손절 시그널 알림을 전송합니다.
        
        Args:
            stock_code: 종목 코드
            stock_name: 종목명
            entry_price: 진입가
            current_price: 현재가
            stop_loss: 손절가
            is_cbt_mode: CBT 모드 여부
            exit_price: 청산가 (LIVE 모드용)
            pnl: 손익금액 (LIVE 모드용)
            
        Returns:
            bool: 전송 성공 여부
        """
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        
        if is_cbt_mode:
            template = MESSAGE_TEMPLATES["stop_loss_cbt"]
            message = template.format(
                stock_code=stock_code,
                stock_name=stock_name,
                entry_price=int(entry_price),
                current_price=int(current_price),
                stop_loss=int(stop_loss),
                pnl_pct=pnl_pct,
                timestamp=self._get_timestamp()
            )
        else:
            template = MESSAGE_TEMPLATES["stop_loss_live"]
            message = template.format(
                stock_code=stock_code,
                stock_name=stock_name,
                entry_price=int(entry_price),
                exit_price=int(exit_price or current_price),
                pnl=int(pnl),
                pnl_pct=pnl_pct,
                timestamp=self._get_timestamp()
            )
        
        return self.send_message(message)
    
    def notify_take_profit(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        current_price: float,
        take_profit: float,
        is_cbt_mode: bool = True,
        exit_price: float = 0,
        pnl: float = 0
    ) -> bool:
        """
        익절 시그널 알림을 전송합니다.
        
        Args:
            stock_code: 종목 코드
            stock_name: 종목명
            entry_price: 진입가
            current_price: 현재가
            take_profit: 익절가
            is_cbt_mode: CBT 모드 여부
            exit_price: 청산가 (LIVE 모드용)
            pnl: 손익금액 (LIVE 모드용)
            
        Returns:
            bool: 전송 성공 여부
        """
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        
        if is_cbt_mode:
            template = MESSAGE_TEMPLATES["take_profit_cbt"]
            message = template.format(
                stock_code=stock_code,
                stock_name=stock_name,
                entry_price=int(entry_price),
                current_price=int(current_price),
                take_profit=int(take_profit),
                pnl_pct=pnl_pct,
                timestamp=self._get_timestamp()
            )
        else:
            template = MESSAGE_TEMPLATES["take_profit_live"]
            message = template.format(
                stock_code=stock_code,
                stock_name=stock_name,
                entry_price=int(entry_price),
                exit_price=int(exit_price or current_price),
                pnl=int(pnl),
                pnl_pct=pnl_pct,
                timestamp=self._get_timestamp()
            )
        
        return self.send_message(message)
    
    # ════════════════════════════════════════════════════════════════
    # 시스템 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_system_start(
        self,
        mode: str,
        stock_list: list,
        entry_start: str,
        entry_end: str,
        close_time: str
    ) -> bool:
        """
        시스템 시작 알림을 전송합니다.
        
        Args:
            mode: 거래 모드 (CBT/LIVE)
            stock_list: 감시 종목 리스트 [(code, name), ...]
            entry_start: 진입 시작 시간
            entry_end: 진입 마감 시간
            close_time: 종료 시간
            
        Returns:
            bool: 전송 성공 여부
        """
        # 종목 리스트 문자열 생성
        stock_lines = []
        for i, (code, name) in enumerate(stock_list[:10], 1):  # 최대 10개만 표시
            stock_lines.append(f"  {i}. `{code}` {name}")
        
        if len(stock_list) > 10:
            stock_lines.append(f"  ... 외 {len(stock_list) - 10}개")
        
        stock_list_str = "\n".join(stock_lines) if stock_lines else "  (없음)"
        
        message = MESSAGE_TEMPLATES["system_start"].format(
            mode=mode,
            stock_count=len(stock_list),
            entry_start=entry_start,
            entry_end=entry_end,
            close_time=close_time,
            stock_list=stock_list_str,
            timestamp=self._get_timestamp()
        )
        
        return self.send_message(message)
    
    def notify_system_stop(
        self,
        reason: str,
        duration: str,
        entry_count: int,
        stop_loss_count: int,
        take_profit_count: int,
        waiting_count: int
    ) -> bool:
        """
        시스템 종료 알림을 전송합니다.
        
        Args:
            reason: 종료 사유
            duration: 실행 시간
            entry_count: 진입 건수
            stop_loss_count: 손절 건수
            take_profit_count: 익절 건수
            waiting_count: 대기중 건수
            
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["system_stop"].format(
            reason=reason,
            duration=duration,
            entry_count=entry_count,
            stop_loss_count=stop_loss_count,
            take_profit_count=take_profit_count,
            waiting_count=waiting_count,
            timestamp=self._get_timestamp()
        )
        
        return self.send_message(message)
    
    def notify_error(self, error_type: str, error_message: str) -> bool:
        """
        오류 발생 알림을 전송합니다.
        
        Args:
            error_type: 오류 유형
            error_message: 오류 메시지
            
        Returns:
            bool: 전송 성공 여부
        """
        # 마크다운 특수문자 이스케이프
        safe_message = self._escape_markdown(error_message)
        
        message = MESSAGE_TEMPLATES["error"].format(
            error_type=error_type,
            error_message=safe_message[:500],  # 메시지 길이 제한
            timestamp=self._get_timestamp()
        )
        
        return self.send_message(message)
    
    def notify_ws_reconnect(
        self,
        attempt: int,
        max_attempts: int,
        reason: str
    ) -> bool:
        """
        WebSocket 재연결 알림을 전송합니다.
        
        Args:
            attempt: 현재 시도 횟수
            max_attempts: 최대 시도 횟수
            reason: 재연결 사유
            
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["ws_reconnect"].format(
            attempt=attempt,
            max_attempts=max_attempts,
            reason=reason,
            timestamp=self._get_timestamp()
        )
        
        return self.send_message(message, disable_notification=True)
    
    @staticmethod
    def _escape_markdown(text: str) -> str:
        """마크다운 특수문자를 이스케이프합니다."""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text
    
    def test_connection(self) -> bool:
        """
        텔레그램 연결을 테스트합니다.
        
        Returns:
            bool: 연결 성공 여부
        """
        if not self.config.enabled:
            logger.warning("[TELEGRAM] 알림이 비활성화되어 테스트를 건너뜁니다.")
            return False
        
        test_message = f"""
🔔 *텔레그램 알림 테스트*
━━━━━━━━━━━━━━━━━━
✅ 연결이 정상적으로 설정되었습니다.
━━━━━━━━━━━━━━━━━━
⏰ {self._get_timestamp()}
"""
        
        result = self.send_message(test_message)
        
        if result:
            logger.info("[TELEGRAM] 연결 테스트 성공")
        else:
            logger.error("[TELEGRAM] 연결 테스트 실패")
        
        return result


# ════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ════════════════════════════════════════════════════════════════

_notifier_instance: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """
    싱글톤 TelegramNotifier 인스턴스를 반환합니다.
    
    Returns:
        TelegramNotifier: 텔레그램 알림기 인스턴스
    """
    global _notifier_instance
    
    if _notifier_instance is None:
        _notifier_instance = TelegramNotifier()
    
    return _notifier_instance


# ════════════════════════════════════════════════════════════════
# 직접 실행 시 테스트
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    
    notifier = get_notifier()
    
    if notifier.enabled:
        print("텔레그램 연결 테스트 중...")
        if notifier.test_connection():
            print("✅ 연결 성공!")
        else:
            print("❌ 연결 실패")
    else:
        print("⚠️ 텔레그램 알림이 비활성화 상태입니다.")
        print("   .env 파일에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 설정하세요.")
