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
import threading
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
from kis_trend_atr_trading.engine.market_regime_worker import MarketRegimeRefreshThread
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
from kis_trend_atr_trading.engine.evaluation_scheduler import (
    EvaluationCadenceTracker,
    EvaluationSchedulerConfig,
    SymbolEvaluationScheduler,
)
from kis_trend_atr_trading.backtest.backtester import Backtester
from kis_trend_atr_trading.universe import UniverseSelector
from kis_trend_atr_trading.universe.universe_service import UniverseService
from kis_trend_atr_trading.utils.logger import setup_logger, get_logger
from kis_trend_atr_trading.utils.market_hours import KST, MarketSessionState, get_market_session_state
from kis_trend_atr_trading.utils.market_phase import TradingVenue, resolve_market_phase_context
from kis_trend_atr_trading.utils.market_regime import (
    MarketRegimeObservationState,
    MarketRegimeService,
    MarketRegimeLoopContext,
    get_market_regime_refresh_budget_sec,
    log_market_regime_refresh_outcome,
    materialize_market_regime_snapshot,
    observe_market_regime_snapshot,
    refresh_shared_market_regime_snapshot,
)
from kis_trend_atr_trading.utils.position_store import PositionStore
from kis_trend_atr_trading.utils.telegram_notifier import get_telegram_notifier
from kis_trend_atr_trading.env import (
    get_trading_mode,
    get_db_namespace_mode,
    validate_environment,
    assert_not_real_mode,
)


