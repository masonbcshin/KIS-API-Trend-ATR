"""
KIS WebSocket 자동매매 시스템 - WebSocket 클라이언트 모듈

한국투자증권 WebSocket을 통해 실시간 주식 시세를 수신합니다.

주요 기능:
    - WebSocket 연결 및 인증
    - 종목별 실시간 현재가 구독
    - 재연결 로직 (네트워크 단절 대응)
    - 콜백 함수를 통한 가격 업데이트 전달

KIS WebSocket 프로토콜:
    - 접속: ws://ops.koreainvestment.com:21000
    - 인증: 접속 시 approval_key 발급 후 사용
    - 구독: H0STCNT0 (실시간 체결가) TR 사용
    - 데이터: | 구분자로 분리된 문자열
"""

import asyncio
import json
import logging
import time
import hashlib
from datetime import datetime
from typing import Callable, Dict, Optional, List
from dataclasses import dataclass

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatusCode
)
import requests

from config import (
    KISConfig,
    get_kis_config,
    WS_RECONNECT_DELAY,
    WS_MAX_RECONNECT_ATTEMPTS,
    WS_PING_INTERVAL,
    API_TIMEOUT
)


# 로거 설정
logger = logging.getLogger("websocket")


# ════════════════════════════════════════════════════════════════
# 상수
# ════════════════════════════════════════════════════════════════

# KIS WebSocket URL
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"  # 실전
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"  # 모의

# TR 코드
TR_SUBSCRIBE = "H0STCNT0"  # 실시간 체결가 구독
TR_UNSUBSCRIBE = "H0STCNT0"  # 구독 취소도 동일 TR

# 데이터 필드 인덱스 (H0STCNT0 응답)
# 한국투자증권 실시간 체결가 데이터 필드
FIELD_STOCK_CODE = 0       # 종목코드
FIELD_TIME = 1             # 체결시간 (HHMMSS)
FIELD_CURRENT_PRICE = 2    # 현재가
FIELD_CHANGE_SIGN = 3      # 전일대비 부호 (1: 상한, 2: 상승, 3: 보합, 4: 하한, 5: 하락)
FIELD_CHANGE_PRICE = 4     # 전일대비
FIELD_CHANGE_RATE = 5      # 등락률
FIELD_WEIGHTED_AVG = 6     # 가중평균가
FIELD_OPEN_PRICE = 7       # 시가
FIELD_HIGH_PRICE = 8       # 고가
FIELD_LOW_PRICE = 9        # 저가
FIELD_ASK_PRICE1 = 10      # 매도호가1
FIELD_BID_PRICE1 = 11      # 매수호가1
FIELD_VOLUME = 12          # 거래량
FIELD_TURNOVER = 13        # 거래대금


# ════════════════════════════════════════════════════════════════
# 데이터 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class TickData:
    """
    실시간 체결 데이터
    
    Attributes:
        stock_code: 종목코드
        current_price: 현재가
        change_price: 전일대비
        change_rate: 등락률
        volume: 거래량
        time: 체결시간
        received_at: 수신시간
    """
    stock_code: str
    current_price: float
    change_price: float = 0.0
    change_rate: float = 0.0
    volume: int = 0
    time: str = ""
    received_at: datetime = None
    
    def __post_init__(self):
        if self.received_at is None:
            self.received_at = datetime.now()


# ════════════════════════════════════════════════════════════════
# KIS WebSocket 클라이언트 클래스
# ════════════════════════════════════════════════════════════════

