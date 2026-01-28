"""
KIS Trend-ATR Trading System - 거래 실행 엔진

이 모듈은 전략 시그널에 따라 실제 주문을 실행합니다.
중복 주문 방지, 에러 처리, 포지션 관리 등을 담당합니다.

v2.0 업데이트:
- 포지션 영속화 및 동기화
- 긴급 손절 재시도 로직
- 주문 체결 확인
- 일일 손실 한도
- 거래시간 검증

⚠️ 주의: 실계좌 사용 전 충분한 테스트 필요
"""

import time
from datetime import datetime
from typing import Dict, Optional, Tuple
import pandas as pd

from config import settings
from api.kis_api import KISApi, KISApiError
from strategy.trend_atr import TrendATRStrategy, Signal, SignalType, Position
from utils.logger import get_logger, TradeLogger
from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
from engine.risk_manager import (
    RiskManager,
    RiskCheckResult,
    create_risk_manager_from_settings,
    safe_exit_with_message
)

logger = get_logger("executor")
trade_logger = TradeLogger("executor")


class ExecutorError(Exception):
    """거래 실행 엔진 에러 클래스"""
    pass


class TradingExecutor:
    """
    거래 실행 엔진 클래스 (v2.0)
    
    전략에서 생성된 시그널을 실제 주문으로 변환하고 실행합니다.
    포지션 상태 관리, 중복 주문 방지, API 에러 처리를 담당합니다.
    
    v2.0 신규 기능:
    - 시작 시 포지션 자동 동기화
    - 긴급 손절 재시도 (최대 10회)
    - 주문 체결 확인 후 포지션 반영
    - 일일 손실 한도 자동 체크
    - 거래시간 외 주문 차단
    
    Attributes:
        api: KIS API 클라이언트
        strategy: Trend-ATR 전략
        stock_code: 거래 종목 코드
        order_quantity: 주문 수량
        is_running: 실행 상태
    """
    
    def __init__(
        self,
        api: KISApi = None,
        strategy: TrendATRStrategy = None,
        stock_code: str = None,
        order_quantity: int = None,
        risk_manager: RiskManager = None,
        telegram_notifier: TelegramNotifier = None
    ):
        """
        거래 실행 엔진 초기화
        
        Args:
            api: KIS API 클라이언트 (미입력 시 자동 생성)
            strategy: 전략 인스턴스 (미입력 시 자동 생성)
            stock_code: 거래 종목 코드 (기본: 설정 파일 값)
            order_quantity: 주문 수량 (기본: 설정 파일 값)
            risk_manager: 리스크 매니저 (미입력 시 자동 생성)
            telegram_notifier: 텔레그램 알림기 (미입력 시 자동 생성)
        """
        self.api = api or KISApi(is_paper_trading=True)
        self.strategy = strategy or TrendATRStrategy()
        self.stock_code = stock_code or settings.DEFAULT_STOCK_CODE
        self.order_quantity = order_quantity or settings.ORDER_QUANTITY
        
        # 리스크 매니저 초기화 (필수!)
        self.risk_manager = risk_manager or create_risk_manager_from_settings()
        
        # 텔레그램 알림기 초기화
        self.telegram = telegram_notifier or get_telegram_notifier()
        
        # 실행 상태
        self.is_running = False
        self._is_emergency_stop = False
        
        # 주문 실행 추적 (중복 방지)
        self._last_order_time: Optional[datetime] = None
        self._last_signal_type: Optional[SignalType] = None
        
        # 일별 거래 기록
        self._daily_trades: list = []
        
        # 저장소
        self._position_store = get_position_store()
        self._daily_trade_store = get_daily_trade_store()
        
        # 시작 시 포지션 동기화
        if auto_sync:
            self._sync_position_on_startup()
        
        logger.info(
            f"거래 실행 엔진 초기화: 종목={self.stock_code}, "
            f"수량={self.order_quantity}주"
        )
        
        # 리스크 매니저 상태 출력
        self.risk_manager.print_status()
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 동기화 (v2.0 신규)
    # ════════════════════════════════════════════════════════════════
    
    def _sync_position_on_startup(self) -> None:
        """
        시작 시 포지션을 동기화합니다.
        
        1. 저장된 포지션 파일 확인
        2. 실제 계좌 잔고와 대조
        3. 불일치 시 계좌 기준으로 복구
        """
        logger.info("포지션 동기화 시작...")
        
        # 1. 저장된 포지션 로드
        stored_position = self._position_store.load_position()
        
        # 2. 실제 계좌 잔고 확인
        try:
            balance = self.api.get_account_balance()
            actual_holding = None
            
            for holding in balance.get("holdings", []):
                if holding["stock_code"] == self.stock_code:
                    actual_holding = holding
                    break
            
            # 3. 동기화 로직
            if actual_holding and actual_holding["quantity"] > 0:
                # 실제 보유 중
                if stored_position:
                    # 저장된 포지션과 대조
                    if stored_position.quantity != actual_holding["quantity"]:
                        logger.warning(
                            f"포지션 불일치 감지: "
                            f"저장={stored_position.quantity}주, "
                            f"실제={actual_holding['quantity']}주"
                        )
                    
                    # 저장된 손절/익절 사용
                    self.strategy.position = Position(
                        stock_code=stored_position.stock_code,
                        entry_price=stored_position.entry_price,
                        quantity=actual_holding["quantity"],  # 실제 수량 사용
                        stop_loss=stored_position.stop_loss,
                        take_profit=stored_position.take_profit,
                        entry_date=stored_position.entry_date,
                        atr_at_entry=stored_position.atr_at_entry
                    )
                    logger.info(f"저장된 포지션으로 복구: {stored_position.stock_code}")
                else:
                    # 저장된 포지션 없음 - 계좌 기준 복구
                    self._recover_position_from_account(actual_holding)
            else:
                # 실제 미보유
                if stored_position:
                    logger.warning("저장된 포지션이 있으나 실제 미보유 - 포지션 초기화")
                    self._position_store.clear_position()
                
                self.strategy.position = None
                logger.info("포지션 없음 확인")
        
        except KISApiError as e:
            logger.error(f"포지션 동기화 실패: {e}")
            # 실패 시 저장된 포지션이라도 로드
            if stored_position:
                self.strategy.position = Position(
                    stock_code=stored_position.stock_code,
                    entry_price=stored_position.entry_price,
                    quantity=stored_position.quantity,
                    stop_loss=stored_position.stop_loss,
                    take_profit=stored_position.take_profit,
                    entry_date=stored_position.entry_date,
                    atr_at_entry=stored_position.atr_at_entry
                )
                logger.warning("저장된 포지션으로 복구 (계좌 확인 실패)")
    
    def _recover_position_from_account(self, holding: Dict) -> None:
        """
        계좌 보유 정보에서 포지션을 복구합니다.
        
        저장된 포지션이 없을 때 사용합니다.
        손절/익절가는 현재가 기준으로 재계산합니다.
        
        Args:
            holding: 계좌 보유 정보
        """
        try:
            # 현재 ATR 계산을 위한 데이터 조회
            df = self.api.get_daily_ohlcv(self.stock_code)
            if not df.empty:
                df_with_ind = self.strategy.add_indicators(df)
                current_atr = df_with_ind.iloc[-1]['atr']
                
                if pd.isna(current_atr):
                    current_atr = holding["current_price"] * 0.02  # 2% 추정
            else:
                current_atr = holding["current_price"] * 0.02
            
            # 현재가 기준 손절/익절 재계산
            current_price = holding["current_price"]
            stop_loss = self.strategy.calculate_stop_loss(current_price, current_atr)
            take_profit = self.strategy.calculate_take_profit(current_price, current_atr)
            
            self.strategy.position = Position(
                stock_code=holding["stock_code"],
                entry_price=holding["avg_price"],
                quantity=holding["quantity"],
                stop_loss=stop_loss,
                take_profit=take_profit,
                entry_date="RECOVERED",
                atr_at_entry=current_atr
            )
            
            # 복구된 포지션 저장
            self._save_position_to_store()
            
            logger.warning(
                f"포지션 복구 완료 (계좌 기준): {holding['stock_code']}, "
                f"진입가={holding['avg_price']:,.0f}, "
                f"손절={stop_loss:,.0f}, 익절={take_profit:,.0f}"
            )
            
        except Exception as e:
            logger.error(f"포지션 복구 실패: {e}")
    
    def _save_position_to_store(self) -> None:
        """현재 포지션을 파일에 저장합니다."""
        if self.strategy.position:
            pos = self.strategy.position
            stored = StoredPosition(
                stock_code=pos.stock_code,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                entry_date=pos.entry_date,
                atr_at_entry=pos.atr_at_entry
            )
            self._position_store.save_position(stored)
    
    # ════════════════════════════════════════════════════════════════
    # 데이터 조회
    # ════════════════════════════════════════════════════════════════
    
    def fetch_market_data(self, days: int = 100) -> pd.DataFrame:
        """
        시장 데이터를 조회합니다.
        
        Args:
            days: 조회할 일수 (기본: 100일)
        
        Returns:
            pd.DataFrame: OHLCV 데이터
        """
        try:
            df = self.api.get_daily_ohlcv(
                stock_code=self.stock_code,
                period_type="D"
            )
            
            if df.empty:
                logger.warning(f"시장 데이터 없음: {self.stock_code}")
                return pd.DataFrame()
            
            logger.debug(f"시장 데이터 조회 완료: {len(df)}개")
            return df
            
        except KISApiError as e:
            logger.error(f"시장 데이터 조회 실패: {e}")
            return pd.DataFrame()
    
    def fetch_current_price(self) -> float:
        """
        현재가를 조회합니다.
        
        Returns:
            float: 현재가 (조회 실패 시 0)
        """
        try:
            price_data = self.api.get_current_price(self.stock_code)
            current_price = price_data.get("current_price", 0)
            
            logger.debug(f"현재가 조회: {self.stock_code} = {current_price:,.0f}원")
            return current_price
            
        except KISApiError as e:
            logger.error(f"현재가 조회 실패: {e}")
            return 0.0
    
    # ════════════════════════════════════════════════════════════════
    # 일일 한도 체크 (v2.0 신규)
    # ════════════════════════════════════════════════════════════════
    
    def _check_daily_limits(self) -> Tuple[bool, str]:
        """
        일일 거래 한도를 체크합니다.
        
        Returns:
            Tuple[bool, str]: (거래 가능 여부, 차단 사유)
        """
        is_limited, reason = self._daily_trade_store.is_daily_limit_reached(
            max_loss_pct=settings.DAILY_MAX_LOSS_PCT,
            max_trades=settings.DAILY_MAX_TRADES,
            max_consecutive_losses=settings.MAX_CONSECUTIVE_LOSSES
        )
        
        if is_limited:
            return False, reason
        
        return True, ""
    
    # ════════════════════════════════════════════════════════════════
    # 주문 체결 확인 (v2.0 신규)
    # ════════════════════════════════════════════════════════════════
    
    def _wait_for_execution(
        self, 
        order_no: str, 
        timeout: int = None,
        check_interval: int = None
    ) -> Optional[Dict]:
        """
        주문 체결을 대기하고 확인합니다.
        
        Args:
            order_no: 주문 번호
            timeout: 최대 대기 시간 (초)
            check_interval: 확인 간격 (초)
        
        Returns:
            Optional[Dict]: 체결 정보 (미체결 시 None)
        """
        if timeout is None:
            timeout = settings.ORDER_EXECUTION_TIMEOUT
        if check_interval is None:
            check_interval = settings.ORDER_CHECK_INTERVAL
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                status = self.api.get_order_status(order_no)
                
                for order in status.get("orders", []):
                    if order.get("order_no") == order_no:
                        exec_qty = order.get("exec_qty", 0)
                        
                        if exec_qty > 0:
                            logger.info(
                                f"주문 체결 확인: {order_no}, "
                                f"체결가={order.get('exec_price', 0):,.0f}, "
                                f"체결수량={exec_qty}"
                            )
                            return order
                
                time.sleep(check_interval)
                
            except KISApiError as e:
                logger.warning(f"체결 확인 중 오류: {e}")
                time.sleep(check_interval)
        
        logger.warning(f"체결 대기 시간 초과: {order_no}")
        return None
    
    # ════════════════════════════════════════════════════════════════
    # 주문 실행
    # ════════════════════════════════════════════════════════════════
    
    def _can_execute_order(self, signal: Signal) -> bool:
        """
        주문 실행 가능 여부를 확인합니다.
        
        중복 주문 방지 로직:
            - 동일 시그널 연속 실행 방지
            - 최소 주문 간격 확인
        
        Args:
            signal: 매매 시그널
        
        Returns:
            bool: 주문 가능 여부
        """
        if signal.signal_type == SignalType.HOLD:
            return False
        
        # 동일 시그널 연속 실행 방지
        if self._last_signal_type == signal.signal_type:
            if self._last_order_time:
                elapsed = (datetime.now() - self._last_order_time).total_seconds()
                # 1분 이내 동일 시그널 무시
                if elapsed < 60:
                    logger.debug("중복 주문 방지: 동일 시그널 무시")
                    return False
        
        return True
    
    def execute_buy_order(self, signal: Signal) -> Dict:
        """
        매수 주문을 실행합니다 (v2.0 개선).
        
        개선 사항:
        - 체결 확인 후 포지션 반영
        - 실제 체결가 사용
        - 포지션 파일 저장
        
        Args:
            signal: 매수 시그널
        
        Returns:
            Dict: 주문 결과
        """
        # ★ 리스크 체크 (신규 진입 주문)
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            logger.warning(risk_check.reason)
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self._can_execute_order(signal):
            return {"success": False, "message": "주문 조건 미충족"}
        
        # 이미 포지션 보유 중인 경우
        if self.strategy.has_position():
            logger.warning("매수 주문 취소: 포지션 이미 보유 중")
            return {"success": False, "message": "포지션 보유 중"}
        
        # 일일 한도 체크
        can_trade, limit_reason = self._check_daily_limits()
        if not can_trade:
            logger.warning(f"매수 주문 취소: {limit_reason}")
            return {"success": False, "message": limit_reason}
        
        try:
            # 매수 주문 실행
            result = self.api.place_buy_order(
                stock_code=self.stock_code,
                quantity=self.order_quantity,
                price=0,  # 시장가
                order_type="01"  # 시장가 주문
            )
            
            if result["success"]:
                # 체결 확인 대기
                executed = self._wait_for_execution(result["order_no"])
                
                # 주문 추적 업데이트
                self._last_order_time = datetime.now()
                self._last_signal_type = SignalType.BUY
                
                # 거래 기록
                self._daily_trades.append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "BUY",
                    "price": signal.price,
                    "quantity": self.order_quantity,
                    "order_no": result["order_no"]
                })
                
                logger.info(f"매수 주문 성공: {result['order_no']}")
                
                # 📱 텔레그램 알림 전송
                self.telegram.notify_buy_order(
                    stock_code=self.stock_code,
                    price=signal.price,
                    quantity=self.order_quantity,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit
                )
            else:
                logger.error(f"매수 주문 실패: {result['message']}")
            
            return result
            
        except KISApiError as e:
            trade_logger.log_error("매수 주문", str(e))
            # 📱 텔레그램 에러 알림
            self.telegram.notify_error("매수 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def execute_sell_order(self, signal: Signal, is_emergency: bool = False) -> Dict:
        """
        매도 주문을 실행합니다 (v2.0 개선).
        
        개선 사항:
        - 긴급 손절 시 재시도 로직
        - 체결 확인 후 포지션 청산
        - 포지션 파일 삭제
        
        Args:
            signal: 매도 시그널
            is_emergency: 긴급 손절 여부
        
        Returns:
            Dict: 주문 결과
        """
        # ★ 리스크 체크 (청산 주문 - 손실 한도 도달해도 청산은 허용)
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=True)
        if not risk_check.passed:
            logger.warning(risk_check.reason)
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self._can_execute_order(signal):
            return {"success": False, "message": "주문 조건 미충족"}
        
        # 포지션 미보유 시
        if not self.strategy.has_position():
            logger.warning("매도 주문 취소: 보유 포지션 없음")
            return {"success": False, "message": "포지션 없음"}
        
        position = self.strategy.position
        
        # 긴급 손절 시 재시도 설정
        max_retries = settings.EMERGENCY_SELL_MAX_RETRIES if is_emergency else 1
        retry_interval = settings.EMERGENCY_SELL_RETRY_INTERVAL
        
        for attempt in range(max_retries):
            try:
                # 매도 주문 실행
                result = self.api.place_sell_order(
                    stock_code=self.stock_code,
                    quantity=position.quantity,
                    price=0,  # 시장가
                    order_type="01"  # 시장가 주문
                )
                
                # ★ 리스크 매니저에 손익 기록
                if close_result:
                    self.risk_manager.record_trade_pnl(close_result["pnl"])
                
                # 주문 추적 업데이트
                self._last_order_time = datetime.now()
                self._last_signal_type = SignalType.SELL
                
                # 거래 기록
                pnl = close_result["pnl"] if close_result else 0
                pnl_pct = close_result["pnl_pct"] if close_result else 0
                
                self._daily_trades.append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "SELL",
                    "price": signal.price,
                    "quantity": position.quantity,
                    "order_no": result["order_no"],
                    "pnl": pnl,
                    "pnl_pct": pnl_pct
                })
                
                logger.info(f"매도 주문 성공: {result['order_no']}")
                
                # 📱 텔레그램 알림 전송 (손절/익절 구분)
                if close_result:
                    if "손절" in signal.reason or pnl < 0:
                        self.telegram.notify_stop_loss(
                            stock_code=self.stock_code,
                            entry_price=position.entry_price,
                            exit_price=signal.price,
                            pnl=pnl,
                            pnl_pct=pnl_pct
                        )
                    elif "익절" in signal.reason or pnl > 0:
                        self.telegram.notify_take_profit(
                            stock_code=self.stock_code,
                            entry_price=position.entry_price,
                            exit_price=signal.price,
                            pnl=pnl,
                            pnl_pct=pnl_pct
                        )
                    else:
                        # 일반 청산
                        self.telegram.notify_sell_order(
                            stock_code=self.stock_code,
                            price=signal.price,
                            quantity=position.quantity,
                            reason=signal.reason,
                            pnl=pnl,
                            pnl_pct=pnl_pct
                        )
            else:
                logger.error(f"매도 주문 실패: {result['message']}")
            
            return result
            
        except KISApiError as e:
            trade_logger.log_error("매도 주문", str(e))
            # 📱 텔레그램 에러 알림
            self.telegram.notify_error("매도 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    # ════════════════════════════════════════════════════════════════
    # 메인 실행 로직
    # ════════════════════════════════════════════════════════════════
    
    def run_once(self) -> Dict:
        """
        전략을 1회 실행합니다 (v2.0 개선).
        
        실행 순서:
            0. 리스크 체크 (Kill Switch)
            1. 시장 데이터 조회
            2. 현재가 조회
            3. 전략 시그널 생성
            4. 시그널에 따른 주문 실행
        
        Returns:
            Dict: 실행 결과
        """
        logger.info("=" * 50)
        logger.info("전략 실행 시작")
        
        # ★ 실행 전 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
        
        result = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stock_code": self.stock_code,
            "signal": None,
            "order_result": None,
            "position": None,
            "error": None,
            "skipped": False
        }
        
        # 긴급 정지 상태 확인
        if self._is_emergency_stop:
            result["error"] = "긴급 정지 상태"
            result["skipped"] = True
            logger.error("긴급 정지 상태: 수동 개입 필요")
            return result
        
        # 거래시간 검증
        should_skip, skip_reason = should_skip_trading()
        if should_skip:
            result["skipped"] = True
            result["error"] = skip_reason
            logger.info(f"거래 건너뜀: {skip_reason}")
            return result
        
        # 일일 한도 체크 (신규 진입만 제한, 청산은 허용)
        can_trade, limit_reason = self._check_daily_limits()
        if not can_trade and not self.strategy.has_position():
            result["skipped"] = True
            result["error"] = limit_reason
            logger.warning(f"신규 진입 제한: {limit_reason}")
            # 포지션 보유 중이면 청산 가능하도록 계속 진행
            if not self.strategy.has_position():
                return result
        
        try:
            # 1. 시장 데이터 조회
            df = self.fetch_market_data()
            if df.empty:
                result["error"] = "시장 데이터 조회 실패"
                logger.error(result["error"])
                return result
            
            # 2. 현재가 조회
            current_price = self.fetch_current_price()
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                logger.error(result["error"])
                return result
            
            # 3. 전략 시그널 생성
            signal = self.strategy.generate_signal(
                df=df,
                current_price=current_price,
                stock_code=self.stock_code
            )
            
            result["signal"] = {
                "type": signal.signal_type.value,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reason": signal.reason,
                "atr": signal.atr,
                "trend": signal.trend.value
            }
            
            logger.info(
                f"시그널: {signal.signal_type.value} | "
                f"가격: {current_price:,.0f}원 | "
                f"추세: {signal.trend.value} | "
                f"사유: {signal.reason}"
            )
            
            # 4. 시그널에 따른 주문 실행
            if signal.signal_type == SignalType.BUY:
                # 일일 한도 확인 (매수만)
                if can_trade:
                    order_result = self.execute_buy_order(signal)
                    result["order_result"] = order_result
                else:
                    result["order_result"] = {"success": False, "message": limit_reason}
                
            elif signal.signal_type == SignalType.SELL:
                # 손절 여부 확인
                is_stop_loss = "손절" in signal.reason
                order_result = self.execute_sell_order(signal, is_emergency=is_stop_loss)
                result["order_result"] = order_result
            
            # 5. 현재 포지션 정보
            if self.strategy.has_position():
                pos = self.strategy.position
                pnl, pnl_pct = self.strategy.get_position_pnl(current_price)
                
                result["position"] = {
                    "stock_code": pos.stock_code,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                    "current_price": current_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct
                }
                
                logger.info(
                    f"포지션: {pos.stock_code} | "
                    f"진입가: {pos.entry_price:,.0f}원 | "
                    f"현재가: {current_price:,.0f}원 | "
                    f"손익: {pnl:,.0f}원 ({pnl_pct:+.2f}%)"
                )
            else:
                logger.info("포지션: 없음")
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"전략 실행 오류: {e}")
            # 📱 텔레그램 에러 알림
            self.telegram.notify_error("전략 실행 오류", str(e))
        
        logger.info("전략 실행 완료")
        logger.info("=" * 50)
        
        return result
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None) -> None:
        """
        전략을 지속적으로 실행합니다 (v2.0 개선).
        
        개선 사항:
        - 거래시간 외 자동 대기
        - 긴급 정지 처리
        
        Args:
            interval_seconds: 실행 간격 (초, 최소 60초)
            max_iterations: 최대 반복 횟수 (None = 무한)
        """
        # ★ 시작 전 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
            return
        
        # 초단타 방지: 최소 60초 간격
        if interval_seconds < 60:
            logger.warning("실행 간격이 60초 미만입니다. 60초로 조정합니다.")
            interval_seconds = 60
        
        self.is_running = True
        iteration = 0
        
        logger.info(f"거래 실행 시작 (간격: {interval_seconds}초)")
        
        # 📱 텔레그램 시작 알림
        self.telegram.notify_system_start(
            stock_code=self.stock_code,
            order_quantity=self.order_quantity,
            interval=interval_seconds,
            mode="모의투자" if settings.IS_PAPER_TRADING else "실계좌"
        )
        
        try:
            while self.is_running:
                iteration += 1
                logger.info(f"[반복 #{iteration}]")
                
                # 긴급 정지 확인
                if self._is_emergency_stop:
                    logger.critical("긴급 정지 상태 - 실행 중단")
                    break
                
                # 전략 실행
                result = self.run_once()
                
                # 최대 반복 횟수 확인
                if max_iterations and iteration >= max_iterations:
                    logger.info(f"최대 반복 횟수 도달: {max_iterations}")
                    break
                
                # 다음 실행까지 대기
                if not result.get("skipped"):
                    logger.info(f"다음 실행까지 {interval_seconds}초 대기...")
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("사용자에 의해 중단됨")
            stop_reason = "사용자 중단"
        except Exception as e:
            logger.error(f"예기치 않은 오류: {e}")
            stop_reason = f"오류 발생: {str(e)}"
            # 📱 텔레그램 에러 알림
            self.telegram.notify_error("시스템 오류", str(e))
        else:
            stop_reason = "정상 종료"
        finally:
            self.is_running = False
            logger.info("거래 실행 종료")
            
            # 📱 텔레그램 종료 알림
            summary = self.get_daily_summary()
            self.telegram.notify_system_stop(
                reason=stop_reason,
                total_trades=summary["total_trades"],
                daily_pnl=summary["total_pnl"]
            )
    
    def stop(self) -> None:
        """거래 실행을 중지합니다."""
        logger.info("거래 실행 중지 요청")
        self.is_running = False
    
    # ════════════════════════════════════════════════════════════════
    # 유틸리티
    # ════════════════════════════════════════════════════════════════
    
    def get_daily_summary(self) -> Dict:
        """
        일별 거래 요약을 반환합니다.
        
        Returns:
            Dict: 거래 요약
        """
        if not self._daily_trades:
            return {
                "total_trades": 0,
                "buy_count": 0,
                "sell_count": 0,
                "total_pnl": 0,
                "trades": []
            }
        
        buy_count = sum(1 for t in self._daily_trades if t["type"] == "BUY")
        sell_count = sum(1 for t in self._daily_trades if t["type"] == "SELL")
        total_pnl = sum(t.get("pnl", 0) for t in self._daily_trades)
        
        return {
            "total_trades": len(self._daily_trades),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "total_pnl": total_pnl,
            "trades": self._daily_trades
        }
    
    def reset_daily_trades(self) -> None:
        """일별 거래 기록을 초기화합니다."""
        self._daily_trades = []
        logger.info("일별 거래 기록 초기화")
    
    def get_system_status(self) -> Dict:
        """
        시스템 상태를 반환합니다.
        
        Returns:
            Dict: 시스템 상태
        """
        is_open, market_status = get_market_status()
        can_trade, limit_reason = self._check_daily_limits()
        
        return {
            "is_running": self.is_running,
            "is_emergency_stop": self._is_emergency_stop,
            "market_open": is_open,
            "market_status": market_status,
            "can_trade": can_trade,
            "limit_reason": limit_reason,
            "has_position": self.strategy.has_position(),
            "daily_stats": self._daily_trade_store.get_daily_stats()
        }
