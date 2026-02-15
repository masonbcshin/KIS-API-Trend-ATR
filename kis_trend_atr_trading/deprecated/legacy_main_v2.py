#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 메인 실행 파일
═══════════════════════════════════════════════════════════════════════════════

한국투자증권 Open API를 사용한 Trend + ATR 기반 자동매매 시스템입니다.

★★★ 실행 환경 분리 ★★★

    - 기본 환경: DEV (모의투자) - 설정 없이 바로 사용 가능
    - 실계좌: PROD - 명시적 설정 + 2단계 안전장치 필요

★ 모의투자 실행 방법:
    # 기본적으로 DEV(모의투자) 환경
    python main_v2.py --mode trade
    python main_v2.py --mode backtest

★ 실계좌 실행 방법:
    # 1. 환경변수 설정
    export TRADING_MODE=PROD
    
    # 2. config/prod.yaml에서 allow_order=true로 변경
    
    # 3. 실행 (2단계 안전장치 자동 적용)
    python main_v2.py --mode trade

★ 안전장치 요약:
    1단계: config/prod.yaml의 allow_order=true 확인
    2단계: 주문 시 콘솔에서 "YES" 입력 요구

═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import sys
import time
from datetime import datetime
from typing import Optional

# 환경 판별 (가장 먼저 임포트)
from env import (
    get_environment, 
    is_dev, 
    is_prod, 
    Environment,
    validate_environment
)

# 설정 로딩
from config_loader import (
    get_config, 
    print_config_summary,
    Config
)

# 트레이더 (안전장치 포함)
from trader import (
    Trader, 
    get_trader,
    OrderNotAllowedError,
    OrderConfirmationError
)

# 전략 (환경 독립)
from strategy.trend_atr_v2 import (
    TrendATRStrategy,
    StrategyParams,
    Signal,
    SignalType
)

from utils.market_hours import KST


