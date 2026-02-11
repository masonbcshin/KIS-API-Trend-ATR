"""
KIS Trend-ATR Trading System - CBT 거래 실행 엔진

이 모듈은 CBT 모드에서 가상 체결을 처리합니다.
LIVE/PAPER 모드의 TradingExecutor와 동일한 인터페이스를 제공하면서,
실제 주문 대신 가상 체결을 수행합니다.

핵심 차이점:
    - 실제 API 주문을 전송하지 않음
    - KIS 시세 API로 현재가만 조회
    - 가상 계좌에서 체결 처리
    - 모든 거래를 Trade Log에 저장

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
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
from utils.market_hours import KST
from engine.risk_manager import (
    RiskManager,
    create_risk_manager_from_settings,
    safe_exit_with_message
)

from .virtual_account import VirtualAccount
from .trade_store import TradeStore, Trade
from .metrics import CBTMetrics, PerformanceReport

logger = get_logger("cbt_executor")
trade_logger = TradeLogger("cbt_executor")


class CBTExecutorError(Exception):
    """CBT 실행 엔진 에러 클래스"""
    pass


class CBTExecutor:
    """
    CBT 거래 실행 엔진
    
    LIVE/PAPER 모드의 TradingExecutor와 동일한 인터페이스로,
    가상 체결을 통해 전략 성과를 측정합니다.
    
    ⚠️ 실제 주문은 절대 발생하지 않습니다.
    
    Attributes:
        api: KIS API 클라이언트 (시세 조회 전용)
        strategy: Trend-ATR 전략
        account: 가상 계좌
        trade_store: 거래 기록 저장소
        metrics: 성과 지표 계산기
    
    Usage:
        executor = CBTExecutor(stock_code="005930")
        
        # 단일 실행
        result = executor.run_once()
        
        # 연속 실행
        executor.run(interval_seconds=60, max_iterations=100)
        
        # 성과 리포트
        report = executor.get_performance_report()
    """
    
    def __init__(
        self,
        api: KISApi = None,
        strategy: TrendATRStrategy = None,
        stock_code: str = None,
        order_quantity: int = None,
        risk_manager: RiskManager = None,
        telegram_notifier: TelegramNotifier = None,
        virtual_account: VirtualAccount = None,
        trade_store: TradeStore = None
    ):
        """
        CBT 실행 엔진 초기화
        
        Args:
            api: KIS API 클라이언트 (미입력 시 자동 생성)
            strategy: 전략 인스턴스 (미입력 시 자동 생성)
            stock_code: 거래 종목 코드
            order_quantity: 주문 수량
            risk_manager: 리스크 매니저
            telegram_notifier: 텔레그램 알림기
            virtual_account: 가상 계좌 (미입력 시 자동 생성)
            trade_store: 거래 저장소 (미입력 시 자동 생성)
        """
        # API는 시세 조회용으로만 사용
        self.api = api or KISApi(is_paper_trading=True)
        self.strategy = strategy or TrendATRStrategy()
        self.stock_code = stock_code or settings.DEFAULT_STOCK_CODE
        self.order_quantity = order_quantity or settings.ORDER_QUANTITY
        
        # 리스크 매니저
        self.risk_manager = risk_manager or create_risk_manager_from_settings()
        
        # 텔레그램 알림기
        self.telegram = telegram_notifier or get_telegram_notifier()
        
        # CBT 전용 컴포넌트
        self.account = virtual_account or VirtualAccount()
        self.trade_store = trade_store or TradeStore()
        self.metrics = CBTMetrics(self.account, self.trade_store)
        
        # 실행 상태
        self.is_running = False
        
        # 주문 실행 추적 (중복 방지)
        self._last_order_time: Optional[datetime] = None
        self._last_signal_type: Optional[SignalType] = None
        
        # 일별 거래 기록 (요약용)
        self._daily_trades: list = []
        
        logger.info(
            f"[CBT] 실행 엔진 초기화: 종목={self.stock_code}, "
            f"수량={self.order_quantity}주, "
            f"초기자본={self.account.initial_capital:,}원"
        )
        
        # 리스크 매니저 상태 출력
        self.risk_manager.print_status()
    
    # ════════════════════════════════════════════════════════════════
    # 데이터 조회
    # ════════════════════════════════════════════════════════════════
    
    def fetch_market_data(self, days: int = 100) -> pd.DataFrame:
        """
        시장 데이터 조회 (KIS API)
        
        Args:
            days: 조회할 일수
        
        Returns:
            pd.DataFrame: OHLCV 데이터
        """
        try:
            df = self.api.get_daily_ohlcv(
                stock_code=self.stock_code,
                period_type="D"
            )
            
            if df.empty:
                logger.warning(f"[CBT] 시장 데이터 없음: {self.stock_code}")
                return pd.DataFrame()
            
            logger.debug(f"[CBT] 시장 데이터 조회 완료: {len(df)}개")
            return df
            
        except KISApiError as e:
            logger.error(f"[CBT] 시장 데이터 조회 실패: {e}")
            return pd.DataFrame()
    
    def fetch_current_price(self) -> float:
        """
        현재가 조회 (KIS API)
        
        가상 체결가로 사용됩니다.
        
        Returns:
            float: 현재가 (조회 실패 시 0)
        """
        try:
            price_data = self.api.get_current_price(self.stock_code)
            current_price = price_data.get("current_price", 0)
            
            logger.debug(f"[CBT] 현재가 조회: {self.stock_code} = {current_price:,.0f}원")
            return current_price
            
        except KISApiError as e:
            logger.error(f"[CBT] 현재가 조회 실패: {e}")
            return 0.0
    
    # ════════════════════════════════════════════════════════════════
    # 가상 주문 실행
    # ════════════════════════════════════════════════════════════════
    
    def _can_execute_order(self, signal: Signal) -> bool:
        """주문 실행 가능 여부 확인"""
        if signal.signal_type == SignalType.HOLD:
            return False
        
        # 동일 시그널 연속 실행 방지
        if self._last_signal_type == signal.signal_type:
            if self._last_order_time:
                elapsed = (datetime.now(KST) - self._last_order_time).total_seconds()
                if elapsed < 60:
                    logger.debug("[CBT] 중복 주문 방지: 동일 시그널 무시")
                    return False
        
        return True
    
    def execute_virtual_buy(self, signal: Signal) -> Dict:
        """
        가상 매수 체결
        
        실제 주문은 발생하지 않습니다.
        
        Args:
            signal: 매수 시그널
        
        Returns:
            Dict: 체결 결과
        """
        # 리스크 체크
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            logger.warning(f"[CBT] {risk_check.reason}")
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self._can_execute_order(signal):
            return {"success": False, "message": "주문 조건 미충족"}
        
        # 이미 포지션 보유 중인 경우
        if self.account.has_position():
            logger.warning("[CBT] 매수 취소: 포지션 이미 보유 중")
            return {"success": False, "message": "포지션 보유 중"}
        
        # 가상 매수 실행
        result = self.account.execute_buy(
            stock_code=self.stock_code,
            price=signal.price,
            quantity=self.order_quantity,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            atr=signal.atr
        )
        
        if result["success"]:
            # 전략 포지션도 동기화
            self.strategy.open_position(
                stock_code=self.stock_code,
                entry_price=signal.price,
                quantity=self.order_quantity,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                entry_date=datetime.now(KST).strftime("%Y-%m-%d"),
                atr=signal.atr
            )
            
            # 주문 추적 업데이트
            self._last_order_time = datetime.now(KST)
            self._last_signal_type = SignalType.BUY
            
            # 일별 거래 기록
            self._daily_trades.append({
                "time": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                "type": "BUY",
                "price": signal.price,
                "quantity": self.order_quantity,
                "order_no": result["order_no"]
            })
            
            logger.info(f"[CBT] 가상 매수 성공: {result['order_no']}")
            
            # 텔레그램 알림 (CBT 표시)
            self._notify_cbt_buy(signal)
        
        return result
    
    def execute_virtual_sell(self, signal: Signal) -> Dict:
        """
        가상 매도 체결
        
        실제 주문은 발생하지 않습니다.
        
        Args:
            signal: 매도 시그널
        
        Returns:
            Dict: 체결 결과 (손익 정보 포함)
        """
        # 리스크 체크 (청산은 항상 허용)
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=True)
        if not risk_check.passed:
            logger.warning(f"[CBT] {risk_check.reason}")
        
        if not self._can_execute_order(signal):
            return {"success": False, "message": "주문 조건 미충족"}
        
        # 포지션 미보유 시
        if not self.account.has_position():
            logger.warning("[CBT] 매도 취소: 보유 포지션 없음")
            return {"success": False, "message": "포지션 없음"}
        
        # 청산 사유 결정
        exit_reason = self._determine_exit_reason(signal)
        
        # 가상 매도 실행
        result = self.account.execute_sell(
            price=signal.price,
            reason=exit_reason
        )
        
        if result["success"]:
            # 리스크 매니저에 손익 기록
            self.risk_manager.record_trade_pnl(result["net_pnl"])
            
            # Trade Log 저장
            trade = self.trade_store.add_trade_from_result(result)
            
            # 전략 포지션 청산
            self.strategy.close_position(
                exit_price=signal.price,
                reason=exit_reason
            )
            
            # 주문 추적 업데이트
            self._last_order_time = datetime.now(KST)
            self._last_signal_type = SignalType.SELL
            
            # 일별 거래 기록
            self._daily_trades.append({
                "time": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                "type": "SELL",
                "price": signal.price,
                "quantity": result["quantity"],
                "order_no": result["order_no"],
                "pnl": result["net_pnl"],
                "pnl_pct": result["return_pct"]
            })
            
            logger.info(
                f"[CBT] 가상 매도 성공: {result['order_no']}, "
                f"손익: {result['net_pnl']:+,.0f}원 ({result['return_pct']:+.2f}%)"
            )
            
            # 텔레그램 알림 (CBT 표시)
            self._notify_cbt_sell(result)
        
        return result
    
    def _determine_exit_reason(self, signal: Signal) -> str:
        """청산 사유 결정"""
        reason = signal.reason.upper() if signal.reason else ""
        
        if "손절" in reason or "STOP" in reason:
            return "ATR_STOP"
        elif "익절" in reason or "PROFIT" in reason or "TARGET" in reason:
            return "TAKE_PROFIT"
        elif "추세" in reason or "TREND" in reason:
            return "TREND_BROKEN"
        elif "트레일링" in reason or "TRAILING" in reason:
            return "TRAILING_STOP"
        else:
            return "OTHER"
    
    # ════════════════════════════════════════════════════════════════
    # 메인 실행 로직
    # ════════════════════════════════════════════════════════════════
    
    def run_once(self) -> Dict:
        """
        전략 1회 실행
        
        Returns:
            Dict: 실행 결과
        """
        logger.info("=" * 50)
        logger.info("[CBT] 전략 실행 시작")
        
        # 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
        
        result = {
            "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "stock_code": self.stock_code,
            "mode": "CBT",
            "signal": None,
            "order_result": None,
            "position": None,
            "account": None,
            "error": None
        }
        
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
            
            # 3. 포지션 미실현 손익 업데이트
            self.account.update_position_price(current_price)
            
            # 4. 전략 시그널 생성
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
                f"[CBT] 시그널: {signal.signal_type.value} | "
                f"가격: {current_price:,.0f}원 | "
                f"추세: {signal.trend.value} | "
                f"사유: {signal.reason}"
            )
            
            # 5. 시그널에 따른 가상 주문 실행
            if signal.signal_type == SignalType.BUY:
                order_result = self.execute_virtual_buy(signal)
                result["order_result"] = order_result
                
            elif signal.signal_type == SignalType.SELL:
                order_result = self.execute_virtual_sell(signal)
                result["order_result"] = order_result
            
            # 6. 현재 포지션 정보
            if self.account.has_position():
                pos_info = self.account.get_position_info()
                pos = self.account.position
                pnl, pnl_pct = self.strategy.get_position_pnl(current_price)
                
                result["position"] = {
                    **pos_info,
                    "current_price": current_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct
                }
                
                logger.info(
                    f"[CBT] 포지션: {pos.stock_code} | "
                    f"진입가: {pos.entry_price:,.0f}원 | "
                    f"현재가: {current_price:,.0f}원 | "
                    f"손익: {pnl:,.0f}원 ({pnl_pct:+.2f}%)"
                )
            else:
                logger.info("[CBT] 포지션: 없음")
            
            # 7. 계좌 요약
            result["account"] = self.account.get_account_summary(current_price)
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[CBT] 전략 실행 오류: {e}")
            self.telegram.notify_error("CBT 전략 실행 오류", str(e))
        
        logger.info("[CBT] 전략 실행 완료")
        logger.info("=" * 50)
        
        return result
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None) -> None:
        """
        전략 연속 실행
        
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
        
        # 최소 간격 60초
        if interval_seconds < 60:
            logger.warning("[CBT] 실행 간격이 60초 미만입니다. 60초로 조정합니다.")
            interval_seconds = 60
        
        self.is_running = True
        iteration = 0
        
        logger.info(f"[CBT] 거래 실행 시작 (간격: {interval_seconds}초)")
        
        # 텔레그램 시작 알림
        self.telegram.notify_system_start(
            stock_code=self.stock_code,
            order_quantity=self.order_quantity,
            interval=interval_seconds,
            mode="🧪 CBT (종이매매)"
        )
        
        try:
            while self.is_running:
                iteration += 1
                logger.info(f"[CBT] [반복 #{iteration}]")
                
                # 전략 실행
                self.run_once()
                
                # 최대 반복 횟수 확인
                if max_iterations and iteration >= max_iterations:
                    logger.info(f"[CBT] 최대 반복 횟수 도달: {max_iterations}")
                    break
                
                # 다음 실행까지 대기
                logger.info(f"[CBT] 다음 실행까지 {interval_seconds}초 대기...")
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("[CBT] 사용자에 의해 중단됨")
            stop_reason = "사용자 중단"
        except Exception as e:
            logger.error(f"[CBT] 예기치 않은 오류: {e}")
            stop_reason = f"오류 발생: {str(e)}"
            self.telegram.notify_error("CBT 시스템 오류", str(e))
        else:
            stop_reason = "정상 종료"
        finally:
            self.is_running = False
            logger.info("[CBT] 거래 실행 종료")
            
            # 성과 리포트 전송
            self._send_final_report(stop_reason)
    
    def stop(self) -> None:
        """거래 실행 중지"""
        logger.info("[CBT] 거래 실행 중지 요청")
        self.is_running = False
    
    # ════════════════════════════════════════════════════════════════
    # 성과 리포트
    # ════════════════════════════════════════════════════════════════
    
    def get_performance_report(self, current_price: float = None) -> PerformanceReport:
        """
        성과 리포트 생성
        
        Args:
            current_price: 현재가
        
        Returns:
            PerformanceReport: 성과 리포트
        """
        if current_price is None:
            current_price = self.fetch_current_price()
        
        return self.metrics.generate_report(current_price)
    
    def get_daily_summary(self) -> Dict:
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
    
    def reset_daily_trades(self) -> None:
        """일별 거래 기록 초기화"""
        self._daily_trades = []
        logger.info("[CBT] 일별 거래 기록 초기화")
    
    # ════════════════════════════════════════════════════════════════
    # 텔레그램 알림
    # ════════════════════════════════════════════════════════════════
    
    def _notify_cbt_buy(self, signal: Signal) -> None:
        """CBT 매수 알림"""
        self.telegram.notify_cbt_signal(
            signal_type="📈 가상 매수",
            stock_code=self.stock_code,
            price=signal.price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            atr=signal.atr,
            trend=signal.trend.value,
            reason=signal.reason
        )
    
    def _notify_cbt_sell(self, result: Dict) -> None:
        """CBT 매도 알림 (손익 포함)"""
        pnl = result.get("net_pnl", 0)
        pnl_pct = result.get("return_pct", 0)
        
        message = f"""
🧪 *[CBT] 가상 매도 체결*
━━━━━━━━━━━━━━━━━━
• 종목: `{result.get('stock_code', self.stock_code)}`
• 진입가: {result.get('entry_price', 0):,.0f}원
• 청산가: {result.get('exit_price', 0):,.0f}원
• 수량: {result.get('quantity', 0)}주
━━━━━━━━━━━━━━━━━━
• 순손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)
• 보유일수: {result.get('holding_days', 0)}일
• 청산사유: {result.get('exit_reason', 'OTHER')}
━━━━━━━━━━━━━━━━━━
🔒 CBT 모드: 실주문 없음
⏰ {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}
"""
        self.telegram.send_message(message)
    
    def _send_final_report(self, stop_reason: str) -> None:
        """최종 성과 리포트 전송"""
        try:
            current_price = self.fetch_current_price()
            report = self.get_performance_report(current_price)
            
            message = f"""
🧪 *CBT 세션 종료 리포트*
━━━━━━━━━━━━━━━━━━
📅 종료 시간: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}
📝 종료 사유: {stop_reason}

💰 최종 성과
━━━━━━━━━━━━━━━━━━
• 초기 자본금: {report.initial_capital:,.0f}원
• 최종 평가금: {report.final_equity:,.0f}원
• 총 수익률: {report.total_return_pct:+.2f}%
• 실현 손익: {report.realized_pnl:+,.0f}원

📊 거래 통계
━━━━━━━━━━━━━━━━━━
• 총 거래: {report.total_trades}회
• 승률: {report.win_rate:.1f}%
• Expectancy: {report.expectancy:+,.0f}원
• MDD: {report.max_drawdown_pct:.2f}%
• Profit Factor: {report.profit_factor:.2f}

━━━━━━━━━━━━━━━━━━
🔒 CBT 모드: 실주문 없음
"""
            self.telegram.send_message(message)
            
        except Exception as e:
            logger.error(f"[CBT] 최종 리포트 전송 실패: {e}")
    
    def send_periodic_report(self) -> None:
        """
        정기 성과 리포트 전송
        
        cron 등에서 호출하여 정기적으로 리포트를 전송합니다.
        """
        try:
            current_price = self.fetch_current_price()
            report = self.get_performance_report(current_price)
            
            summary = report.get_summary_text()
            
            # 텔레그램으로 요약 전송
            message = f"🧪 *CBT 정기 리포트*\n{summary}"
            
            # 메시지 길이 제한 (텔레그램 4096자)
            if len(message) > 4000:
                message = message[:4000] + "\n..."
            
            self.telegram.send_message(message)
            
            logger.info("[CBT] 정기 리포트 전송 완료")
            
        except Exception as e:
            logger.error(f"[CBT] 정기 리포트 전송 실패: {e}")
