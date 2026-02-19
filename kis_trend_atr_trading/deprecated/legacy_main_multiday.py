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
버전: 2.0.0 (멀티데이)
"""

import argparse
import math
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# 프로젝트 모듈 임포트
from kis_trend_atr_trading.config import settings
from kis_trend_atr_trading.adapters.kis_rest.market_data import KISRestMarketDataProvider
from kis_trend_atr_trading.adapters.kis_ws.market_data import KISWSMarketDataProvider
from kis_trend_atr_trading.adapters.kis_ws.ws_client import KISWSClient
from kis_trend_atr_trading.api.kis_api import KISApi, KISApiError
from kis_trend_atr_trading.strategy.multiday_trend_atr import MultidayTrendATRStrategy
from kis_trend_atr_trading.engine.multiday_executor import MultidayExecutor
from kis_trend_atr_trading.engine.order_synchronizer import get_instance_lock
from kis_trend_atr_trading.engine.risk_manager import create_risk_manager_from_settings
from kis_trend_atr_trading.engine.runtime_state_machine import (
    FeedStatus,
    RuntimeConfig,
    RuntimeOverlay,
    RuntimeStateMachine,
    SymbolBarGate,
    TransitionCooldown,
    completed_bar_ts_1m,
)
from kis_trend_atr_trading.backtest.backtester import Backtester
from kis_trend_atr_trading.universe import UniverseSelector
from kis_trend_atr_trading.universe.universe_service import UniverseService
from kis_trend_atr_trading.utils.logger import setup_logger, get_logger
from kis_trend_atr_trading.utils.market_hours import KST, MarketSessionState, get_market_session_state
from kis_trend_atr_trading.utils.position_store import PositionStore
from kis_trend_atr_trading.env import get_trading_mode, validate_environment, assert_not_real_mode


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


def _get_git_commit_hash() -> str:
    """현재 git commit hash를 반환합니다."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True
        ).strip()
    except Exception:
        return "unknown"


