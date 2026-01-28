"""
trader/notifier.py - 텔레그램 알림 모듈

모든 주요 이벤트를 텔레그램으로 실시간 알림합니다.
API 실패 시 재시도 로직을 포함합니다.
"""

import os
import time
import traceback
from datetime import datetime
from typing import Optional

import requests
from requests.exceptions import RequestException, Timeout


class TelegramNotifier:
    """
    텔레그램 알림 클래스
    
    주요 이벤트 발생 시 텔레그램 메시지를 전송합니다.
    """
    
    TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: int = 10,
        max_retries: int = 3
    ):
        """
        Args:
            bot_token: 텔레그램 봇 토큰 (미입력 시 환경변수에서 로딩)
            chat_id: 텔레그램 채팅 ID (미입력 시 환경변수에서 로딩)
            timeout: API 타임아웃 (초)
            max_retries: 최대 재시도 횟수
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self.max_retries = max_retries
        
        self._enabled = bool(self.bot_token and self.chat_id)
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def send(self, message: str, parse_mode: str = "Markdown") -> bool:
        """
        텔레그램 메시지 전송
        
        Args:
            message: 전송할 메시지
            parse_mode: 파싱 모드 (Markdown, HTML)
        
        Returns:
            bool: 전송 성공 여부
        """
        if not self._enabled:
            print(f"[TELEGRAM] 비활성화 상태 - 메시지: {message[:50]}...")
            return False
        
        url = self.TELEGRAM_API_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": message[:4096],  # 텔레그램 메시지 길이 제한
            "parse_mode": parse_mode
        }
        
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(url, json=payload, timeout=self.timeout)
                
                if response.status_code == 200 and response.json().get("ok"):
                    return True
                    
            except (Timeout, RequestException) as e:
                if attempt < self.max_retries:
                    time.sleep(1 * attempt)  # 지수 백오프
                continue
        
        return False
    
    # ═══════════════════════════════════════════════════════════════
    # 이벤트별 알림 메서드
    # ═══════════════════════════════════════════════════════════════
    
    def notify_start(self, mode: str, trading_mode: str) -> bool:
        """프로그램 시작 알림"""
        message = f"""
🚀 *자동매매 시스템 시작*
━━━━━━━━━━━━━━━━━━
• 투자 성향: `{mode}`
• 거래 환경: `{trading_mode}`
• 시작 시간: `{self._timestamp()}`
━━━━━━━━━━━━━━━━━━
✅ 시스템이 정상적으로 시작되었습니다.
"""
        return self.send(message)
    
    def notify_stop(self, reason: str) -> bool:
        """프로그램 종료 알림"""
        message = f"""
⏹️ *자동매매 시스템 종료*
━━━━━━━━━━━━━━━━━━
• 종료 사유: {reason}
• 종료 시간: `{self._timestamp()}`
━━━━━━━━━━━━━━━━━━
"""
        return self.send(message)
    
    def notify_buy(
        self,
        stock_code: str,
        stock_name: str,
        price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float
    ) -> bool:
        """매수 체결 알림"""
        message = f"""
📈 *매수 체결*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}` {stock_name}
• 체결가: {price:,.0f}원
• 수량: {quantity:,}주
• 손절가: {stop_loss:,.0f}원 ({((stop_loss/price)-1)*100:.1f}%)
• 익절가: {take_profit:,.0f}원 (+{((take_profit/price)-1)*100:.1f}%)
━━━━━━━━━━━━━━━━━━
⏰ {self._timestamp()}
"""
        return self.send(message)
    
    def notify_sell(
        self,
        stock_code: str,
        stock_name: str,
        price: float,
        quantity: int,
        pnl: float,
        pnl_pct: float,
        reason: str
    ) -> bool:
        """매도 체결 알림"""
        emoji = "🎯" if pnl >= 0 else "📉"
        message = f"""
{emoji} *매도 체결*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}` {stock_name}
• 청산가: {price:,.0f}원
• 수량: {quantity:,}주
• 손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)
• 청산 사유: {reason}
━━━━━━━━━━━━━━━━━━
⏰ {self._timestamp()}
"""
        return self.send(message)
    
    def notify_stop_loss(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        loss: float,
        loss_pct: float
    ) -> bool:
        """손절 실행 알림"""
        message = f"""
🛑 *손절 실행*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}` {stock_name}
• 진입가: {entry_price:,.0f}원
• 손절가: {exit_price:,.0f}원
• 수량: {quantity:,}주
• 손실: {loss:,.0f}원 ({loss_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
💡 손절 기준에 따라 포지션이 청산되었습니다.
⏰ {self._timestamp()}
"""
        return self.send(message)
    
    def notify_take_profit(
        self,
        stock_code: str,
        stock_name: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        profit: float,
        profit_pct: float
    ) -> bool:
        """익절 실행 알림"""
        message = f"""
🎯 *익절 실행*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}` {stock_name}
• 진입가: {entry_price:,.0f}원
• 익절가: {exit_price:,.0f}원
• 수량: {quantity:,}주
• 수익: +{profit:,.0f}원 (+{profit_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
🎉 목표 수익에 도달했습니다!
⏰ {self._timestamp()}
"""
        return self.send(message)
    
    def notify_error(self, error: Exception, context: str = "") -> bool:
        """예외 발생 알림"""
        # Stack trace 요약 (최대 500자)
        tb = traceback.format_exc()
        tb_summary = tb[-500:] if len(tb) > 500 else tb
        
        message = f"""
❌ *예외 발생*
━━━━━━━━━━━━━━━━━━
• 컨텍스트: {context or "알 수 없음"}
• 오류 유형: `{type(error).__name__}`
• 오류 내용: {str(error)[:200]}
━━━━━━━━━━━━━━━━━━
```
{tb_summary}
```
━━━━━━━━━━━━━━━━━━
🔧 즉시 확인이 필요합니다.
⏰ {self._timestamp()}
"""
        return self.send(message)
    
    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════
# 전역 싱글톤 및 헬퍼 함수
# ═══════════════════════════════════════════════════════════════

_notifier_instance: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """싱글톤 TelegramNotifier 인스턴스 반환"""
    global _notifier_instance
    if _notifier_instance is None:
        _notifier_instance = TelegramNotifier()
    return _notifier_instance


def send_telegram(message: str) -> bool:
    """
    텔레그램 메시지 전송 헬퍼 함수
    
    Args:
        message: 전송할 메시지
    
    Returns:
        bool: 전송 성공 여부
    """
    return get_notifier().send(message)