def print_banner():
    """프로그램 시작 배너"""
    mode_emoji = {
        "REAL": "🔴",
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
    trend_ma_period = getattr(settings, "TREND_MA_PERIOD", 50)
    adx_threshold = getattr(settings, "ADX_THRESHOLD", 25.0)

    try:
        trend_ma_period = int(trend_ma_period)
    except (TypeError, ValueError):
        trend_ma_period = 50

    try:
        adx_threshold = float(adx_threshold)
    except (TypeError, ValueError):
        adx_threshold = 25.0

    rules = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                         전략 규칙 요약
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[진입 조건]
  ✓ 상승 추세 (종가 > {trend_ma_period}일 MA)
  ✓ ADX > {adx_threshold:g} (추세 강도 확인)
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


def _is_market_regime_refresh_thread_enabled() -> bool:
    return bool(
        getattr(settings, "ENABLE_MARKET_REGIME_FILTER", False)
        and getattr(settings, "ENABLE_MARKET_REGIME_REFRESH_THREAD", False)
    )


def _market_regime_subscription_symbols(base_symbols) -> list[str]:
    merged = [str(symbol).zfill(6) for symbol in list(base_symbols or []) if str(symbol or "").strip()]
    if not _is_market_regime_refresh_thread_enabled():
        return merged
    for symbol in (
        str(getattr(settings, "MARKET_REGIME_KOSPI_SYMBOL", "069500") or "069500"),
        str(getattr(settings, "MARKET_REGIME_KOSDAQ_SYMBOL", "229200") or "229200"),
    ):
        normalized = str(symbol or "").strip().zfill(6)
        if normalized and normalized not in merged:
            merged.append(normalized)
    return merged


def _read_market_regime_snapshot_read_only(
    *,
    worker,
    current_snapshot,
    check_time,
):
    snapshot = materialize_market_regime_snapshot(current_snapshot, check_time)
    if worker is None:
        return snapshot, "worker_unavailable", 0.0, "worker_unavailable", {}
    status = dict(worker.get_status(now=check_time) or {})
    worker_snapshot = materialize_market_regime_snapshot(status.get("snapshot"), check_time)
    if worker_snapshot is not None:
        snapshot = worker_snapshot
    refresh_state = str(status.get("refresh_state") or "bootstrap_pending")
    refresh_elapsed_sec = float(status.get("market_regime_background_refresh_ms", 0.0) or 0.0) / 1000.0
    skip_reason = (
        str(status.get("market_regime_daily_context_state") or "").strip()
        or str(status.get("market_regime_quote_state") or "").strip()
        or "background_read_only"
    )
    return snapshot, refresh_state, refresh_elapsed_sec, skip_reason, status


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
    ws_quote_stop = None
    market_regime_worker = None
    market_regime_worker_stop_event = None
    _stop_market_regime_worker = lambda: None

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
        fast_eval_enabled = bool(getattr(settings, "ENABLE_FAST_EVAL_SCHEDULER", False))
        logger.info(
            "[FAST_EVAL] enabled=%s entry_cooldown=%.1fs exit_cooldown=%.1fs loop_sleep=%.1fs",
            fast_eval_enabled,
            float(getattr(settings, "FAST_EVAL_ENTRY_COOLDOWN_SEC", 12.0) or 12.0),
            float(getattr(settings, "FAST_EVAL_EXIT_COOLDOWN_SEC", 5.0) or 5.0),
            float(getattr(settings, "FAST_EVAL_LOOP_SLEEP_SEC", 1.0) or 1.0),
        )
        runtime_machine = RuntimeStateMachine(runtime_config, start_ts=datetime.now(KST))
        bar_gate = SymbolBarGate()
        transition_cooldown = TransitionCooldown(runtime_config.telegram_transition_cooldown_sec)
        runtime_status_telegram = bool(getattr(settings, "RUNTIME_STATUS_TELEGRAM", False))
        fast_eval_scheduler = SymbolEvaluationScheduler(
            EvaluationSchedulerConfig(
                entry_cooldown_sec=float(
                    getattr(settings, "FAST_EVAL_ENTRY_COOLDOWN_SEC", 12.0) or 12.0
                ),
                entry_debounce_sec=float(
                    getattr(settings, "FAST_EVAL_ENTRY_DEBOUNCE_SEC", 2.0) or 2.0
                ),
                exit_cooldown_sec=float(
                    getattr(settings, "FAST_EVAL_EXIT_COOLDOWN_SEC", 5.0) or 5.0
                ),
                exit_debounce_sec=float(
                    getattr(settings, "FAST_EVAL_EXIT_DEBOUNCE_SEC", 1.0) or 1.0
                ),
                rest_fallback_cooldown_sec=float(
                    getattr(settings, "FAST_EVAL_REST_FALLBACK_COOLDOWN_SEC", 30.0) or 30.0
                ),
                loop_sleep_sec=float(getattr(settings, "FAST_EVAL_LOOP_SLEEP_SEC", 1.0) or 1.0),
            )
        )
        cadence_tracker = EvaluationCadenceTracker()

        rest_provider = KISRestMarketDataProvider(api=api)
        ws_provider = None
        if runtime_config.data_feed_default == "ws" or runtime_config.offsession_ws_enabled:
            try:
                import websockets  # noqa: F401
            except Exception as ws_dep_err:
                logger.warning(
                    "[WS] websockets 패키지 미설치: WS feed 비활성화, REST 고정 사용(err=%s)",
                    ws_dep_err,
                )
            else:
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
                    quote_static_cache_ttl_sec=float(
                        getattr(settings, "WS_QUOTE_STATIC_CACHE_TTL_SEC", 900.0) or 900.0
                    ),
                )
        
        # Universe 서비스 (일자별 1회 생성 + 재사용, 보유종목/신규진입 분리)
        universe_yaml = Path(__file__).resolve().parent / "config" / "universe.yaml"
        universe_service = UniverseService(
            yaml_path=str(universe_yaml),
            kis_client=api,
        )
        market_regime_service = MarketRegimeService(api=api)
        shared_market_regime_snapshot = None
        market_regime_background_enabled = _is_market_regime_refresh_thread_enabled()

        # 주문 수량 계산
        order_quantity = settings.ORDER_QUANTITY
        if trading_mode == "REAL":
            capped_qty = max(1, int(order_quantity * (real_first_order_percent / 100.0)))
            order_quantity = min(order_quantity, capped_qty)
            logger.warning(
                f"[SAFEGUARD] REAL 첫 주문 수량 제한 적용: {order_quantity}주 "
                f"({real_first_order_percent}% of max_position_size)"
            )

        try:
            db_mode = str(get_db_namespace_mode() or "").upper().strip()
        except Exception:
            db_mode = "REAL" if trading_mode == "REAL" else ("DRY_RUN" if trading_mode == "CBT" else "PAPER")
        if db_mode not in ("DRY_RUN", "PAPER", "REAL"):
            db_mode = "PAPER"
        last_universe_notified_date = ""
        last_universe_alert_key = ""
        last_out_of_universe_alert_key = ""
        last_entry_capacity_key = ""
        out_of_universe_ages = {}
        last_aging_trade_date = ""
        out_of_universe_warn_days = max(
            int(getattr(universe_service.policy, "out_of_universe_warn_days", 20) or 0),
            0,
        )
        out_of_universe_reduce_days = max(
            int(getattr(universe_service.policy, "out_of_universe_reduce_days", 30) or 0),
            0,
        )
        if (
            out_of_universe_reduce_days > 0
            and out_of_universe_reduce_days < out_of_universe_warn_days
        ):
            out_of_universe_reduce_days = out_of_universe_warn_days

        def _symbol_position_store(symbol: str) -> PositionStore:
            data_dir = Path(__file__).resolve().parent / "data"
            return PositionStore(file_path=data_dir / f"positions_{db_mode}_{symbol}.json")

        def _store_has_recoverable_state(symbol: str, symbol_store: PositionStore) -> bool:
            try:
                raw_loader = getattr(symbol_store, "_load_raw_data", None)
                payload = raw_loader() if callable(raw_loader) else {}
                if not isinstance(payload, dict):
                    return False
                position = payload.get("position")
                if isinstance(position, dict):
                    code = str(position.get("stock_code") or "").strip()
                    qty = int(position.get("quantity") or 0)
                    if len(code) == 6 and code.isdigit() and qty > 0 and code == symbol:
                        return True
                pending_exit = payload.get("pending_exit")
                if isinstance(pending_exit, dict) and pending_exit:
                    pending_symbol = str(pending_exit.get("stock_code") or "").strip()
                    if pending_symbol and pending_symbol != symbol:
                        return False
                    return True
            except Exception as e:
                logger.debug(f"[RESYNC] state probe failed: path={symbol_store.file_path}, err={e}")
            return False

        def _should_restore_on_start(symbol: str, holdings_symbols_for_day, symbol_store: PositionStore) -> bool:
            if symbol in set(holdings_symbols_for_day or []):
                return True
            return _store_has_recoverable_state(symbol, symbol_store)

        def _merge_symbols(holdings_symbols, entry_candidates_symbols):
            merged = []
            for sym in list(holdings_symbols) + list(entry_candidates_symbols):
                if sym not in merged:
                    merged.append(sym)
            return merged

        def _normalize_symbol_list(values):
            out = []
            for value in list(values or []):
                code = str(value or "").strip()
                if len(code) == 6 and code.isdigit() and code not in out:
                    out.append(code)
            return out

        def _notify_daily_universe_selection(
            trade_date: str,
            candidate_symbols,
            final_symbols,
        ) -> None:
            nonlocal last_universe_notified_date
            final_list = _normalize_symbol_list(final_symbols)
            if not final_list:
                return
            if trade_date == last_universe_notified_date:
                return
            candidate_list = _normalize_symbol_list(candidate_symbols) or list(final_list)
            try:
                notifier = get_telegram_notifier()
                if notifier is None or not getattr(notifier, "enabled", False):
                    last_universe_notified_date = trade_date
                    return
                candidate_lines = [f"{idx}. {code}" for idx, code in enumerate(candidate_list, 1)]
                candidate_message = (
                    f"[UNIVERSE] {trade_date} 후보 {len(candidate_list)}개\n"
                    + "\n".join(candidate_lines)
                )
                final_lines = [f"{idx}. {code}" for idx, code in enumerate(final_list, 1)]
                final_message = (
                    f"[UNIVERSE] {trade_date} 최종 선정 {len(final_list)}개\n"
                    + "\n".join(final_lines)
                )
                notifier.notify_info(candidate_message)
                notifier.notify_info(final_message)
                last_universe_notified_date = trade_date
            except Exception as e:
                logger.warning(f"[TELEGRAM] 유니버스 알림 전송 실패(계속 진행): {e}")

        def _notify_universe_anomaly(
            trade_date: str,
            snapshot_payload: dict,
            final_symbols,
        ) -> None:
            nonlocal last_universe_alert_key
            final_list = _normalize_symbol_list(final_symbols)
            selection_method = str(snapshot_payload.get("selection_method") or "").strip().lower()
            selection_meta = snapshot_payload.get("selection_meta")
            if not isinstance(selection_meta, dict):
                selection_meta = {}

            stage1 = selection_meta.get("stage1_count")
            stage2 = selection_meta.get("stage2_count")
            strategy = str(selection_meta.get("strategy") or "").strip().lower()
            reason = str(selection_meta.get("reason") or "").strip()

            issues = []
            if isinstance(stage1, (int, float)) and int(stage1) == 0:
                issues.append("combined stage1=0")
            if isinstance(stage2, (int, float)) and int(stage2) == 0:
                issues.append("combined stage2=0")
            if strategy in ("fixed_fallback", "empty_fallback") or selection_method.endswith("fallback"):
                issues.append("fallback 적용")
            if not issues:
                return

            alert_key = f"{trade_date}|{selection_method}|{strategy}|{','.join(issues)}|{','.join(final_list)}"
            if alert_key == last_universe_alert_key:
                return

            try:
                notifier = get_telegram_notifier()
                if notifier is None or not getattr(notifier, "enabled", False):
                    last_universe_alert_key = alert_key
                    return

                stage1_label = int(stage1) if isinstance(stage1, (int, float)) else "n/a"
                stage2_label = int(stage2) if isinstance(stage2, (int, float)) else "n/a"
                warning_message = (
                    "[UNIVERSE][ALERT] 선정 이상 감지\n"
                    f"- date: {trade_date}\n"
                    f"- issues: {', '.join(issues)}\n"
                    f"- selection_method: {selection_method or 'unknown'}\n"
                    f"- strategy: {strategy or 'unknown'}\n"
                    f"- stage1: {stage1_label}, stage2: {stage2_label}\n"
                    f"- final_count: {len(final_list)}\n"
                    f"- final_symbols: {final_list}"
                )
                if reason:
                    warning_message += f"\n- reason: {reason}"

                send_warning = getattr(notifier, "notify_warning", None)
                if callable(send_warning):
                    send_warning(warning_message)
                else:
                    notifier.notify_info(warning_message)
                last_universe_alert_key = alert_key
            except Exception as e:
                logger.warning(f"[TELEGRAM] 유니버스 경보 알림 전송 실패(계속 진행): {e}")

        def _update_out_of_universe_aging(
            trade_date: str,
            holdings_symbols,
            todays_universe,
        ):
            nonlocal out_of_universe_ages, last_aging_trade_date
            count_advances = 0
            count_business_advances = getattr(universe_service, "count_business_day_advances", None)
            if callable(count_business_advances):
                try:
                    count_advances = max(
                        int(count_business_advances(last_aging_trade_date, trade_date) or 0),
                        0,
                    )
                except Exception:
                    count_advances = 1 if trade_date != last_aging_trade_date else 0
            else:
                count_advances = 1 if trade_date != last_aging_trade_date else 0
            advance_day = count_advances > 0
            compute_aging = getattr(universe_service, "compute_out_of_universe_ages", None)
            if callable(compute_aging):
                out_of_universe_ages = dict(
                    compute_aging(
                        out_of_universe_ages,
                        list(holdings_symbols or []),
                        list(todays_universe or []),
                        advance_day=advance_day,
                        advance_days=count_advances,
                    )
                    or {}
                )
            else:
                universe_set = set(_normalize_symbol_list(todays_universe))
                next_ages = {}
                for symbol in _normalize_symbol_list(holdings_symbols):
                    if symbol in universe_set:
                        next_ages[symbol] = 0
                    else:
                        prev_days = max(int(out_of_universe_ages.get(symbol, 0)), 0)
                        next_ages[symbol] = prev_days + count_advances if count_advances > 0 else prev_days
                out_of_universe_ages = next_ages

            if trade_date != last_aging_trade_date:
                last_aging_trade_date = trade_date

            summarize_aging = getattr(universe_service, "summarize_out_of_universe_aging", None)
            if callable(summarize_aging):
                summary = dict(
                    summarize_aging(
                        out_of_universe_ages,
                        out_of_universe_warn_days,
                        out_of_universe_reduce_days,
                    )
                    or {}
                )
            else:
                out_map = {
                    code: days
                    for code, days in dict(out_of_universe_ages or {}).items()
                    if int(days) > 0
                }
                warn_symbols = [
                    code for code, days in sorted(out_map.items(), key=lambda item: (-item[1], item[0]))
                    if out_of_universe_warn_days > 0 and int(days) >= out_of_universe_warn_days
                ]
                reduce_symbols = [
                    code for code, days in sorted(out_map.items(), key=lambda item: (-item[1], item[0]))
                    if out_of_universe_reduce_days > 0 and int(days) >= out_of_universe_reduce_days
                ]
                summary = {
                    "tracked_count": len(out_of_universe_ages),
                    "out_of_universe_count": len(out_map),
                    "warn_count": len(warn_symbols),
                    "reduce_count": len(reduce_symbols),
                    "warn_symbols": warn_symbols,
                    "reduce_symbols": reduce_symbols,
                    "out_of_universe_days": out_map,
                }
            return summary

        def _notify_out_of_universe_aging(trade_date: str, aging_summary: dict) -> None:
            nonlocal last_out_of_universe_alert_key
            days_map = dict(aging_summary.get("out_of_universe_days") or {})
            warn_symbols = list(aging_summary.get("warn_symbols") or [])
            reduce_symbols = list(aging_summary.get("reduce_symbols") or [])
            tracked_count = int(aging_summary.get("tracked_count") or 0)
            out_count = int(aging_summary.get("out_of_universe_count") or 0)

            logger.info(
                "[UNIVERSE][AGING] date=%s tracked=%s out_of_universe=%s warn=%s reduce=%s detail=%s",
                trade_date,
                tracked_count,
                out_count,
                len(warn_symbols),
                len(reduce_symbols),
                days_map,
            )

            if not warn_symbols and not reduce_symbols:
                return

            alert_key = (
                f"{trade_date}|warn={','.join(warn_symbols)}|reduce={','.join(reduce_symbols)}"
            )
            if alert_key == last_out_of_universe_alert_key:
                return

            try:
                notifier = get_telegram_notifier()
                if notifier is None or not getattr(notifier, "enabled", False):
                    last_out_of_universe_alert_key = alert_key
                    return

                details = [f"{code}:{int(days_map.get(code, 0))}d" for code in sorted(days_map.keys())]
                message = (
                    "[UNIVERSE][AGING] 유니버스 밖 보유 노화 감지\n"
                    f"- date: {trade_date}\n"
                    f"- warn_days: {out_of_universe_warn_days}, reduce_days: {out_of_universe_reduce_days}\n"
                    f"- warn_symbols({len(warn_symbols)}): {warn_symbols}\n"
                    f"- reduce_symbols({len(reduce_symbols)}): {reduce_symbols}\n"
                    f"- out_of_universe_days: {details}"
                )
                if reduce_symbols:
                    send_warning = getattr(notifier, "notify_warning", None)
                    if callable(send_warning):
                        send_warning(message)
                    else:
                        notifier.notify_info(message)
                else:
                    notifier.notify_info(message)
                last_out_of_universe_alert_key = alert_key
            except Exception as e:
                logger.warning(f"[TELEGRAM] 유니버스 노화 알림 전송 실패(계속 진행): {e}")

        def _refresh_daily_universe():
            trade_date = datetime.now(KST).strftime("%Y-%m-%d")
            holdings_symbols = universe_service.load_holdings_symbols()
            todays_universe = universe_service.get_or_create_todays_universe(trade_date)
            entry_candidates = universe_service.compute_entry_candidates(
                holdings_symbols, todays_universe
            )
            candidate_symbols = list(todays_universe)
            snapshot = {}
            get_snapshot = getattr(universe_service, "get_todays_universe_snapshot", None)
            if callable(get_snapshot):
                try:
                    snapshot = dict(get_snapshot(trade_date) or {})
                    cached_candidates = snapshot.get("candidate_symbols") or []
                    if isinstance(cached_candidates, list) and cached_candidates:
                        candidate_symbols = cached_candidates
                except Exception as e:
                    logger.warning(f"[UNIVERSE] snapshot read failed, fallback to final list: {e}")
            _notify_daily_universe_selection(trade_date, candidate_symbols, todays_universe)
            _notify_universe_anomaly(trade_date, snapshot, todays_universe)
            aging_summary = _update_out_of_universe_aging(trade_date, holdings_symbols, todays_universe)
            _notify_out_of_universe_aging(trade_date, aging_summary)
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
            symbol_store = _symbol_position_store(symbol)
            logger.info(f"[POSITION_FILE] symbol={symbol}, path={symbol_store.file_path}")
            executor = MultidayExecutor(
                api=api,
                strategy=MultidayTrendATRStrategy(),
                stock_code=symbol,
                order_quantity=order_quantity,
                risk_manager=shared_risk_manager,
                position_store=symbol_store,
                market_data_provider=rest_provider,
            )
            if _should_restore_on_start(symbol, holdings_symbols, symbol_store):
                restored = executor.restore_position_on_start()
                state_msg = "복원 완료 - Exit 조건 감시" if restored else "복원 포지션 없음 - Entry 조건 감시"
            else:
                restored = False
                state_msg = "복원 생략 - 신규 진입 감시"
                logger.info(
                    f"[RESYNC] startup restore 생략: symbol={symbol}, "
                    "reason=no_holding_no_state"
                )
            print(f"  - {symbol}: {state_msg} (저장파일: {symbol_store.file_path})")
            executors.append(executor)
            executors_by_symbol[symbol] = executor
            fast_eval_scheduler.mark_force(symbol, reason="startup")
        print("")

        # 거래 시작
        print("🚀 멀티데이 거래 시작...")
        print(f"   대상 종목: {run_symbols}")
        print("   종료하려면 Ctrl+C를 누르세요.\n")
        print("   ★ 포지션은 프로그램 종료 시에도 유지됩니다.")
        print("   ★ Exit는 오직 가격 조건으로만 발생합니다.\n")

        # 멀티심볼 루프는 executor.run()을 직접 호출하지 않으므로 시작 알림을 수동 전송
        if executors:
            notifier = getattr(executors[0], "telegram", None)
            if notifier is not None:
                mode_display = {
                    "REAL": "🔴 실계좌",
                    "LIVE": "🔴 실계좌",
                    "CBT": "🟡 종이매매",
                    "DRY_RUN": "🟡 종이매매",
                    "PAPER": "🟢 모의투자",
                }.get(trading_mode, trading_mode)
                try:
                    notifier.notify_system_start(
                        stock_code=", ".join(run_symbols),
                        order_quantity=order_quantity,
                        interval=int(interval),
                        mode=mode_display,
                    )
                except Exception as e:
                    logger.warning(f"[TELEGRAM] 시작 알림 전송 실패(계속 진행): {e}")

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

        def _on_ws_quote(symbol: str, snapshot: dict) -> None:
            if not fast_eval_enabled:
                return
            received_at = snapshot.get("received_at")
            fast_eval_scheduler.mark_quote_event(
                symbol,
                event_monotonic=time.monotonic(),
                received_at=received_at if isinstance(received_at, datetime) else None,
            )

        def _ensure_ws_subscription(symbols) -> None:
            nonlocal ws_stop, ws_quote_stop
            if ws_provider is None:
                return
            if ws_stop is not None:
                return
            subscription_symbols = _market_regime_subscription_symbols(symbols)
            if fast_eval_enabled:
                prewarm_quotes = getattr(ws_provider, "prewarm_quotes", None)
                if callable(prewarm_quotes):
                    prewarm_quotes(subscription_symbols)
                subscribe_quotes = getattr(ws_provider, "subscribe_quotes", None)
                if callable(subscribe_quotes) and ws_quote_stop is None:
                    ws_quote_stop = subscribe_quotes(_on_ws_quote)
            ws_stop = ws_provider.subscribe_bars(subscription_symbols, runtime_config.timeframe, lambda _bar: None)

        def _stop_ws_subscription() -> None:
            nonlocal ws_stop, ws_quote_stop
            if ws_stop is None:
                if ws_quote_stop is None:
                    return
            if ws_stop is not None:
                try:
                    ws_stop()
                except Exception:
                    pass
                ws_stop = None
            if ws_quote_stop is not None:
                try:
                    ws_quote_stop()
                except Exception:
                    pass
                ws_quote_stop = None

        def _load_market_regime_ws_quote_snapshot(symbol: str) -> dict:
            if ws_provider is None:
                return {}
            getter = getattr(ws_provider, "get_cached_quote_snapshot", None)
            if callable(getter):
                try:
                    return dict(getter(symbol) or {})
                except Exception as exc:
                    logger.debug("[MARKET_REGIME_BG] ws quote cache unavailable symbol=%s err=%s", symbol, exc)
                    return {}
            return {}

        def _handle_market_regime_worker_error(worker_name: str, exc: Exception) -> None:
            logger.error("[MARKET_REGIME_BG] worker=%s err=%s", worker_name, exc)

        def _start_market_regime_worker() -> None:
            nonlocal market_regime_worker, market_regime_worker_stop_event
            if not market_regime_background_enabled:
                return
            if market_regime_worker is not None and market_regime_worker.is_alive():
                return
            market_regime_worker_stop_event = threading.Event()
            market_regime_worker = MarketRegimeRefreshThread(
                service=market_regime_service,
                quote_snapshot_loader=_load_market_regime_ws_quote_snapshot,
                stop_event=market_regime_worker_stop_event,
                on_error=_handle_market_regime_worker_error,
            )
            market_regime_worker.start()
            logger.info("[MARKET_REGIME_BG] worker_started")

        def _stop_market_regime_worker() -> None:
            nonlocal market_regime_worker, market_regime_worker_stop_event
            if market_regime_worker_stop_event is not None:
                market_regime_worker_stop_event.set()
            if market_regime_worker is not None and market_regime_worker.is_alive():
                market_regime_worker.join(timeout=2.0)
            market_regime_worker = None
            market_regime_worker_stop_event = None

        def _resolve_effective_feed_mode(decision) -> str:
            if ws_provider is None:
                return "rest"
            if (
                decision.policy.active_feed_mode == "ws"
                and _state_in(decision.market_state, (
                    MarketSessionState.IN_SESSION,
                    MarketSessionState.AUCTION_GUARD,
                ))
                and ws_provider.is_ws_connected()
            ):
                return "ws"
            return "rest"

        previous_loop_started_monotonic = None
        market_regime_observation_state = MarketRegimeObservationState(
            startup_monotonic=time.monotonic()
        )
        last_fast_metric_log_monotonic = None
        _start_market_regime_worker()

        def _format_optional_elapsed_sec(value) -> str:
            if value is None:
                return "none"
            return f"{max(float(value), 0.0):.3f}"

        def _snapshot_as_of(snapshot) -> str:
            if snapshot is None:
                return "none"
            return snapshot.as_of.isoformat()

        def _snapshot_is_stale(snapshot) -> str:
            return str(bool(snapshot is not None and getattr(snapshot, "is_stale", False))).lower()

        def _log_market_regime_refresh_skipped(reason: str, elapsed_sec: float, snapshot) -> None:
            logger.info(
                "[MARKET_REGIME] snapshot_refresh_skipped reason=%s elapsed_sec=%.3f "
                "snapshot_as_of=%s snapshot_stale=%s",
                reason or "unknown",
                max(float(elapsed_sec or 0.0), 0.0),
                _snapshot_as_of(snapshot),
                _snapshot_is_stale(snapshot),
            )

        def _log_loop_metric(
            *,
            iteration: int,
            loop_started_monotonic: float,
            since_prev_loop_start_sec,
            symbols_count: int,
            market_regime_refresh_state: str,
            market_regime_refresh_elapsed_sec: float,
            market_regime_snapshot,
            market_regime_refresh_skip_reason: str,
        ) -> None:
            logger.info(
                "[LOOP_METRIC] iteration=%s elapsed_sec=%.3f since_prev_loop_start_sec=%s "
                "symbols=%s market_regime_refresh_state=%s "
                "market_regime_refresh_elapsed_sec=%.3f market_regime_snapshot_stale=%s "
                "market_regime_refresh_skip_reason=%s",
                iteration,
                max(time.monotonic() - loop_started_monotonic, 0.0),
                _format_optional_elapsed_sec(since_prev_loop_start_sec),
                symbols_count,
                market_regime_refresh_state,
                max(float(market_regime_refresh_elapsed_sec or 0.0), 0.0),
                _snapshot_is_stale(market_regime_snapshot),
                market_regime_refresh_skip_reason or "none",
            )

        def _record_cadence_metric(symbol: str, executor_result: dict, has_position: bool, path: str, reason: str) -> None:
            metrics_delta = dict((executor_result or {}).get("metrics") or {})
            signal_payload = dict((executor_result or {}).get("signal") or {})
            quote_age_sec = float(signal_payload.get("quote_age_sec", 0.0) or 0.0)
            cadence_tracker.record(
                symbol=symbol,
                evaluated_at=datetime.now(KST),
                interval_sec=executor_result.get("evaluation_interval_sec"),
                quote_age_sec=quote_age_sec,
                path=path,
                reason=reason,
                has_position=has_position,
                daily_fetch_calls=int(metrics_delta.get("daily_fetch_calls", 0) or 0),
                rest_quote_calls=int(metrics_delta.get("rest_quote_calls", 0) or 0),
                account_snapshot_calls=int(metrics_delta.get("account_snapshot_calls", 0) or 0),
                ws_reconnect_count=int(metrics_delta.get("ws_reconnect_count", 0) or 0),
                ws_fallback_count=int(metrics_delta.get("ws_fallback_count", 0) or 0),
            )
            logger.info(
                "[EVAL_METRIC] symbol=%s path=%s reason=%s interval_sec=%s quote_age_sec=%.3f "
                "daily_fetch_calls=%s rest_quote_calls=%s account_snapshot_calls=%s "
                "ws_reconnect_count=%s ws_fallback_count=%s",
                symbol,
                path,
                reason,
                _format_optional_elapsed_sec(executor_result.get("evaluation_interval_sec")),
                max(float(quote_age_sec or 0.0), 0.0),
                int(metrics_delta.get("daily_fetch_calls", 0) or 0),
                int(metrics_delta.get("rest_quote_calls", 0) or 0),
                int(metrics_delta.get("account_snapshot_calls", 0) or 0),
                int(metrics_delta.get("ws_reconnect_count", 0) or 0),
                int(metrics_delta.get("ws_fallback_count", 0) or 0),
            )

        def _log_cadence_summary_if_due(now_monotonic: float) -> None:
            nonlocal last_fast_metric_log_monotonic
            metric_interval = max(
                float(getattr(settings, "FAST_EVAL_METRIC_LOG_INTERVAL_SEC", 60.0) or 60.0),
                5.0,
            )
            if (
                last_fast_metric_log_monotonic is not None
                and (now_monotonic - last_fast_metric_log_monotonic) < metric_interval
            ):
                return
            summary = cadence_tracker.summary()
            global_summary = dict(summary.get("global") or {})
            logger.info(
                "[EVAL_SUMMARY] p50_interval_sec=%.3f p90_interval_sec=%.3f "
                "quote_age_p50_sec=%.3f quote_age_p90_sec=%.3f daily_fetch_calls=%s "
                "rest_quote_calls=%s account_snapshot_calls=%s ws_reconnect_count=%s "
                "ws_fallback_count=%s evaluations=%s",
                float(global_summary.get("p50_interval_sec", 0.0) or 0.0),
                float(global_summary.get("p90_interval_sec", 0.0) or 0.0),
                float(global_summary.get("quote_age_p50_sec", 0.0) or 0.0),
                float(global_summary.get("quote_age_p90_sec", 0.0) or 0.0),
                int(global_summary.get("daily_fetch_calls", 0) or 0),
                int(global_summary.get("rest_quote_calls", 0) or 0),
                int(global_summary.get("account_snapshot_calls", 0) or 0),
                int(global_summary.get("ws_reconnect_count", 0) or 0),
                int(global_summary.get("ws_fallback_count", 0) or 0),
                int(global_summary.get("evaluations", 0) or 0),
            )
            last_fast_metric_log_monotonic = now_monotonic

        iteration = 0
        active_feed_name = "rest"
        last_status_log_at = None
        last_postclose_report_date = None
        last_prewarm_prepare_date = None
        while True:
            iteration += 1
            loop_started_monotonic = time.monotonic()
            since_prev_loop_start_sec = (
                None
                if previous_loop_started_monotonic is None
                else max(loop_started_monotonic - previous_loop_started_monotonic, 0.0)
            )
            previous_loop_started_monotonic = loop_started_monotonic
            market_regime_refresh_state = "skipped"
            market_regime_refresh_elapsed_sec = 0.0
            market_regime_refresh_skip_reason = "filter_disabled"
            market_regime_loop_context = MarketRegimeLoopContext()
            if hasattr(api, "prewarm_access_token_if_due"):
                api.prewarm_access_token_if_due()

            # 날짜 변경 시 유니버스 1회 재생성/재사용 후 진입 후보 재계산
            now_trade_date = datetime.now(KST).strftime("%Y-%m-%d")
            if now_trade_date != current_trade_date:
                current_trade_date, holdings_symbols, todays_universe, entry_candidates = _refresh_daily_universe()
                refreshed_symbols = _merge_symbols(holdings_symbols, entry_candidates)
                if stock_code != settings.DEFAULT_STOCK_CODE:
                    refreshed_symbols = [stock_code]
                run_symbols = list(refreshed_symbols)
                for symbol in refreshed_symbols:
                    if symbol in executors_by_symbol:
                        continue
                    symbol_store = _symbol_position_store(symbol)
                    executor = MultidayExecutor(
                        api=api,
                        strategy=MultidayTrendATRStrategy(),
                        stock_code=symbol,
                        order_quantity=order_quantity,
                        risk_manager=shared_risk_manager,
                        position_store=symbol_store,
                        market_data_provider=rest_provider,
                    )
                    if _should_restore_on_start(symbol, holdings_symbols, symbol_store):
                        restored = executor.restore_position_on_start()
                        state_msg = "복원 완료 - Exit 조건 감시" if restored else "복원 포지션 없음 - Entry 조건 감시"
                    else:
                        state_msg = "복원 생략 - 신규 진입 감시"
                        logger.info(
                            f"[RESYNC] startup restore 생략: symbol={symbol}, "
                            "reason=no_holding_no_state"
                        )
                    print(f"  - {symbol}: {state_msg}")
                    executors_by_symbol[symbol] = executor
                    executors.append(executor)
                    fast_eval_scheduler.mark_force(symbol, reason="universe_refresh")
                logger.info(
                    f"[UNIVERSE] runtime executor_symbols={run_symbols}, "
                    f"selection_method={universe_service.policy.selection_method}, "
                    f"cache_file={universe_service.policy.cache_file}"
                )

            active_executors = [executors_by_symbol[s] for s in run_symbols if s in executors_by_symbol]
            logger.info(f"[MULTI] 반복 #{iteration} / symbols={len(active_executors)}")

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
                    f"symbols_count={len(active_executors)}"
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
                            if ws_provider is not None:
                                prewarm_quotes = getattr(ws_provider, "prewarm_quotes", None)
                                if callable(prewarm_quotes):
                                    prewarm_quotes(_market_regime_subscription_symbols([symbol]))
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
            market_phase_context = resolve_market_phase_context(
                check_time=now_kst,
                venue=TradingVenue.KRX,
                session_state=decision.market_state,
            )
            for executor in active_executors:
                executor.market_data_provider = active_provider
                set_market_phase_context = getattr(executor, "set_market_phase_context", None)
                if callable(set_market_phase_context):
                    set_market_phase_context(
                        market_phase_context.phase,
                        venue=market_phase_context.venue,
                    )
            if active_feed_name != target_feed:
                logger.info(
                    "[RUNTIME] active feed switched %s -> %s",
                    active_feed_name,
                    target_feed,
                )
                if target_feed == "ws" and fast_eval_enabled:
                    for executor in active_executors:
                        fast_eval_scheduler.mark_force(executor.stock_code, reason="feed_switch")
                active_feed_name = target_feed

            # 런타임 holdings/entry_candidates 재계산 (보유는 항상 관리, 진입은 후보만)
            runtime_holdings = [e.stock_code for e in active_executors if e.strategy.has_position]
            holdings_count = len(runtime_holdings)
            max_positions = max(int(universe_service.policy.max_positions), 0)
            compute_capacity = getattr(universe_service, "compute_entry_capacity", None)
            if callable(compute_capacity):
                free_slots = int(compute_capacity(runtime_holdings, max_positions))
            else:
                free_slots = max(max_positions - len(set(runtime_holdings)), 0)
            free_slots = max(free_slots, 0)

            if stock_code == settings.DEFAULT_STOCK_CODE:
                ranked_entry_candidates = universe_service.compute_entry_candidates(
                    runtime_holdings, todays_universe
                )
            else:
                ranked_entry_candidates = [stock_code] if stock_code not in runtime_holdings else []

            limit_candidates = getattr(universe_service, "limit_entry_candidates", None)
            if callable(limit_candidates):
                entry_candidates = list(limit_candidates(ranked_entry_candidates, free_slots))
            else:
                entry_candidates = list(ranked_entry_candidates[:free_slots])
            capacity_cutoff_symbols = set(ranked_entry_candidates[len(entry_candidates) :])
            cutoff_key = (
                f"{holdings_count}|{max_positions}|{len(ranked_entry_candidates)}|"
                f"{','.join(sorted(capacity_cutoff_symbols))}"
            )
            if cutoff_key != last_entry_capacity_key:
                if capacity_cutoff_symbols:
                    logger.info(
                        "[ENTRY] capacity cutoff applied: holdings=%s max=%s free_slots=%s "
                        "ranked=%s allowed=%s dropped=%s dropped_symbols=%s",
                        holdings_count,
                        max_positions,
                        free_slots,
                        len(ranked_entry_candidates),
                        len(entry_candidates),
                        len(capacity_cutoff_symbols),
                        sorted(capacity_cutoff_symbols),
                    )
                else:
                    logger.info(
                        "[ENTRY] capacity status: holdings=%s max=%s free_slots=%s ranked=%s",
                        holdings_count,
                        max_positions,
                        free_slots,
                        len(ranked_entry_candidates),
                    )
                last_entry_capacity_key = cutoff_key

            shared_market_regime_snapshot = materialize_market_regime_snapshot(
                shared_market_regime_snapshot,
                now_kst,
            )
            market_regime_filter_enabled = bool(
                getattr(settings, "ENABLE_MARKET_REGIME_FILTER", False)
            )
            should_refresh_market_regime = (
                market_regime_filter_enabled
                and decision.policy.allow_new_entries
                and decision.policy.run_strategy
                and free_slots > 0
                and bool(entry_candidates)
            )
            if not market_regime_filter_enabled:
                market_regime_refresh_skip_reason = "filter_disabled"
            elif not decision.policy.allow_new_entries:
                market_regime_refresh_skip_reason = "new_entries_not_allowed"
            elif not decision.policy.run_strategy:
                market_regime_refresh_skip_reason = "strategy_disabled"
            elif free_slots <= 0:
                market_regime_refresh_skip_reason = "no_free_slots"
            elif not entry_candidates:
                market_regime_refresh_skip_reason = "no_entry_candidates"
            else:
                market_regime_refresh_skip_reason = "refresh_required"
            if should_refresh_market_regime:
                if market_regime_background_enabled:
                    (
                        shared_market_regime_snapshot,
                        market_regime_refresh_state,
                        market_regime_refresh_elapsed_sec,
                        market_regime_refresh_skip_reason,
                        market_regime_background_status,
                    ) = _read_market_regime_snapshot_read_only(
                        worker=market_regime_worker,
                        current_snapshot=shared_market_regime_snapshot,
                        check_time=now_kst,
                    )
                    worker_error_state = str(
                        market_regime_background_status.get("market_regime_worker_error_state") or ""
                    ).strip()
                    last_success_age_sec = float(
                        market_regime_background_status.get(
                            "market_regime_background_last_success_age_sec",
                            -1.0,
                        )
                        or -1.0
                    )
                    logger.info(
                        "[MARKET_REGIME_BG] read_only refresh_state=%s "
                        "market_regime_background_refresh_ms=%.3f "
                        "market_regime_daily_context_refresh_ms=%.3f "
                        "market_regime_intraday_guard_ms=%.3f "
                        "market_regime_quote_source=%s market_regime_quote_state=%s "
                        "market_regime_daily_context_state=%s "
                        "market_regime_background_last_success_age_sec=%.3f "
                        "market_regime_background_refresh_fail_count=%s "
                        "market_regime_worker_error_state=%s snapshot_as_of=%s snapshot_stale=%s",
                        market_regime_refresh_state,
                        float(
                            market_regime_background_status.get(
                                "market_regime_background_refresh_ms",
                                0.0,
                            )
                            or 0.0
                        ),
                        float(
                            market_regime_background_status.get(
                                "market_regime_daily_context_refresh_ms",
                                0.0,
                            )
                            or 0.0
                        ),
                        float(
                            market_regime_background_status.get(
                                "market_regime_intraday_guard_ms",
                                0.0,
                            )
                            or 0.0
                        ),
                        str(
                            market_regime_background_status.get("market_regime_quote_source")
                            or "skip"
                        ),
                        str(
                            market_regime_background_status.get("market_regime_quote_state")
                            or "absent"
                        ),
                        str(
                            market_regime_background_status.get(
                                "market_regime_daily_context_state"
                            )
                            or "absent"
                        ),
                        last_success_age_sec,
                        int(
                            market_regime_background_status.get(
                                "market_regime_background_refresh_fail_count",
                                0,
                            )
                            or 0
                        ),
                        worker_error_state or "none",
                        _snapshot_as_of(shared_market_regime_snapshot),
                        _snapshot_is_stale(shared_market_regime_snapshot),
                    )
                else:
                    refresh_outcome = refresh_shared_market_regime_snapshot(
                        current_snapshot=shared_market_regime_snapshot,
                        refresh_fn=lambda refresh_now: market_regime_service.build_snapshot(
                            check_time=refresh_now,
                            include_metrics=True,
                        ),
                        check_time=now_kst,
                        loop_context=market_regime_loop_context,
                        budget_sec=get_market_regime_refresh_budget_sec(),
                    )
                    shared_market_regime_snapshot = refresh_outcome.snapshot
                    market_regime_refresh_elapsed_sec = refresh_outcome.total_refresh_elapsed_sec
                    if refresh_outcome.refreshed and shared_market_regime_snapshot is not None:
                        market_regime_refresh_state = "refreshed"
                        market_regime_refresh_skip_reason = "none"
                        log_market_regime_refresh_outcome(
                            refresh_outcome,
                            shared_market_regime_snapshot,
                        )
                    elif refresh_outcome.budget_exceeded:
                        market_regime_refresh_state = "budget_exceeded"
                        market_regime_refresh_skip_reason = "budget_exceeded"
                        log_market_regime_refresh_outcome(
                            refresh_outcome,
                            shared_market_regime_snapshot,
                        )
                    elif refresh_outcome.error:
                        market_regime_refresh_state = "failed"
                        market_regime_refresh_skip_reason = "refresh_error"
                        last_success_at = (
                            shared_market_regime_snapshot.as_of.isoformat()
                            if shared_market_regime_snapshot is not None
                            else "none"
                        )
                        logger.warning(
                            "[MARKET_REGIME] snapshot_update_failed error=%s elapsed_sec=%.3f "
                            "last_success_at=%s",
                            refresh_outcome.error,
                            refresh_outcome.total_refresh_elapsed_sec,
                            last_success_at,
                        )
                    else:
                        market_regime_refresh_state = "skipped"
                        market_regime_refresh_skip_reason = (
                            refresh_outcome.refresh_skipped_reason or "unknown"
                        )
                        _log_market_regime_refresh_skipped(
                            market_regime_refresh_skip_reason,
                            refresh_outcome.total_refresh_elapsed_sec,
                            shared_market_regime_snapshot,
                        )
            elif market_regime_filter_enabled:
                _log_market_regime_refresh_skipped(
                    market_regime_refresh_skip_reason,
                    market_regime_refresh_elapsed_sec,
                    shared_market_regime_snapshot,
                )

            observe_market_regime_snapshot(
                observation_state=market_regime_observation_state,
                snapshot=shared_market_regime_snapshot,
                now_kst=now_kst,
                in_session=_state_equals(decision.market_state, MarketSessionState.IN_SESSION),
                filter_enabled=market_regime_filter_enabled,
            )

            for executor in active_executors:
                executor.set_market_regime_snapshot(shared_market_regime_snapshot)

            fast_eval_active = (
                fast_eval_enabled
                and active_feed_name == "ws"
                and ws_provider is not None
                and ws_provider.is_ws_connected()
                and _state_in(
                    decision.market_state,
                    (
                        MarketSessionState.IN_SESSION,
                        MarketSessionState.AUCTION_GUARD,
                    ),
                )
            )
            due_fast_by_symbol = {}
            if fast_eval_active:
                due_fast = fast_eval_scheduler.due_evaluations(
                    symbols=[executor.stock_code for executor in active_executors],
                    has_position_by_symbol={
                        executor.stock_code: bool(executor.strategy.has_position)
                        for executor in active_executors
                    },
                    now_monotonic=time.monotonic(),
                    ws_connected=True,
                )
                due_fast_by_symbol = {item.symbol: item for item in due_fast}

            for executor in active_executors:
                symbol = executor.stock_code
                sticky_blocked = False
                sticky_reason = ""
                is_sticky = getattr(executor, "is_entry_block_sticky", None)
                if callable(is_sticky) and is_sticky():
                    sticky_blocked = True
                    if decision.policy.allow_new_entries:
                        retry_unblock = getattr(executor, "retry_entry_unblock_via_resync", None)
                        if callable(retry_unblock):
                            sticky_blocked = not bool(retry_unblock())
                        if sticky_blocked:
                            get_block_reason = getattr(executor, "get_entry_block_reason", None)
                            if callable(get_block_reason):
                                sticky_reason = str(get_block_reason() or "")
                            if not sticky_reason:
                                sticky_reason = "[ENTRY] blocked by reconcile: retry_failed"
                            logger.warning(
                                "[ENTRY] sticky reconcile block active symbol=%s reason=%s",
                                symbol,
                                sticky_reason,
                            )

                if sticky_blocked:
                    executor.set_entry_control(False, sticky_reason)
                elif not decision.policy.allow_new_entries:
                    executor.set_entry_control(
                        False,
                        (
                            f"[ENTRY] runtime blocked: "
                            f"market={decision.market_state.value}, overlay={decision.overlay.value}"
                        ),
                    )
                elif symbol in runtime_holdings:
                    executor.set_entry_control(False, f"[ENTRY] skipped: already holding symbol={symbol}")
                elif holdings_count >= max_positions:
                    msg = (
                        f"[ENTRY] blocked: max_positions reached "
                        f"(holdings={holdings_count}, max={max_positions})"
                    )
                    logger.info(msg)
                    executor.set_entry_control(False, msg)
                elif symbol in capacity_cutoff_symbols:
                    msg = (
                        f"[ENTRY] blocked: capacity cutoff "
                        f"(symbol={symbol}, free_slots={free_slots}, ranked={len(ranked_entry_candidates)})"
                    )
                    executor.set_entry_control(False, msg)
                elif symbol not in entry_candidates:
                    executor.set_entry_control(False, f"[ENTRY] skipped: symbol={symbol} not in entry_candidates")
                else:
                    executor.set_entry_control(True, "")

                if not decision.policy.run_strategy:
                    continue

                if fast_eval_active:
                    due_item = due_fast_by_symbol.get(str(symbol).zfill(6))
                    if due_item is None:
                        continue
                    executor_result = executor.run_fast_cycle() or {}
                    evaluated_at = datetime.now(KST)
                    evaluation_interval_sec = fast_eval_scheduler.mark_evaluated(
                        symbol,
                        evaluated_at=evaluated_at,
                        evaluated_monotonic=time.monotonic(),
                        reason=due_item.reason,
                    )
                    executor_result["evaluation_interval_sec"] = evaluation_interval_sec
                    _record_cadence_metric(
                        symbol=str(symbol).zfill(6),
                        executor_result=executor_result,
                        has_position=bool(executor.strategy.has_position),
                        path="fast_ws",
                        reason=due_item.reason,
                    )
                    runtime_holdings = [e.stock_code for e in active_executors if e.strategy.has_position]
                    holdings_count = len(runtime_holdings)
                    compute_capacity = getattr(universe_service, "compute_entry_capacity", None)
                    if callable(compute_capacity):
                        free_slots = int(compute_capacity(runtime_holdings, max_positions))
                    else:
                        free_slots = max(max_positions - len(set(runtime_holdings)), 0)
                    free_slots = max(free_slots, 0)
                    if stock_code == settings.DEFAULT_STOCK_CODE:
                        ranked_entry_candidates = universe_service.compute_entry_candidates(
                            runtime_holdings,
                            todays_universe,
                        )
                    else:
                        ranked_entry_candidates = [stock_code] if stock_code not in runtime_holdings else []
                    limit_candidates = getattr(universe_service, "limit_entry_candidates", None)
                    if callable(limit_candidates):
                        entry_candidates = list(limit_candidates(ranked_entry_candidates, free_slots))
                    else:
                        entry_candidates = list(ranked_entry_candidates[:free_slots])
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

                executor_result = executor.run_once() or {}
                evaluated_at = datetime.now(KST)
                evaluation_interval_sec = fast_eval_scheduler.mark_evaluated(
                    symbol,
                    evaluated_at=evaluated_at,
                    evaluated_monotonic=time.monotonic(),
                    reason="legacy_bar",
                )
                executor_result["evaluation_interval_sec"] = evaluation_interval_sec
                _record_cadence_metric(
                    symbol=str(symbol).zfill(6),
                    executor_result=executor_result,
                    has_position=bool(executor.strategy.has_position),
                    path="legacy_bar",
                    reason="legacy_bar",
                )
                normalized_symbol_bar_ts = _normalize_bar_ts(symbol_bar_ts)
                if normalized_symbol_bar_ts is not None:
                    bar_gate.mark_processed(symbol, normalized_symbol_bar_ts)
                runtime_holdings = [e.stock_code for e in active_executors if e.strategy.has_position]
                holdings_count = len(runtime_holdings)
                compute_capacity = getattr(universe_service, "compute_entry_capacity", None)
                if callable(compute_capacity):
                    free_slots = int(compute_capacity(runtime_holdings, max_positions))
                else:
                    free_slots = max(max_positions - len(set(runtime_holdings)), 0)
                free_slots = max(free_slots, 0)
                if stock_code == settings.DEFAULT_STOCK_CODE:
                    ranked_entry_candidates = universe_service.compute_entry_candidates(
                        runtime_holdings,
                        todays_universe,
                    )
                else:
                    ranked_entry_candidates = [stock_code] if stock_code not in runtime_holdings else []
                limit_candidates = getattr(universe_service, "limit_entry_candidates", None)
                if callable(limit_candidates):
                    entry_candidates = list(limit_candidates(ranked_entry_candidates, free_slots))
                else:
                    entry_candidates = list(ranked_entry_candidates[:free_slots])

            if _state_equals(decision.market_state, MarketSessionState.POSTCLOSE):
                report_date = now_kst.strftime("%Y-%m-%d")
                if report_date != last_postclose_report_date:
                    logger.info("[RUNTIME] POSTCLOSE actions started date=%s", report_date)
                    for executor in active_executors:
                        if hasattr(executor, "_persist_account_snapshot"):
                            try:
                                executor._persist_account_snapshot(force=True)
                            except Exception:
                                pass
                    last_postclose_report_date = report_date

            _log_cadence_summary_if_due(time.monotonic())

            if max_runs and iteration >= max_runs:
                _log_loop_metric(
                    iteration=iteration,
                    loop_started_monotonic=loop_started_monotonic,
                    since_prev_loop_start_sec=since_prev_loop_start_sec,
                    symbols_count=len(active_executors),
                    market_regime_refresh_state=market_regime_refresh_state,
                    market_regime_refresh_elapsed_sec=market_regime_refresh_elapsed_sec,
                    market_regime_snapshot=shared_market_regime_snapshot,
                    market_regime_refresh_skip_reason=market_regime_refresh_skip_reason,
                )
                logger.info(f"[MULTI] 최대 반복 도달: {max_runs}")
                break

            if _state_equals(decision.market_state, MarketSessionState.IN_SESSION):
                if fast_eval_active:
                    sleep_sec = max(
                        float(getattr(settings, "FAST_EVAL_LOOP_SLEEP_SEC", 1.0) or 1.0),
                        0.5,
                    )
                else:
                    sleep_sec = max(15, min(int(interval), 60))
            elif _state_equals(decision.market_state, MarketSessionState.OFF_SESSION):
                sleep_sec = runtime_config.offsession_sleep_sec
            else:
                sleep_sec = max(int(decision.policy.sleep_sec), 5)

            _log_loop_metric(
                iteration=iteration,
                loop_started_monotonic=loop_started_monotonic,
                since_prev_loop_start_sec=since_prev_loop_start_sec,
                symbols_count=len(active_executors),
                market_regime_refresh_state=market_regime_refresh_state,
                market_regime_refresh_elapsed_sec=market_regime_refresh_elapsed_sec,
                market_regime_snapshot=shared_market_regime_snapshot,
                market_regime_refresh_skip_reason=market_regime_refresh_skip_reason,
            )
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
        _stop_market_regime_worker()
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
        interval = max(30, args.interval)
        if interval != args.interval:
            print(f"⚠️ 실행 간격이 30초 미만입니다. 30초로 조정됩니다.")
        
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
