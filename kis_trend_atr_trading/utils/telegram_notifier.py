"""
KIS Trend-ATR Trading System - 텔레그램 알림 모듈

이 모듈은 자동매매 시스템의 주요 이벤트를 텔레그램으로 알림합니다.

지원 이벤트:
    - 매수/매도 주문 체결
    - 손절/익절 청산
    - 일일 손실 한도 도달
    - 킬 스위치 발동
    - 시스템 오류 발생

설정 방법:
    1. BotFather (@BotFather)에서 봇 생성 후 토큰 발급
    2. 봇과 대화 시작 후 chat_id 확인
    3. .env 파일에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 설정

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
"""

import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

import requests
from requests.exceptions import RequestException, Timeout

from .logger import get_logger
from .market_hours import KST
from .symbol_resolver import SymbolResolver, get_symbol_resolver

logger = get_logger("telegram_notifier")


# ════════════════════════════════════════════════════════════════
# 상수 및 열거형
# ════════════════════════════════════════════════════════════════

class AlertType(Enum):
    """알림 유형 열거형"""
    # 거래 알림
    BUY_ORDER = "📈 매수 주문"
    SELL_ORDER = "📉 매도 주문"
    STOP_LOSS = "🛑 손절 청산"
    TAKE_PROFIT = "🎯 익절 청산"
    
    # 리스크 알림
    DAILY_LOSS_LIMIT = "⚠️ 일일 손실 한도"
    KILL_SWITCH = "🚨 킬 스위치 발동"
    
    # 시스템 알림
    SYSTEM_START = "🚀 시스템 시작"
    SYSTEM_STOP = "⏹️ 시스템 종료"
    ERROR = "❌ 오류 발생"
    WARNING = "⚠️ 경고"
    INFO = "ℹ️ 정보"


# 기본 설정값
DEFAULT_TIMEOUT = 10  # 초
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0  # 초
TELEGRAM_API_BASE_URL = "https://api.telegram.org/bot"


# ════════════════════════════════════════════════════════════════
# 메시지 템플릿
# ════════════════════════════════════════════════════════════════

