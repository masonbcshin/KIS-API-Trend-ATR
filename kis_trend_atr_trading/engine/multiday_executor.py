"""
KIS Trend-ATR Trading System - 멀티데이 거래 실행 엔진

★ 전략의 본질:
    - 당일 매수·당일 매도(Day Trading)가 아닌
    - 익절 또는 손절 신호가 발생할 때까지 보유(Hold until Exit)

★ 절대 금지 사항:
    - ❌ 장 마감(EOD) 시간 기준 강제 청산 로직
    - ❌ 시간 기반 종료 조건
    - ❌ 익일 ATR 재계산

★ 핵심 기능:
    1. 프로그램 시작 시 포지션 복원
    2. API를 통한 실제 보유 확인
    3. 모드별 주문 처리 (LIVE/CBT/PAPER)
    4. 포지션 영속화 (프로그램 종료 시 저장)
"""

import time
import signal
import sys
from datetime import datetime
from typing import Dict, Optional, Any
import pandas as pd

from config import settings
from api.kis_api import KISApi, KISApiError
from strategy.multiday_trend_atr import (
    MultidayTrendATRStrategy,
    TradingSignal,
    SignalType,
    ExitReason
)
from engine.trading_state import TradingState, MultidayPosition
from engine.risk_manager import (
    RiskManager,
    create_risk_manager_from_settings,
    safe_exit_with_message
)
from utils.position_store import (
    PositionStore,
    StoredPosition,
    get_position_store
)
from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
from utils.logger import get_logger, TradeLogger

logger = get_logger("multiday_executor")
trade_logger = TradeLogger("multiday_executor")


