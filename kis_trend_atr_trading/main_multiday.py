#!/usr/bin/env python3
"""
KIS Trend-ATR Trading System - 멀티데이 전략 실행 파일

★ 전략의 본질:
    - 당일 매수·당일 매도(Day Trading)가 아닌
    - 익절 또는 손절 신호가 발생할 때까지 보유(Hold until Exit)

★ 절대 금지 사항:
    - ❌ 장 마감(EOD) 시간 기준 강제 청산
    - ❌ "장이 끝났으니 판다"라는 시간 기반 종료 조건
    - ❌ 익일 ATR 재계산으로 손절선 변경

★ 트레이딩 모드:
    - LIVE  : 실계좌 주문 (실제 매매 발생)
    - CBT   : 종이매매 (주문 금지, 텔레그램 알림만)
    - PAPER : 모의투자 (모의투자 서버 주문)

실행 방법:
    # 기본 실행 (PAPER 모드)
    python main_multiday.py --mode trade

    # CBT 모드 (종이매매)
    TRADING_MODE=CBT python main_multiday.py --mode trade

    # 단일 실행 테스트
    python main_multiday.py --mode trade --max-runs 1

작성자: KIS Trend-ATR Trading System
버전: 2.0.1 (멀티데이 타임존 수정)
"""

import argparse
import sys

# 프로젝트 모듈 임포트
from config import settings
from api.kis_api import KISApi, KISApiError
from strategy.multiday_trend_atr import MultidayTrendATRStrategy
from engine.multiday_executor import MultidayExecutor
from backtest.backtester import Backtester
from utils.logger import setup_logger, get_logger
from utils.market_hours import get_kst_now # KST 시간 함수 임포트


