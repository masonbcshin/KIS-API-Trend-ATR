"""
KIS Trend-ATR Trading System - 거래 실행 엔진

이 모듈은 전략 시그널에 따라 실제 주문을 실행합니다.
중복 주문 방지, 에러 처리, 포지션 관리 등을 담당합니다.

⚠️ 주의: 모의투자 전용으로 설계되었습니다.
"""

import time
from datetime import datetime
from typing import Dict, Optional
import pandas as pd

from config import settings
from api.kis_api import KISApi, KISApiError
from strategy.trend_atr import TrendATRStrategy, Signal, SignalType
from utils.logger import get_logger, TradeLogger
from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
from utils.market_hours import get_kst_now, get_kst_today # KST 시간 함수 임포트
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
    거래 실행 엔진 클래스
    
    전략에서 생성된 시그널을 실제 주문으로 변환하고 실행합니다.
    포지션 상태 관리, 중복 주문 방지, API 에러 처리를 담당합니다.
    
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
        self.api = api or KISApi(is_paper_trading=True)
        self.strategy = strategy or TrendATRStrategy()
        self.stock_code = stock_code or settings.DEFAULT_STOCK_CODE
        self.order_quantity = order_quantity or settings.ORDER_QUANTITY
        self.risk_manager = risk_manager or create_risk_manager_from_settings()
        self.telegram = telegram_notifier or get_telegram_notifier()
        self.is_running = False
        self._last_order_time: Optional[datetime] = None
        self._last_signal_type: Optional[SignalType] = None
        self._daily_trades: list = []
        
        logger.info(
            f"거래 실행 엔진 초기화: 종목={self.stock_code}, "
            f"수량={self.order_quantity}주"
        )
        self.risk_manager.print_status()
    
    def fetch_market_data(self, days: int = 100) -> pd.DataFrame:
        try:
            df = self.api.get_daily_ohlcv(stock_code=self.stock_code, period_type="D")
            if df.empty:
                logger.warning(f"시장 데이터 없음: {self.stock_code}")
                return pd.DataFrame()
            logger.debug(f"시장 데이터 조회 완료: {len(df)}개")
            return df
        except KISApiError as e:
            logger.error(f"시장 데이터 조회 실패: {e}")
            return pd.DataFrame()
    
    def fetch_current_price(self) -> float:
        try:
            price_data = self.api.get_current_price(self.stock_code)
            current_price = price_data.get("current_price", 0)
            logger.debug(f"현재가 조회: {self.stock_code} = {current_price:,.0f}원")
            return current_price
        except KISApiError as e:
            logger.error(f"현재가 조회 실패: {e}")
            return 0.0
    
    def _can_execute_order(self, signal: Signal) -> bool:
        if signal.signal_type == SignalType.HOLD:
            return False
        if self._last_signal_type == signal.signal_type:
            if self._last_order_time:
                elapsed = (get_kst_now() - self._last_order_time).total_seconds()
                if elapsed < 60:
                    logger.debug("중복 주문 방지: 1분 내 동일 시그널 무시")
                    return False
        return True
    
    def execute_buy_order(self, signal: Signal) -> Dict:
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            logger.warning(risk_check.reason)
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self._can_execute_order(signal):
            return {"success": False, "message": "주문 조건 미충족"}
        
        if self.strategy.has_position():
            logger.warning("매수 주문 취소: 포지션 이미 보유 중")
            return {"success": False, "message": "포지션 보유 중"}
        
        try:
            result = self.api.place_buy_order(
                stock_code=self.stock_code, quantity=self.order_quantity, price=0, order_type="01"
            )
            if result["success"]:
                now_kst = get_kst_now()
                self.strategy.open_position(
                    stock_code=self.stock_code, entry_price=signal.price, quantity=self.order_quantity,
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit, 
                    entry_date=now_kst.date(), atr=signal.atr
                )
                self._last_order_time = now_kst
                self._last_signal_type = SignalType.BUY
                self._daily_trades.append({
                    "time": now_kst.strftime("%Y-%m-%d %H:%M:%S"), "type": "BUY", "price": signal.price,
                    "quantity": self.order_quantity, "order_no": result["order_no"]
                })
                logger.info(f"매수 주문 성공: {result['order_no']}")
                self.telegram.notify_buy_order(
                    stock_code=self.stock_code, price=signal.price, quantity=self.order_quantity,
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit
                )
            else:
                logger.error(f"매수 주문 실패: {result['message']}")
            return result
        except KISApiError as e:
            trade_logger.log_error("매수 주문", str(e))
            self.telegram.notify_error("매수 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def execute_sell_order(self, signal: Signal) -> Dict:
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=True)
        if not risk_check.passed:
            logger.warning(risk_check.reason)
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self._can_execute_order(signal):
            return {"success": False, "message": "주문 조건 미충족"}
        
        if not self.strategy.has_position():
            logger.warning("매도 주문 취소: 보유 포지션 없음")
            return {"success": False, "message": "포지션 없음"}
        
        try:
            position = self.strategy.position
            result = self.api.place_sell_order(
                stock_code=self.stock_code, quantity=position.quantity, price=0, order_type="01"
            )
            if result["success"]:
                now_kst = get_kst_now()
                close_result = self.strategy.close_position(exit_price=signal.price, reason=signal.reason)
                if close_result:
                    self.risk_manager.record_trade_pnl(close_result["pnl"])
                self._last_order_time = now_kst
                self._last_signal_type = SignalType.SELL
                pnl = close_result.get("pnl", 0) if close_result else 0
                pnl_pct = close_result.get("pnl_pct", 0) if close_result else 0
                self._daily_trades.append({
                    "time": now_kst.strftime("%Y-%m-%d %H:%M:%S"), "type": "SELL", "price": signal.price,
                    "quantity": position.quantity, "order_no": result["order_no"], "pnl": pnl, "pnl_pct": pnl_pct
                })
                logger.info(f"매도 주문 성공: {result['order_no']}")
                if close_result:
                    if "손절" in signal.reason or pnl < 0:
                        self.telegram.notify_stop_loss(
                            stock_code=self.stock_code, entry_price=position.entry_price, exit_price=signal.price,
                            pnl=pnl, pnl_pct=pnl_pct
                        )
                    elif "익절" in signal.reason or pnl > 0:
                        self.telegram.notify_take_profit(
                            stock_code=self.stock_code, entry_price=position.entry_price, exit_price=signal.price,
                            pnl=pnl, pnl_pct=pnl_pct
                        )
                    else:
                        self.telegram.notify_sell_order(
                            stock_code=self.stock_code, price=signal.price, quantity=position.quantity,
                            reason=signal.reason, pnl=pnl, pnl_pct=pnl_pct
                        )
            else:
                logger.error(f"매도 주문 실패: {result['message']}")
            return result
        except KISApiError as e:
            trade_logger.log_error("매도 주문", str(e))
            self.telegram.notify_error("매도 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def run_once(self) -> Dict:
        logger.info("=" * 50)
        logger.info(f"전략 실행 시작 ({get_kst_now().strftime('%H:%M:%S %Z')})")
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
        result = {
            "timestamp": get_kst_now().strftime("%Y-%m-%d %H:%M:%S %Z"), "stock_code": self.stock_code,
            "signal": None, "order_result": None, "position": None, "error": None
        }
        try:
            df = self.fetch_market_data()
            if df.empty:
                result["error"] = "시장 데이터 조회 실패"
                logger.error(result["error"])
                return result
            current_price = self.fetch_current_price()
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                logger.error(result["error"])
                return result
            signal = self.strategy.generate_signal(df=df, current_price=current_price, stock_code=self.stock_code)
            result["signal"] = {
                "type": signal.signal_type.value, "price": signal.price, "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit, "reason": signal.reason, "atr": signal.atr, "trend": signal.trend.value
            }
            logger.info(
                f"시그널: {signal.signal_type.value} | 가격: {current_price:,.0f}원 | "
                f"추세: {signal.trend.value} | 사유: {signal.reason}"
            )
            if signal.signal_type == SignalType.BUY:
                order_result = self.execute_buy_order(signal)
                result["order_result"] = order_result
            elif signal.signal_type == SignalType.SELL:
                order_result = self.execute_sell_order(signal)
                result["order_result"] = order_result
            if self.strategy.has_position():
                pos = self.strategy.position
                pnl, pnl_pct = self.strategy.get_position_pnl(current_price)
                result["position"] = {
                    "stock_code": pos.stock_code, "entry_price": pos.entry_price, "quantity": pos.quantity,
                    "stop_loss": pos.stop_loss, "take_profit": pos.take_profit, "current_price": current_price,
                    "pnl": pnl, "pnl_pct": pnl_pct
                }
                logger.info(
                    f"포지션: {pos.stock_code} | 진입가: {pos.entry_price:,.0f}원 | "
                    f"현재가: {current_price:,.0f}원 | 손익: {pnl:,.0f}원 ({pnl_pct:+.2f}%)"
                )
            else:
                logger.info("포지션: 없음")
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"전략 실행 오류: {e}")
            self.telegram.notify_error("전략 실행 오류", str(e))
        logger.info("전략 실행 완료")
        logger.info("=" * 50)
        return result
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None) -> None:
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
            return
        if interval_seconds < 60:
            logger.warning("실행 간격이 60초 미만입니다. 60초로 조정합니다.")
            interval_seconds = 60
        self.is_running = True
        iteration = 0
        logger.info(f"거래 실행 시작 (간격: {interval_seconds}초)")
        self.telegram.notify_system_start(
            stock_code=self.stock_code, order_quantity=self.order_quantity, interval=interval_seconds,
            mode="모의투자" if settings.IS_PAPER_TRADING else "실계좌"
        )
        try:
            while self.is_running:
                iteration += 1
                logger.info(f"[반복 #{iteration}]")
                self.run_once()
                if max_iterations and iteration >= max_iterations:
                    logger.info(f"최대 반복 횟수 도달: {max_iterations}")
                    break
                logger.info(f"다음 실행까지 {interval_seconds}초 대기...")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("사용자에 의해 중단됨")
            stop_reason = "사용자 중단"
        except Exception as e:
            logger.error(f"예기치 않은 오류: {e}")
            stop_reason = f"오류 발생: {str(e)}"
            self.telegram.notify_error("시스템 오류", str(e))
        else:
            stop_reason = "정상 종료"
        finally:
            self.is_running = False
            logger.info("거래 실행 종료")
            summary = self.get_daily_summary()
            self.telegram.notify_system_stop(
                reason=stop_reason, total_trades=summary["total_trades"], daily_pnl=summary["total_pnl"]
            )
    
    def stop(self) -> None:
        logger.info("거래 실행 중지 요청")
        self.is_running = False
    
    def get_daily_summary(self) -> Dict:
        if not self._daily_trades:
            return {"total_trades": 0, "buy_count": 0, "sell_count": 0, "total_pnl": 0, "trades": []}
        buy_count = sum(1 for t in self._daily_trades if t["type"] == "BUY")
        sell_count = sum(1 for t in self._daily_trades if t["type"] == "SELL")
        total_pnl = sum(t.get("pnl", 0) for t in self._daily_trades)
        return {
            "total_trades": len(self._daily_trades), "buy_count": buy_count, "sell_count": sell_count,
            "total_pnl": total_pnl, "trades": self._daily_trades
        }
    
    def reset_daily_trades(self) -> None:
        self._daily_trades = []
        logger.info("일별 거래 기록 초기화")
