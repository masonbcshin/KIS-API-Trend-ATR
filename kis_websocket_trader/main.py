"""
KIS WebSocket 자동매매 시스템 - 메인 컨트롤러

장 시작 전에 선정된 종목 리스트를 대상으로 WebSocket을 이용해
실시간 시세를 감시하고, ATR 기준 진입/손절/익절 조건을 체크합니다.

실행 방법:
    python main.py

기능:
    - trade_universe.json에서 종목 리스트 로딩
    - WebSocket 실시간 시세 수신
    - 상태 관리 (WAIT → ENTERED → EXITED)
    - CBT 모드: 주문 없이 텔레그램 알림만 전송
    - LIVE 모드: 실제 주문 실행 (구조만 설계)
    - 운영 시간 제어 (09:00~15:20 진입, 15:30 종료)
"""

import asyncio
import signal
import logging
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional
import time

from config import (
    get_config,
    TradeMode,
    StockState,
    LOG_FORMAT,
    LOG_DATE_FORMAT,
    LOG_LEVEL
)
from strategy import (
    ATRStrategy,
    StockPosition,
    SignalType,
    load_universe_from_json
)
from websocket_client import KISWebSocketClient, TickData
from notifier import TelegramNotifier, get_notifier


# ════════════════════════════════════════════════════════════════
# 로깅 설정
# ════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT
)

logger = logging.getLogger("main")


# ════════════════════════════════════════════════════════════════
# 메인 트레이딩 컨트롤러
# ════════════════════════════════════════════════════════════════

