#!/usr/bin/env python3
"""
KIS Trend-ATR Trading System - 메인 실행 파일

한국투자증권 Open API를 사용한 Trend + ATR 기반 자동매매 시스템입니다.

실행 방법:
    - 백테스트: python main.py --mode backtest
    - 모의투자: python main.py --mode trade

⚠️ 주의사항:
    - 실계좌 사용 절대 금지
    - 모의투자 전용으로만 사용
    - .env 파일에 API 키 설정 필요

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
"""

import argparse
import sys
from datetime import datetime

# 프로젝트 모듈 임포트
from config import settings
from api.kis_api import KISApi, KISApiError
from strategy.trend_atr import TrendATRStrategy
from engine.executor import TradingExecutor
from backtest.backtester import Backtester
from utils.logger import setup_logger, get_logger


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
║                     ⚠️  모의투자 전용 - 실계좌 사용 금지  ⚠️                   ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
    print(banner)


def run_backtest(stock_code: str, days: int = 365):
    """
    백테스트를 실행합니다.
    
    과거 데이터를 기반으로 전략 성과를 검증합니다.
    실제 주문은 발생하지 않습니다.
    
    Args:
        stock_code: 백테스트 대상 종목 코드
        days: 백테스트 기간 (일)
    """
    logger = get_logger("main")
    
    print("\n" + "=" * 70)
    print("                         백테스트 모드")
    print("=" * 70)
    print(f"\n📊 종목코드: {stock_code}")
    print(f"📅 기간: 최근 {days}일")
    print(f"💰 초기 자본금: {settings.BACKTEST_INITIAL_CAPITAL:,}원")
    print(f"\n전략 파라미터:")
    print(f"  - ATR 기간: {settings.ATR_PERIOD}일")
    print(f"  - 추세 MA: {settings.TREND_MA_PERIOD}일")
    print(f"  - 손절 배수: {settings.ATR_MULTIPLIER_SL}x ATR")
    print(f"  - 익절 배수: {settings.ATR_MULTIPLIER_TP}x ATR")
    print("=" * 70 + "\n")
    
    try:
        # API 클라이언트 생성 (데이터 조회용)
        api = KISApi(is_paper_trading=True)
        
        # 토큰 발급
        print("🔑 API 토큰 발급 중...")
        api.get_access_token()
        print("✅ 토큰 발급 완료\n")
        
        # 일봉 데이터 조회
        print("📈 시장 데이터 조회 중...")
        df = api.get_daily_ohlcv(stock_code)
        
        if df.empty:
            print("❌ 데이터 조회 실패: 데이터가 없습니다.")
            logger.error(f"백테스트 실패: {stock_code} 데이터 없음")
            return
        
        print(f"✅ 데이터 조회 완료: {len(df)}개 캔들\n")
        
        # 백테스터 생성 및 실행
        print("🔄 백테스트 실행 중...\n")
        backtester = Backtester()
        result = backtester.run(df, stock_code)
        
        # 거래 내역 출력
        if result.trades:
            print("\n📋 거래 내역:")
            print("-" * 90)
            print(f"{'진입일':<12} {'청산일':<12} {'진입가':>10} {'청산가':>10} "
                  f"{'수량':>6} {'손익':>12} {'손익률':>8} {'청산사유':<10}")
            print("-" * 90)
            
            for trade in result.trades:
                print(f"{trade.entry_date:<12} {trade.exit_date:<12} "
                      f"{trade.entry_price:>10,.0f} {trade.exit_price:>10,.0f} "
                      f"{trade.quantity:>6} {trade.pnl:>12,.0f} "
                      f"{trade.pnl_pct:>7.2f}% {trade.exit_reason:<10}")
            
            print("-" * 90)
        
        logger.info(f"백테스트 완료: 총 수익률 {result.total_return:.2f}%")
        
    except KISApiError as e:
        print(f"\n❌ API 오류: {e}")
        logger.error(f"백테스트 API 오류: {e}")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        logger.error(f"백테스트 오류: {e}")


