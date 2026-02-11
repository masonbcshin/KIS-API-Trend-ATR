#!/usr/bin/env python3
"""
KIS Trend-ATR Trading System - CBT (Closed Beta Test) 실행 파일

CBT 모드는 실계좌 주문 없이 가상 체결로 전략 성과를 측정합니다.

주요 기능:
    - 실계좌 주문 절대 전송하지 않음
    - KIS 시세 API 기준 현재가로 가상 체결
    - 모든 거래를 Trade Log에 저장
    - 성과 지표 자동 계산
    - 텔레그램 CBT 리포트 전송

실행 방법:
    - CBT 시작: python main_cbt.py --mode cbt
    - 성과 조회: python main_cbt.py --mode report
    - 계좌 초기화: python main_cbt.py --mode reset

⚠️ 주의사항:
    - 실계좌 주문이 발생하지 않습니다
    - 모든 체결은 가상으로 처리됩니다
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
from cbt import CBTExecutor, VirtualAccount, TradeStore, CBTMetrics
from utils.logger import setup_logger, get_logger
from utils.market_hours import KST


def print_cbt_banner():
    """CBT 모드 배너 출력"""
    banner = """
╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║      ██████╗██████╗ ████████╗    ███╗   ███╗ ██████╗ ██████╗ ███████╗        ║
║     ██╔════╝██╔══██╗╚══██╔══╝    ████╗ ████║██╔═══██╗██╔══██╗██╔════╝        ║
║     ██║     ██████╔╝   ██║       ██╔████╔██║██║   ██║██║  ██║█████╗          ║
║     ██║     ██╔══██╗   ██║       ██║╚██╔╝██║██║   ██║██║  ██║██╔══╝          ║
║     ╚██████╗██████╔╝   ██║       ██║ ╚═╝ ██║╚██████╔╝██████╔╝███████╗        ║
║      ╚═════╝╚═════╝    ╚═╝       ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝        ║
║                                                                               ║
║                    Closed Beta Test - 가상 체결 성과 측정                     ║
║                                                                               ║
║                   🔒 실계좌 주문 없음 - 가상 체결 전용 🔒                     ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
    print(banner)


def run_cbt_trading(stock_code: str, interval: int = 60, max_runs: int = None):
    """
    CBT 모드로 가상 거래 실행
    
    Args:
        stock_code: 거래 종목 코드
        interval: 전략 실행 간격 (초)
        max_runs: 최대 실행 횟수 (None = 무한)
    """
    logger = get_logger("main_cbt")
    
    print("\n" + "=" * 70)
    print("                        CBT 거래 모드")
    print("=" * 70)
    print(f"\n📊 종목코드: {stock_code}")
    print(f"⏱️  실행 간격: {interval}초")
    print(f"🔄 최대 실행 횟수: {max_runs if max_runs else '무제한'}")
    print(f"\n🔒 CBT 모드: 실계좌 주문이 발생하지 않습니다.")
    print(f"    모든 체결은 가상으로 처리됩니다.")
    print("=" * 70 + "\n")
    
    # CBT 설정 요약 출력
    print(settings.get_cbt_settings_summary())
    
    try:
        # API 클라이언트 생성 (시세 조회 전용)
        api = KISApi(is_paper_trading=True)
        
        # 토큰 발급
        print("🔑 API 토큰 발급 중...")
        api.get_access_token()
        print("✅ 토큰 발급 완료\n")
        
        # 전략 생성
        strategy = TrendATRStrategy()
        
        # CBT 실행 엔진 생성
        executor = CBTExecutor(
            api=api,
            strategy=strategy,
            stock_code=stock_code,
            order_quantity=settings.ORDER_QUANTITY
        )
        
        # 현재 가상 계좌 상태 출력
        account_summary = executor.account.get_account_summary()
        print("\n" + "=" * 50)
        print("              가상 계좌 현황")
        print("=" * 50)
        print(f"초기 자본금: {account_summary['initial_capital']:,}원")
        print(f"현재 현금: {account_summary['cash']:,}원")
        print(f"실현 손익: {account_summary['realized_pnl']:+,}원")
        print(f"총 거래: {account_summary['total_trades']}회")
        print(f"승률: {account_summary['win_rate']:.1f}%")
        print("=" * 50 + "\n")
        
        # 거래 시작
        print("🚀 CBT 거래 시작...\n")
        print("   종료하려면 Ctrl+C를 누르세요.\n")
        
        executor.run(
            interval_seconds=interval,
            max_iterations=max_runs
        )
        
    except KISApiError as e:
        print(f"\n❌ API 오류: {e}")
        logger.error(f"CBT API 오류: {e}")
    except KeyboardInterrupt:
        print("\n\n🛑 사용자에 의해 중단됨")
        logger.info("CBT 중단: 사용자 요청")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        logger.error(f"CBT 오류: {e}")