class TradingController:
    """
    자동매매 시스템 메인 컨트롤러
    
    WebSocket을 통해 실시간 시세를 수신하고, ATR 전략에 따라
    진입/손절/익절 시그널을 생성합니다.
    
    CBT 모드에서는 시그널 발생 시 텔레그램 알림만 전송하고,
    LIVE 모드에서는 실제 주문을 실행합니다.
    
    Attributes:
        strategy: ATR 전략 객체
        ws_client: WebSocket 클라이언트
        notifier: 텔레그램 알림기
        trade_mode: 거래 모드 (CBT/LIVE)
    """
    
    def __init__(self):
        """컨트롤러 초기화"""
        # 설정 로드
        self.kis_config, self.telegram_config, self.trading_config = get_config()
        
        # 거래 모드
        self.trade_mode = self.trading_config.mode
        self.is_cbt_mode = self.trade_mode == TradeMode.CBT
        
        # 컴포넌트 초기화
        self.strategy = ATRStrategy()
        self.ws_client = KISWebSocketClient(
            config=self.kis_config,
            is_paper_trading=True  # 항상 모의투자 서버 사용
        )
        self.notifier = get_notifier()
        
        # 상태 변수
        self._is_running = False
        self._start_time: Optional[datetime] = None
        
        # 통계
        self._stats = {
            "entry_count": 0,
            "stop_loss_count": 0,
            "take_profit_count": 0
        }
        
        logger.info(
            f"[MAIN] 컨트롤러 초기화 완료 | "
            f"모드: {self.trade_mode.value} | "
            f"진입: {self.trading_config.entry_start_time}~{self.trading_config.entry_end_time} | "
            f"종료: {self.trading_config.close_time}"
        )
    
    # ════════════════════════════════════════════════════════════════
    # 종목 로딩
    # ════════════════════════════════════════════════════════════════
    
    def load_universe(self) -> bool:
        """
        trade_universe.json에서 종목 리스트를 로드합니다.
        
        Returns:
            bool: 로딩 성공 여부
        """
        # 파일 경로 확인
        base_dir = Path(__file__).parent
        universe_path = base_dir / self.trading_config.universe_file
        
        if not universe_path.exists():
            logger.error(f"[MAIN] 종목 파일 없음: {universe_path}")
            return False
        
        # 종목 로딩
        positions = load_universe_from_json(str(universe_path))
        
        if not positions:
            logger.error("[MAIN] 로드된 종목이 없습니다.")
            return False
        
        # 전략에 종목 추가
        for position in positions:
            self.strategy.add_position(position)
        
        logger.info(f"[MAIN] {len(positions)}개 종목 로드 완료")
        
        return True
    
    # ════════════════════════════════════════════════════════════════
    # 시간 체크
    # ════════════════════════════════════════════════════════════════
    
    def _parse_time(self, time_str: str) -> dt_time:
        """
        시간 문자열(HH:MM)을 time 객체로 변환합니다.
        
        Args:
            time_str: 시간 문자열
            
        Returns:
            dt_time: time 객체
        """
        parts = time_str.split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    
    def is_entry_allowed(self) -> bool:
        """
        현재 시간이 신규 진입 허용 시간인지 확인합니다.
        
        Returns:
            bool: 진입 허용 여부
        """
        now = datetime.now().time()
        start = self._parse_time(self.trading_config.entry_start_time)
        end = self._parse_time(self.trading_config.entry_end_time)
        
        return start <= now <= end
    
    def is_close_time(self) -> bool:
        """
        현재 시간이 시스템 종료 시간인지 확인합니다.
        
        Returns:
            bool: 종료 시간 여부
        """
        now = datetime.now().time()
        close = self._parse_time(self.trading_config.close_time)
        
        return now >= close
    
    def update_entry_permission(self) -> None:
        """운영 시간에 따라 진입 허용 상태를 업데이트합니다."""
        if self.is_entry_allowed():
            self.strategy.enable_entry()
        else:
            self.strategy.disable_entry()
    
    # ════════════════════════════════════════════════════════════════
    # 가격 업데이트 처리 (WebSocket 콜백)
    # ════════════════════════════════════════════════════════════════
    
    async def on_price_update(self, tick: TickData) -> None:
        """
        실시간 가격 업데이트 콜백
        
        WebSocket에서 체결가 수신 시 호출됩니다.
        전략에 따라 시그널을 체크하고, 필요한 액션을 수행합니다.
        
        Args:
            tick: 체결 데이터
        """
        stock_code = tick.stock_code
        current_price = tick.current_price
        
        # 운영 시간 체크
        self.update_entry_permission()
        
        # 시그널 체크
        signal = self.strategy.check_signal(stock_code, current_price)
        
        # 시그널에 따른 처리
        if signal.signal_type == SignalType.ENTRY:
            await self._handle_entry_signal(stock_code, current_price)
            
        elif signal.signal_type == SignalType.STOP_LOSS:
            await self._handle_stop_loss_signal(stock_code, current_price)
            
        elif signal.signal_type == SignalType.TAKE_PROFIT:
            await self._handle_take_profit_signal(stock_code, current_price)
    
    async def _handle_entry_signal(
        self,
        stock_code: str,
        current_price: float
    ) -> None:
        """
        진입 시그널 처리
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
        """
        position = self.strategy.get_position(stock_code)
        
        if position is None or position.state != StockState.WAIT:
            return
        
        logger.info(f"[MAIN] 진입 시그널 처리: {stock_code} @ {current_price:,.0f}")
        
        if self.is_cbt_mode:
            # CBT 모드: 알림만 전송
            self.notifier.notify_entry_signal(
                stock_code=stock_code,
                stock_name=position.stock_name,
                current_price=current_price,
                entry_price=position.entry_price,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                is_cbt_mode=True
            )
            
            # 상태 업데이트 (가상 진입)
            self.strategy.update_state_to_entered(stock_code, current_price)
            
        else:
            # LIVE 모드: 실제 주문 실행 (구조만 설계)
            # TODO: KIS API를 통한 실제 매수 주문 구현
            logger.info(f"[MAIN] [LIVE] 매수 주문 실행: {stock_code}")
            
            # 주문 성공 시
            self.notifier.notify_entry_signal(
                stock_code=stock_code,
                stock_name=position.stock_name,
                current_price=current_price,
                entry_price=current_price,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                is_cbt_mode=False,
                quantity=position.quantity
            )
            
            self.strategy.update_state_to_entered(stock_code, current_price, position.quantity)
        
        self._stats["entry_count"] += 1
    
    async def _handle_stop_loss_signal(
        self,
        stock_code: str,
        current_price: float
    ) -> None:
        """
        손절 시그널 처리
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
        """
        position = self.strategy.get_position(stock_code)
        
        if position is None or position.state != StockState.ENTERED:
            return
        
        logger.info(f"[MAIN] 손절 시그널 처리: {stock_code} @ {current_price:,.0f}")
        
        if self.is_cbt_mode:
            # CBT 모드: 알림만 전송
            self.notifier.notify_stop_loss(
                stock_code=stock_code,
                stock_name=position.stock_name,
                entry_price=position.entered_price,
                current_price=current_price,
                stop_loss=position.stop_loss,
                is_cbt_mode=True
            )
            
        else:
            # LIVE 모드: 실제 매도 주문 실행
            # TODO: KIS API를 통한 실제 매도 주문 구현
            logger.info(f"[MAIN] [LIVE] 손절 매도 주문 실행: {stock_code}")
            
            pnl = (current_price - position.entered_price) * position.quantity
            
            self.notifier.notify_stop_loss(
                stock_code=stock_code,
                stock_name=position.stock_name,
                entry_price=position.entered_price,
                current_price=current_price,
                stop_loss=position.stop_loss,
                is_cbt_mode=False,
                exit_price=current_price,
                pnl=pnl
            )
        
        # 상태 업데이트
        self.strategy.update_state_to_exited(stock_code)
        
        self._stats["stop_loss_count"] += 1
    
    async def _handle_take_profit_signal(
        self,
        stock_code: str,
        current_price: float
    ) -> None:
        """
        익절 시그널 처리
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
        """
        position = self.strategy.get_position(stock_code)
        
        if position is None or position.state != StockState.ENTERED:
            return
        
        logger.info(f"[MAIN] 익절 시그널 처리: {stock_code} @ {current_price:,.0f}")
        
        if self.is_cbt_mode:
            # CBT 모드: 알림만 전송
            self.notifier.notify_take_profit(
                stock_code=stock_code,
                stock_name=position.stock_name,
                entry_price=position.entered_price,
                current_price=current_price,
                take_profit=position.take_profit,
                is_cbt_mode=True
            )
            
        else:
            # LIVE 모드: 실제 매도 주문 실행
            # TODO: KIS API를 통한 실제 매도 주문 구현
            logger.info(f"[MAIN] [LIVE] 익절 매도 주문 실행: {stock_code}")
            
            pnl = (current_price - position.entered_price) * position.quantity
            
            self.notifier.notify_take_profit(
                stock_code=stock_code,
                stock_name=position.stock_name,
                entry_price=position.entered_price,
                current_price=current_price,
                take_profit=position.take_profit,
                is_cbt_mode=False,
                exit_price=current_price,
                pnl=pnl
            )
        
        # 상태 업데이트
        self.strategy.update_state_to_exited(stock_code)
        
        self._stats["take_profit_count"] += 1
    
    # ════════════════════════════════════════════════════════════════
    # 시스템 제어
    # ════════════════════════════════════════════════════════════════
    
    async def start(self) -> None:
        """
        자동매매 시스템을 시작합니다.
        """
        logger.info("[MAIN] 시스템 시작")
        
        self._is_running = True
        self._start_time = datetime.now()
        
        # 종목 로드
        if not self.load_universe():
            logger.error("[MAIN] 종목 로드 실패 - 시스템 종료")
            return
        
        # 초기 상태 출력
        self.strategy.print_status()
        
        # WebSocket 콜백 설정
        self.ws_client.set_on_price_callback(self.on_price_update)
        self.ws_client.set_on_connect_callback(self._on_ws_connected)
        self.ws_client.set_on_disconnect_callback(self._on_ws_disconnected)
        
        # 구독할 종목 설정
        subscribe_codes = self.strategy.get_subscribed_codes()
        self.ws_client.subscribe(subscribe_codes)
        
        # 시작 알림
        positions = self.strategy.get_all_positions()
        stock_list = [(p.stock_code, p.stock_name) for p in positions.values()]
        
        self.notifier.notify_system_start(
            mode=self.trade_mode.value,
            stock_list=stock_list,
            entry_start=self.trading_config.entry_start_time,
            entry_end=self.trading_config.entry_end_time,
            close_time=self.trading_config.close_time
        )
        
        # 시간 체크 태스크 시작
        time_check_task = asyncio.create_task(self._time_check_loop())
        
        try:
            # WebSocket 실행
            await self.ws_client.run()
            
        except Exception as e:
            logger.error(f"[MAIN] 실행 오류: {e}")
            self.notifier.notify_error("시스템 오류", str(e))
            
        finally:
            time_check_task.cancel()
            await self.stop("메인 루프 종료")
    
    async def stop(self, reason: str = "사용자 요청") -> None:
        """
        자동매매 시스템을 종료합니다.
        
        Args:
            reason: 종료 사유
        """
        if not self._is_running:
            return
        
        logger.info(f"[MAIN] 시스템 종료: {reason}")
        
        self._is_running = False
        
        # WebSocket 종료
        self.ws_client.stop()
        
        # 실행 시간 계산
        duration = "0분"
        if self._start_time:
            elapsed = datetime.now() - self._start_time
            minutes = int(elapsed.total_seconds() / 60)
            duration = f"{minutes}분"
        
        # 통계 계산
        stats = self.strategy.get_statistics()
        
        # 종료 알림
        self.notifier.notify_system_stop(
            reason=reason,
            duration=duration,
            entry_count=self._stats["entry_count"],
            stop_loss_count=self._stats["stop_loss_count"],
            take_profit_count=self._stats["take_profit_count"],
            waiting_count=stats["wait"]
        )
        
        # 최종 상태 출력
        self.strategy.print_status()
    
    async def _time_check_loop(self) -> None:
        """
        운영 시간을 주기적으로 체크하는 루프
        
        15:30 이후 시스템을 자동으로 종료합니다.
        """
        while self._is_running:
            try:
                # 운영 시간 체크
                self.update_entry_permission()
                
                # 종료 시간 체크
                if self.is_close_time():
                    logger.info("[MAIN] 장 마감 시간 도달")
                    await self.stop("장 마감 시간 (15:30)")
                    break
                
                # 1분마다 체크
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MAIN] 시간 체크 오류: {e}")
                await asyncio.sleep(60)
    
    def _on_ws_connected(self) -> None:
        """WebSocket 연결 성공 콜백"""
        logger.info("[MAIN] WebSocket 연결됨")
    
    def _on_ws_disconnected(self, reason: str) -> None:
        """WebSocket 연결 해제 콜백"""
        logger.info(f"[MAIN] WebSocket 연결 해제: {reason}")


