#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System v3.0 - 완전 자동 무인 운용 버전
═══════════════════════════════════════════════════════════════════════════════

한국투자증권 Open API를 사용한 Trend + ATR 기반 자동매매 시스템입니다.

★★★ v3.0 주요 기능 ★★★

    1. 포지션 영속 저장 및 자동 복구
    2. 익절/손절/추세이탈 자동 청산
    3. 전체 트레이딩 성과 측정 (MDD, Profit Factor 등)
    4. 종목 선정 로직 (YAML 기반)
    5. CBT/DRY_RUN/REAL 모드 지원
    6. Kill Switch (API 에러, 수동 플래그)
    7. 장 운영 스케줄러 (자동 대기/실행)
    8. 감사 추적 로깅

★ 실행 모드:
    - REAL: 실계좌 거래 (2단계 안전장치)
    - CBT: 가상 체결 (종이매매)
    - DRY_RUN: 시그널만 확인 (주문 없음)

★ 실행 방법:
    # 모의투자 (DEV)
    python main_v3.py --mode trade
    
    # 실계좌 (PROD)
    export TRADING_MODE=PROD
    python main_v3.py --mode trade
    
    # 스케줄러 모드 (장 시간에만 자동 실행)
    python main_v3.py --mode scheduler

═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import sys
import signal
from datetime import datetime
from typing import Optional

# ═══════════════════════════════════════════════════════════════════════════════
# 환경 및 설정 로딩
# ═══════════════════════════════════════════════════════════════════════════════

from env import (
    get_environment, 
    is_dev, 
    is_prod, 
    Environment,
    validate_environment
)

from config_loader import (
    get_config, 
    print_config_summary,
    Config
)

# ═══════════════════════════════════════════════════════════════════════════════
# 핵심 모듈
# ═══════════════════════════════════════════════════════════════════════════════

from trader import (
    Trader, 
    get_trader,
    OrderNotAllowedError,
    OrderConfirmationError
)

from strategy.trend_atr_v2 import (
    TrendATRStrategy,
    StrategyParams,
    Signal,
    SignalType,
    TrendType
)

# ═══════════════════════════════════════════════════════════════════════════════
# 신규 모듈 (v3.0)
# ═══════════════════════════════════════════════════════════════════════════════

from engine.position_manager import (
    PositionManager,
    ManagedPosition,
    ExitReason,
    get_position_manager
)

from engine.risk_manager import (
    RiskManager,
    RiskCheckResult,
    create_risk_manager_from_settings,
    safe_exit_with_message
)

from engine.market_scheduler import (
    MarketScheduler,
    MarketPhase,
    SchedulerState,
    get_market_scheduler
)

from universe import (
    UniverseManager,
    UniverseConfig,
    SelectionMethod,
    get_universe_manager
)

from report.trade_reporter import (
    TradeReporter,
    TradeRecord,
    get_trade_reporter
)

from utils.audit_logger import (
    AuditLogger,
    AuditEventType,
    get_audit_logger
)

from utils.market_hours import (
    is_market_open,
    get_market_status,
    should_skip_trading
)

from utils.telegram_notifier import get_telegram_notifier


# ═══════════════════════════════════════════════════════════════════════════════
# 상수
# ═══════════════════════════════════════════════════════════════════════════════

VERSION = "3.0.0"

