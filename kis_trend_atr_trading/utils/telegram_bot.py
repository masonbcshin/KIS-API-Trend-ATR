"""
KIS Trend-ATR Trading System - Telegram Bot 명령 처리

═══════════════════════════════════════════════════════════════════════════════
⚠️ 이 모듈은 텔레그램 봇 명령을 처리합니다.
═══════════════════════════════════════════════════════════════════════════════

★ 지원 명령:
  /halt - 즉시 모든 거래 중단 (Kill Switch)
  /status - 현재 시스템 상태 확인
  /resume - Kill Switch 해제 (신중하게!)
  /positions - 현재 포지션 조회
  /performance - 성과 요약 조회

★ 사용 방법:
  봇에게 직접 메시지를 보내거나 그룹에서 명령어를 사용합니다.

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

import os
import time
import threading
from datetime import datetime
from typing import Callable, Dict, Optional, Any
from dataclasses import dataclass

import requests
from requests.exceptions import RequestException

from utils.logger import get_logger

logger = get_logger("telegram_bot")

# 텔레그램 API
TELEGRAM_API_BASE = "https://api.telegram.org/bot"


@dataclass
class BotCommand:
    """봇 명령 데이터 클래스"""
    command: str
    description: str
    handler: Callable


class TelegramBotHandler:
    """
    텔레그램 봇 명령 핸들러
    
    ★ Kill Switch, 상태 조회 등의 명령을 처리합니다.
    ★ 백그라운드 스레드에서 메시지를 폴링합니다.
    """
    
    def __init__(
        self,
        bot_token: str = None,
        allowed_chat_ids: list = None,
        poll_interval: int = 5
    ):
        """
        Args:
            bot_token: 텔레그램 봇 토큰
            allowed_chat_ids: 허용된 채팅 ID 목록
            poll_interval: 메시지 폴링 간격 (초)
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.poll_interval = poll_interval
        
        # 허용된 채팅 ID
        allowed = os.getenv("TELEGRAM_CHAT_ID", "")
        if allowed_chat_ids:
            self.allowed_chat_ids = [str(c) for c in allowed_chat_ids]
        elif allowed:
            self.allowed_chat_ids = [allowed]
        else:
            self.allowed_chat_ids = []
        
        # API URL
        self.api_url = f"{TELEGRAM_API_BASE}{self.bot_token}"
        
        # 명령 핸들러
        self._commands: Dict[str, BotCommand] = {}
        self._register_default_commands()
        
        # 폴링 상태
        self._polling = False
        self._poll_thread: Optional[threading.Thread] = None
        self._last_update_id = 0
        
        # 콜백 함수들
        self._on_halt_callback: Optional[Callable] = None
        self._on_resume_callback: Optional[Callable] = None
        
        logger.info("[BOT] Telegram 봇 핸들러 초기화")
    
    def _register_default_commands(self) -> None:
        """기본 명령 등록"""
        self.register_command(
            "/halt",
            "즉시 모든 거래 중단 (Kill Switch)",
            self._handle_halt
        )
        self.register_command(
            "/status",
            "현재 시스템 상태 확인",
            self._handle_status
        )
        self.register_command(
            "/resume",
            "Kill Switch 해제 (신중하게!)",
            self._handle_resume
        )
        self.register_command(
            "/positions",
            "현재 포지션 조회",
            self._handle_positions
        )
        self.register_command(
            "/performance",
            "성과 요약 조회",
            self._handle_performance
        )
        self.register_command(
            "/help",
            "도움말",
            self._handle_help
        )
    
    def register_command(
        self,
        command: str,
        description: str,
        handler: Callable
    ) -> None:
        """명령 등록"""
        self._commands[command] = BotCommand(
            command=command,
            description=description,
            handler=handler
        )
    
    def set_halt_callback(self, callback: Callable) -> None:
        """Kill Switch 콜백 설정"""
        self._on_halt_callback = callback
    
    def set_resume_callback(self, callback: Callable) -> None:
        """Resume 콜백 설정"""
        self._on_resume_callback = callback
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 명령 핸들러
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _handle_halt(self, chat_id: str, message: str) -> str:
        """
        /halt 명령 처리 - Kill Switch 활성화
        """
        logger.warning(f"[BOT] /halt 명령 수신 from {chat_id}")
        
        try:
            # Kill Switch 활성화
            from config.execution_mode import get_execution_mode_manager
            manager = get_execution_mode_manager()
            manager.activate_kill_switch(f"텔레그램 /halt 명령 (chat_id: {chat_id})")
            
            # 콜백 실행
            if self._on_halt_callback:
                self._on_halt_callback()
            
            return """
🚨 *KILL SWITCH 활성화*
━━━━━━━━━━━━━━━━━━
⛔ 모든 거래가 즉시 중단되었습니다.

상태 확인: /status
재개하려면: /resume

⚠️ /resume은 매우 신중하게 사용하세요!
"""
        except Exception as e:
            logger.error(f"[BOT] /halt 처리 오류: {e}")
            return f"❌ Kill Switch 활성화 실패: {e}"
    
    def _handle_resume(self, chat_id: str, message: str) -> str:
        """
        /resume 명령 처리 - Kill Switch 해제
        """
        logger.warning(f"[BOT] /resume 명령 수신 from {chat_id}")
        
        # 확인 문구 체크
        if "CONFIRM" not in message.upper():
            return """
⚠️ *Kill Switch 해제 확인*
━━━━━━━━━━━━━━━━━━
Kill Switch를 해제하려면 다음 명령을 입력하세요:

`/resume CONFIRM`

🔴 신중하게 결정하세요!
"""
        
        try:
            from config.execution_mode import get_execution_mode_manager
            manager = get_execution_mode_manager()
            manager.deactivate_kill_switch()
            
            # 콜백 실행
            if self._on_resume_callback:
                self._on_resume_callback()
            
            return """
✅ *Kill Switch 해제됨*
━━━━━━━━━━━━━━━━━━
거래가 재개될 수 있습니다.

현재 상태: /status
"""
        except Exception as e:
            logger.error(f"[BOT] /resume 처리 오류: {e}")
            return f"❌ Kill Switch 해제 실패: {e}"
    
    def _handle_status(self, chat_id: str, message: str) -> str:
        """
        /status 명령 처리 - 시스템 상태 확인
        """
        try:
            from config.execution_mode import get_execution_mode_manager
            manager = get_execution_mode_manager()
            status = manager.get_status_dict()
            
            return f"""
📊 *시스템 상태*
━━━━━━━━━━━━━━━━━━
• 실행 모드: {status['mode_display']}
• Kill Switch: {'⛔ 활성화' if status['kill_switch_active'] else '✅ 비활성화'}
• 주문 가능: {'✅ 가능' if status['can_place_orders'] else '❌ 불가'}
• API URL: {status['api_url'][:30]}...

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        except Exception as e:
            logger.error(f"[BOT] /status 처리 오류: {e}")
            return f"❌ 상태 조회 실패: {e}"
    
    def _handle_positions(self, chat_id: str, message: str) -> str:
        """
        /positions 명령 처리 - 포지션 조회
        """
        try:
            from performance import get_performance_tracker
            tracker = get_performance_tracker()
            positions = tracker.get_open_positions()
            
            if not positions:
                return "📭 현재 열린 포지션이 없습니다."
            
            result = """