class KISWebSocketClient:
    """
    한국투자증권 WebSocket 클라이언트
    
    실시간 주식 시세를 수신하고, 가격 업데이트 시 콜백 함수를 호출합니다.
    
    Usage:
        async def on_price_update(tick: TickData):
            print(f"{tick.stock_code}: {tick.current_price}원")
        
        client = KISWebSocketClient()
        client.set_on_price_callback(on_price_update)
        client.subscribe(["005930", "000660"])
        await client.run()
    
    Attributes:
        config: KIS API 설정
        approval_key: WebSocket 인증키
        subscribed_codes: 구독 중인 종목 코드 리스트
        on_price_callback: 가격 업데이트 콜백 함수
    """
    
    def __init__(
        self,
        config: Optional[KISConfig] = None,
        is_paper_trading: bool = True
    ):
        """
        WebSocket 클라이언트 초기화
        
        Args:
            config: KIS API 설정 (None이면 환경변수에서 로드)
            is_paper_trading: 모의투자 여부 (True: 모의, False: 실전)
        """
        self.config = config or get_kis_config()
        self.is_paper_trading = is_paper_trading
        
        # WebSocket URL 설정
        self._ws_url = WS_URL_PAPER if is_paper_trading else WS_URL_REAL
        
        # WebSocket 연결 관련
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._approval_key: Optional[str] = None
        self._is_connected: bool = False
        self._is_running: bool = False
        
        # 구독 관리
        self._subscribed_codes: List[str] = []
        self._pending_subscribe: List[str] = []
        
        # 콜백 함수
        self._on_price_callback: Optional[Callable[[TickData], None]] = None
        self._on_connect_callback: Optional[Callable[[], None]] = None
        self._on_disconnect_callback: Optional[Callable[[str], None]] = None
        
        # 재연결 관련
        self._reconnect_count: int = 0
        self._last_message_time: float = 0
        
        logger.info(
            f"[WS] WebSocket 클라이언트 초기화 | "
            f"URL: {self._ws_url} | 모의투자: {is_paper_trading}"
        )
    
    # ════════════════════════════════════════════════════════════════
    # 콜백 설정
    # ════════════════════════════════════════════════════════════════
    
    def set_on_price_callback(
        self,
        callback: Callable[[TickData], None]
    ) -> None:
        """
        가격 업데이트 콜백 함수를 설정합니다.
        
        Args:
            callback: 콜백 함수 (TickData를 인자로 받음)
        """
        self._on_price_callback = callback
        logger.debug("[WS] 가격 업데이트 콜백 설정됨")
    
    def set_on_connect_callback(
        self,
        callback: Callable[[], None]
    ) -> None:
        """
        연결 성공 콜백 함수를 설정합니다.
        
        Args:
            callback: 콜백 함수
        """
        self._on_connect_callback = callback
        logger.debug("[WS] 연결 성공 콜백 설정됨")
    
    def set_on_disconnect_callback(
        self,
        callback: Callable[[str], None]
    ) -> None:
        """
        연결 해제 콜백 함수를 설정합니다.
        
        Args:
            callback: 콜백 함수 (해제 사유를 인자로 받음)
        """
        self._on_disconnect_callback = callback
        logger.debug("[WS] 연결 해제 콜백 설정됨")
    
    # ════════════════════════════════════════════════════════════════
    # 인증키 발급
    # ════════════════════════════════════════════════════════════════
    
    def _get_approval_key(self) -> str:
        """
        WebSocket 접속용 인증키(approval_key)를 발급받습니다.
        
        KIS REST API를 통해 발급받으며, 24시간 유효합니다.
        
        Returns:
            str: approval_key
            
        Raises:
            Exception: 인증키 발급 실패 시
        """
        # 이미 발급받은 키가 있으면 재사용
        if self._approval_key:
            return self._approval_key
        
        # API URL 설정
        if self.is_paper_trading:
            base_url = self.config.paper_base_url
        else:
            base_url = self.config.base_url
        
        url = f"{base_url}/oauth2/Approval"
        
        headers = {"content-type": "application/json; charset=utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "secretkey": self.config.app_secret
        }
        
        logger.info("[WS] WebSocket 인증키 발급 요청...")
        
        try:
            response = requests.post(url, headers=headers, json=body, timeout=API_TIMEOUT)
            
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}: {response.text}")
            
            data = response.json()
            
            if "approval_key" not in data:
                raise Exception(f"approval_key 없음: {data}")
            
            self._approval_key = data["approval_key"]
            logger.info("[WS] 인증키 발급 완료")
            
            return self._approval_key
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[WS] 인증키 발급 실패: {e}")
            raise
    
    # ════════════════════════════════════════════════════════════════
    # 구독 관리
    # ════════════════════════════════════════════════════════════════
    
    def subscribe(self, stock_codes: List[str]) -> None:
        """
        종목 시세 구독을 요청합니다.
        
        WebSocket이 연결되지 않은 경우, 연결 후 자동으로 구독됩니다.
        
        Args:
            stock_codes: 종목 코드 리스트
        """
        for code in stock_codes:
            code = str(code).zfill(6)  # 6자리로 패딩
            
            if code not in self._subscribed_codes and code not in self._pending_subscribe:
                self._pending_subscribe.append(code)
                logger.debug(f"[WS] 구독 대기열 추가: {code}")
    
    def unsubscribe(self, stock_codes: List[str]) -> None:
        """
        종목 시세 구독을 취소합니다.
        
        Args:
            stock_codes: 종목 코드 리스트
        """
        for code in stock_codes:
            code = str(code).zfill(6)
            
            if code in self._subscribed_codes:
                self._subscribed_codes.remove(code)
                
                if self._is_connected:
                    asyncio.create_task(self._send_unsubscribe(code))
                
                logger.debug(f"[WS] 구독 취소: {code}")
    
    async def _send_subscribe(self, stock_code: str) -> None:
        """
        종목 구독 요청을 WebSocket으로 전송합니다.
        
        Args:
            stock_code: 종목 코드
        """
        if not self._ws or not self._is_connected:
            return
        
        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",  # 개인
                "tr_type": "1",   # 등록
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": TR_SUBSCRIBE,
                    "tr_key": stock_code
                }
            }
        }
        
        try:
            await self._ws.send(json.dumps(message))
            logger.info(f"[WS] 구독 요청 전송: {stock_code}")
        except Exception as e:
            logger.error(f"[WS] 구독 요청 실패: {stock_code} - {e}")
    
    async def _send_unsubscribe(self, stock_code: str) -> None:
        """
        종목 구독 취소 요청을 WebSocket으로 전송합니다.
        
        Args:
            stock_code: 종목 코드
        """
        if not self._ws or not self._is_connected:
            return
        
        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "2",  # 해제
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": TR_UNSUBSCRIBE,
                    "tr_key": stock_code
                }
            }
        }
        
        try:
            await self._ws.send(json.dumps(message))
            logger.info(f"[WS] 구독 취소 요청 전송: {stock_code}")
        except Exception as e:
            logger.error(f"[WS] 구독 취소 요청 실패: {stock_code} - {e}")
    
    async def _subscribe_pending(self) -> None:
        """대기 중인 모든 종목을 구독합니다."""
        while self._pending_subscribe:
            code = self._pending_subscribe.pop(0)
            
            await self._send_subscribe(code)
            self._subscribed_codes.append(code)
            
            # Rate limit 준수
            await asyncio.sleep(0.1)
    
    # ════════════════════════════════════════════════════════════════
    # 메시지 처리
    # ════════════════════════════════════════════════════════════════
    
    def _parse_message(self, message: str) -> Optional[TickData]:
        """
        WebSocket 메시지를 파싱하여 TickData로 변환합니다.
        
        KIS WebSocket 데이터 형식:
            - JSON 응답: 구독 확인/오류 메시지
            - 구분자 응답: | 로 구분된 실시간 체결 데이터
        
        Args:
            message: WebSocket 메시지
            
        Returns:
            TickData: 체결 데이터 (파싱 실패 시 None)
        """
        # JSON 형식 체크 (구독 응답 등)
        if message.startswith("{"):
            try:
                data = json.loads(message)
                
                # 구독 응답 처리
                header = data.get("header", {})
                tr_id = header.get("tr_id", "")
                msg_cd = header.get("msg_cd", "")
                
                if msg_cd == "OPSP0000":
                    logger.debug(f"[WS] 구독 성공: {data.get('body', {}).get('output', {}).get('key', '')}")
                elif msg_cd == "OPSP0001":
                    logger.debug(f"[WS] 구독 해제: {data.get('body', {}).get('output', {}).get('key', '')}")
                elif msg_cd:
                    logger.warning(f"[WS] 응답: {msg_cd} - {header.get('msg1', '')}")
                
                return None
                
            except json.JSONDecodeError:
                pass
        
        # | 구분자 형식 (실시간 체결 데이터)
        # 형식: 0|H0STCNT0|005930|3|091500|71000|2|500|0.71|...
        if "|" in message:
            try:
                parts = message.split("|")
                
                # 최소 필드 수 체크
                if len(parts) < 13:
                    return None
                
                # 데이터 타입 확인 (0: 체결가)
                data_type = parts[0]
                tr_id = parts[1]
                
                if tr_id != TR_SUBSCRIBE:
                    return None
                
                # 체결 데이터 추출
                data_str = parts[2] if len(parts) == 3 else "|".join(parts[2:])
                fields = data_str.split("^")
                
                if len(fields) < 13:
                    return None
                
                stock_code = fields[FIELD_STOCK_CODE]
                
                # 구독 중인 종목인지 확인
                if stock_code not in self._subscribed_codes:
                    return None
                
                # TickData 생성
                tick = TickData(
                    stock_code=stock_code,
                    current_price=float(fields[FIELD_CURRENT_PRICE]),
                    change_price=float(fields[FIELD_CHANGE_PRICE]) if fields[FIELD_CHANGE_PRICE] else 0,
                    change_rate=float(fields[FIELD_CHANGE_RATE]) if fields[FIELD_CHANGE_RATE] else 0,
                    volume=int(fields[FIELD_VOLUME]) if fields[FIELD_VOLUME] else 0,
                    time=fields[FIELD_TIME]
                )
                
                return tick
                
            except (ValueError, IndexError) as e:
                logger.debug(f"[WS] 데이터 파싱 오류: {e}")
                return None
        
        return None
    
    async def _handle_message(self, message: str) -> None:
        """
        수신된 메시지를 처리합니다.
        
        Args:
            message: WebSocket 메시지
        """
        self._last_message_time = time.time()
        
        tick = self._parse_message(message)
        
        if tick is not None:
            logger.debug(
                f"[WS] 체결: {tick.stock_code} | "
                f"현재가: {tick.current_price:,.0f} | "
                f"등락: {tick.change_rate:+.2f}%"
            )
            
            # 콜백 호출
            if self._on_price_callback:
                try:
                    # async 콜백 지원
                    if asyncio.iscoroutinefunction(self._on_price_callback):
                        await self._on_price_callback(tick)
                    else:
                        self._on_price_callback(tick)
                except Exception as e:
                    logger.error(f"[WS] 콜백 실행 오류: {e}")
    
    # ════════════════════════════════════════════════════════════════
    # 연결 관리
    # ════════════════════════════════════════════════════════════════
    
    async def connect(self) -> bool:
        """
        WebSocket에 연결합니다.
        
        Returns:
            bool: 연결 성공 여부
        """
        try:
            # 인증키 발급
            self._get_approval_key()
            
            logger.info(f"[WS] 연결 시도: {self._ws_url}")
            
            # WebSocket 연결
            self._ws = await websockets.connect(
                self._ws_url,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_INTERVAL * 2,
                close_timeout=10
            )
            
            self._is_connected = True
            self._reconnect_count = 0
            self._last_message_time = time.time()
            
            logger.info("[WS] 연결 성공!")
            
            # 연결 성공 콜백
            if self._on_connect_callback:
                try:
                    self._on_connect_callback()
                except Exception as e:
                    logger.error(f"[WS] 연결 콜백 오류: {e}")
            
            # 대기 중인 구독 처리
            await self._subscribe_pending()
            
            return True
            
        except InvalidStatusCode as e:
            logger.error(f"[WS] 연결 실패 (HTTP 오류): {e}")
            return False
        except Exception as e:
            logger.error(f"[WS] 연결 실패: {e}")
            return False
    
    async def disconnect(self, reason: str = "정상 종료") -> None:
        """
        WebSocket 연결을 종료합니다.
        
        Args:
            reason: 종료 사유
        """
        self._is_running = False
        self._is_connected = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        logger.info(f"[WS] 연결 종료: {reason}")
        
        # 연결 해제 콜백
        if self._on_disconnect_callback:
            try:
                self._on_disconnect_callback(reason)
            except Exception as e:
                logger.error(f"[WS] 연결 해제 콜백 오류: {e}")
    
    async def _reconnect(self) -> bool:
        """
        WebSocket 재연결을 시도합니다.
        
        지수 백오프 방식으로 재시도하며, 최대 시도 횟수 초과 시 실패합니다.
        
        Returns:
            bool: 재연결 성공 여부
        """
        self._is_connected = False
        
        while self._reconnect_count < WS_MAX_RECONNECT_ATTEMPTS and self._is_running:
            self._reconnect_count += 1
            
            # 지수 백오프 대기
            delay = WS_RECONNECT_DELAY * (2 ** (self._reconnect_count - 1))
            delay = min(delay, 60)  # 최대 60초
            
            logger.info(
                f"[WS] 재연결 시도 ({self._reconnect_count}/{WS_MAX_RECONNECT_ATTEMPTS}) | "
                f"{delay}초 후 재시도..."
            )
            
            await asyncio.sleep(delay)
            
            if not self._is_running:
                break
            
            if await self.connect():
                return True
        
        logger.error(f"[WS] 재연결 실패: 최대 시도 횟수 초과")
        return False
    
    # ════════════════════════════════════════════════════════════════
    # 메인 루프
    # ════════════════════════════════════════════════════════════════
    
    async def run(self) -> None:
        """
        WebSocket 메인 루프를 실행합니다.
        
        연결 → 메시지 수신 → 처리 → 재연결 사이클을 반복합니다.
        stop() 호출 시 종료됩니다.
        """
        self._is_running = True
        
        # 초기 연결
        if not await self.connect():
            logger.error("[WS] 초기 연결 실패")
            
            if not await self._reconnect():
                self._is_running = False
                return
        
        # 메시지 수신 루프
        while self._is_running:
            try:
                if not self._ws:
                    raise ConnectionClosedError(None, None)
                
                # 메시지 수신 (타임아웃 설정)
                message = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=WS_PING_INTERVAL * 3
                )
                
                await self._handle_message(message)
                
            except asyncio.TimeoutError:
                # 타임아웃 - 연결 상태 확인
                logger.warning("[WS] 메시지 수신 타임아웃")
                
                if not await self._reconnect():
                    break
                    
            except ConnectionClosedOK:
                # 정상 종료
                logger.info("[WS] 연결 정상 종료")
                break
                
            except ConnectionClosedError as e:
                # 비정상 종료 - 재연결 시도
                logger.warning(f"[WS] 연결 끊김: {e}")
                
                if not await self._reconnect():
                    break
                    
            except Exception as e:
                logger.error(f"[WS] 처리 오류: {e}")
                
                if not await self._reconnect():
                    break
        
        # 종료 처리
        await self.disconnect("메인 루프 종료")
    
    def stop(self) -> None:
        """
        WebSocket 실행을 중지합니다.
        """
        logger.info("[WS] 중지 요청됨")
        self._is_running = False
    
    # ════════════════════════════════════════════════════════════════
    # 상태 조회
    # ════════════════════════════════════════════════════════════════
    
    @property
    def is_connected(self) -> bool:
        """연결 상태"""
        return self._is_connected
    
    @property
    def is_running(self) -> bool:
        """실행 상태"""
        return self._is_running
    
    @property
    def subscribed_codes(self) -> List[str]:
        """구독 중인 종목 코드"""
        return self._subscribed_codes.copy()
    
    def get_status(self) -> dict:
        """
        현재 상태 정보를 반환합니다.
        
        Returns:
            dict: 상태 정보
        """
        return {
            "is_connected": self._is_connected,
            "is_running": self._is_running,
            "subscribed_count": len(self._subscribed_codes),
            "reconnect_count": self._reconnect_count,
            "last_message_time": datetime.fromtimestamp(self._last_message_time).isoformat()
            if self._last_message_time else None
        }


# ════════════════════════════════════════════════════════════════
# 직접 실행 시 테스트
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    
    async def test_callback(tick: TickData):
        print(f"[콜백] {tick.stock_code}: {tick.current_price:,.0f}원 ({tick.change_rate:+.2f}%)")
    
    async def main():
        # 클라이언트 생성
        client = KISWebSocketClient(is_paper_trading=True)
        
        # 콜백 설정
        client.set_on_price_callback(test_callback)
        
        # 종목 구독
        client.subscribe(["005930", "000660"])
        
        print("\n테스트를 위해서는 .env 파일에 KIS API 정보가 설정되어 있어야 합니다.")
        print("Ctrl+C로 종료합니다.\n")
        
        try:
            await client.run()
        except KeyboardInterrupt:
            client.stop()
    
    asyncio.run(main())
