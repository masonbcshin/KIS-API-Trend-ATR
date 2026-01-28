"""
KIS Trend-ATR Trading System - 텔레그램 리포트 전송기

일일 리포트를 텔레그램으로 전송합니다.
전송 실패 시 자동 재시도 기능을 포함합니다.

텔레그램 봇 생성 방법:
    1. 텔레그램에서 @BotFather 검색하여 대화 시작
    2. /newbot 명령어 입력
    3. 봇 이름 입력 (예: KIS Trading Report)
    4. 봇 사용자명 입력 (예: kis_trading_report_bot)
    5. 발급된 토큰을 TELEGRAM_BOT_TOKEN 환경변수에 설정
    
Chat ID 확인 방법:
    1. 생성한 봇과 대화 시작 후 /start 전송
    2. 브라우저에서 https://api.telegram.org/bot<토큰>/getUpdates 접속
    3. 응답에서 "chat":{"id":XXXXXXXX} 확인
    4. TELEGRAM_CHAT_ID 환경변수에 설정
"""

import os
import time
from dataclasses import dataclass
from typing import Optional

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError

from utils.logger import get_logger

logger = get_logger("telegram_sender")


# ════════════════════════════════════════════════════════════════
# 상수 정의
# ════════════════════════════════════════════════════════════════

TELEGRAM_API_BASE_URL = "https://api.telegram.org/bot"

DEFAULT_TIMEOUT = 10  # 초
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0  # 초 (지수 백오프 시작값)


# ════════════════════════════════════════════════════════════════
# 설정 데이터 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class TelegramConfig:
    """텔레그램 설정"""
    bot_token: str
    chat_id: str
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_delay: float = DEFAULT_RETRY_DELAY


# ════════════════════════════════════════════════════════════════
# 텔레그램 리포트 전송기 클래스
# ════════════════════════════════════════════════════════════════