def print_banner():
    """프로그램 시작 배너를 출력합니다."""
    banner = """
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
║                         DEV/PROD 환경 분리 버전                                ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
    print(banner)


class TradingEngine:
    """
    거래 실행 엔진
    
    전략 시그널을 받아 트레이더를 통해 주문을 실행합니다.
    
    ★ 구조적 안전장치:
        - 전략(strategy)은 환경을 모릅니다.
        - 트레이더(trader)가 환경별 안전장치를 적용합니다.
    """
    
    def __init__(self):
        """엔진 초기화"""
        # 설정 로드
        self.config: Config = get_config()
        
        # 트레이더 초기화 (안전장치 포함)
        self.trader: Trader = get_trader()
        
        # ★ 전략 초기화 (환경 독립)
        # 설정 값을 전략에 주입합니다. 전략은 설정 파일을 직접 읽지 않습니다.
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
        
        # 실행 상태
        self.is_running = False
        self.stock_code = self.config.order.default_stock_code
        self.order_quantity = self.config.order.default_quantity
    
    def run_once(self) -> dict:
        """
        전략을 1회 실행합니다.
        
        Returns:
            dict: 실행 결과
        """
        result = {
            "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "stock_code": self.stock_code,
            "signal": None,
            "order_result": None,
            "error": None
        }
        
        try:
            # 1. 시장 데이터 조회
            print(f"\n[Engine] 시장 데이터 조회 중... ({self.stock_code})")
            df = self.trader.get_daily_ohlcv(self.stock_code)
            
            if df.empty:
                result["error"] = "시장 데이터 조회 실패"
                print(f"[Engine] ❌ {result['error']}")
                return result
            
            print(f"[Engine] ✅ 데이터 조회 완료: {len(df)}개 캔들")
            
            # 2. 현재가 조회
            price_data = self.trader.get_current_price(self.stock_code)
            current_price = price_data.get("current_price", 0)
            
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                print(f"[Engine] ❌ {result['error']}")
                return result
            
            print(f"[Engine] 현재가: {current_price:,.0f}원")
            
            # 3. ★ 전략 시그널 생성 (환경 독립)
            signal = self.strategy.generate_signal(df, current_price)
            
            result["signal"] = {
                "type": signal.signal_type.value,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reason": signal.reason,
                "trend": signal.trend.value
            }
            
            print(f"[Engine] 시그널: {signal.signal_type.value} | 추세: {signal.trend.value}")
            print(f"[Engine] 사유: {signal.reason}")
            
            # 4. ★ 시그널에 따른 주문 실행 (안전장치 적용)
            if signal.signal_type == SignalType.BUY:
                print(f"\n[Engine] 매수 시그널 감지 - 주문 실행 시작")
                
                try:
                    # trader.buy()는 PROD 환경에서 2단계 안전장치를 적용합니다.
                    order_result = self.trader.buy(
                        stock_code=self.stock_code,
                        quantity=self.order_quantity,
                        price=0,  # 시장가
                        order_type="01"
                    )
                    
                    if order_result.success:
                        # 전략에 포지션 기록
                        self.strategy.open_position(
                            stock_code=self.stock_code,
                            entry_price=current_price,
                            quantity=self.order_quantity,
                            stop_loss=signal.stop_loss,
                            take_profit=signal.take_profit,
                            entry_date=datetime.now(KST).strftime("%Y-%m-%d"),
                            atr=signal.atr
                        )
                        print(f"[Engine] ✅ 매수 주문 성공: 주문번호 {order_result.order_no}")
                    else:
                        print(f"[Engine] ❌ 매수 주문 실패: {order_result.message}")
                    
                    result["order_result"] = {
                        "success": order_result.success,
                        "order_no": order_result.order_no,
                        "message": order_result.message
                    }
                    
                except (OrderNotAllowedError, OrderConfirmationError) as e:
                    # 안전장치에 의해 주문 차단됨
                    result["order_result"] = {
                        "success": False,
                        "message": "안전장치에 의해 주문 차단됨"
                    }
                    print(f"[Engine] 🛡️ 안전장치 작동 - 주문 차단")
                    
            elif signal.signal_type == SignalType.SELL:
                print(f"\n[Engine] 매도 시그널 감지 - 주문 실행 시작")
                
                if not self.strategy.has_position():
                    print("[Engine] 보유 포지션 없음 - 매도 생략")
                else:
                    try:
                        position = self.strategy.position
                        
                        # trader.sell()은 PROD 환경에서 2단계 안전장치를 적용합니다.
                        order_result = self.trader.sell(
                            stock_code=self.stock_code,
                            quantity=position.quantity,
                            price=0,
                            order_type="01"
                        )
                        
                        if order_result.success:
                            close_result = self.strategy.close_position(
                                exit_price=current_price,
                                reason=signal.reason
                            )
                            print(f"[Engine] ✅ 매도 주문 성공: 주문번호 {order_result.order_no}")
                            if close_result:
                                print(f"[Engine] 손익: {close_result['pnl']:,.0f}원 ({close_result['pnl_pct']:+.2f}%)")
                        else:
                            print(f"[Engine] ❌ 매도 주문 실패: {order_result.message}")
                        
                        result["order_result"] = {
                            "success": order_result.success,
                            "order_no": order_result.order_no,
                            "message": order_result.message
                        }
                        
                    except (OrderNotAllowedError, OrderConfirmationError):
                        result["order_result"] = {
                            "success": False,
                            "message": "안전장치에 의해 주문 차단됨"
                        }
                        print(f"[Engine] 🛡️ 안전장치 작동 - 주문 차단")
            
            # 5. 현재 포지션 상태
            if self.strategy.has_position():
                pos = self.strategy.position
                pnl, pnl_pct = self.strategy.get_position_pnl(current_price)
                print(f"\n[Engine] 현재 포지션:")
                print(f"  - 진입가: {pos.entry_price:,.0f}원")
                print(f"  - 손절가: {pos.stop_loss:,.0f}원")
                print(f"  - 익절가: {pos.take_profit:,.0f}원")
                print(f"  - 현재 손익: {pnl:,.0f}원 ({pnl_pct:+.2f}%)")
            
        except Exception as e:
            result["error"] = str(e)
            print(f"[Engine] ❌ 오류: {e}")
        
        return result
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None):
        """
        전략을 지속적으로 실행합니다.
        
        Args:
            interval_seconds: 실행 간격 (초)
            max_iterations: 최대 반복 횟수
        """
        if interval_seconds < 60:
            print("[Engine] ⚠️ 실행 간격이 60초 미만입니다. 60초로 조정합니다.")
            interval_seconds = 60
        
        self.is_running = True
        iteration = 0
        
        print(f"\n[Engine] 거래 시작 (간격: {interval_seconds}초)")
        print("[Engine] 종료하려면 Ctrl+C를 누르세요.\n")
        
        try:
            while self.is_running:
                iteration += 1
                print(f"\n{'═' * 60}")
                print(f"  반복 #{iteration} - {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'═' * 60}")
                
                self.run_once()
                
                if max_iterations and iteration >= max_iterations:
                    print(f"\n[Engine] 최대 반복 횟수 도달: {max_iterations}")
                    break
                
                print(f"\n[Engine] 다음 실행까지 {interval_seconds}초 대기...")
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            print("\n\n[Engine] 🛑 사용자에 의해 중단됨")
        finally:
            self.is_running = False


def run_trade(stock_code: str = None, interval: int = 60, max_runs: int = None):
    """
    거래 모드를 실행합니다.
    
    Args:
        stock_code: 거래 종목 코드
        interval: 실행 간격 (초)
        max_runs: 최대 실행 횟수
    """
    config = get_config()
    
    print("\n" + "=" * 70)
    env_label = "모의투자" if is_dev() else "실계좌"
    print(f"                        {env_label} 거래 모드")
    print("=" * 70)
    
    stock = stock_code or config.order.default_stock_code
    
    print(f"\n📊 종목코드: {stock}")
    print(f"⏱️  실행 간격: {interval}초")
    print(f"🔄 최대 실행 횟수: {max_runs if max_runs else '무제한'}")
    
    if is_prod():
        print("\n⚠️⚠️⚠️ 실계좌 환경입니다! ⚠️⚠️⚠️")
        print("모든 주문은 실제로 체결됩니다.")
        print("2단계 안전장치가 적용됩니다:")
        print("  1단계: config/prod.yaml의 allow_order=true 확인")
        print("  2단계: 주문 시 YES 입력 요구")
    else:
        print("\n✅ 모의투자 환경입니다. 실제 손익이 발생하지 않습니다.")
    
    print("=" * 70 + "\n")
    
    # 설정 요약 출력
    print_config_summary()
    
    # 환경 검증
    if not validate_environment():
        print("❌ 환경 검증 실패")
        return
    
    # 엔진 생성 및 실행
    engine = TradingEngine()
    if stock_code:
        engine.stock_code = stock_code
    
    engine.run(interval_seconds=interval, max_iterations=max_runs)


def run_backtest(stock_code: str = None, days: int = 365):
    """
    백테스트 모드를 실행합니다.
    
    Args:
        stock_code: 백테스트 종목 코드
        days: 백테스트 기간
    """
    config = get_config()
    
    print("\n" + "=" * 70)
    print("                         백테스트 모드")
    print("=" * 70)
    
    stock = stock_code or config.order.default_stock_code
    
    print(f"\n📊 종목코드: {stock}")
    print(f"📅 기간: 최근 {days}일")
    print(f"💰 초기 자본금: {config.backtest.initial_capital:,}원")
    print(f"\n전략 파라미터:")
    print(f"  - ATR 기간: {config.strategy.atr_period}일")
    print(f"  - 추세 MA: {config.strategy.trend_ma_period}일")
    print(f"  - 손절 배수: {config.strategy.atr_multiplier_sl}x ATR")
    print(f"  - 익절 배수: {config.strategy.atr_multiplier_tp}x ATR")
    print("=" * 70 + "\n")
    
    # 트레이더로 데이터 조회
    trader = get_trader()
    
    print("📈 시장 데이터 조회 중...")
    df = trader.get_daily_ohlcv(stock)
    
    if df.empty:
        print("❌ 데이터 조회 실패")
        return
    
    print(f"✅ 데이터 조회 완료: {len(df)}개 캔들\n")
    
    # 전략 파라미터 설정
    strategy_params = StrategyParams(
        atr_period=config.strategy.atr_period,
        trend_ma_period=config.strategy.trend_ma_period,
        atr_multiplier_sl=config.strategy.atr_multiplier_sl,
        atr_multiplier_tp=config.strategy.atr_multiplier_tp,
        max_loss_pct=config.risk.max_loss_pct,
        atr_spike_threshold=config.risk.atr_spike_threshold,
        adx_threshold=config.risk.adx_threshold,
        adx_period=config.risk.adx_period
    )
    strategy = TrendATRStrategy(strategy_params)
    
    # 백테스트 실행
    print("🔄 백테스트 실행 중...\n")
    
    initial_capital = config.backtest.initial_capital
    capital = initial_capital
    trades = []
    
    for i in range(config.strategy.trend_ma_period, len(df)):
        df_slice = df.iloc[:i+1]
        current_price = df_slice.iloc[-1]['close']
        current_date = df_slice.iloc[-1]['date'].strftime("%Y-%m-%d")
        
        signal = strategy.generate_signal(df_slice, current_price)
        
        if signal.signal_type == SignalType.BUY and not strategy.has_position():
            # 매수
            quantity = int(capital * 0.95 / current_price)  # 자본의 95% 사용
            if quantity > 0:
                strategy.open_position(
                    stock_code=stock,
                    entry_price=current_price,
                    quantity=quantity,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    entry_date=current_date,
                    atr=signal.atr
                )
                
        elif signal.signal_type == SignalType.SELL and strategy.has_position():
            # 매도
            position = strategy.position
            close_result = strategy.close_position(current_price, signal.reason)
            
            if close_result:
                capital += close_result["pnl"]
                trades.append({
                    "entry_date": close_result["entry_date"],
                    "exit_date": current_date,
                    "entry_price": close_result["entry_price"],
                    "exit_price": close_result["exit_price"],
                    "quantity": close_result["quantity"],
                    "pnl": close_result["pnl"],
                    "pnl_pct": close_result["pnl_pct"],
                    "reason": close_result["reason"]
                })
    
    # 결과 출력
    print("=" * 70)
    print("                      백테스트 결과")
    print("=" * 70)
    
    total_pnl = capital - initial_capital
    total_return = (capital / initial_capital - 1) * 100
    
    print(f"\n초기 자본금: {initial_capital:,}원")
    print(f"최종 자본금: {capital:,.0f}원")
    print(f"총 수익: {total_pnl:,.0f}원 ({total_return:+.2f}%)")
    print(f"총 거래 횟수: {len(trades)}회")
    
    if trades:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] <= 0)
        win_rate = wins / len(trades) * 100 if trades else 0
        
        print(f"\n승률: {win_rate:.1f}% ({wins}승 / {losses}패)")
        
        print("\n거래 내역:")
        print("-" * 90)
        print(f"{'진입일':<12} {'청산일':<12} {'진입가':>10} {'청산가':>10} "
              f"{'수량':>6} {'손익':>12} {'손익률':>8}")
        print("-" * 90)
        
        for trade in trades:
            print(f"{trade['entry_date']:<12} {trade['exit_date']:<12} "
                  f"{trade['entry_price']:>10,.0f} {trade['exit_price']:>10,.0f} "
                  f"{trade['quantity']:>6} {trade['pnl']:>12,.0f} "
                  f"{trade['pnl_pct']:>7.2f}%")
        
        print("-" * 90)
    
    print("=" * 70)


def main():
    """메인 함수"""
    # 명령행 인자 파서
    parser = argparse.ArgumentParser(
        description="KIS Trend-ATR Trading System (DEV/PROD 환경 분리)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
═══════════════════════════════════════════════════════════════════════════════
실행 예시:
═══════════════════════════════════════════════════════════════════════════════

★ 모의투자 실행 (기본):
    python main_v2.py --mode trade
    python main_v2.py --mode backtest
    
★ 실계좌 실행:
    # 1. 환경변수 설정
    export TRADING_MODE=PROD
    
    # 2. config/prod.yaml에서 allow_order=true로 변경
    
    # 3. 실행
    python main_v2.py --mode trade
    
    ※ 실계좌에서는 주문마다 YES 입력이 필요합니다.

═══════════════════════════════════════════════════════════════════════════════
        """
    )
    
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["backtest", "trade"],
        help="실행 모드 (backtest: 백테스트, trade: 거래)"
    )
    
    parser.add_argument(
        "--stock",
        type=str,
        default=None,
        help="종목 코드 (기본: 설정 파일 값)"
    )
    
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="전략 실행 간격 (초, 기본: 60, 최소: 60)"
    )
    
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="최대 실행 횟수 (기본: 무제한)"
    )
    
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="백테스트 기간 (일, 기본: 365)"
    )
    
    args = parser.parse_args()
    
    # 배너 출력
    print_banner()
    
    # 시작 시간 기록
    start_time = datetime.now(KST)
    print(f"프로그램 시작: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 환경 정보 출력 (env.py에서 자동으로 출력됨)
    env = get_environment()
    
    # 모드별 실행
    if args.mode == "backtest":
        run_backtest(
            stock_code=args.stock,
            days=args.days
        )
    elif args.mode == "trade":
        interval = max(60, args.interval)
        if interval != args.interval:
            print(f"⚠️ 실행 간격이 60초 미만입니다. 60초로 조정됩니다.")
        
        run_trade(
            stock_code=args.stock,
            interval=interval,
            max_runs=args.max_runs
        )
    
    # 종료 시간 기록
    end_time = datetime.now(KST)
    elapsed = (end_time - start_time).total_seconds()
    
    print(f"\n✅ 프로그램 종료 (실행 시간: {elapsed:.1f}초)")


if __name__ == "__main__":
    main()