# ════════════════════════════════════════════════════════════════
# 시그널 핸들러
# ════════════════════════════════════════════════════════════════

_controller: Optional[TradingController] = None


def signal_handler(signum, frame):
    """종료 시그널 핸들러"""
    global _controller
    
    logger.info(f"[MAIN] 종료 시그널 수신: {signum}")
    
    if _controller:
        _controller._is_running = False
        _controller.ws_client.stop()


# ════════════════════════════════════════════════════════════════
# 메인 엔트리포인트
# ════════════════════════════════════════════════════════════════

async def main():
    """메인 함수"""
    global _controller
    
    print("\n" + "=" * 60)
    print("  KIS WebSocket 자동매매 시스템")
    print("=" * 60)
    print()
    
    # 시그널 핸들러 등록
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # 컨트롤러 생성
        _controller = TradingController()
        
        # 시스템 시작
        await _controller.start()
        
    except ValueError as e:
        logger.error(f"[MAIN] 설정 오류: {e}")
        print(f"\n❌ 설정 오류: {e}")
        print("   .env 파일을 확인하세요.\n")
        
    except KeyboardInterrupt:
        logger.info("[MAIN] 키보드 인터럽트")
        if _controller:
            await _controller.stop("키보드 인터럽트")
            
    except Exception as e:
        logger.error(f"[MAIN] 예상치 못한 오류: {e}")
        if _controller and _controller.notifier:
            _controller.notifier.notify_error("시스템 오류", str(e))


if __name__ == "__main__":
    asyncio.run(main())