📈 *현재 포지션*
━━━━━━━━━━━━━━━━━━
"""
            for pos in positions:
                result += f"""
• `{pos.symbol}`
  진입가: {pos.entry_price:,.0f}원
  현재가: {pos.current_price:,.0f}원
  손익: {pos.unrealized_pnl:+,.0f}원 ({pos.unrealized_pnl_pct:+.2f}%)
  보유: {pos.get_holding_days()}일
"""
            
            return result
        except Exception as e:
            logger.error(f"[BOT] /positions 처리 오류: {e}")
            return f"❌ 포지션 조회 실패: {e}"
    
    def _handle_performance(self, chat_id: str, message: str) -> str:
        """
        /performance 명령 처리 - 성과 조회
        """
        try:
            from performance import get_performance_tracker
            tracker = get_performance_tracker()
            return tracker.generate_summary_text()
        except Exception as e:
            logger.error(f"[BOT] /performance 처리 오류: {e}")
            return f"❌ 성과 조회 실패: {e}"
    
    def _handle_help(self, chat_id: str, message: str) -> str:
        """
        /help 명령 처리 - 도움말
        """
        result = """
📚 *KIS Trend-ATR Bot 도움말*
━━━━━━━━━━━━━━━━━━

*사용 가능한 명령:*

"""
        for cmd in self._commands.values():
            result += f"• `{cmd.command}` - {cmd.description}\n"
        
        result += """