def run_trade(stock_code: str, interval: int = 60, max_runs: int = None):
    """
    모의투자 거래를 실행합니다.
    
    ⚠️ 모의투자 전용: 실계좌 주문 불가
    
    Args:
        stock_code: 거래 종목 코드
        interval: 전략 실행 간격 (초, 최소 60초)
        max_runs: 최대 실행 횟수 (None = 무한)
    """
    logger = get_logger("main")
    
    print("\n" + "=" * 70)
    print("                        모의투자 거래 모드")
    print("=" * 70)
    print(f"\n📊 종목코드: {stock_code}")
    print(f"⏱️  실행 간격: {interval}초")
    print(f"🔄 최대 실행 횟수: {max_runs if max_runs else '무제한'}")
    print(f"\n⚠️  주의: 모의투자 전용입니다. 실계좌 주문이 발생하지 않습니다.")
    print("=" * 70 + "\n")
    
    # 설정 검증
    if not settings.validate_settings():
        print("\n❌ 설정 오류: .env 파일을 확인하세요.")
        print("   필요한 환경변수: KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO")
        return
    
    # 설정 요약 출력
    print(settings.get_settings_summary())
    
    try:
        # API 클라이언트 생성
        api = KISApi(is_paper_trading=True)
        
        # 토큰 발급
        print("🔑 API 토큰 발급 중...")
        api.get_access_token()
        print("✅ 토큰 발급 완료\n")
        
        # 전략 생성
        strategy = TrendATRStrategy()
        
        # 실행 엔진 생성
        executor = TradingExecutor(
            api=api,
            strategy=strategy,
            stock_code=stock_code,
            order_quantity=settings.ORDER_QUANTITY
        )
        
        # 거래 시작
        print("🚀 거래 시작...\n")
        print("   종료하려면 Ctrl+C를 누르세요.\n")
        
        executor.run(
            interval_seconds=interval,
            max_iterations=max_runs
        )
        
        # 거래 요약 출력
        summary = executor.get_daily_summary()
        print("\n" + "=" * 50)
        print("                  거래 요약")
        print("=" * 50)
        print(f"총 거래: {summary['total_trades']}회")
        print(f"  - 매수: {summary['buy_count']}회")
        print(f"  - 매도: {summary['sell_count']}회")
        print(f"총 손익: {summary['total_pnl']:,.0f}원")
        print("=" * 50)
        
    except KISApiError as e:
        print(f"\n❌ API 오류: {e}")
        logger.error(f"거래 API 오류: {e}")
    except KeyboardInterrupt:
        print("\n\n🛑 사용자에 의해 중단됨")
        logger.info("거래 중단: 사용자 요청")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        logger.error(f"거래 오류: {e}")


def main():
    """메인 함수"""
    # 로거 초기화
    setup_logger("main", settings.LOG_LEVEL)
    logger = get_logger("main")
    
    # 명령행 인자 파서
    parser = argparse.ArgumentParser(
        description="KIS Trend-ATR Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  백테스트 실행:
    python main.py --mode backtest
    python main.py --mode backtest --stock 005930
    
  모의투자 실행:
    python main.py --mode trade
    python main.py --mode trade --stock 005930 --interval 120
    python main.py --mode trade --max-runs 10
    
⚠️ 주의: 실계좌 사용 금지. 모의투자 전용입니다.
        """
    )
    
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["backtest", "trade"],
        help="실행 모드 (backtest: 백테스트, trade: 모의투자)"
    )
    
    parser.add_argument(
        "--stock",
        type=str,
        default=settings.DEFAULT_STOCK_CODE,
        help=f"종목 코드 (기본: {settings.DEFAULT_STOCK_CODE})"
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
    start_time = datetime.now()
    logger.info(f"프로그램 시작: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"실행 모드: {args.mode}, 종목: {args.stock}")
    
    # 모드별 실행
    if args.mode == "backtest":
        run_backtest(
            stock_code=args.stock,
            days=args.days
        )
    elif args.mode == "trade":
        # 실행 간격 검증 (최소 60초)
        interval = max(60, args.interval)
        if interval != args.interval:
            print(f"⚠️ 실행 간격이 60초 미만입니다. 60초로 조정됩니다.")
        
        run_trade(
            stock_code=args.stock,
            interval=interval,
            max_runs=args.max_runs
        )
    
    # 종료 시간 기록
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    logger.info(f"프로그램 종료: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"총 실행 시간: {elapsed:.1f}초")
    
    print(f"\n✅ 프로그램 종료 (실행 시간: {elapsed:.1f}초)")


if __name__ == "__main__":
    main()