class MultidayExecutor:
    """
    멀티데이 거래 실행 엔진
    
    ★ 핵심 원칙:
        1. EOD 청산 로직 절대 없음
        2. Exit는 오직 가격 조건으로만 발생
        3. ATR은 진입 시 고정
        4. 프로그램 종료 시 포지션 상태 저장
        5. 프로그램 시작 시 포지션 복원
    """
    
    def __init__(
        self,
        api: KISApi = None,
        strategy: MultidayTrendATRStrategy = None,
        stock_code: str = None,
        order_quantity: int = None,
        risk_manager: RiskManager = None,
        telegram: TelegramNotifier = None,
        position_store: PositionStore = None
    ):
        """
        멀티데이 실행 엔진 초기화
        
        Args:
            api: KIS API 클라이언트
            strategy: 멀티데이 전략
            stock_code: 거래 종목
            order_quantity: 주문 수량
            risk_manager: 리스크 매니저
            telegram: 텔레그램 알림기
            position_store: 포지션 저장소
        """
        # 트레이딩 모드 확인
        self.trading_mode = settings.TRADING_MODE
        
        # API 클라이언트 (CBT 모드에서도 데이터 조회용으로 필요)
        is_paper = self.trading_mode != "LIVE"
        self.api = api or KISApi(is_paper_trading=is_paper)
        
        # 전략 초기화
        self.strategy = strategy or MultidayTrendATRStrategy()
        
        # 기본 설정
        self.stock_code = stock_code or settings.DEFAULT_STOCK_CODE
        self.order_quantity = order_quantity or settings.ORDER_QUANTITY
        
        # 리스크 매니저
        self.risk_manager = risk_manager or create_risk_manager_from_settings()
        
        # 텔레그램 알림기
        self.telegram = telegram or get_telegram_notifier()
        
        # 포지션 저장소
        self.position_store = position_store or get_position_store()
        
        # 실행 상태
        self.is_running = False
        
        # 알림 추적 (중복 방지)
        self._last_near_sl_alert = None
        self._last_near_tp_alert = None
        self._last_trailing_update = None
        
        # 일별 거래 기록
        self._daily_trades = []
        
        # 시그널 핸들러 등록 (종료 시 포지션 저장)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info(
            f"멀티데이 실행 엔진 초기화: "
            f"모드={self.trading_mode}, 종목={self.stock_code}, "
            f"수량={self.order_quantity}"
        )
        
        # 리스크 매니저 상태 출력
        self.risk_manager.print_status()
    
    def _signal_handler(self, signum, frame):
        """종료 시그널 핸들러"""
        logger.info(f"종료 시그널 수신: {signum}")
        self._save_position_on_exit()
        sys.exit(0)
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 영속화
    # ════════════════════════════════════════════════════════════════
    
    def _save_position_on_exit(self) -> None:
        """
        프로그램 종료 시 포지션 저장
        
        ★ 포지션 보유 중이면 저장
        ★ 포지션 없으면 저장 파일 클리어
        """
        if self.strategy.has_position:
            pos = self.strategy.position
            stored = StoredPosition.from_multiday_position(pos)
            self.position_store.save_position(stored)
            logger.info(f"포지션 저장 완료: {pos.symbol}")
        else:
            self.position_store.clear_position()
            logger.info("포지션 없음 - 저장 파일 클리어")
    
    def restore_position_on_start(self) -> bool:
        """
        프로그램 시작 시 포지션 복원
        
        ★ 순서:
            1. 저장된 포지션 로드
            2. API로 실제 보유 확인
            3. 정합성 검증
            4. 전략에 복원
            5. 텔레그램 알림
        
        Returns:
            bool: 복원 성공 여부
        """
        logger.info("=" * 50)
        logger.info("포지션 복원 프로세스 시작")
        logger.info("=" * 50)
        
        # 1. 저장된 포지션 로드
        stored = self.position_store.load_position()
        
        if stored is None:
            logger.info("저장된 포지션 없음")
            return False
        
        logger.info(
            f"저장된 포지션 발견: {stored.stock_code} @ {stored.entry_price:,.0f}원, "
            f"ATR={stored.atr_at_entry:,.0f} (고정)"
        )
        
        # 2. API로 실제 보유 확인
        try:
            self.api.get_access_token()
            validated, status_msg = self.position_store.reconcile_position(
                self.api, stored
            )
            
            logger.info(f"정합성 검증 결과: {status_msg}")
            
            if validated is None:
                logger.warning("포지션 복원 실패 - 정합성 불일치")
                return False
            
        except Exception as e:
            logger.warning(f"API 검증 실패, 저장된 데이터로 복원: {e}")
            validated = stored
        
        # 3. 전략에 복원
        multiday_pos = validated.to_multiday_position()
        self.strategy.restore_position(multiday_pos)
        
        # 4. 보유 일수 계산
        holding_days = self._calculate_holding_days(validated.entry_date)
        
        # 5. 텔레그램 알림
        self.telegram.notify_position_restored(
            stock_code=validated.stock_code,
            entry_price=validated.entry_price,
            quantity=validated.quantity,
            entry_date=validated.entry_date,
            holding_days=holding_days,
            stop_loss=validated.stop_loss,
            take_profit=validated.take_profit,
            trailing_stop=validated.trailing_stop,
            atr_at_entry=validated.atr_at_entry
        )
        
        logger.info(
            f"포지션 복원 완료: 보유 {holding_days}일째, "
            f"Exit 조건 감시 재개"
        )
        
        return True
    
    def _calculate_holding_days(self, entry_date: str) -> int:
        """보유 일수 계산"""
        try:
            entry = datetime.strptime(entry_date, "%Y-%m-%d")
            return (datetime.now() - entry).days + 1
        except ValueError:
            return 0
    
    # ════════════════════════════════════════════════════════════════
    # 데이터 조회
    # ════════════════════════════════════════════════════════════════
    
    def fetch_market_data(self) -> pd.DataFrame:
        """시장 데이터 조회"""
        try:
            df = self.api.get_daily_ohlcv(
                stock_code=self.stock_code,
                period_type="D"
            )
            
            if df.empty:
                logger.warning(f"시장 데이터 없음: {self.stock_code}")
            
            return df
            
        except KISApiError as e:
            logger.error(f"시장 데이터 조회 실패: {e}")
            return pd.DataFrame()
    
    def fetch_current_price(self) -> tuple:
        """
        현재가 및 시가 조회
        
        Returns:
            tuple: (현재가, 시가)
        """
        try:
            price_data = self.api.get_current_price(self.stock_code)
            current = price_data.get("current_price", 0)
            open_price = price_data.get("open_price", 0)
            
            return current, open_price
            
        except KISApiError as e:
            logger.error(f"현재가 조회 실패: {e}")
            return 0.0, 0.0
    
    # ════════════════════════════════════════════════════════════════
    # 주문 실행 (모드별)
    # ════════════════════════════════════════════════════════════════
    
    def _can_place_orders(self) -> bool:
        """실제 주문 가능 여부"""
        return self.trading_mode in ("LIVE", "PAPER")
    
    def execute_buy(self, signal: TradingSignal) -> Dict[str, Any]:
        """
        매수 주문 실행
        
        ★ 모드별 처리:
            - LIVE/PAPER: 실제 주문
            - CBT: 텔레그램 알림만
        """
        # 리스크 체크
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            logger.warning(f"리스크 체크 실패: {risk_check.reason}")
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        # 이미 포지션 보유
        if self.strategy.has_position:
            return {"success": False, "message": "이미 포지션 보유 중"}
        
        # CBT 모드: 알림만
        if self.trading_mode == "CBT":
            logger.info(f"[CBT] 매수 시그널: {self.stock_code} @ {signal.price:,.0f}원")
            
            self.telegram.notify_cbt_signal(
                signal_type="📈 매수 (BUY)",
                stock_code=self.stock_code,
                price=signal.price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                atr=signal.atr,
                trend=signal.trend.value,
                reason=signal.reason
            )
            
            # 가상 포지션 오픈 (추적용)
            self.strategy.open_position(
                symbol=self.stock_code,
                entry_price=signal.price,
                quantity=self.order_quantity,
                atr=signal.atr,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit
            )
            
            return {"success": True, "message": "[CBT] 가상 매수", "order_no": "CBT-VIRTUAL"}
        
        # LIVE/PAPER: 실제 주문
        try:
            result = self.api.place_buy_order(
                stock_code=self.stock_code,
                quantity=self.order_quantity,
                price=0,  # 시장가
                order_type="01"
            )
            
            if result["success"]:
                # 포지션 오픈
                self.strategy.open_position(
                    symbol=self.stock_code,
                    entry_price=signal.price,
                    quantity=self.order_quantity,
                    atr=signal.atr,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit
                )
                
                # 포지션 저장
                self._save_position_on_exit()
                
                # 거래 기록
                self._daily_trades.append({
                    "time": datetime.now().isoformat(),
                    "type": "BUY",
                    "price": signal.price,
                    "quantity": self.order_quantity,
                    "order_no": result["order_no"]
                })
                
                # 텔레그램 알림
                self.telegram.notify_buy_order(
                    stock_code=self.stock_code,
                    price=signal.price,
                    quantity=self.order_quantity,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit or 0
                )
                
                logger.info(f"매수 주문 성공: {result['order_no']}")
            else:
                logger.error(f"매수 주문 실패: {result['message']}")
            
            return result
            
        except KISApiError as e:
            logger.error(f"매수 주문 에러: {e}")
            self.telegram.notify_error("매수 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def execute_sell(self, signal: TradingSignal) -> Dict[str, Any]:
        """
        매도 주문 실행 (청산)
        
        ★ 허용된 Exit 사유만 처리
        ★ EOD 청산은 절대 불가
        """
        # 리스크 체크 (청산은 항상 허용)
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=True)
        if not risk_check.passed:
            logger.warning(f"리스크 체크 실패 (청산): {risk_check.reason}")
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self.strategy.has_position:
            return {"success": False, "message": "청산할 포지션 없음"}
        
        pos = self.strategy.position
        exit_reason = signal.exit_reason or ExitReason.MANUAL_EXIT
        
        # CBT 모드: 알림만
        if self.trading_mode == "CBT":
            logger.info(
                f"[CBT] 매도 시그널: {self.stock_code} @ {signal.price:,.0f}원, "
                f"사유={exit_reason.value}"
            )
            
            self.telegram.notify_cbt_signal(
                signal_type=f"📉 매도 ({exit_reason.value})",
                stock_code=self.stock_code,
                price=signal.price,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                atr=pos.atr_at_entry,
                trend=signal.trend.value,
                reason=signal.reason
            )
            
            # 가상 포지션 청산
            result = self.strategy.close_position(signal.price, exit_reason)
            
            if result:
                # 리스크 매니저에 손익 기록
                self.risk_manager.record_trade_pnl(result["pnl"])
            
            # 포지션 저장 파일 클리어
            self.position_store.clear_position()
            
            return {"success": True, "message": "[CBT] 가상 청산", "order_no": "CBT-VIRTUAL"}
        
        # LIVE/PAPER: 실제 주문
        try:
            result = self.api.place_sell_order(
                stock_code=self.stock_code,
                quantity=pos.quantity,
                price=0,  # 시장가
                order_type="01"
            )
            
            if result["success"]:
                # 포지션 청산
                close_result = self.strategy.close_position(signal.price, exit_reason)
                
                # 리스크 매니저에 손익 기록
                if close_result:
                    self.risk_manager.record_trade_pnl(close_result["pnl"])
                    
                    # 거래 기록
                    self._daily_trades.append({
                        "time": datetime.now().isoformat(),
                        "type": "SELL",
                        "price": signal.price,
                        "quantity": pos.quantity,
                        "order_no": result["order_no"],
                        "pnl": close_result["pnl"],
                        "pnl_pct": close_result["pnl_pct"],
                        "exit_reason": exit_reason.value
                    })
                    
                    # 텔레그램 알림 (청산 유형별)
                    self._send_exit_notification(
                        exit_reason,
                        pos,
                        signal.price,
                        close_result
                    )
                
                # 포지션 저장 파일 클리어
                self.position_store.clear_position()
                
                logger.info(f"매도 주문 성공: {result['order_no']}")
            else:
                logger.error(f"매도 주문 실패: {result['message']}")
            
            return result
            
        except KISApiError as e:
            logger.error(f"매도 주문 에러: {e}")
            self.telegram.notify_error("매도 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def _send_exit_notification(
        self,
        exit_reason: ExitReason,
        position: MultidayPosition,
        exit_price: float,
        close_result: Dict
    ) -> None:
        """청산 유형별 텔레그램 알림"""
        if exit_reason == ExitReason.ATR_STOP_LOSS:
            self.telegram.notify_stop_loss(
                stock_code=position.symbol,
                entry_price=position.entry_price,
                exit_price=exit_price,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        elif exit_reason == ExitReason.ATR_TAKE_PROFIT:
            self.telegram.notify_take_profit(
                stock_code=position.symbol,
                entry_price=position.entry_price,
                exit_price=exit_price,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        elif exit_reason == ExitReason.TRAILING_STOP:
            self.telegram.notify_sell_order(
                stock_code=position.symbol,
                price=exit_price,
                quantity=position.quantity,
                reason="트레일링 스탑",
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        elif exit_reason == ExitReason.GAP_PROTECTION:
            self.telegram.notify_gap_protection(
                stock_code=position.symbol,
                open_price=exit_price,
                stop_loss=position.stop_loss,
                entry_price=position.entry_price,
                gap_loss_pct=abs(close_result["pnl_pct"]),
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        else:
            self.telegram.notify_sell_order(
                stock_code=position.symbol,
                price=exit_price,
                quantity=position.quantity,
                reason=exit_reason.value,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
    
    # ════════════════════════════════════════════════════════════════
    # 근접 알림
    # ════════════════════════════════════════════════════════════════
    
    def _check_and_send_alerts(self, signal: TradingSignal, current_price: float) -> None:
        """손절/익절 근접 알림 체크 및 전송"""
        if not self.strategy.has_position:
            return
        
        pos = self.strategy.position
        pnl, pnl_pct = pos.get_pnl(current_price)
        
        # 손절선 근접 알림
        if signal.near_stop_loss_pct >= settings.ALERT_NEAR_STOPLOSS_PCT:
            alert_key = f"SL_{pos.symbol}_{int(signal.near_stop_loss_pct)}"
            
            if self._last_near_sl_alert != alert_key:
                self.telegram.notify_near_stop_loss(
                    stock_code=pos.symbol,
                    current_price=current_price,
                    entry_price=pos.entry_price,
                    stop_loss=pos.stop_loss,
                    progress=signal.near_stop_loss_pct,
                    pnl=pnl,
                    pnl_pct=pnl_pct
                )
                self._last_near_sl_alert = alert_key
        
        # 익절선 근접 알림
        if signal.near_take_profit_pct >= settings.ALERT_NEAR_TAKEPROFIT_PCT:
            alert_key = f"TP_{pos.symbol}_{int(signal.near_take_profit_pct)}"
            
            if self._last_near_tp_alert != alert_key:
                self.telegram.notify_near_take_profit(
                    stock_code=pos.symbol,
                    current_price=current_price,
                    entry_price=pos.entry_price,
                    take_profit=pos.take_profit,
                    progress=signal.near_take_profit_pct,
                    pnl=pnl,
                    pnl_pct=pnl_pct
                )
                self._last_near_tp_alert = alert_key
        
        # 트레일링 스탑 갱신 알림
        if settings.ENABLE_TRAILING_STOP and pos.trailing_stop > 0:
            trailing_key = f"TS_{pos.symbol}_{int(pos.trailing_stop)}"
            
            if (self._last_trailing_update != trailing_key and 
                pos.trailing_stop > pos.stop_loss):
                self.telegram.notify_trailing_stop_updated(
                    stock_code=pos.symbol,
                    highest_price=pos.highest_price,
                    trailing_stop=pos.trailing_stop,
                    entry_price=pos.entry_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct
                )
                self._last_trailing_update = trailing_key
    
    # ════════════════════════════════════════════════════════════════
    # 메인 실행 로직
    # ════════════════════════════════════════════════════════════════
    
    def run_once(self) -> Dict[str, Any]:
        """
        전략 1회 실행
        
        ★ EOD 청산 로직 없음
        ★ Exit는 오직 가격 조건으로만 발생
        """
        logger.info("=" * 50)
        logger.info(f"[{self.trading_mode}] 전략 실행")
        
        # 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                self._save_position_on_exit()
                safe_exit_with_message(kill_check.reason)
        
        result = {
            "timestamp": datetime.now().isoformat(),
            "mode": self.trading_mode,
            "stock_code": self.stock_code,
            "signal": None,
            "order_result": None,
            "position": None,
            "error": None
        }
        
        try:
            # 1. 시장 데이터 조회
            df = self.fetch_market_data()
            if df.empty:
                result["error"] = "시장 데이터 없음"
                return result
            
            # 2. 현재가/시가 조회
            current_price, open_price = self.fetch_current_price()
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                return result
            
            # 3. 시그널 생성
            signal = self.strategy.generate_signal(
                df=df,
                current_price=current_price,
                open_price=open_price,
                stock_code=self.stock_code
            )
            
            result["signal"] = {
                "type": signal.signal_type.value,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "trailing_stop": signal.trailing_stop,
                "exit_reason": signal.exit_reason.value if signal.exit_reason else None,
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
                order_result = self.execute_buy(signal)
                result["order_result"] = order_result
                
            elif signal.signal_type == SignalType.SELL:
                order_result = self.execute_sell(signal)
                result["order_result"] = order_result
                
            elif signal.signal_type == SignalType.HOLD:
                # 근접 알림 체크
                self._check_and_send_alerts(signal, current_price)
            
            # 5. 현재 포지션 정보
            if self.strategy.has_position:
                pos = self.strategy.position
                pnl, pnl_pct = pos.get_pnl(current_price)
                
                result["position"] = {
                    "symbol": pos.symbol,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                    "trailing_stop": pos.trailing_stop,
                    "highest_price": pos.highest_price,
                    "atr_at_entry": pos.atr_at_entry,
                    "current_price": current_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "entry_date": pos.entry_date
                }
                
                logger.info(
                    f"포지션: {pos.symbol} | "
                    f"진입: {pos.entry_price:,.0f}원 | "
                    f"현재: {current_price:,.0f}원 | "
                    f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)"
                )
            else:
                logger.info("포지션: 없음")
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"전략 실행 오류: {e}")
            self.telegram.notify_error("전략 실행 오류", str(e))
        
        logger.info("=" * 50)
        return result
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None) -> None:
        """
        전략 연속 실행
        
        ★ EOD 청산 로직 없음
        ★ 프로그램 종료 시에도 포지션 유지
        
        Args:
            interval_seconds: 실행 간격 (초, 최소 60초)
            max_iterations: 최대 반복 횟수 (None = 무한)
        """
        # 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
            return
        
        # 최소 간격 보장
        if interval_seconds < 60:
            logger.warning("실행 간격이 60초 미만입니다. 60초로 조정합니다.")
            interval_seconds = 60
        
        self.is_running = True
        iteration = 0
        
        logger.info(f"멀티데이 거래 시작 (모드: {self.trading_mode}, 간격: {interval_seconds}초)")
        
        # 시작 알림
        mode_display = {
            "LIVE": "🔴 실계좌",
            "CBT": "🟡 종이매매",
            "PAPER": "🟢 모의투자"
        }.get(self.trading_mode, self.trading_mode)
        
        self.telegram.notify_system_start(
            stock_code=self.stock_code,
            order_quantity=self.order_quantity,
            interval=interval_seconds,
            mode=mode_display
        )
        
        try:
            while self.is_running:
                iteration += 1
                logger.info(f"[반복 #{iteration}]")
                
                self.run_once()
                
                # 최대 반복 체크
                if max_iterations and iteration >= max_iterations:
                    logger.info(f"최대 반복 도달: {max_iterations}")
                    break
                
                logger.info(f"다음 실행까지 {interval_seconds}초 대기...")
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("사용자 중단")
            stop_reason = "사용자 중단"
        except Exception as e:
            logger.error(f"예기치 않은 오류: {e}")
            stop_reason = f"오류: {str(e)}"
            self.telegram.notify_error("시스템 오류", str(e))
        else:
            stop_reason = "정상 종료"
        finally:
            self.is_running = False
            
            # 포지션 저장
            self._save_position_on_exit()
            
            # 종료 알림
            summary = self.get_daily_summary()
            self.telegram.notify_system_stop(
                reason=stop_reason,
                total_trades=summary["total_trades"],
                daily_pnl=summary["total_pnl"]
            )
            
            logger.info("멀티데이 거래 종료")
    
    def stop(self) -> None:
        """거래 중지"""
        logger.info("거래 중지 요청")
        self.is_running = False
    
    def get_daily_summary(self) -> Dict[str, Any]:
        """일별 거래 요약"""
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