━━━━━━━━━━━━━━━━━━
⚠️ /halt는 즉시 모든 거래를 중단합니다.
   긴급 상황에서만 사용하세요!
"""
        return result
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 메시지 처리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _process_message(self, update: Dict) -> None:
        """수신된 메시지 처리"""
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        
        if not text or not chat_id:
            return
        
        # 권한 확인
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning(f"[BOT] 권한 없는 요청: chat_id={chat_id}")
            self._send_message(chat_id, "⛔ 권한이 없습니다.")
            return
        
        # 명령 처리
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        if command in self._commands:
            handler = self._commands[command].handler
            response = handler(chat_id, args)
            self._send_message(chat_id, response)
        elif text.startswith("/"):
            self._send_message(chat_id, "❓ 알 수 없는 명령입니다. /help를 입력하세요.")
    
    def _send_message(self, chat_id: str, text: str) -> bool:
        """메시지 전송"""
        if not self.bot_token:
            return False
        
        try:
            url = f"{self.api_url}/sendMessage"
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
            return response.status_code == 200
        except RequestException as e:
            logger.error(f"[BOT] 메시지 전송 실패: {e}")
            return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 폴링
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _poll_updates(self) -> None:
        """메시지 폴링 루프"""
        logger.info("[BOT] 메시지 폴링 시작")
        
        while self._polling:
            try:
                url = f"{self.api_url}/getUpdates"
                response = requests.get(
                    url,
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 30
                    },
                    timeout=35
                )
                
                if response.status_code == 200:
                    data = response.json()
                    for update in data.get("result", []):
                        self._last_update_id = update.get("update_id", self._last_update_id)
                        self._process_message(update)
                
            except RequestException as e:
                logger.warning(f"[BOT] 폴링 오류: {e}")
            
            time.sleep(self.poll_interval)
        
        logger.info("[BOT] 메시지 폴링 종료")
    
    def start_polling(self) -> None:
        """폴링 시작 (백그라운드)"""
        if not self.bot_token:
            logger.warning("[BOT] 봇 토큰이 없어 폴링을 시작할 수 없습니다.")
            return
        
        if self._polling:
            logger.warning("[BOT] 이미 폴링 중입니다.")
            return
        
        self._polling = True
        self._poll_thread = threading.Thread(target=self._poll_updates, daemon=True)
        self._poll_thread.start()
        logger.info("[BOT] 백그라운드 폴링 시작됨")
    
    def stop_polling(self) -> None:
        """폴링 중지"""
        self._polling = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        logger.info("[BOT] 폴링 중지됨")
    
    def is_polling(self) -> bool:
        """폴링 상태"""
        return self._polling


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_bot_handler: Optional[TelegramBotHandler] = None


def get_telegram_bot_handler() -> TelegramBotHandler:
    """싱글톤 TelegramBotHandler 인스턴스"""
    global _bot_handler
    
    if _bot_handler is None:
        _bot_handler = TelegramBotHandler()
    
    return _bot_handler


def start_telegram_bot() -> None:
    """텔레그램 봇 시작"""
    handler = get_telegram_bot_handler()
    handler.start_polling()


def stop_telegram_bot() -> None:
    """텔레그램 봇 중지"""
    global _bot_handler
    
    if _bot_handler:
        _bot_handler.stop_polling()