BANNER = """
╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║     ██╗  ██╗██╗███████╗    ████████╗██████╗ ███████╗███╗   ██╗██████╗        ║
║     ██║ ██╔╝██║██╔════╝    ╚══██╔══╝██╔══██╗██╔════╝████╗  ██║██╔══██╗       ║
║     █████╔╝ ██║███████╗       ██║   ██████╔╝█████╗  ██╔██╗ ██║██║  ██║       ║
║     ██╔═██╗ ██║╚════██║       ██║   ██╔══██╗██╔══╝  ██║╚██╗██║██║  ██║       ║
║     ██║  ██╗██║███████║       ██║   ██║  ██║███████╗██║ ╚████║██████╔╝       ║
║     ╚═╝  ╚═╝╚═╝╚══════╝       ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═════╝        ║
║                                                                               ║
║                    ATR-Based Trend Following Trading System                   ║
║                                                                               ║
║                        v3.0 - 완전 자동 무인 운용 버전                         ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 통합 트레이딩 엔진
# ═══════════════════════════════════════════════════════════════════════════════

class TradingEngineV3:
    """
    통합 트레이딩 엔진 v3.0
    
    모든 신규 모듈을 통합하여 완전 자동화된 무인 운용을 지원합니다.
    """
    
    def __init__(self):
        """엔진 초기화"""
        # 설정 로드
        self.config: Config = get_config()
        
        # 감사 로거 (가장 먼저 초기화)
        self.audit = get_audit_logger()
        
        # 트레이더 (안전장치 포함)
        self.trader: Trader = get_trader()
        
        # 리스크 매니저
        self.risk_manager = create_risk_manager_from_settings()
        
        # 포지션 매니저
        self.position_manager = get_position_manager(
            enable_trailing=getattr(self.config.strategy, 'enable_trailing_stop', True),
            trailing_atr_multiplier=getattr(self.config.strategy, 'trailing_stop_atr_multiplier', 2.0),
            enable_gap_protection=getattr(self.config.risk, 'enable_gap_protection', False),
            max_gap_loss_pct=getattr(self.config.risk, 'max_gap_loss_pct', 3.0)
        )
        
        # 유니버스 매니저
        self.universe = get_universe_manager(
            yaml_path="config/universe.yaml"
        )
        
        # 성과 리포터
        self.reporter = get_trade_reporter(
            initial_capital=self.config.backtest.initial_capital
        )
        
        # 전략 (환경 독립)
        strategy_params = StrategyParams(
            atr_period=self.config.strategy.atr_period,
            trend_ma_period=self.config.strategy.trend_ma_period,
            atr_multiplier_sl=self.config.strategy.atr_multiplier_sl,
            atr_multiplier_tp=self.config.strategy.atr_multiplier_tp,
            max_loss_pct=self.config.risk.max_loss_pct,
            atr_spike_threshold=self.config.risk.atr_spike_threshold,
            adx_threshold=self.config.risk.adx_threshold,
            adx_period=self.config.risk.adx_period
        )
        self.strategy: TrendATRStrategy = TrendATRStrategy(strategy_params)
        
        # 텔레그램
        self.telegram = get_telegram_notifier()
        
        # 실행 상태
        self.is_running = False
        self._shutdown_requested = False
        
        # 시그널 핸들러 등록
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # 시작 로깅
        self.audit.log_event(
            event_type=AuditEventType.CONFIG_LOADED,
            message="Trading Engine v3.0 initialized",
            details={
                "version": VERSION,
                "environment": get_environment().value,
                "universe_count": self.universe.count()
            }
        )
        
        print(f"\n✅ Trading Engine v3.0 초기화 완료")
    
    def _signal_handler(self, signum, frame):
        """시그널 핸들러"""
        print(f"\n🛑 종료 시그널 수신 ({signum})")
        self._shutdown_requested = True
        self.stop()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 초기화 및 복구
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize(self) -> bool:
        """
        시스템 초기화 (장 시작 전 실행)
        
        Returns:
            bool: 초기화 성공 여부
        """
        print("\n" + "=" * 60)
        print("           시스템 초기화 중...")
        print("=" * 60)
        
        try:
            # 1. 환경 검증
            if not validate_environment():
                print("❌ 환경 검증 실패")
                return False
            print("✅ 환경 검증 완료")
            
            # 2. API 연결 테스트
            try:
                balance = self.trader.get_account_balance()
                print(f"✅ API 연결 확인 (잔고: {balance.get('cash_balance', 0):,.0f}원)")
            except Exception as e:
                print(f"❌ API 연결 실패: {e}")
                self.risk_manager.record_api_error(str(e))
                return False
            
            # 3. 포지션 복구 및 정합성 검증
            print("\n📊 포지션 정합성 검증 중...")
            restored, mismatched = self.position_manager.restore_from_api(
                api_client=self.trader,
                auto_sync=True
            )
            
            if restored:
                print(f"✅ 포지션 복구 완료: {len(restored)}개")
                for code in restored:
                    pos = self.position_manager.get_position(code)
                    if pos:
                        print(f"   - {code}: {pos.entry_price:,.0f}원 x {pos.quantity}주")
                        
                        # 전략에도 포지션 동기화
                        self.strategy.open_position(
                            stock_code=code,
                            entry_price=pos.entry_price,
                            quantity=pos.quantity,
                            stop_loss=pos.stop_loss,
                            take_profit=pos.take_profit,
                            entry_date=pos.entry_date,
                            atr=pos.atr_at_entry
                        )
            
            if mismatched:
                print(f"⚠️ 불일치 종목: {mismatched}")
            
            # 4. 유니버스 확인
            stocks = self.universe.get_stock_codes()
            print(f"\n📋 거래 대상 종목: {stocks}")
            
            # 5. 리스크 매니저 상태
            self.risk_manager.print_status()
            
            print("\n" + "=" * 60)
            print("           ✅ 시스템 초기화 완료")
            print("=" * 60 + "\n")
            
            self.audit.log_event(
                event_type=AuditEventType.SYSTEM_START,
                message="System initialization completed",
                details={
                    "restored_positions": len(restored),
                    "universe": stocks
                }
            )
            
            return True
            
        except Exception as e:
            print(f"\n❌ 초기화 중 오류: {e}")
            self.audit.log_error("INIT_ERROR", str(e), exception=e)
            return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 메인 트레이딩 루프
    # ═══════════════════════════════════════════════════════════════════════════
    
    def run_once(self, stock_code: str = None) -> dict:
        """
        전략을 1회 실행합니다.
        
        Args:
            stock_code: 종목 코드 (None이면 유니버스 전체)
            
        Returns:
            dict: 실행 결과
        """
        result = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks_processed": 0,
            "signals": [],
            "orders": [],
            "errors": []
        }
        
        # 리스크 체크
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            result["errors"].append(risk_check.reason)
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return result
        
        # 장 시간 체크
        skip, skip_reason = should_skip_trading()
        if skip:
            result["errors"].append(skip_reason)
            return result
        
        # 처리할 종목
        if stock_code:
            stocks = [stock_code]
        else:
            stocks = self.universe.get_stock_codes()
        
        for code in stocks:
            try:
                stock_result = self._process_stock(code)
                result["stocks_processed"] += 1
                
                if stock_result.get("signal"):
                    result["signals"].append(stock_result["signal"])
                if stock_result.get("order"):
                    result["orders"].append(stock_result["order"])
                if stock_result.get("error"):
                    result["errors"].append(stock_result["error"])
                    
            except Exception as e:
                result["errors"].append(f"{code}: {str(e)}")
                self.audit.log_error("TRADE_ERROR", str(e), stock_code=code, exception=e)
                self.risk_manager.record_api_error(str(e))
        
        return result
    
    def _process_stock(self, stock_code: str) -> dict:
        """개별 종목을 처리합니다."""
        result = {
            "stock_code": stock_code,
            "signal": None,
            "order": None,
            "error": None
        }
        
        try:
            # 1. 시장 데이터 조회
            df = self.trader.get_daily_ohlcv(stock_code)
            
            if df.empty:
                result["error"] = "시장 데이터 조회 실패"
                return result
            
            # 2. 현재가 조회
            price_data = self.trader.get_current_price(stock_code)
            current_price = price_data.get("current_price", 0)
            
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                return result
            
            # 3. 포지션 업데이트 (보유 중인 경우)
            if self.position_manager.has_position(stock_code):
                self.position_manager.update_position(stock_code, current_price)
                self.reporter.update_unrealized_pnl(stock_code, current_price)
            
            # 4. 전략 시그널 생성
            signal = self.strategy.generate_signal(df, current_price)
            
            result["signal"] = {
                "type": signal.signal_type.value,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reason": signal.reason,
                "trend": signal.trend.value
            }
            
            # 감사 로깅
            self.audit.log_signal(
                stock_code=stock_code,
                signal_type=signal.signal_type.value,
                reason=signal.reason,
                price=current_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                atr=signal.atr,
                trend=signal.trend.value
            )
            
            # 5. 시그널에 따른 주문 실행
            if signal.signal_type == SignalType.BUY:
                order_result = self._execute_buy(stock_code, signal, current_price)
                result["order"] = order_result
                
            elif signal.signal_type == SignalType.SELL:
                order_result = self._execute_sell(stock_code, signal, current_price)
                result["order"] = order_result
            
            return result
            
        except Exception as e:
            result["error"] = str(e)
            raise
    
    def _execute_buy(self, stock_code: str, signal: Signal, current_price: float) -> dict:
        """매수 주문을 실행합니다."""
        # 이미 포지션 보유 중인 경우
        if self.position_manager.has_position(stock_code):
            return {"success": False, "message": "포지션 이미 보유 중"}
        
        # 리스크 체크
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            return {"success": False, "message": risk_check.reason}
        
        quantity = self.config.order.default_quantity
        
        # 감사 로깅
        self.audit.log_order_requested(
            stock_code=stock_code,
            order_type="BUY",
            price=current_price,
            quantity=quantity
        )
        
        try:
            # 주문 실행
            order_result = self.trader.buy(
                stock_code=stock_code,
                quantity=quantity,
                price=0,  # 시장가
                order_type="01"
            )
            
            if order_result.success:
                # 포지션 매니저에 기록
                position = self.position_manager.open_position(
                    stock_code=stock_code,
                    entry_price=current_price,
                    quantity=quantity,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    atr=signal.atr,
                    order_no=order_result.order_no
                )
                
                # 전략에도 포지션 기록
                self.strategy.open_position(
                    stock_code=stock_code,
                    entry_price=current_price,
                    quantity=quantity,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    entry_date=datetime.now().strftime("%Y-%m-%d"),
                    atr=signal.atr
                )
                
                # 성과 리포터에 기록
                self.reporter.record_entry(
                    trade_id=position.position_id,
                    stock_code=stock_code,
                    entry_price=current_price,
                    quantity=quantity
                )
                
                # 감사 로깅
                self.audit.log_order_filled(
                    stock_code=stock_code,
                    order_no=order_result.order_no,
                    fill_price=current_price,
                    fill_quantity=quantity
                )
                
                print(f"✅ 매수 체결: {stock_code} @ {current_price:,.0f}원 x {quantity}주")
                
            else:
                self.audit.log_order_rejected(
                    stock_code=stock_code,
                    reason=order_result.message
                )
                print(f"❌ 매수 실패: {order_result.message}")
            
            return {
                "success": order_result.success,
                "order_no": order_result.order_no,
                "message": order_result.message
            }
            
        except (OrderNotAllowedError, OrderConfirmationError) as e:
            return {"success": False, "message": "안전장치에 의해 차단됨"}
    
    def _execute_sell(self, stock_code: str, signal: Signal, current_price: float) -> dict:
        """매도 주문을 실행합니다."""
        # 포지션 미보유
        if not self.position_manager.has_position(stock_code):
            return {"success": False, "message": "보유 포지션 없음"}
        
        position = self.position_manager.get_position(stock_code)
        
        # Exit 사유 결정
        exit_reason = self._determine_exit_reason(signal.reason)
        
        # 감사 로깅
        self.audit.log_order_requested(
            stock_code=stock_code,
            order_type="SELL",
            price=current_price,
            quantity=position.quantity
        )
        
        try:
            # 주문 실행
            order_result = self.trader.sell(
                stock_code=stock_code,
                quantity=position.quantity,
                price=0,
                order_type="01"
            )
            
            if order_result.success:
                # 포지션 매니저에서 청산
                closed_position = self.position_manager.close_position(
                    stock_code=stock_code,
                    exit_price=current_price,
                    reason=exit_reason,
                    order_no=order_result.order_no
                )
                
                # 전략 포지션 청산
                close_result = self.strategy.close_position(
                    exit_price=current_price,
                    reason=signal.reason
                )
                
                # 리스크 매니저에 손익 기록
                if closed_position:
                    self.risk_manager.record_trade_pnl(closed_position.realized_pnl)
                
                # 성과 리포터에 기록
                if closed_position:
                    self.reporter.record_exit(
                        trade_id=closed_position.position_id,
                        exit_price=current_price,
                        exit_reason=exit_reason.value
                    )
                
                # 감사 로깅
                pnl = closed_position.realized_pnl if closed_position else 0
                self.audit.log_order_filled(
                    stock_code=stock_code,
                    order_no=order_result.order_no,
                    fill_price=current_price,
                    fill_quantity=position.quantity,
                    pnl=pnl
                )
                
                print(
                    f"✅ 매도 체결: {stock_code} @ {current_price:,.0f}원, "
                    f"손익: {pnl:+,.0f}원"
                )
                
            else:
                self.audit.log_order_rejected(
                    stock_code=stock_code,
                    reason=order_result.message
                )
                print(f"❌ 매도 실패: {order_result.message}")
            
            return {
                "success": order_result.success,
                "order_no": order_result.order_no,
                "message": order_result.message
            }
            
        except (OrderNotAllowedError, OrderConfirmationError) as e:
            return {"success": False, "message": "안전장치에 의해 차단됨"}
    
    def _determine_exit_reason(self, reason_str: str) -> ExitReason:
        """시그널 사유를 Exit 사유로 변환합니다."""
        reason_upper = reason_str.upper() if reason_str else ""
        
        if "손절" in reason_str or "STOP" in reason_upper:
            return ExitReason.ATR_STOP
        elif "익절" in reason_str or "PROFIT" in reason_upper:
            return ExitReason.TAKE_PROFIT
        elif "추세" in reason_str or "TREND" in reason_upper:
            return ExitReason.TREND_BROKEN
        elif "트레일링" in reason_str or "TRAILING" in reason_upper:
            return ExitReason.TRAILING_STOP
        else:
            return ExitReason.OTHER
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 실행 제어
    # ═══════════════════════════════════════════════════════════════════════════
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None):
        """
        지속적으로 전략을 실행합니다.
        
        Args:
            interval_seconds: 실행 간격 (초)
            max_iterations: 최대 반복 횟수
        """
        if interval_seconds < 60:
            print("⚠️ 실행 간격이 60초 미만입니다. 60초로 조정합니다.")
            interval_seconds = 60
        
        # 초기화
        if not self.initialize():
            print("❌ 시스템 초기화 실패")
            return
        
        self.is_running = True
        iteration = 0
        
        print(f"\n🚀 트레이딩 시작 (간격: {interval_seconds}초)")
        print("   종료하려면 Ctrl+C를 누르세요.\n")
        
        # 텔레그램 시작 알림
        self.telegram.notify_system_start(
            stock_code=str(self.universe.get_stock_codes()),
            order_quantity=self.config.order.default_quantity,
            interval=interval_seconds,
            mode="실계좌" if is_prod() else "모의투자"
        )
        
        try:
            while self.is_running and not self._shutdown_requested:
                iteration += 1
                print(f"\n{'═' * 60}")
                print(f"  반복 #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'═' * 60}")
                
                # 장 시간 체크
                skip, skip_reason = should_skip_trading()
                if skip:
                    print(f"⏳ {skip_reason}")
                else:
                    # 전략 실행
                    result = self.run_once()
                    
                    # 결과 출력
                    print(f"\n📊 처리 종목: {result['stocks_processed']}개")
                    if result['signals']:
                        for sig in result['signals']:
                            print(f"   - {sig['type']}: {sig.get('reason', '')}")
                    if result['errors']:
                        for err in result['errors']:
                            print(f"   ❌ {err}")
                
                # 최대 반복 횟수 체크
                if max_iterations and iteration >= max_iterations:
                    print(f"\n최대 반복 횟수 도달: {max_iterations}")
                    break
                
                # 대기
                print(f"\n⏳ 다음 실행까지 {interval_seconds}초 대기...")
                
                # 인터럽트 대응을 위해 짧게 나눠서 대기
                for _ in range(interval_seconds):
                    if self._shutdown_requested:
                        break
                    import time
                    time.sleep(1)
                
        except Exception as e:
            print(f"\n❌ 오류 발생: {e}")
            self.audit.log_error("MAIN_LOOP_ERROR", str(e), exception=e)
            self.telegram.notify_error("시스템 오류", str(e))
        finally:
            self.stop()
    
    def stop(self):
        """시스템을 종료합니다."""
        if not self.is_running:
            return
        
        print("\n🛑 시스템 종료 중...")
        self.is_running = False
        
        # 성과 리포트
        perf = self.reporter.get_account_performance()
        
        # 텔레그램 종료 알림
        self.telegram.notify_system_stop(
            reason="정상 종료" if not self._shutdown_requested else "사용자 중단",
            total_trades=perf.total_trades,
            daily_pnl=perf.realized_pnl
        )
        
        # 감사 로깅
        self.audit.log_system_stop(
            reason="shutdown",
            details={
                "total_trades": perf.total_trades,
                "realized_pnl": perf.realized_pnl,
                "total_return_pct": perf.total_return_pct
            }
        )
        self.audit.close()
        
        # 성과 리포트 출력
        self.reporter.print_report()
        
        print("✅ 시스템 종료 완료")


# ═══════════════════════════════════════════════════════════════════════════════
# 스케줄러 모드
# ═══════════════════════════════════════════════════════════════════════════════

def run_scheduler_mode(interval: int = 60, max_runs: int = None):
    """
    스케줄러 모드로 실행합니다.
    
    장 시간에만 자동으로 트레이딩을 실행합니다.
    """
    print("\n" + "=" * 60)
    print("              장 스케줄러 모드")
    print("=" * 60)
    
    scheduler = get_market_scheduler(
        auto_wait_for_market=True,
        pre_market_minutes=10,
        post_market_minutes=10
    )
    
    engine = TradingEngineV3()
    
    # 콜백 등록
    scheduler.on_pre_market(engine.initialize, name="system_init")
    scheduler.on_market_open(lambda: engine.run_once(), interval=interval)
    scheduler.on_market_close(lambda: engine.reporter.print_report(), name="daily_report")
    
    scheduler.print_status()
    
    print("\n🚀 스케줄러 시작 (Ctrl+C로 종료)")
    scheduler.start(blocking=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 메인 함수
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(
        description="KIS Trend-ATR Trading System v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
═══════════════════════════════════════════════════════════════════════════════
실행 예시:
═══════════════════════════════════════════════════════════════════════════════

★ 모의투자 트레이딩:
    python main_v3.py --mode trade
    
★ 스케줄러 모드 (장 시간에만 자동 실행):
    python main_v3.py --mode scheduler
    
★ 실계좌 트레이딩:
    export TRADING_MODE=PROD
    python main_v3.py --mode trade

═══════════════════════════════════════════════════════════════════════════════
        """
    )
    
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["trade", "scheduler", "status"],
        help="실행 모드"
    )
    
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="실행 간격 (초, 기본: 60)"
    )
    
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="최대 실행 횟수 (기본: 무제한)"
    )
    
    args = parser.parse_args()
    
    # 배너 출력
    print(BANNER)
    print(f"버전: {VERSION}")
    print(f"시작 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 환경 정보
    env = get_environment()
    print(f"환경: {env.value}")
    
    # 설정 요약
    print_config_summary()
    
    # 모드별 실행
    if args.mode == "trade":
        engine = TradingEngineV3()
        engine.run(
            interval_seconds=max(60, args.interval),
            max_iterations=args.max_runs
        )
        
    elif args.mode == "scheduler":
        run_scheduler_mode(
            interval=max(60, args.interval),
            max_runs=args.max_runs
        )
        
    elif args.mode == "status":
        # 현재 상태 출력
        engine = TradingEngineV3()
        engine.initialize()
        
        print("\n📊 현재 포지션:")
        engine.position_manager.print_positions()
        
        print("\n📈 성과 리포트:")
        engine.reporter.print_report()
        
        print("\n⚠️ 리스크 상태:")
        engine.risk_manager.print_status()


if __name__ == "__main__":
    main()