class TelegramReportSender:
    """
    텔레그램으로 리포트를 전송하는 클래스
    
    환경변수:
        TELEGRAM_BOT_TOKEN: 텔레그램 봇 토큰
        TELEGRAM_CHAT_ID: 텔레그램 채팅 ID
    
    Usage:
        sender = TelegramReportSender()
        success = sender.send_report(message)
    """
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY
    ):
        """
        텔레그램 리포트 전송기 초기화
        
        Args:
            bot_token: 텔레그램 봇 토큰 (미입력 시 환경변수에서 로드)
            chat_id: 텔레그램 채팅 ID (미입력 시 환경변수에서 로드)
            timeout: API 요청 타임아웃 (초)
            max_retries: 최대 재시도 횟수
            retry_delay: 재시도 간 대기 시간 (초, 지수 백오프)
        """
        # 환경변수에서 로드
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        
        # API 설정
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        
        # API URL
        self._api_url = f"{TELEGRAM_API_BASE_URL}{self._bot_token}"
        
        # 설정 검증
        self._is_configured = self._validate_config()
        
        if self._is_configured:
            logger.info("[TELEGRAM_SENDER] 텔레그램 리포트 전송기 초기화 완료")
        else:
            logger.warning(
                "[TELEGRAM_SENDER] 텔레그램 설정이 불완전합니다. "
                "TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 확인하세요."
            )
    
    def _validate_config(self) -> bool:
        """설정 유효성을 검증합니다."""
        if not self._bot_token:
            logger.error("[TELEGRAM_SENDER] TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
            return False
        
        if not self._chat_id:
            logger.error("[TELEGRAM_SENDER] TELEGRAM_CHAT_ID가 설정되지 않았습니다.")
            return False
        
        return True
    
    @property
    def is_configured(self) -> bool:
        """설정 완료 여부"""
        return self._is_configured
    
    def send_report(
        self,
        message: str,
        parse_mode: Optional[str] = None,
        disable_notification: bool = False
    ) -> bool:
        """
        리포트 메시지를 텔레그램으로 전송합니다.
        
        전송 실패 시 지수 백오프 방식으로 최대 max_retries회 재시도합니다.
        
        Args:
            message: 전송할 메시지
            parse_mode: 파싱 모드 (None, "Markdown", "HTML")
            disable_notification: 무음 알림 여부
        
        Returns:
            bool: 전송 성공 여부
        """
        if not self._is_configured:
            logger.error("[TELEGRAM_SENDER] 텔레그램 설정이 완료되지 않았습니다.")
            return False
        
        if not message:
            logger.warning("[TELEGRAM_SENDER] 빈 메시지는 전송하지 않습니다.")
            return False
        
        # 메시지 길이 제한 (텔레그램 최대 4096자)
        if len(message) > 4096:
            message = message[:4090] + "\n..."
            logger.warning("[TELEGRAM_SENDER] 메시지가 4096자를 초과하여 잘림")
        
        # 요청 데이터
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "disable_notification": disable_notification
        }
        
        if parse_mode:
            payload["parse_mode"] = parse_mode
        
        # 재시도 로직을 포함한 전송
        return self._send_with_retry(payload)
    
    def _send_with_retry(self, payload: dict) -> bool:
        """
        재시도 로직이 포함된 메시지 전송
        
        지수 백오프: 2초 → 4초 → 8초 ...
        
        Args:
            payload: 요청 데이터
        
        Returns:
            bool: 전송 성공 여부
        """
        url = f"{self._api_url}/sendMessage"
        
        for attempt in range(1, self._max_retries + 1):
            try:
                logger.debug(
                    f"[TELEGRAM_SENDER] 메시지 전송 시도 {attempt}/{self._max_retries}"
                )
                
                response = requests.post(
                    url,
                    json=payload,
                    timeout=self._timeout
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        logger.info(
                            f"[TELEGRAM_SENDER] 메시지 전송 성공 "
                            f"(시도 {attempt}/{self._max_retries})"
                        )
                        return True
                    else:
                        error_desc = result.get("description", "알 수 없는 오류")
                        logger.error(
                            f"[TELEGRAM_SENDER] API 응답 오류: {error_desc}"
                        )
                else:
                    logger.error(
                        f"[TELEGRAM_SENDER] HTTP 오류: {response.status_code}"
                    )
                    
                    # 400 오류는 재시도해도 소용없음
                    if response.status_code == 400:
                        logger.error(
                            f"[TELEGRAM_SENDER] 잘못된 요청 - 재시도 중단"
                        )
                        return False
                    
                    # 401/403은 인증 문제
                    if response.status_code in (401, 403):
                        logger.error(
                            f"[TELEGRAM_SENDER] 인증 오류 - 봇 토큰 확인 필요"
                        )
                        return False
                        
            except Timeout:
                logger.warning(
                    f"[TELEGRAM_SENDER] 요청 타임아웃 "
                    f"(시도 {attempt}/{self._max_retries})"
                )
            except ConnectionError as e:
                logger.warning(
                    f"[TELEGRAM_SENDER] 연결 오류 "
                    f"(시도 {attempt}/{self._max_retries}): {e}"
                )
            except RequestException as e:
                logger.error(
                    f"[TELEGRAM_SENDER] 요청 실패 "
                    f"(시도 {attempt}/{self._max_retries}): {e}"
                )
            
            # 마지막 시도가 아니면 대기 후 재시도
            if attempt < self._max_retries:
                delay = self._retry_delay * (2 ** (attempt - 1))
                logger.info(f"[TELEGRAM_SENDER] {delay:.1f}초 후 재시도...")
                time.sleep(delay)
        
        logger.error(
            f"[TELEGRAM_SENDER] 최대 재시도 횟수({self._max_retries})를 "
            f"초과했습니다. 전송 실패."
        )
        return False
    
    def send_html_report(
        self,
        message: str,
        disable_notification: bool = False
    ) -> bool:
        """
        HTML 형식의 리포트를 전송합니다.
        
        Args:
            message: HTML 형식 메시지
            disable_notification: 무음 알림 여부
        
        Returns:
            bool: 전송 성공 여부
        """
        return self.send_report(
            message,
            parse_mode="HTML",
            disable_notification=disable_notification
        )
    
    def test_connection(self) -> bool:
        """
        텔레그램 연결을 테스트합니다.
        
        Returns:
            bool: 연결 성공 여부
        """
        if not self._is_configured:
            logger.warning(
                "[TELEGRAM_SENDER] 설정이 완료되지 않아 테스트를 건너뜁니다."
            )
            return False
        
        test_message = "🔔 텔레그램 리포트 전송기 연결 테스트\n\n✅ 연결이 정상적으로 설정되었습니다."
        
        result = self.send_report(test_message, disable_notification=True)
        
        if result:
            logger.info("[TELEGRAM_SENDER] 연결 테스트 성공")
        else:
            logger.error("[TELEGRAM_SENDER] 연결 테스트 실패")
        
        return result


# ════════════════════════════════════════════════════════════════
# 팩토리 함수
# ════════════════════════════════════════════════════════════════

def create_telegram_sender(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    **kwargs
) -> TelegramReportSender:
    """
    텔레그램 리포트 전송기를 생성합니다.
    
    Args:
        bot_token: 텔레그램 봇 토큰 (선택, 환경변수 대체 가능)
        chat_id: 텔레그램 채팅 ID (선택, 환경변수 대체 가능)
        **kwargs: 추가 설정 (timeout, max_retries, retry_delay)
    
    Returns:
        TelegramReportSender: 전송기 인스턴스
    """
    return TelegramReportSender(
        bot_token=bot_token,
        chat_id=chat_id,
        **kwargs
    )


# ════════════════════════════════════════════════════════════════
# 직접 실행 시 테스트
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("텔레그램 리포트 전송기 테스트")
    print("=" * 60)
    
    sender = create_telegram_sender()
    
    if sender.is_configured:
        print("\n연결 테스트 중...")
        if sender.test_connection():
            print("✅ 연결 테스트 성공!")
        else:
            print("❌ 연결 테스트 실패. 로그를 확인하세요.")
    else:
        print("\n⚠️ 텔레그램 설정이 완료되지 않았습니다.")
        print("환경변수를 설정하세요:")
        print("  - TELEGRAM_BOT_TOKEN")
        print("  - TELEGRAM_CHAT_ID")