MESSAGE_TEMPLATES = {
    # 매수 주문
    "buy_order": """
📈 *매수 주문 체결*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 체결가: {price:,}원
• 수량: {quantity}주
• 손절가: {stop_loss:,}원
• 익절가: {take_profit:,}원
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",
    
    # 매도 주문
    "sell_order": """
📉 *매도 주문 체결*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 청산가: {price:,}원
• 수량: {quantity}주
• 청산 사유: {reason}
• 손익: {pnl:+,}원 ({pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",
    
    # 손절 청산
    "stop_loss": """
🛑 *손절 청산 완료*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 진입가: {entry_price:,}원
• 청산가: {exit_price:,}원
• 손실: {pnl:,}원 ({pnl_pct:.2f}%)
━━━━━━━━━━━━━━━━━━
💡 손절 기준에 따라 포지션이 청산되었습니다.
⏰ {timestamp}
""",
    
    # 익절 청산
    "take_profit": """
🎯 *익절 청산 완료*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 진입가: {entry_price:,}원
• 청산가: {exit_price:,}원
• 수익: {pnl:+,}원 ({pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
🎉 목표 수익에 도달했습니다!
⏰ {timestamp}
""",
    
    # 일일 손실 한도 도달
    "daily_loss_limit": """
⚠️ *일일 손실 한도 도달*
━━━━━━━━━━━━━━━━━━
• 당일 누적 손실: {daily_loss:,}원
• 손실률: {loss_pct:.2f}%
• 한도: -{max_loss_pct}%
━━━━━━━━━━━━━━━━━━
🔒 신규 주문이 차단되었습니다.
   기존 포지션 청산만 허용됩니다.
⏰ {timestamp}
""",
    
    # 킬 스위치 발동
    "kill_switch": """
🚨 *긴급: 킬 스위치 발동*
━━━━━━━━━━━━━━━━━━
{reason}
━━━━━━━━━━━━━━━━━━
⛔ 모든 거래가 즉시 중단됩니다.
   시스템이 안전하게 종료됩니다.
⏰ {timestamp}
""",
    
    # 시스템 시작
    "system_start": """
🚀 *자동매매 시스템 시작*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 주문 수량: {order_quantity}주
• 실행 간격: {interval}초
• 모드: {mode}
━━━━━━━━━━━━━━━━━━
✅ 시스템이 정상적으로 시작되었습니다.
⏰ {timestamp}
""",
    
    # 시스템 종료
    "system_stop": """
⏹️ *자동매매 시스템 종료*
━━━━━━━━━━━━━━━━━━
• 종료 사유: {reason}
• 당일 거래: {total_trades}회
• 당일 손익: {daily_pnl:+,}원
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
🔧 즉시 확인이 필요합니다.
⏰ {timestamp}
""",
    
    # 일반 경고
    "warning": """
⚠️ *경고*
━━━━━━━━━━━━━━━━━━
{message}
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",
    
    # 일반 정보
    "info": """
ℹ️ *정보*
━━━━━━━━━━━━━━━━━━
{message}
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",

    # 일일 요약
    "daily_summary": """
📊 *일일 거래 요약*
━━━━━━━━━━━━━━━━━━
📅 날짜: {date}
• 총 거래: {total_trades}회
• 매수: {buy_count}회 | 매도: {sell_count}회
• 당일 손익: {daily_pnl:+,}원 ({daily_pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
• 승률: {win_rate:.1f}%
• 최대 수익: {max_profit:+,}원
• 최대 손실: {max_loss:,}원
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""",

    # 포지션 복원 알림 (멀티데이)
    "position_restored": """
🔄 *포지션 복원 완료*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 진입가: {entry_price:,}원
• 보유수량: {quantity}주
• 진입일: {entry_date}
• 보유일수: {holding_days}일
━━━━━━━━━━━━━━━━━━
• 손절가: {stop_loss:,}원
• 익절가: {take_profit}
• 트레일링: {trailing_stop:,}원
• 진입ATR: {atr_at_entry:,.0f} (고정)
━━━━━━━━━━━━━━━━━━
✅ Exit 조건 감시 재개
⏰ {timestamp}
""",

    # 손절선 근접 경고
    "near_stop_loss": """
⚠️ *손절선 근접 경고*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 현재가: {current_price:,}원
• 손절가: {stop_loss:,}원
• 도달률: {progress:.1f}%
━━━━━━━━━━━━━━━━━━
• 진입가: {entry_price:,}원
• 현재 손익: {pnl:+,}원 ({pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
💡 손절선까지 {remaining:,.0f}원 남음
⏰ {timestamp}
""",

    # 익절선 근접 알림
    "near_take_profit": """
🎯 *익절선 근접 알림*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 현재가: {current_price:,}원
• 익절가: {take_profit:,}원
• 도달률: {progress:.1f}%
━━━━━━━━━━━━━━━━━━
• 진입가: {entry_price:,}원
• 현재 손익: {pnl:+,}원 ({pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
🎉 익절선까지 {remaining:,.0f}원 남음
⏰ {timestamp}
""",

    # 트레일링 스탑 갱신 알림
    "trailing_stop_updated": """
📈 *트레일링 스탑 갱신*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 최고가 갱신: {highest_price:,}원
• 새 트레일링: {trailing_stop:,}원
━━━━━━━━━━━━━━━━━━
• 진입가: {entry_price:,}원
• 현재 손익: {pnl:+,}원 ({pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
💡 수익 보호 구간 확대
⏰ {timestamp}
""",

    # CBT 모드 시그널 알림
    "cbt_signal": """
📋 *[CBT] 매매 시그널*
━━━━━━━━━━━━━━━━━━
• 시그널: {signal_type}
• 종목: `{stock_code}`
• 가격: {price:,}원
━━━━━━━━━━━━━━━━━━
• 손절가: {stop_loss:,}원
• 익절가: {take_profit}
• ATR: {atr:,.0f}원
• 추세: {trend}
━━━━━━━━━━━━━━━━━━
📝 사유: {reason}
━━━━━━━━━━━━━━━━━━
🔒 CBT 모드: 실주문 없음
⏰ {timestamp}
""",

    # 갭 보호 발동 알림
    "gap_protection": """
🛡️ *갭 보호 발동*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 시가: {open_price:,}원
• 기준가({reference_type}): {reference_price:,}원
• 손절가: {stop_loss:,}원
• 갭(raw): {raw_gap_pct:.6f}%
• 갭(표시): {gap_loss_pct:.3f}%
• reason: `{reason_code}`
━━━━━━━━━━━━━━━━━━
• 진입가: {entry_price:,}원
• 예상 손익: {pnl:+,}원 ({pnl_pct:+.2f}%)
━━━━━━━━━━━━━━━━━━
⚠️ 즉시 시장가 청산 실행
⏰ {timestamp}
""",

    # CBT 누적 성과 리포트
    "cbt_performance_report": """
🧪 *CBT 성과 리포트*
━━━━━━━━━━━━━━━━━━
📅 기준일: {report_date}

💰 자본금 현황
━━━━━━━━━━━━━━━━━━
• 초기 자본금: {initial_capital:,}원
• 현재 평가금: {final_equity:,}원
• 총 수익률: {total_return_pct:+.2f}%
• 실현 손익: {realized_pnl:+,}원
• 미실현 손익: {unrealized_pnl:+,}원

📈 거래 성과
━━━━━━━━━━━━━━━━━━
• 총 거래: {total_trades}회
• 승률: {win_rate:.1f}%
• Expectancy: {expectancy:+,.0f}원

📉 리스크 지표
━━━━━━━━━━━━━━━━━━
• Maximum Drawdown: {max_drawdown_pct:.2f}%
• Profit Factor: {profit_factor:.2f}

━━━━━━━━━━━━━━━━━━
🔒 CBT 모드: 실주문 없음
⏰ {timestamp}
""",

    # CBT 거래 완료 알림
    "cbt_trade_complete": """
🧪 *[CBT] 거래 완료*
━━━━━━━━━━━━━━━━━━
• 종목: `{stock_code}`
• 방향: {trade_type}
• 진입가: {entry_price:,}원
• 청산가: {exit_price:,}원
• 수량: {quantity}주
━━━━━━━━━━━━━━━━━━
• 순손익: {pnl:+,}원 ({return_pct:+.2f}%)
• 보유일수: {holding_days}일
• 청산사유: {exit_reason}
━━━━━━━━━━━━━━━━━━
📊 누적 성과
• 총 거래: {total_trades}회
• 누적 수익률: {cumulative_return_pct:+.2f}%
• 승률: {win_rate:.1f}%
━━━━━━━━━━━━━━━━━━
🔒 CBT 모드: 실주문 없음
⏰ {timestamp}
"""
}


# ════════════════════════════════════════════════════════════════
# 텔레그램 알림 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class TelegramConfig:
    """텔레그램 설정 데이터 클래스"""
    bot_token: str
    chat_id: str
    enabled: bool = True
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_delay: float = DEFAULT_RETRY_DELAY


class TelegramNotifier:
    """
    텔레그램 알림 클래스
    
    자동매매 시스템의 주요 이벤트를 텔레그램으로 알림합니다.
    API 실패 시 자동 재시도 기능을 제공합니다.
    
    Usage:
        notifier = TelegramNotifier()  # 환경변수에서 자동 로드
        
        # 또는 직접 설정
        notifier = TelegramNotifier(
            bot_token="your_bot_token",
            chat_id="your_chat_id"
        )
        
        # 메시지 전송
        notifier.send_message("테스트 메시지")
        
        # 매수 알림
        notifier.notify_buy_order(
            stock_code="005930",
            price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000
        )
    """
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        symbol_resolver: Optional[SymbolResolver] = None,
    ):
        """
        텔레그램 알림기 초기화
        
        Args:
            bot_token: 텔레그램 봇 토큰 (미입력 시 환경변수에서 로드)
            chat_id: 텔레그램 채팅 ID (미입력 시 환경변수에서 로드)
            enabled: 알림 활성화 여부
            timeout: API 요청 타임아웃 (초)
            max_retries: 최대 재시도 횟수
            retry_delay: 재시도 간 대기 시간 (초)
        """
        # 환경변수에서 로드
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        
        # 활성화 여부 (환경변수 우선)
        env_enabled = os.getenv("TELEGRAM_ENABLED", "true").lower()
        self._enabled = enabled and env_enabled in ("true", "1", "yes")
        
        # API 설정
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        
        # API URL
        self._api_url = f"{TELEGRAM_API_BASE_URL}{self._bot_token}"

        # 종목명 Resolver (알림 포맷 전용)
        self._symbol_resolver = symbol_resolver or get_symbol_resolver()
        
        # 설정 검증
        self._validate_config()
        
        if self._enabled:
            logger.info("[TELEGRAM] 텔레그램 알림 모듈 초기화 완료")
        else:
            logger.warning("[TELEGRAM] 텔레그램 알림이 비활성화되었습니다.")
    
    def _validate_config(self) -> None:
        """설정 유효성 검증"""
        if not self._enabled:
            return
        
        if not self._bot_token:
            logger.warning(
                "[TELEGRAM] TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. "
                "알림이 비활성화됩니다."
            )
            self._enabled = False
            return
        
        if not self._chat_id:
            logger.warning(
                "[TELEGRAM] TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
                "알림이 비활성화됩니다."
            )
            self._enabled = False
            return
    
    @property
    def enabled(self) -> bool:
        """알림 활성화 상태"""
        return self._enabled
    
    def enable(self) -> None:
        """알림 활성화"""
        if self._bot_token and self._chat_id:
            self._enabled = True
            logger.info("[TELEGRAM] 텔레그램 알림 활성화됨")
        else:
            logger.warning(
                "[TELEGRAM] 봇 토큰 또는 채팅 ID가 없어 활성화할 수 없습니다."
            )
    
    def disable(self) -> None:
        """알림 비활성화"""
        self._enabled = False
        logger.info("[TELEGRAM] 텔레그램 알림 비활성화됨")

    def _format_symbol(self, stock_code: str) -> str:
        """종목코드를 `종목명(종목코드)` 형태로 포맷합니다."""
        try:
            return self._symbol_resolver.format_symbol(stock_code)
        except Exception as e:
            code = str(stock_code or "").strip()
            logger.warning(f"[TELEGRAM] 종목명 포맷 실패: code={code}, err={e}")
            return f"UNKNOWN({code})"

    def _format_symbol_codes_in_text(self, text: str) -> str:
        """
        문자열 내 6자리 종목코드를 `종목명(코드)`로 변환합니다.
        (system_start 등 복수 코드 문자열 처리용)
        """
        raw = str(text or "")
        pattern = re.compile(r"\b\d{6}\b")
        return pattern.sub(lambda m: self._format_symbol(m.group(0)), raw)

    def _format_symbol_label_lines(self, text: str) -> str:
        """
        직접 구성된 메시지에서 `종목:`/`종목코드:` 라인의 코드만 안전하게 포맷합니다.
        (`•` 불릿 유무와 무관)
        """
        if not text:
            return text
        pattern = re.compile(
            r"(^\s*(?:•\s*)?종목(?:코드)?\s*:\s*`?)(\d{6})(`?)",
            re.MULTILINE,
        )
        return pattern.sub(
            lambda m: f"{m.group(1)}{self._format_symbol(m.group(2))}{m.group(3)}",
            text,
        )
    
    # ════════════════════════════════════════════════════════════════
    # 핵심 전송 메서드
    # ════════════════════════════════════════════════════════════════
    
    def send_message(
        self,
        text: str,
        parse_mode: str = "Markdown",
        disable_notification: bool = False
    ) -> bool:
        """
        텔레그램 메시지 전송
        
        Args:
            text: 전송할 메시지 텍스트
            parse_mode: 파싱 모드 (Markdown, HTML, None)
            disable_notification: 무음 알림 여부
        
        Returns:
            bool: 전송 성공 여부
        """
        if not self._enabled:
            logger.debug("[TELEGRAM] 알림 비활성화 상태 - 전송 건너뜀")
            return False

        # 엔진에서 직접 구성한 메시지(• 종목: 005930)도 알림 전송 시점에 보정
        text = self._format_symbol_label_lines(text)
        
        # 메시지 길이 제한 (텔레그램 최대 4096자)
        if len(text) > 4096:
            text = text[:4090] + "\n..."
            logger.warning("[TELEGRAM] 메시지가 4096자를 초과하여 잘림")
        
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification
        }
        
        return self._send_request("sendMessage", payload)
    
    def _send_request(
        self,
        method: str,
        payload: Dict[str, Any]
    ) -> bool:
        """
        텔레그램 API 요청 전송 (재시도 로직 포함)
        
        Args:
            method: API 메서드명
            payload: 요청 데이터
        
        Returns:
            bool: 요청 성공 여부
        """
        url = f"{self._api_url}/{method}"
        
        for attempt in range(1, self._max_retries + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=self._timeout
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        logger.debug(f"[TELEGRAM] 메시지 전송 성공")
                        return True
                    else:
                        logger.error(
                            f"[TELEGRAM] API 응답 오류: {result.get('description')}"
                        )
                else:
                    logger.error(
                        f"[TELEGRAM] HTTP 오류: {response.status_code}"
                    )
                    
            except Timeout:
                logger.warning(
                    f"[TELEGRAM] 요청 타임아웃 (시도 {attempt}/{self._max_retries})"
                )
            except RequestException as e:
                logger.error(
                    f"[TELEGRAM] 요청 실패 (시도 {attempt}/{self._max_retries}): {e}"
                )
            
            # 마지막 시도가 아니면 대기 후 재시도
            if attempt < self._max_retries:
                delay = self._retry_delay * (2 ** (attempt - 1))  # 지수 백오프
                logger.debug(f"[TELEGRAM] {delay}초 후 재시도...")
                time.sleep(delay)
        
        logger.error(
            f"[TELEGRAM] 최대 재시도 횟수({self._max_retries})를 초과했습니다."
        )
        return False
    
    # ════════════════════════════════════════════════════════════════
    # 거래 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_buy_order(
        self,
        stock_code: str,
        price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float
    ) -> bool:
        """
        매수 주문 체결 알림
        
        Args:
            stock_code: 종목 코드
            price: 체결가
            quantity: 수량
            stop_loss: 손절가
            take_profit: 익절가
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        try:
            message = MESSAGE_TEMPLATES["buy_order"].format(
                stock_code=display_symbol,
                price=int(float(price)),
                quantity=int(quantity),
                stop_loss=int(float(stop_loss)),
                take_profit=int(float(take_profit)),
                timestamp=self._get_timestamp()
            )
            return self.send_message(message)
        except Exception as e:
            logger.error(f"[TELEGRAM] 매수 알림 포맷 실패: {e}")
            # 포맷 실패 시 단순 텍스트로 폴백
            fallback = (
                f"[BUY] {display_symbol} {quantity}주 체결 "
                f"price={price}, stop={stop_loss}, take={take_profit}, "
                f"time={self._get_timestamp()}"
            )
            return self.send_message(fallback, parse_mode=None)
    
    def notify_sell_order(
        self,
        stock_code: str,
        price: float,
        quantity: int,
        reason: str,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        매도 주문 체결 알림
        
        Args:
            stock_code: 종목 코드
            price: 청산가
            quantity: 수량
            reason: 청산 사유
            pnl: 손익 금액
            pnl_pct: 손익률
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        message = MESSAGE_TEMPLATES["sell_order"].format(
            stock_code=display_symbol,
            price=int(price),
            quantity=quantity,
            reason=reason,
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_stop_loss(
        self,
        stock_code: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        손절 청산 알림
        
        Args:
            stock_code: 종목 코드
            entry_price: 진입가
            exit_price: 청산가
            pnl: 손실 금액
            pnl_pct: 손실률
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        message = MESSAGE_TEMPLATES["stop_loss"].format(
            stock_code=display_symbol,
            entry_price=int(entry_price),
            exit_price=int(exit_price),
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_take_profit(
        self,
        stock_code: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        익절 청산 알림
        
        Args:
            stock_code: 종목 코드
            entry_price: 진입가
            exit_price: 청산가
            pnl: 수익 금액
            pnl_pct: 수익률
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        message = MESSAGE_TEMPLATES["take_profit"].format(
            stock_code=display_symbol,
            entry_price=int(entry_price),
            exit_price=int(exit_price),
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    # ════════════════════════════════════════════════════════════════
    # 리스크 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_daily_loss_limit(
        self,
        daily_loss: float,
        loss_pct: float,
        max_loss_pct: float
    ) -> bool:
        """
        일일 손실 한도 도달 알림
        
        Args:
            daily_loss: 당일 누적 손실
            loss_pct: 손실률
            max_loss_pct: 최대 손실 한도
        
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["daily_loss_limit"].format(
            daily_loss=int(abs(daily_loss)),
            loss_pct=abs(loss_pct),
            max_loss_pct=max_loss_pct,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_kill_switch(self, reason: str) -> bool:
        """
        킬 스위치 발동 알림
        
        Args:
            reason: 발동 사유
        
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["kill_switch"].format(
            reason=reason,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    # ════════════════════════════════════════════════════════════════
    # 시스템 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_system_start(
        self,
        stock_code: str,
        order_quantity: int,
        interval: int,
        mode: str = "모의투자"
    ) -> bool:
        """
        시스템 시작 알림
        
        Args:
            stock_code: 종목 코드
            order_quantity: 주문 수량
            interval: 실행 간격
            mode: 실행 모드
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbols = self._format_symbol_codes_in_text(stock_code)
        message = MESSAGE_TEMPLATES["system_start"].format(
            stock_code=display_symbols,
            order_quantity=order_quantity,
            interval=interval,
            mode=mode,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_system_stop(
        self,
        reason: str,
        total_trades: int,
        daily_pnl: float
    ) -> bool:
        """
        시스템 종료 알림
        
        Args:
            reason: 종료 사유
            total_trades: 당일 거래 횟수
            daily_pnl: 당일 손익
        
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["system_stop"].format(
            reason=reason,
            total_trades=total_trades,
            daily_pnl=int(daily_pnl),
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_error(
        self,
        error_type: str,
        error_message: str
    ) -> bool:
        """
        오류 발생 알림
        
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
            error_message=safe_message,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_warning(self, message: str) -> bool:
        """
        경고 알림
        
        Args:
            message: 경고 메시지
        
        Returns:
            bool: 전송 성공 여부
        """
        display_message = self._format_symbol_codes_in_text(message)
        formatted = MESSAGE_TEMPLATES["warning"].format(
            message=display_message,
            timestamp=self._get_timestamp()
        )
        return self.send_message(formatted)
    
    def notify_info(self, message: str) -> bool:
        """
        정보 알림
        
        Args:
            message: 정보 메시지
        
        Returns:
            bool: 전송 성공 여부
        """
        display_message = self._format_symbol_codes_in_text(message)
        formatted = MESSAGE_TEMPLATES["info"].format(
            message=display_message,
            timestamp=self._get_timestamp()
        )
        return self.send_message(formatted)
    
    def notify_daily_summary(
        self,
        date: str,
        total_trades: int,
        buy_count: int,
        sell_count: int,
        daily_pnl: float,
        daily_pnl_pct: float,
        win_rate: float,
        max_profit: float,
        max_loss: float
    ) -> bool:
        """
        일일 요약 알림
        
        Args:
            date: 날짜
            total_trades: 총 거래 횟수
            buy_count: 매수 횟수
            sell_count: 매도 횟수
            daily_pnl: 당일 손익
            daily_pnl_pct: 당일 손익률
            win_rate: 승률
            max_profit: 최대 수익
            max_loss: 최대 손실
        
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["daily_summary"].format(
            date=date,
            total_trades=total_trades,
            buy_count=buy_count,
            sell_count=sell_count,
            daily_pnl=int(daily_pnl),
            daily_pnl_pct=daily_pnl_pct,
            win_rate=win_rate,
            max_profit=int(max_profit),
            max_loss=int(max_loss),
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    # ════════════════════════════════════════════════════════════════
    # 멀티데이 전용 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_position_restored(
        self,
        stock_code: str,
        entry_price: float,
        quantity: int,
        entry_date: str,
        holding_days: int,
        stop_loss: float,
        take_profit: Optional[float],
        trailing_stop: float,
        atr_at_entry: float
    ) -> bool:
        """
        포지션 복원 알림 (멀티데이)
        
        Args:
            stock_code: 종목 코드
            entry_price: 진입가
            quantity: 수량
            entry_date: 진입일
            holding_days: 보유일수
            stop_loss: 손절가
            take_profit: 익절가
            trailing_stop: 트레일링 스탑
            atr_at_entry: 진입 시 ATR
        
        Returns:
            bool: 전송 성공 여부
        """
        tp_str = f"{int(take_profit):,}원" if take_profit else "트레일링만"
        display_symbol = self._format_symbol(stock_code)
        
        message = MESSAGE_TEMPLATES["position_restored"].format(
            stock_code=display_symbol,
            entry_price=int(entry_price),
            quantity=quantity,
            entry_date=entry_date,
            holding_days=holding_days,
            stop_loss=int(stop_loss),
            take_profit=tp_str,
            trailing_stop=int(trailing_stop),
            atr_at_entry=atr_at_entry,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_near_stop_loss(
        self,
        stock_code: str,
        current_price: float,
        entry_price: float,
        stop_loss: float,
        progress: float,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        손절선 근접 경고 알림
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
            entry_price: 진입가
            stop_loss: 손절가
            progress: 손절선 도달률 (%)
            pnl: 현재 손익
            pnl_pct: 손익률
        
        Returns:
            bool: 전송 성공 여부
        """
        remaining = current_price - stop_loss
        display_symbol = self._format_symbol(stock_code)
        
        message = MESSAGE_TEMPLATES["near_stop_loss"].format(
            stock_code=display_symbol,
            current_price=int(current_price),
            stop_loss=int(stop_loss),
            progress=progress,
            entry_price=int(entry_price),
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            remaining=remaining,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_near_take_profit(
        self,
        stock_code: str,
        current_price: float,
        entry_price: float,
        take_profit: float,
        progress: float,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        익절선 근접 알림
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
            entry_price: 진입가
            take_profit: 익절가
            progress: 익절선 도달률 (%)
            pnl: 현재 손익
            pnl_pct: 손익률
        
        Returns:
            bool: 전송 성공 여부
        """
        remaining = take_profit - current_price
        display_symbol = self._format_symbol(stock_code)
        
        message = MESSAGE_TEMPLATES["near_take_profit"].format(
            stock_code=display_symbol,
            current_price=int(current_price),
            take_profit=int(take_profit),
            progress=progress,
            entry_price=int(entry_price),
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            remaining=remaining,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_trailing_stop_updated(
        self,
        stock_code: str,
        highest_price: float,
        trailing_stop: float,
        entry_price: float,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        트레일링 스탑 갱신 알림
        
        Args:
            stock_code: 종목 코드
            highest_price: 최고가
            trailing_stop: 새 트레일링 스탑
            entry_price: 진입가
            pnl: 현재 손익
            pnl_pct: 손익률
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        message = MESSAGE_TEMPLATES["trailing_stop_updated"].format(
            stock_code=display_symbol,
            highest_price=int(highest_price),
            trailing_stop=int(trailing_stop),
            entry_price=int(entry_price),
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_cbt_signal(
        self,
        signal_type: str,
        stock_code: str,
        price: float,
        stop_loss: float,
        take_profit: Optional[float],
        atr: float,
        trend: str,
        reason: str
    ) -> bool:
        """
        CBT 모드 시그널 알림 (실주문 없음)
        
        Args:
            signal_type: 시그널 타입 (BUY/SELL)
            stock_code: 종목 코드
            price: 가격
            stop_loss: 손절가
            take_profit: 익절가
            atr: ATR
            trend: 추세
            reason: 사유
        
        Returns:
            bool: 전송 성공 여부
        """
        tp_str = f"{int(take_profit):,}원" if take_profit else "트레일링만"
        display_symbol = self._format_symbol(stock_code)
        
        message = MESSAGE_TEMPLATES["cbt_signal"].format(
            signal_type=signal_type,
            stock_code=display_symbol,
            price=int(price),
            stop_loss=int(stop_loss),
            take_profit=tp_str,
            atr=atr,
            trend=trend,
            reason=reason,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_gap_protection(
        self,
        stock_code: str,
        open_price: float,
        stop_loss: float,
        entry_price: float,
        gap_loss_pct: float,
        raw_gap_pct: float,
        reference_price: float,
        reference_type: str,
        reason_code: str,
        pnl: float,
        pnl_pct: float
    ) -> bool:
        """
        갭 보호 발동 알림
        
        Args:
            stock_code: 종목 코드
            open_price: 시가
            stop_loss: 손절가
            entry_price: 진입가
            gap_loss_pct: 표시용 갭 손실률
            raw_gap_pct: 내부 계산 raw 갭 손실률
            reference_price: 갭 판단 기준가
            reference_type: 갭 판단 기준 종류
            reason_code: 갭 보호 판단 코드
            pnl: 예상 손익
            pnl_pct: 예상 손익률
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        message = MESSAGE_TEMPLATES["gap_protection"].format(
            stock_code=display_symbol,
            open_price=int(open_price),
            reference_price=int(reference_price),
            reference_type=str(reference_type),
            stop_loss=int(stop_loss),
            entry_price=int(entry_price),
            gap_loss_pct=gap_loss_pct,
            raw_gap_pct=raw_gap_pct,
            reason_code=str(reason_code),
            pnl=int(pnl),
            pnl_pct=pnl_pct,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    # ════════════════════════════════════════════════════════════════
    # CBT 전용 알림 메서드
    # ════════════════════════════════════════════════════════════════
    
    def notify_cbt_performance_report(
        self,
        report_date: str,
        initial_capital: float,
        final_equity: float,
        total_return_pct: float,
        realized_pnl: float,
        unrealized_pnl: float,
        total_trades: int,
        win_rate: float,
        expectancy: float,
        max_drawdown_pct: float,
        profit_factor: float
    ) -> bool:
        """
        CBT 성과 리포트 알림
        
        Args:
            report_date: 리포트 날짜
            initial_capital: 초기 자본금
            final_equity: 최종 평가금
            total_return_pct: 총 수익률
            realized_pnl: 실현 손익
            unrealized_pnl: 미실현 손익
            total_trades: 총 거래 횟수
            win_rate: 승률
            expectancy: 기대값
            max_drawdown_pct: 최대 낙폭
            profit_factor: Profit Factor
        
        Returns:
            bool: 전송 성공 여부
        """
        message = MESSAGE_TEMPLATES["cbt_performance_report"].format(
            report_date=report_date,
            initial_capital=int(initial_capital),
            final_equity=int(final_equity),
            total_return_pct=total_return_pct,
            realized_pnl=int(realized_pnl),
            unrealized_pnl=int(unrealized_pnl),
            total_trades=total_trades,
            win_rate=win_rate,
            expectancy=expectancy,
            max_drawdown_pct=max_drawdown_pct,
            profit_factor=profit_factor,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    def notify_cbt_trade_complete(
        self,
        stock_code: str,
        trade_type: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        pnl: float,
        return_pct: float,
        holding_days: int,
        exit_reason: str,
        total_trades: int,
        cumulative_return_pct: float,
        win_rate: float
    ) -> bool:
        """
        CBT 거래 완료 알림 (누적 성과 포함)
        
        Args:
            stock_code: 종목 코드
            trade_type: 거래 유형 (매수/매도)
            entry_price: 진입가
            exit_price: 청산가
            quantity: 수량
            pnl: 손익
            return_pct: 수익률
            holding_days: 보유일수
            exit_reason: 청산 사유
            total_trades: 누적 거래 횟수
            cumulative_return_pct: 누적 수익률
            win_rate: 승률
        
        Returns:
            bool: 전송 성공 여부
        """
        display_symbol = self._format_symbol(stock_code)
        message = MESSAGE_TEMPLATES["cbt_trade_complete"].format(
            stock_code=display_symbol,
            trade_type=trade_type,
            entry_price=int(entry_price),
            exit_price=int(exit_price),
            quantity=quantity,
            pnl=int(pnl),
            return_pct=return_pct,
            holding_days=holding_days,
            exit_reason=exit_reason,
            total_trades=total_trades,
            cumulative_return_pct=cumulative_return_pct,
            win_rate=win_rate,
            timestamp=self._get_timestamp()
        )
        return self.send_message(message)
    
    # ════════════════════════════════════════════════════════════════
    # 유틸리티 메서드
    # ════════════════════════════════════════════════════════════════
    
    @staticmethod
    def _get_timestamp() -> str:
        """현재 시간 문자열 반환"""
        return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    
    @staticmethod
    def _escape_markdown(text: str) -> str:
        """마크다운 특수문자 이스케이프"""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text
    
    def test_connection(self) -> bool:
        """
        텔레그램 연결 테스트
        
        Returns:
            bool: 연결 성공 여부
        """
        if not self._enabled:
            logger.warning("[TELEGRAM] 알림이 비활성화되어 테스트를 건너뜁니다.")
            return False
        
        test_message = """
🔔 *텔레그램 알림 테스트*
━━━━━━━━━━━━━━━━━━
✅ 연결이 정상적으로 설정되었습니다.
━━━━━━━━━━━━━━━━━━
⏰ {timestamp}
""".format(timestamp=self._get_timestamp())
        
        result = self.send_message(test_message)
        
        if result:
            logger.info("[TELEGRAM] 연결 테스트 성공")
        else:
            logger.error("[TELEGRAM] 연결 테스트 실패")
        
        return result


# ════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스 및 헬퍼 함수
# ════════════════════════════════════════════════════════════════

# 전역 싱글톤 인스턴스
_notifier_instance: Optional[TelegramNotifier] = None


def get_telegram_notifier() -> TelegramNotifier:
    """
    싱글톤 TelegramNotifier 인스턴스를 반환합니다.
    
    Returns:
        TelegramNotifier: 텔레그램 알림기 인스턴스
    """
    global _notifier_instance
    
    if _notifier_instance is None:
        _notifier_instance = TelegramNotifier()
    
    return _notifier_instance


def create_notifier_from_settings() -> TelegramNotifier:
    """
    settings.py의 설정값으로 TelegramNotifier를 생성합니다.
    
    Returns:
        TelegramNotifier: 설정된 텔레그램 알림기
    """
    try:
        from config import settings
        
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
        enabled = getattr(settings, "TELEGRAM_ENABLED", True)
        
        return TelegramNotifier(
            bot_token=bot_token,
            chat_id=chat_id,
            enabled=enabled
        )
    except ImportError:
        # settings를 임포트할 수 없으면 환경변수만 사용
        return TelegramNotifier()


# ════════════════════════════════════════════════════════════════
# 텔레그램 봇 설정 가이드 (문서용)
# ════════════════════════════════════════════════════════════════

SETUP_GUIDE = """
═══════════════════════════════════════════════════════════════════════════════
                        텔레그램 봇 설정 가이드
═══════════════════════════════════════════════════════════════════════════════

1. 봇 생성 및 토큰 발급
─────────────────────────────────────────────────────────────────────────────────
   1) 텔레그램에서 @BotFather 검색하여 대화 시작
   2) /newbot 명령어 입력
   3) 봇 이름 입력 (예: KIS Trading Alert)
   4) 봇 사용자명 입력 (예: kis_trading_alert_bot)
   5) 발급된 토큰 복사 (예: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz)
   
   ⚠️ 토큰은 절대 공개하지 마세요!

2. Chat ID 확인 방법
─────────────────────────────────────────────────────────────────────────────────
   [방법 1: 1:1 채팅]
   1) 생성한 봇 검색하여 대화 시작
   2) /start 메시지 전송
   3) 브라우저에서 아래 URL 접속:
      https://api.telegram.org/bot<토큰>/getUpdates
   4) 응답에서 "chat":{"id":XXXXXXXX} 확인
   
   [방법 2: 그룹 채팅]
   1) 봇을 그룹에 추가
   2) 그룹에서 /start 메시지 전송
   3) 위와 동일하게 getUpdates로 chat_id 확인
   
   💡 그룹 chat_id는 음수입니다 (예: -1001234567890)

3. 환경변수 설정
─────────────────────────────────────────────────────────────────────────────────
   .env 파일에 추가:
   
   TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
   TELEGRAM_CHAT_ID=123456789
   TELEGRAM_ENABLED=true

4. 테스트
─────────────────────────────────────────────────────────────────────────────────
   Python에서 테스트:
   
   from utils.telegram_notifier import get_telegram_notifier
   
   notifier = get_telegram_notifier()
   notifier.test_connection()

═══════════════════════════════════════════════════════════════════════════════
"""


def print_setup_guide():
    """텔레그램 봇 설정 가이드를 출력합니다."""
    print(SETUP_GUIDE)


# ════════════════════════════════════════════════════════════════
# 직접 실행 시 테스트
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print_setup_guide()
    
    # 연결 테스트
    notifier = get_telegram_notifier()
    
    if notifier.enabled:
        print("\n텔레그램 연결 테스트 중...")
        if notifier.test_connection():
            print("✅ 텔레그램 연결 성공!")
        else:
            print("❌ 텔레그램 연결 실패. 설정을 확인하세요.")
    else:
        print("\n⚠️ 텔레그램 알림이 비활성화되어 있습니다.")
        print("   TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 설정하세요.")