def show_performance_report():
    """CBT 성과 리포트 출력"""
    logger = get_logger("main_cbt")
    
    print("\n" + "=" * 70)
    print("                     CBT 성과 리포트")
    print("=" * 70 + "\n")
    
    try:
        # 컴포넌트 초기화
        account = VirtualAccount()
        trade_store = TradeStore()
        metrics = CBTMetrics(account, trade_store)
        
        # 현재가 조회 (포지션 평가용)
        current_price = None
        if account.has_position():
            try:
                api = KISApi(is_paper_trading=True)
                api.get_access_token()
                price_data = api.get_current_price(account.position.stock_code)
                current_price = price_data.get("current_price", 0)
            except:
                pass
        
        # 성과 리포트 생성
        report = metrics.generate_report(current_price)
        
        # 텍스트 요약 출력
        print(report.get_summary_text())
        
        # 최근 거래 목록
        recent_trades = trade_store.get_recent_trades(10)
        if recent_trades:
            print("\n📋 최근 거래 내역 (최대 10건)")
            print("-" * 90)
            print(f"{'청산일':<20} {'종목':^8} {'진입가':>10} {'청산가':>10} {'손익':>12} {'손익률':>8} {'사유':<12}")
            print("-" * 90)
            
            for trade in recent_trades:
                print(f"{trade.exit_date:<20} {trade.stock_code:^8} "
                      f"{trade.entry_price:>10,.0f} {trade.exit_price:>10,.0f} "
                      f"{trade.pnl:>+12,.0f} {trade.return_pct:>+7.2f}% {trade.exit_reason:<12}")
            
            print("-" * 90)
        
        logger.info("CBT 성과 리포트 출력 완료")
        
    except Exception as e:
        print(f"❌ 리포트 생성 실패: {e}")
        logger.error(f"CBT 리포트 오류: {e}")


def reset_cbt_account():
    """CBT 가상 계좌 초기화"""
    logger = get_logger("main_cbt")
    
    print("\n" + "=" * 70)
    print("                    CBT 계좌 초기화")
    print("=" * 70 + "\n")
    
    print("⚠️  경고: 이 작업은 다음 데이터를 삭제합니다:")
    print("    - 가상 계좌 상태 (잔고, 손익)")
    print("    - 모든 거래 기록")
    print("    - Equity Curve\n")
    
    confirm = input("계속하려면 'RESET'을 입력하세요: ").strip()
    
    if confirm != "RESET":
        print("\n❌ 초기화 취소됨")
        return
    
    try:
        # 계좌 초기화
        account = VirtualAccount()
        account.reset()
        
        # 거래 기록 초기화
        trade_store = TradeStore()
        trade_store.clear_all_trades()
        
        print("\n✅ CBT 계좌가 초기화되었습니다.")
        print(f"   초기 자본금: {settings.CBT_INITIAL_CAPITAL:,}원")
        
        logger.info("CBT 계좌 초기화 완료")
        
    except Exception as e:
        print(f"\n❌ 초기화 실패: {e}")
        logger.error(f"CBT 초기화 오류: {e}")


def export_trades_csv():
    """거래 기록 CSV 내보내기"""
    logger = get_logger("main_cbt")
    
    print("\n" + "=" * 70)
    print("                  거래 기록 CSV 내보내기")
    print("=" * 70 + "\n")
    
    try:
        trade_store = TradeStore()
        filepath = trade_store.export_to_csv()
        
        if filepath:
            print(f"✅ CSV 파일 생성 완료: {filepath}")
            logger.info(f"CSV 내보내기 완료: {filepath}")
        else:
            print("❌ 내보낼 거래 기록이 없습니다.")
            
    except Exception as e:
        print(f"❌ 내보내기 실패: {e}")
        logger.error(f"CSV 내보내기 오류: {e}")


def main():
    """메인 함수"""
    # 로거 초기화
    setup_logger("main_cbt", settings.LOG_LEVEL)
    logger = get_logger("main_cbt")
    
    # 명령행 인자 파서
    parser = argparse.ArgumentParser(
        description="KIS Trend-ATR Trading System - CBT Mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  CBT 거래 시작:
    python main_cbt.py --mode cbt
    python main_cbt.py --mode cbt --stock 005930 --interval 120
    python main_cbt.py --mode cbt --max-runs 100
    
  성과 리포트 조회:
    python main_cbt.py --mode report
    
  계좌 초기화:
    python main_cbt.py --mode reset
    
  거래 내역 CSV 내보내기:
    python main_cbt.py --mode export
    
⚠️ CBT 모드: 실계좌 주문 없음. 가상 체결 전용입니다.
        """
    )
    
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["cbt", "report", "reset", "export"],
        help="실행 모드 (cbt: 가상거래, report: 성과조회, reset: 초기화, export: CSV내보내기)"
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
    
    args = parser.parse_args()
    
    # 배너 출력
    print_cbt_banner()
    
    # 시작 시간 기록
    start_time = datetime.now(KST)
    logger.info(f"CBT 프로그램 시작: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"실행 모드: {args.mode}")
    
    # 모드별 실행
    if args.mode == "cbt":
        # 실행 간격 검증 (최소 60초)
        interval = max(60, args.interval)
        if interval != args.interval:
            print(f"⚠️ 실행 간격이 60초 미만입니다. 60초로 조정됩니다.")
        
        run_cbt_trading(
            stock_code=args.stock,
            interval=interval,
            max_runs=args.max_runs
        )
    elif args.mode == "report":
        show_performance_report()
    elif args.mode == "reset":
        reset_cbt_account()
    elif args.mode == "export":
        export_trades_csv()
    
    # 종료 시간 기록
    end_time = datetime.now(KST)
    elapsed = (end_time - start_time).total_seconds()
    logger.info(f"CBT 프로그램 종료: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"총 실행 시간: {elapsed:.1f}초")
    
    print(f"\n✅ 프로그램 종료 (실행 시간: {elapsed:.1f}초)")


if __name__ == "__main__":
    main()