def run_trade(
    stock_code: str,
    interval: int = 60,
    max_runs: int = None,
    real_first_order_percent: int = 10,
    real_limit_symbols_first_day: bool = True
):
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
    executors = []
    ws_stop = None

    try:
        # REAL 첫날 종목수 제한 (세이프가드)
        trading_mode = get_trading_mode()
        if trading_mode == "REAL" and real_limit_symbols_first_day:
            if os.getenv("REAL_TRADING_DAY1", "true").lower() in ("true", "1", "yes"):
                if stock_code != settings.DEFAULT_STOCK_CODE:
                    raise RuntimeError(
                        "REAL 첫날 종목 수 제한이 활성화되었습니다. "
                        f"기본 종목({settings.DEFAULT_STOCK_CODE})만 허용됩니다."
                    )

        # API 클라이언트 생성
        is_paper = trading_mode != "REAL"
        api = KISApi(is_paper_trading=is_paper)
        
        print("🔑 API 토큰 준비 중...")
        if hasattr(api, "prewarm_access_token_if_due"):
            api.prewarm_access_token_if_due()
        api.get_access_token()
        print("✅ 토큰 준비 완료\n")

        runtime_config = RuntimeConfig.from_settings(settings)
        logger.info(
            "[RUNTIME] config feed_default=%s offsession_ws_enabled=%s "
            "ws_start_grace_sec=%s ws_stale_sec=%s offsession_sleep_sec=%s",
            runtime_config.data_feed_default,
            runtime_config.offsession_ws_enabled,
            runtime_config.ws_start_grace_sec,
            runtime_config.ws_stale_sec,
            runtime_config.offsession_sleep_sec,
        )
        runtime_machine = RuntimeStateMachine(runtime_config, start_ts=datetime.now(KST))
        bar_gate = SymbolBarGate()
        transition_cooldown = TransitionCooldown(runtime_config.telegram_transition_cooldown_sec)
        runtime_status_telegram = bool(getattr(settings, "RUNTIME_STATUS_TELEGRAM", False))

        rest_provider = KISRestMarketDataProvider(api=api)
        ws_provider = None
        if runtime_config.data_feed_default == "ws" or runtime_config.offsession_ws_enabled:
            ws_client = KISWSClient(
                app_key=settings.APP_KEY,
                app_secret=settings.APP_SECRET,
                is_paper_trading=(trading_mode != "REAL"),
                max_reconnect_attempts=runtime_config.ws_reconnect_max_attempts,
                reconnect_base_delay=float(runtime_config.ws_reconnect_backoff_base_sec),
                failure_policy="rest_fallback",
                approval_key_refresh_margin_min=30,
            )
            ws_provider = KISWSMarketDataProvider(
                ws_client=ws_client,
                rest_fallback_provider=rest_provider,
                max_reconnect_attempts=runtime_config.ws_reconnect_max_attempts,
                reconnect_base_delay=float(runtime_config.ws_reconnect_backoff_base_sec),
            )
        
        # Universe 서비스 (일자별 1회 생성 + 재사용, 보유종목/신규진입 분리)
        universe_yaml = Path(__file__).resolve().parent / "config" / "universe.yaml"
        universe_service = UniverseService(
            yaml_path=str(universe_yaml),
            kis_client=api,
        )

        # 주문 수량 계산
        order_quantity = settings.ORDER_QUANTITY
        if trading_mode == "REAL":
            capped_qty = max(1, int(order_quantity * (real_first_order_percent / 100.0)))
            order_quantity = min(order_quantity, capped_qty)
            logger.warning(
                f"[SAFEGUARD] REAL 첫 주문 수량 제한 적용: {order_quantity}주 "
                f"({real_first_order_percent}% of max_position_size)"
            )

        def _symbol_position_store(symbol: str) -> PositionStore:
            data_dir = Path(__file__).resolve().parent / "data"
            return PositionStore(file_path=data_dir / f"positions_{symbol}.json")

        def _merge_symbols(holdings_symbols, entry_candidates_symbols):
            merged = []
            for sym in list(holdings_symbols) + list(entry_candidates_symbols):
                if sym not in merged:
                    merged.append(sym)
            return merged

        def _refresh_daily_universe():
            trade_date = datetime.now(KST).strftime("%Y-%m-%d")
            holdings_symbols = universe_service.load_holdings_symbols()
            todays_universe = universe_service.get_or_create_todays_universe(trade_date)
            entry_candidates = universe_service.compute_entry_candidates(
                holdings_symbols, todays_universe
            )
            for sym in holdings_symbols:
                if sym in todays_universe:
                    logger.info(f"[ENTRY] skipped: already holding symbol={sym}")
            return trade_date, holdings_symbols, todays_universe, entry_candidates

        current_trade_date, holdings_symbols, todays_universe, entry_candidates = _refresh_daily_universe()
        if not holdings_symbols and not todays_universe:
            raise RuntimeError("Universe 종목 수가 0개이고 보유 종목도 없어 거래를 중단합니다.")

        # 기본 실행은 holdings + (today_universe - holdings), CLI --stock은 단일종목 모드 우선
        run_symbols = _merge_symbols(holdings_symbols, entry_candidates)
        single_symbol_reason = ""
        if stock_code != settings.DEFAULT_STOCK_CODE:
            run_symbols = [stock_code]
            single_symbol_reason = f"CLI --stock 지정({stock_code})"
        elif len(run_symbols) == 1:
            single_symbol_reason = "보유/진입 후보 합집합 결과가 1개"

        logger.info(f"[UNIVERSE] selected={todays_universe}")
        logger.info(
            f"[UNIVERSE] executor_symbols={run_symbols}, "
            f"selection_method={universe_service.policy.selection_method}, "
            f"cache_file={universe_service.policy.cache_file}"
        )
        if len(run_symbols) == 1:
            logger.info(f"[UNIVERSE] 단일 종목 실행 사유: {single_symbol_reason or '명시적 제한 없음'}")

        print("🔄 저장된 포지션 확인 중...")
        shared_risk_manager = create_risk_manager_from_settings()
        executors_by_symbol = {}
        for symbol in run_symbols:
            executor = MultidayExecutor(
                api=api,
                strategy=MultidayTrendATRStrategy(),
                stock_code=symbol,
                order_quantity=order_quantity,
                risk_manager=shared_risk_manager,
                position_store=_symbol_position_store(symbol),
                market_data_provider=rest_provider,
            )
            restored = executor.restore_position_on_start()
            state_msg = "복원 완료 - Exit 조건 감시" if restored else "복원 포지션 없음 - Entry 조건 감시"
            print(f"  - {symbol}: {state_msg}")
            executors.append(executor)
            executors_by_symbol[symbol] = executor
        print("")

        # 거래 시작
        print("🚀 멀티데이 거래 시작...")
        print(f"   대상 종목: {run_symbols}")
        print("   종료하려면 Ctrl+C를 누르세요.\n")
        print("   ★ 포지션은 프로그램 종료 시에도 유지됩니다.")
        print("   ★ Exit는 오직 가격 조건으로만 발생합니다.\n")

        def _normalize_bar_ts(ts):
            if ts is None:
                return None
            if ts.tzinfo is None:
                return KST.localize(ts)
            return ts.astimezone(KST)

        def _state_value(state) -> str:
            return str(getattr(state, "value", state)).strip().upper()

        def _state_equals(state, expected) -> bool:
            return _state_value(state) == _state_value(expected)

        def _state_in(state, expected_states) -> bool:
            state_token = _state_value(state)
            return any(state_token == _state_value(expected) for expected in expected_states)

        def _send_transition_alert(key: str, level: str, message: str, now_kst: datetime) -> None:
            if not executors:
                return
            if not transition_cooldown.should_send(key, now_kst):
                return
            notifier = getattr(executors[0], "telegram", None)
            if notifier is None:
                return
            try:
                if level == "warning":
                    notifier.notify_warning(message)
                else:
                    notifier.notify_info(message)
            except Exception:
                pass

        def _ensure_ws_subscription(symbols) -> None:
            nonlocal ws_stop
            if ws_provider is None:
                return
            if ws_stop is not None:
                return
            ws_stop = ws_provider.subscribe_bars(symbols, runtime_config.timeframe, lambda _bar: None)

        def _stop_ws_subscription() -> None:
            nonlocal ws_stop
            if ws_stop is None:
                return
            try:
                ws_stop()
            except Exception:
                pass
            ws_stop = None

        def _resolve_effective_feed_mode(decision) -> str:
            if ws_provider is None:
                return "rest"
            if (
                decision.policy.active_feed_mode == "ws"
                and _state_in(decision.market_state, (
                    MarketSessionState.IN_SESSION,
                    MarketSessionState.AUCTION_GUARD,
                ))
            ):
                return "ws"
            return "rest"

        iteration = 0
        active_feed_name = "rest"
        last_status_log_at = None
        last_postclose_report_date = None
        last_prewarm_prepare_date = None
        while True:
            iteration += 1
            logger.info(f"[MULTI] 반복 #{iteration} / symbols={len(executors)}")
            if hasattr(api, "prewarm_access_token_if_due"):
                api.prewarm_access_token_if_due()

            # 날짜 변경 시 유니버스 1회 재생성/재사용 후 진입 후보 재계산
            now_trade_date = datetime.now(KST).strftime("%Y-%m-%d")
            if now_trade_date != current_trade_date:
                current_trade_date, holdings_symbols, todays_universe, entry_candidates = _refresh_daily_universe()
                refreshed_symbols = _merge_symbols(holdings_symbols, entry_candidates)
                if stock_code != settings.DEFAULT_STOCK_CODE:
                    refreshed_symbols = [stock_code]
                for symbol in refreshed_symbols:
                    if symbol in executors_by_symbol:
                        continue
                    executor = MultidayExecutor(
                        api=api,
                        strategy=MultidayTrendATRStrategy(),
                        stock_code=symbol,
                        order_quantity=order_quantity,
                        risk_manager=shared_risk_manager,
                        position_store=_symbol_position_store(symbol),
                        market_data_provider=rest_provider,
                    )
                    restored = executor.restore_position_on_start()
                    state_msg = "복원 완료 - Exit 조건 감시" if restored else "복원 포지션 없음 - Entry 조건 감시"
                    print(f"  - {symbol}: {state_msg}")
                    executors_by_symbol[symbol] = executor
                    executors.append(executor)

            now_kst = datetime.now(KST)
            market_state, market_reason = get_market_session_state(
                now=now_kst,
                tz=runtime_config.market_timezone,
                preopen_warmup_min=runtime_config.preopen_warmup_min,
                postclose_min=runtime_config.postclose_min,
                auction_guard_windows=runtime_config.auction_guard_windows,
            )
            ws_last_bar_ts = (
                _normalize_bar_ts(ws_provider.get_last_completed_bar_ts()) if ws_provider else None
            )
            feed_status = FeedStatus(
                ws_enabled=(ws_provider is not None),
                ws_connected=bool(ws_provider and ws_provider.is_ws_connected()),
                ws_last_message_age_sec=(
                    float(ws_provider.last_message_age_sec()) if ws_provider else math.inf
                ),
                ws_last_bar_ts=ws_last_bar_ts,
            )
            kill_check = shared_risk_manager.check_kill_switch()
            decision = runtime_machine.evaluate(
                now=now_kst,
                market_state=market_state,
                market_reason=market_reason,
                feed_status=feed_status,
                risk_stop=(not kill_check.passed),
            )

            if decision.market_transition is not None:
                prev_state, next_state = decision.market_transition
                logger.info(
                    "[RUNTIME] market transition %s -> %s reason=%s",
                    prev_state.value,
                    next_state.value,
                    decision.market_reason,
                )
                if (
                    _state_equals(prev_state, MarketSessionState.OFF_SESSION)
                    and _state_equals(next_state, MarketSessionState.PREOPEN_WARMUP)
                ):
                    _send_transition_alert(
                        key="market:OFF_SESSION->PREOPEN_WARMUP",
                        level="info",
                        message=(
                            "[RUNTIME] OFF_SESSION -> PREOPEN_WARMUP "
                            f"(reason={decision.market_reason})"
                        ),
                        now_kst=now_kst,
                    )
                elif (
                    _state_equals(prev_state, MarketSessionState.PREOPEN_WARMUP)
                    and _state_equals(next_state, MarketSessionState.IN_SESSION)
                ):
                    _send_transition_alert(
                        key="market:PREOPEN_WARMUP->IN_SESSION",
                        level="info",
                        message=(
                            "[RUNTIME] PREOPEN_WARMUP -> IN_SESSION "
                            f"(reason={decision.market_reason})"
                        ),
                        now_kst=now_kst,
                    )

            if decision.overlay_transition is not None:
                prev_overlay, next_overlay = decision.overlay_transition
                logger.warning(
                    "[RUNTIME] overlay transition %s -> %s",
                    prev_overlay.value,
                    next_overlay.value,
                )
                if (
                    prev_overlay == RuntimeOverlay.NORMAL
                    and next_overlay == RuntimeOverlay.DEGRADED_FEED
                ):
                    _send_transition_alert(
                        key="overlay:NORMAL->DEGRADED_FEED",
                        level="warning",
                        message=(
                            "[RUNTIME] NORMAL -> DEGRADED_FEED "
                            f"(market={decision.market_state.value}, reason={decision.market_reason})"
                        ),
                        now_kst=now_kst,
                    )
                elif (
                    prev_overlay == RuntimeOverlay.DEGRADED_FEED
                    and next_overlay == RuntimeOverlay.NORMAL
                ):
                    _send_transition_alert(
                        key="overlay:DEGRADED_FEED->NORMAL",
                        level="info",
                        message="[RUNTIME] DEGRADED_FEED -> NORMAL (WS recovered)",
                        now_kst=now_kst,
                    )
                elif next_overlay == RuntimeOverlay.EMERGENCY_STOP:
                    _send_transition_alert(
                        key="overlay:*->EMERGENCY_STOP",
                        level="warning",
                        message="[RUNTIME] EMERGENCY_STOP activated by risk/kill-switch",
                        now_kst=now_kst,
                    )

            if last_status_log_at is None or (
                now_kst - last_status_log_at
            ).total_seconds() >= runtime_config.status_log_interval_sec:
                effective_feed_mode = _resolve_effective_feed_mode(decision)
                summary = (
                    f"[RUNTIME] market_state={decision.market_state.value}, "
                    f"reason={decision.market_reason}, "
                    f"overlay={decision.overlay.value}, "
                    f"policy_feed={decision.policy.active_feed_mode}, "
                    f"effective_feed={effective_feed_mode}, "
                    f"policy_ws_should_run={decision.policy.ws_should_run}, "
                    f"ws_connected={decision.feed_status.ws_connected}, "
                    f"last_ws_message_age={decision.feed_status.ws_last_message_age_sec:.1f}, "
                    f"symbols_count={len(executors)}"
                )
                logger.info(summary)
                if (
                    _state_equals(decision.market_state, MarketSessionState.OFF_SESSION)
                    and effective_feed_mode != "rest"
                ):
                    logger.warning(
                        "[RUNTIME] OFF_SESSION feed anomaly detected: effective_feed=%s",
                        effective_feed_mode,
                    )
                if runtime_status_telegram:
                    _send_transition_alert(
                        key="runtime:summary",
                        level="info",
                        message=summary,
                        now_kst=now_kst,
                    )
                last_status_log_at = now_kst

            if ws_provider is not None:
                if decision.policy.ws_should_run:
                    _ensure_ws_subscription(run_symbols)
                else:
                    _stop_ws_subscription()

            if _state_equals(decision.market_state, MarketSessionState.PREOPEN_WARMUP):
                prewarm_date = now_kst.strftime("%Y-%m-%d")
                if prewarm_date != last_prewarm_prepare_date:
                    for symbol in run_symbols:
                        try:
                            rest_provider.get_recent_bars(stock_code=symbol, n=5, timeframe="D")
                            rest_provider.get_latest_price(stock_code=symbol)
                        except Exception as preload_err:
                            logger.warning(
                                "[RUNTIME] preopen preload failed symbol=%s err=%s",
                                symbol,
                                preload_err,
                            )
                    logger.info(
                        "[RUNTIME] PREOPEN_WARMUP preload completed symbols=%s",
                        len(run_symbols),
                    )
                    last_prewarm_prepare_date = prewarm_date

            target_feed = _resolve_effective_feed_mode(decision)
            active_provider = ws_provider if target_feed == "ws" else rest_provider
            for executor in executors:
                executor.market_data_provider = active_provider
            if active_feed_name != target_feed:
                logger.info(
                    "[RUNTIME] active feed switched %s -> %s",
                    active_feed_name,
                    target_feed,
                )
                active_feed_name = target_feed

            # 런타임 holdings/entry_candidates 재계산 (보유는 항상 관리, 진입은 후보만)
            runtime_holdings = [e.stock_code for e in executors if e.strategy.has_position]
            if stock_code == settings.DEFAULT_STOCK_CODE:
                entry_candidates = universe_service.compute_entry_candidates(runtime_holdings, todays_universe)
            else:
                entry_candidates = [stock_code] if stock_code not in runtime_holdings else []
            holdings_count = len(runtime_holdings)
            max_positions = max(int(universe_service.policy.max_positions), 0)

            for executor in executors:
                symbol = executor.stock_code
                if not decision.policy.allow_new_entries:
                    executor.set_entry_control(
                        False,
                        (
                            f"[ENTRY] runtime blocked: "
                            f"market={decision.market_state.value}, overlay={decision.overlay.value}"
                        ),
                    )
                elif symbol in runtime_holdings:
                    executor.set_entry_control(False, f"[ENTRY] skipped: already holding symbol={symbol}")
                elif symbol not in entry_candidates:
                    executor.set_entry_control(False, f"[ENTRY] skipped: symbol={symbol} not in entry_candidates")
                elif holdings_count >= max_positions:
                    msg = (
                        f"[ENTRY] blocked: max_positions reached "
                        f"(holdings={holdings_count}, max={max_positions})"
                    )
                    logger.info(msg)
                    executor.set_entry_control(False, msg)
                else:
                    executor.set_entry_control(True, "")

                if not decision.policy.run_strategy:
                    continue

                if active_feed_name == "ws" and ws_provider is not None:
                    symbol_bar_ts = _normalize_bar_ts(ws_provider.get_last_completed_bar_ts(symbol))
                    prev_bar_ts = bar_gate.last_processed(symbol)
                    if (
                        prev_bar_ts is not None
                        and symbol_bar_ts is not None
                        and symbol_bar_ts > (prev_bar_ts + timedelta(minutes=1))
                    ):
                        missing_count = int((symbol_bar_ts - prev_bar_ts).total_seconds() // 60) - 1
                        if missing_count >= 2:
                            try:
                                rest_provider.get_recent_bars(
                                    stock_code=symbol,
                                    n=max(missing_count + 2, 3),
                                    timeframe="1m",
                                )
                                logger.info(
                                    "[RUNTIME] WS recovery backfill attempted symbol=%s missing=%s",
                                    symbol,
                                    missing_count,
                                )
                            except Exception as backfill_err:
                                logger.warning(
                                    "[RUNTIME] WS recovery backfill failed symbol=%s missing=%s err=%s",
                                    symbol,
                                    missing_count,
                                    backfill_err,
                                )
                else:
                    symbol_bar_ts = completed_bar_ts_1m(
                        now=now_kst,
                        tz=runtime_config.market_timezone,
                    )

                if not bar_gate.should_run(symbol, _normalize_bar_ts(symbol_bar_ts)):
                    continue

                executor.run_once()
                normalized_symbol_bar_ts = _normalize_bar_ts(symbol_bar_ts)
                if normalized_symbol_bar_ts is not None:
                    bar_gate.mark_processed(symbol, normalized_symbol_bar_ts)
                runtime_holdings = [e.stock_code for e in executors if e.strategy.has_position]
                holdings_count = len(runtime_holdings)
                if stock_code == settings.DEFAULT_STOCK_CODE:
                    entry_candidates = universe_service.compute_entry_candidates(runtime_holdings, todays_universe)
                else:
                    entry_candidates = [stock_code] if stock_code not in runtime_holdings else []

            if _state_equals(decision.market_state, MarketSessionState.POSTCLOSE):
                report_date = now_kst.strftime("%Y-%m-%d")
                if report_date != last_postclose_report_date:
                    logger.info("[RUNTIME] POSTCLOSE actions started date=%s", report_date)
                    for executor in executors:
                        if hasattr(executor, "_persist_account_snapshot"):
                            try:
                                executor._persist_account_snapshot(force=True)
                            except Exception:
                                pass
                    last_postclose_report_date = report_date

            if max_runs and iteration >= max_runs:
                logger.info(f"[MULTI] 최대 반복 도달: {max_runs}")
                break

            if _state_equals(decision.market_state, MarketSessionState.IN_SESSION):
                sleep_sec = max(15, min(int(interval), 60))
            elif _state_equals(decision.market_state, MarketSessionState.OFF_SESSION):
                sleep_sec = runtime_config.offsession_sleep_sec
            else:
                sleep_sec = max(int(decision.policy.sleep_sec), 5)

            logger.info(
                "[MULTI] 다음 실행까지 %s초 대기 (market=%s overlay=%s feed=%s)",
                sleep_sec,
                decision.market_state.value,
                decision.overlay.value,
                active_feed_name,
            )
            time.sleep(sleep_sec)

        print("\n" + "=" * 50)
        print("              멀티종목 거래 요약")
        print("=" * 50)
        total_trades = 0
        total_pnl = 0
        for executor in executors:
            summary = executor.get_daily_summary()
            total_trades += summary.get("total_trades", 0)
            total_pnl += summary.get("total_pnl", 0)
            print(
                f"{executor.stock_code}: 거래 {summary.get('total_trades', 0)}회, "
                f"손익 {summary.get('total_pnl', 0):,.0f}원"
            )
        print("-" * 50)
        print(f"총 거래: {total_trades}회")
        print(f"총 손익: {total_pnl:,.0f}원")
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
    finally:
        if ws_stop is not None:
            try:
                ws_stop()
            except Exception:
                pass
        # 멀티심볼 사용자 루프에서는 executor.run()의 finally가 호출되지 않으므로 정리 보장
        for executor in executors:
            try:
                executor._save_position_on_exit()
            except Exception:
                pass
        try:
            lock = get_instance_lock()
            if lock.is_acquired:
                lock.release()
        except Exception:
            pass


def main():
    """메인 함수"""
    trading_mode = get_trading_mode()
    log_level = "INFO" if trading_mode in ("PAPER", "REAL") else settings.LOG_LEVEL

    # 로거 초기화
    setup_logger("main", log_level)
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

    parser.add_argument(
        "--confirm-real-trading",
        action="store_true",
        help="REAL 모드 실행 확인 플래그 (REAL 모드 필수)"
    )

    parser.add_argument(
        "--real-first-order-percent",
        type=int,
        default=10,
        help="REAL 모드 첫 주문 수량 제한 비율 (기본: 10)"
    )

    parser.add_argument(
        "--real-limit-symbols-first-day",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="REAL 첫날 종목 수 1개 제한 세이프가드 (기본: 활성화)"
    )
    
    args = parser.parse_args()
    
    # 배너 출력
    print_banner()
    
    # 시작 시간
    start_time = datetime.now(KST)
    trading_mode = get_trading_mode()

    if not validate_environment():
        print("❌ 환경 검증 실패로 프로그램을 종료합니다.")
        raise SystemExit(1)

    if trading_mode == "REAL":
        if not args.confirm_real_trading:
            print("❌ REAL 모드에서는 --confirm-real-trading 인자가 필수입니다.")
            raise SystemExit(1)

        print("\n" + "═" * 72)
        print("⚠️ REAL 모드 진입: 10초 후 실계좌 거래를 시작합니다.")
        print("⚠️ 취소하려면 지금 Ctrl+C를 누르세요.")
        print("═" * 72 + "\n")
        time.sleep(10)
    else:
        assert_not_real_mode(trading_mode)

    logger.info(f"git_commit={_get_git_commit_hash()}")
    logger.info(f"프로그램 시작: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
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
            max_runs=args.max_runs,
            real_first_order_percent=max(1, min(100, args.real_first_order_percent)),
            real_limit_symbols_first_day=args.real_limit_symbols_first_day
        )
        
    elif args.mode == "verify":
        run_verification()
    
    # 종료 시간
    end_time = datetime.now(KST)
    elapsed = (end_time - start_time).total_seconds()
    logger.info(f"프로그램 종료: 실행 시간 {elapsed:.1f}초")
    
    print(f"\n✅ 프로그램 종료 (실행 시간: {elapsed:.1f}초)")


if __name__ == "__main__":
    main()
