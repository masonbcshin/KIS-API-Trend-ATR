#!/usr/bin/env python3
"""
main.py - KIS 자동매매 시스템 진입점

모든 예외는 텔레그램으로 통지되며,
프로그램이 조용히 죽지 않도록 보장합니다.
"""

import sys
import time
import signal
import traceback
from datetime import datetime

import schedule

from config.settings import get_settings
from trader.broker_kis import KISBroker
from trader.risk_manager import RiskManager
from trader.strategy import TradingStrategy
from trader.notifier import get_notifier, send_telegram


# ═══════════════════════════════════════════════════════════════
# 전역 변수
# ═══════════════════════════════════════════════════════════════

# 감시 종목 리스트 (필요에 따라 수정)
WATCHLIST = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "035420",  # NAVER
    "035720",  # 카카오
    "051910",  # LG화학
]

# 실행 주기 (초)
RUN_INTERVAL = 60

# 종료 플래그
shutdown_requested = False


# ═══════════════════════════════════════════════════════════════
# 시그널 핸들러
# ═══════════════════════════════════════════════════════════════

def signal_handler(signum, frame):
    """종료 시그널 핸들러"""
    global shutdown_requested
    shutdown_requested = True
    print("\n[INFO] 종료 요청 수신...")


# ═══════════════════════════════════════════════════════════════
# 메인 트레이딩 루프
# ═══════════════════════════════════════════════════════════════

def run_trading_loop():
    """
    메인 트레이딩 루프
    
    설정된 주기로 전략을 실행합니다.
    """
    settings = get_settings()
    notifier = get_notifier()
    
    # 설정 검증
    is_valid, errors = settings.validate()
    if not is_valid:
        error_msg = "\n".join(errors)
        print(f"[ERROR] 설정 오류:\n{error_msg}")
        send_telegram(f"❌ *설정 오류*\n```\n{error_msg}\n```")
        return
    
    # 설정 요약 출력
    print(settings.get_summary())
    
    # 모듈 초기화
    broker = KISBroker()
    risk_manager = RiskManager(broker)
    strategy = TradingStrategy(broker, risk_manager)
    
    # 시작 알림
    notifier.notify_start(
        mode=settings.MODE,
        trading_mode="모의투자" if settings.IS_PAPER_TRADING else "실계좌"
    )
    
    print(f"[INFO] 자동매매 시작 - 실행 주기: {RUN_INTERVAL}초")
    print(f"[INFO] 감시 종목: {', '.join(WATCHLIST)}")
    print("[INFO] 종료하려면 Ctrl+C를 누르세요.\n")
    
    # 브로커 포지션 동기화
    try:
        risk_manager.sync_positions_from_broker()
    except Exception as e:
        notifier.notify_error(e, "포지션 동기화")
    
    # 메인 루프
    last_run = datetime.min
    
    while not shutdown_requested:
        try:
            now = datetime.now()
            
            # 실행 주기 체크
            if (now - last_run).total_seconds() < RUN_INTERVAL:
                time.sleep(1)
                continue
            
            # 장 운영 시간 체크
            if not broker.is_market_open():
                print(f"[{now.strftime('%H:%M:%S')}] 장 마감 - 대기 중...")
                time.sleep(60)
                continue
            
            # 전략 실행
            print(f"\n[{now.strftime('%H:%M:%S')}] 전략 실행 중...")
            strategy.run_strategy(WATCHLIST)
            
            last_run = now
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            # 모든 예외를 텔레그램으로 알림
            notifier.notify_error(e, "메인 루프")
            print(f"[ERROR] {e}")
            time.sleep(10)  # 오류 발생 시 잠시 대기
    
    # 종료 알림
    notifier.notify_stop("사용자 요청" if shutdown_requested else "정상 종료")
    print("\n[INFO] 자동매매 종료")


# ═══════════════════════════════════════════════════════════════
# 메인 함수
# ═══════════════════════════════════════════════════════════════

def main():
    """
    메인 함수
    
    전체 실행을 try/except로 감싸서
    어떤 예외도 텔레그램으로 통지합니다.
    """
    # 시그널 핸들러 등록
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    notifier = get_notifier()
    
    try:
        print("=" * 60)
        print("      KIS Auto Trader - 자동매매 시스템")
        print("=" * 60)
        print()
        
        run_trading_loop()
        
    except Exception as e:
        # 최상위 예외 핸들링 - 반드시 텔레그램 알림
        error_trace = traceback.format_exc()
        print(f"\n[CRITICAL] 치명적 오류 발생:\n{error_trace}")
        
        # 텔레그램 알림 (실패해도 계속)
        try:
            notifier.notify_error(e, "메인 함수")
        except:
            pass
        
        sys.exit(1)
    
    finally:
        # 최종 정리
        print("\n[INFO] 프로그램 종료")


if __name__ == "__main__":
    main()