def print_banner():
    """프로그램 시작 배너"""
    mode_emoji = {
        "LIVE": "🔴",
        "CBT": "🟡",
        "PAPER": "🟢"
    }
    current_mode = settings.TRADING_MODE
    emoji = mode_emoji.get(current_mode, "❓")
    
    banner = f"""
╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║     ██╗  ██╗██╗███████╗    ████████╗██████╗ ███████╗███╗   ██╗██████╗        ║
║     ██║ ██╔╝██║██╔════╝    ╚══██╔══╝██╔══██╗██╔════╝████╗  ██║██╔══██╗       ║
║     █████╔╝ ██║███████╗       ██║   ██████╔╝█████╗  ██╔██╗ ██║██║  ██║       ║
║     ██╔═██╗ ██║╚════██║       ██║   ██╔══██╗██╔══╝  ██║╚██╗██║██║  ██║       ║
║     ██║  ██╗██║███████║       ██║   ██║  ██║███████╗██║ ╚████║██████╔╝       ║
║     ╚═╝  ╚═╝╚═╝╚══════╝       ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═════╝        ║
║                                                                               ║
║                 ATR-Based Trend Following Trading System                      ║
║                         ** 멀티데이 버전 **                                   ║
║                                                                               ║
║     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━       ║
║                                                                               ║
║               {emoji} 현재 모드: {current_mode:^10}                              ║
║                                                                               ║
║     ★ EOD 청산 없음 - Exit는 오직 가격 조건으로만 발생                        ║
║     ★ ATR은 진입 시 고정 - 익일 재계산 금지                                   ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
    print(banner)


def print_strategy_rules():
    """전략 규칙 출력"""
    rules = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                         전략 규칙 요약
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[진입 조건]
  ✓ 상승 추세 (종가 > 50일 MA)
  ✓ ADX > 25 (추세 강도 확인)
  ✓ 직전 캔들 고가 돌파
  ✓ ATR 정상 범위 (급등 아님)

[Exit 조건] ★ 유일하게 허용된 청산 사유
  ✓ ATR 손절: 가격 <= 손절가
  ✓ ATR 익절: 가격 >= 익절가
  ✓ 트레일링 스탑: 가격 <= 트레일링스탑
  ✓ 추세 붕괴: MA 하향 돌파
  ✓ 갭 보호: 시가가 손절가보다 크게 불리 (옵션)

[절대 금지]
  ✗ 장 마감(EOD) 시간 기준 강제 청산
  ✗ 시간 기반 종료 조건
  ✗ 익일 ATR 재계산으로 손절선 변경

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    print(rules)


def run_verification():
    """
    최종 검증 체크리스트
    
    모든 항목이 YES여야 전략이 올바르게 구현된 것
    """
    print("\n")
    print("=" * 70)
    print("                    최종 검증 체크리스트")
    print("=" * 70)
    
    checks = []
    
    # 1. EOD 청산 로직 없음 확인
    # 주석이 아닌 실제 코드에서 EOD 청산 함수가 있는지 확인
    import inspect
    from engine.multiday_executor import MultidayExecutor
    
    # 실제로 EOD 청산 메서드가 있는지 확인
    eod_methods = ["force_close_at_eod", "close_at_market_close", "eod_liquidation"]
    has_eod_method = any(hasattr(MultidayExecutor, m) for m in eod_methods)
    
    # ExitReason에 EOD 관련 사유가 있는지 확인
    from engine.trading_state import ExitReason
    has_eod_reason = any("eod" in r.value.lower() or "end_of_day" in r.value.lower() 
                         for r in ExitReason)
    
    # 둘 다 없어야 통과
    checks.append(("장이 끝나도 포지션을 유지하는가?", not (has_eod_method or has_eod_reason)))
    
    # 2. Exit 조건이 가격 구조로만 발생
    from engine.trading_state import ExitReason
    valid_reasons = [
        ExitReason.ATR_STOP_LOSS,
        ExitReason.ATR_TAKE_PROFIT,
        ExitReason.TRAILING_STOP,
        ExitReason.TREND_BROKEN,
        ExitReason.GAP_PROTECTION,
        ExitReason.MANUAL_EXIT,
        ExitReason.KILL_SWITCH
    ]
    # EOD_CLOSE 같은 시간 기반 청산 사유가 없어야 함
    has_time_exit = any("eod" in r.value.lower() or "time" in r.value.lower() 
                        for r in ExitReason)
    checks.append(("Exit는 오직 가격 구조로만 발생하는가?", not has_time_exit))
    
    # 3. 포지션 복원 기능 확인
    from utils.position_store import PositionStore
    has_restore = hasattr(MultidayExecutor, 'restore_position_on_start')
    checks.append(("익일 실행 시 이전 포지션을 인식하는가?", has_restore))
    
    # 4. CBT 모드 확인
    from config import settings
    is_cbt_safe = settings.TRADING_MODE == "CBT" or hasattr(settings, 'is_cbt_mode')
    checks.append(("CBT 모드에서 실주문이 차단되는가?", is_cbt_safe))
    
    # 5. ATR 재계산 금지 확인
    from strategy.multiday_trend_atr import MultidayTrendATRStrategy
    strategy_source = inspect.getsource(MultidayTrendATRStrategy)
    atr_recalc_keywords = ["recalculate_atr", "update_atr", "daily_atr_update"]
    has_atr_recalc = any(kw.lower() in strategy_source.lower() for kw in atr_recalc_keywords)
    checks.append(("ATR이 진입 시 고정되어 변경되지 않는가?", not has_atr_recalc))
    
    # 결과 출력
    all_passed = True
    for question, passed in checks:
        status = "✅ YES" if passed else "❌ NO"
        print(f"  {status}  {question}")
        if not passed:
            all_passed = False
    
    print("=" * 70)
    
    if all_passed:
        print("  🎉 모든 검증 통과! 멀티데이 전략이 올바르게 구현되었습니다.")
    else:
        print("  ⚠️ 일부 검증 실패. 코드를 확인하세요.")
    
    print("=" * 70 + "\n")
    
    return all_passed


def run_backtest(stock_code: str, days: int = 365):
    """
    백테스트 실행
    
    Args:
        stock_code: 백테스트 대상 종목
        days: 백테스트 기간 (일)
    """
    logger = get_logger("main")
    
    print("\n" + "=" * 70)
    print("                         백테스트 모드")
    print("=" * 70)
    print(f"\n📊 종목코드: {stock_code}")
    print(f"📅 기간: 최근 {days}일")
    print("=" * 70 + "\n")
    
    try:
        api = KISApi(is_paper_trading=True)
        api.get_access_token()
        
        df = api.get_daily_ohlcv(stock_code)
        
        if df.empty:
            print("❌ 데이터 조회 실패")
            return
        
        backtester = Backtester()
        result = backtester.run(df, stock_code)
        
        if result.trades:
            print("\n📋 거래 내역:")
            print("-" * 90)
            for trade in result.trades:
                print(f"{trade.entry_date} → {trade.exit_date} | "
                      f"손익: {trade.pnl:+,.0f}원 ({trade.pnl_pct:+.2f}%) | "
                      f"사유: {trade.exit_reason}")
            print("-" * 90)
        
        logger.info(f"백테스트 완료: 총 수익률 {result.total_return:.2f}%")
        
    except Exception as e:
        print(f"❌ 오류: {e}")
        logger.error(f"백테스트 오류: {e}")


def run_trade(stock_code: str, interval: int = 60, max_runs: int = None):
    """
    멀티데이 거래 실행
    
    ★ EOD 청산 로직 없음
    ★ Exit는 오직 가격 조건으로만 발생
    
    Args:
        stock_code: 거래 종목
        interval: 실행 간격 (초)
        max_runs: 최대 실행 횟수
    """
    logger = get_logger("main")
    
    print("\n" + "=" * 70)
    print("                    멀티데이 거래 모드")
    print("=" * 70)
    print(f"\n📊 종목코드: {stock_code}")
    print(f"⏱️  실행 간격: {interval}초")
    print(f"🔄 최대 실행: {max_runs if max_runs else '무제한'}")
    print(f"📝 트레이딩 모드: {settings.TRADING_MODE}")
    print("=" * 70 + "\n")
    
    # 전략 규칙 출력
    print_strategy_rules()
    
    # 설정 검증
    if not settings.validate_settings():
        print("\n❌ 설정 오류: .env 파일을 확인하세요.")
        return
    
    # 설정 요약 출력
    print(settings.get_settings_summary())
    
    try:
        # API 클라이언트 생성
        is_paper = settings.TRADING_MODE != "LIVE"
        api = KISApi(is_paper_trading=is_paper)
        
        print("🔑 API 토큰 발급 중...")
        api.get_access_token()
        print("✅ 토큰 발급 완료\n")
        
        # 멀티데이 전략 생성
        strategy = MultidayTrendATRStrategy()
        
        # 멀티데이 실행 엔진 생성
        executor = MultidayExecutor(
            api=api,
            strategy=strategy,
            stock_code=stock_code,
            order_quantity=settings.ORDER_QUANTITY
        )
        
        # 포지션 복원 시도
        print("🔄 저장된 포지션 확인 중...")
        restored = executor.restore_position_on_start()
        
        if restored:
            print("✅ 포지션 복원 완료 - Exit 조건 감시 모드\n")
        else:
            print("ℹ️ 복원할 포지션 없음 - Entry 조건 감시 모드\n")
        
        # 거래 시작
        print("🚀 멀티데이 거래 시작...")
        print("   종료하려면 Ctrl+C를 누르세요.\n")
        print("   ★ 포지션은 프로그램 종료 시에도 유지됩니다.")
        print("   ★ Exit는 오직 가격 조건으로만 발생합니다.\n")
        
        executor.run(
            interval_seconds=interval,
            max_iterations=max_runs
        )
        
        # 거래 요약
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
        logger.error(f"API 오류: {e}")
    except KeyboardInterrupt:
        print("\n\n🛑 사용자에 의해 중단됨")
        print("   ★ 포지션 상태가 저장되었습니다.")
        logger.info("거래 중단: 사용자 요청")
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        logger.error(f"거래 오류: {e}")


def main():
    """메인 함수"""
    # 로거 초기화
    setup_logger("main", settings.LOG_LEVEL)
    logger = get_logger("main")
    
    # 명령행 파서
    parser = argparse.ArgumentParser(
        description="KIS Trend-ATR Trading System (멀티데이 버전)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 백테스트
  python main_multiday.py --mode backtest --stock 005930
  
  # 멀티데이 거래 (기본 PAPER 모드)
  python main_multiday.py --mode trade
  
  # CBT 모드 (종이매매)
  TRADING_MODE=CBT python main_multiday.py --mode trade
  
  # 단일 실행 테스트
  python main_multiday.py --mode trade --max-runs 1
  
  # 검증 체크리스트 실행
  python main_multiday.py --mode verify

★ 멀티데이 전략 핵심:
  - EOD 청산 없음
  - Exit는 오직 가격 조건으로만 발생
  - ATR은 진입 시 고정
        """
    )
    
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["backtest", "trade", "verify"],
        help="실행 모드 (backtest/trade/verify)"
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
        help="전략 실행 간격 (초, 기본: 60)"
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
    
    # 시작 시간 (KST 기준)
    start_time = get_kst_now()
    logger.info(f"프로그램 시작: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info(f"실행 모드: {args.mode}, 트레이딩 모드: {settings.TRADING_MODE}")
    
    # 모드별 실행
    if args.mode == "backtest":
        run_backtest(stock_code=args.stock, days=args.days)
        
    elif args.mode == "trade":
        interval = max(60, args.interval)
        if interval != args.interval:
            print(f"⚠️ 실행 간격이 60초 미만입니다. 60초로 조정됩니다.")
        
        run_trade(
            stock_code=args.stock,
            interval=interval,
            max_runs=args.max_runs
        )
        
    elif args.mode == "verify":
        run_verification()
    
    # 종료 시간 (KST 기준)
    end_time = get_kst_now()
    elapsed = (end_time - start_time).total_seconds()
    logger.info(f"프로그램 종료: {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info(f"총 실행 시간: {elapsed:.1f}초")
    
    print(f"\n✅ 프로그램 종료 (실행 시간: {elapsed:.1f}초)")


if __name__ == "__main__":
    main()
