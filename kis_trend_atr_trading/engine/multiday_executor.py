"""
KIS Trend-ATR Trading System - 멀티데이 거래 실행 엔진

★ 전략의 본질:
    - 당일 매수·당일 매도(Day Trading)가 아닌
    - 익절 또는 손절 신호가 발생할 때까지 보유(Hold until Exit)

★ 절대 금지 사항:
    - ❌ 장 마감(EOD) 시간 기준 강제 청산 로직
    - ❌ 시간 기반 종료 조건
    - ❌ 익일 ATR 재계산

★ 핵심 기능:
    1. 프로그램 시작 시 포지션 복원
    2. API를 통한 실제 보유 확인
    3. 모드별 주문 처리 (LIVE/CBT/PAPER)
    4. 포지션 영속화 (프로그램 종료 시 저장)
"""

import time
import signal
import sys
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Any, List
import pandas as pd

try:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.api.kis_api import KISApi, KISApiError
    from kis_trend_atr_trading.strategy.multiday_trend_atr import (
        MultidayTrendATRStrategy,
        TradingSignal,
        SignalType,
        ExitReason,
    )
    from kis_trend_atr_trading.utils.gap_protection import GAP_REASON_FALLBACK, GAP_REASON_OTHER
    from kis_trend_atr_trading.engine.trading_state import TradingState, MultidayPosition
    from kis_trend_atr_trading.engine.risk_manager import (
        RiskManager,
        create_risk_manager_from_settings,
        safe_exit_with_message
    )
    from kis_trend_atr_trading.engine.order_synchronizer import (
        SingleInstanceLock,
        MarketHoursChecker,
        OrderSynchronizer,
        PositionResynchronizer,
        OrderExecutionResult,
        ensure_single_instance,
        get_instance_lock,
        get_market_checker
    )
    from kis_trend_atr_trading.utils.position_store import (
        PositionStore,
        StoredPosition,
        get_position_store
    )
    from kis_trend_atr_trading.db.repository import get_position_repository
    from kis_trend_atr_trading.db.repository import get_trade_repository
    from kis_trend_atr_trading.db.mysql import get_db_manager, QueryError
    from kis_trend_atr_trading.core.market_data import MarketDataProvider
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        AccountRiskSnapshot,
        HoldingsRiskSnapshot,
    )
    from kis_trend_atr_trading.engine.pullback_pipeline_stores import (
        AccountRiskStore,
        ArmedCandidateStore,
        DailyContextStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from kis_trend_atr_trading.engine.pullback_pipeline_workers import (
        DailyRefreshThread,
        RiskSnapshotThread,
        PullbackSetupWorker,
        PullbackTimingWorker,
        OrderExecutionWorker,
    )
    from kis_trend_atr_trading.engine.strategy_pipeline_registry import build_default_strategy_registry
    from kis_trend_atr_trading.utils.avg_price import calc_weighted_avg, quantize_price, reduce_quantity_after_sell
    from kis_trend_atr_trading.utils.entry_utils import (
        ASSET_TYPE_ETF,
        align_price_to_tick,
        compute_extension_pct,
        detect_asset_type,
        get_tick_size,
    )
    from kis_trend_atr_trading.utils.market_regime import (
        MarketRegime,
        MarketRegimeSnapshot,
        get_market_regime_fail_mode,
        materialize_market_regime_snapshot,
    )
    from kis_trend_atr_trading.utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
    from kis_trend_atr_trading.utils.logger import get_logger, TradeLogger
    from kis_trend_atr_trading.utils.market_hours import KST
    from kis_trend_atr_trading.env import get_db_namespace_mode
except ImportError:
    from config import settings
    from api.kis_api import KISApi, KISApiError
    from strategy.multiday_trend_atr import (
        MultidayTrendATRStrategy,
        TradingSignal,
        SignalType,
        ExitReason,
    )
    from utils.gap_protection import GAP_REASON_FALLBACK, GAP_REASON_OTHER
    from engine.trading_state import TradingState, MultidayPosition
    from engine.risk_manager import (
        RiskManager,
        create_risk_manager_from_settings,
        safe_exit_with_message
    )
    from engine.order_synchronizer import (
        SingleInstanceLock,
        MarketHoursChecker,
        OrderSynchronizer,
        PositionResynchronizer,
        OrderExecutionResult,
        ensure_single_instance,
        get_instance_lock,
        get_market_checker
    )
    from utils.position_store import (
        PositionStore,
        StoredPosition,
        get_position_store
    )
    from db.repository import get_position_repository
    from db.repository import get_trade_repository
    from db.mysql import get_db_manager, QueryError
    from core.market_data import MarketDataProvider
    from engine.pullback_pipeline_models import (
        AccountRiskSnapshot,
        HoldingsRiskSnapshot,
    )
    from engine.pullback_pipeline_stores import (
        AccountRiskStore,
        ArmedCandidateStore,
        DailyContextStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from engine.pullback_pipeline_workers import (
        DailyRefreshThread,
        RiskSnapshotThread,
        PullbackSetupWorker,
        PullbackTimingWorker,
        OrderExecutionWorker,
    )
    from engine.strategy_pipeline_registry import build_default_strategy_registry
    from utils.avg_price import calc_weighted_avg, quantize_price, reduce_quantity_after_sell
    from utils.entry_utils import (
        ASSET_TYPE_ETF,
        align_price_to_tick,
        compute_extension_pct,
        detect_asset_type,
        get_tick_size,
    )
    from utils.market_regime import (
        MarketRegime,
        MarketRegimeSnapshot,
        get_market_regime_fail_mode,
        materialize_market_regime_snapshot,
    )
    from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
    from utils.logger import get_logger, TradeLogger
    from utils.market_hours import KST
    from env import get_db_namespace_mode

logger = get_logger("multiday_executor")
trade_logger = TradeLogger("multiday_executor")

try:
    from kis_trend_atr_trading.api.kis_api import KISApiError as _PKG_KIS_API_ERROR
except Exception:
    _PKG_KIS_API_ERROR = None
_KIS_API_ERROR_TYPES = tuple(
    err
    for err in (KISApiError, _PKG_KIS_API_ERROR)
    if isinstance(err, type) and issubclass(err, Exception)
)


@dataclass
class DailySignalSnapshotCache:
    trade_date: str
    source_frame: pd.DataFrame
    last_refreshed_at: datetime
    stock_name: str = ""
    open_price: float = 0.0
    prev_high: float = 0.0
    prev_close: float = 0.0
    atr: float = 0.0
    adx: float = 0.0
    trend: str = "SIDEWAYS"


@dataclass
class PreparedEvaluationContext:
    decision_time: datetime
    df: pd.DataFrame
    quote_snapshot: Dict[str, Any]
    current_price: float
    open_price: float
    intraday_bars: List[dict] = field(default_factory=list)
    has_pending_order: bool = False
    used_cached_daily: bool = False


class MultidayExecutor:
    """
    멀티데이 거래 실행 엔진
    
    ★ 핵심 원칙:
        1. EOD 청산 로직 절대 없음
        2. Exit는 오직 가격 조건으로만 발생
        3. ATR은 진입 시 고정
        4. 프로그램 종료 시 포지션 상태 저장
        5. 프로그램 시작 시 포지션 복원
    """
    _shared_account_snapshot: Optional[Dict[str, Any]] = None
    _shared_account_snapshot_ts: Optional[datetime] = None
    _shared_account_snapshot_fetch_count: int = 0
    _pending_recovery_done: bool = False
    _pending_recovery_count: int = 0
    _startup_resync_summary_notified: bool = False

    @staticmethod
    def _normalize_mode_label(mode: Optional[str]) -> str:
        """모드 문자열을 내부 표준값(CBT/PAPER/REAL)으로 정규화합니다."""
        normalized = str(mode or "CBT").upper().strip()
        if normalized == "DRY_RUN":
            return "CBT"
        if normalized == "LIVE":
            return "REAL"
        return normalized

    @classmethod
    def _resolve_resync_mode(
        cls,
        trading_mode: Optional[str],
        api_obj: Optional[Any],
        api_was_injected: bool = False,
    ) -> str:
        """
        포지션 복원용 동기화 모드를 결정합니다.

        기본은 설정값(trading_mode)을 따릅니다.
        CBT는 절대 계좌 동기화 모드(PAPER/REAL)로 보정하지 않습니다.

        NOTE:
            이전 구현은 외부 API 객체 주입 시 CBT를 PAPER/REAL로 보정했지만,
            이 경우 포지션 자동정리 로직이 활성화되어 CBT 포지션 지속성이 깨질 수 있습니다.
        """
        normalized_mode = cls._normalize_mode_label(trading_mode)
        if normalized_mode in ("CBT", "PAPER", "REAL"):
            return normalized_mode

        if not api_was_injected:
            return normalized_mode

        api_is_paper = getattr(api_obj, "is_paper_trading", None)
        if isinstance(api_is_paper, bool):
            return "PAPER" if api_is_paper else "REAL"

        return normalized_mode
    
    def __init__(
        self,
        api: KISApi = None,
        strategy: MultidayTrendATRStrategy = None,
        stock_code: str = None,
        order_quantity: int = None,
        risk_manager: RiskManager = None,
        telegram: TelegramNotifier = None,
        position_store: PositionStore = None,
        market_data_provider: Optional[MarketDataProvider] = None,
    ):
        """
        멀티데이 실행 엔진 초기화
        
        Args:
            api: KIS API 클라이언트
            strategy: 멀티데이 전략
            stock_code: 거래 종목
            order_quantity: 주문 수량
            risk_manager: 리스크 매니저
            telegram: 텔레그램 알림기
            position_store: 포지션 저장소
        """
        # ★ 단일 인스턴스 강제 (감사 보고서 지적 해결)
        if getattr(settings, 'ENFORCE_SINGLE_INSTANCE', True):
            if not ensure_single_instance():
                raise RuntimeError("이미 실행 중인 인스턴스가 있습니다. 프로그램을 종료합니다.")
        
        # 트레이딩 모드 확인 (하위 호환 모드명 포함)
        self.trading_mode = self._normalize_mode_label(
            getattr(settings, "TRADING_MODE", "CBT")
        )
        
        # API 클라이언트 (CBT 모드에서도 데이터 조회용으로 필요)
        is_real_mode = self.trading_mode in ("REAL", "LIVE")
        is_paper = not is_real_mode
        self.api = api or KISApi(is_paper_trading=is_paper)
        
        # 전략 초기화
        self.strategy = strategy or MultidayTrendATRStrategy()
        
        # 기본 설정
        self.stock_code = stock_code or settings.DEFAULT_STOCK_CODE
        self.order_quantity = order_quantity or settings.ORDER_QUANTITY
        self.market_data_provider = market_data_provider
        
        # 리스크 매니저
        self.risk_manager = risk_manager or create_risk_manager_from_settings()
        
        # 텔레그램 알림기
        self.telegram = telegram or get_telegram_notifier()
        self.market_regime_snapshot: Optional[MarketRegimeSnapshot] = None
        self.market_phase_context: Optional[Any] = None
        self.market_venue_context: str = "KRX"
        
        # 포지션 저장소
        self.position_store = position_store or get_position_store()

        # DB 포지션 리포지토리 (실계좌 기준 동기화용)
        try:
            self.db_position_repo = get_position_repository()
        except Exception:
            self.db_position_repo = None
        try:
            self.db_trade_repo = get_trade_repository()
        except Exception:
            self.db_trade_repo = None

        # 리포트용 DB 성과 적재 (실패 시 매매 로직에는 영향 없음)
        try:
            self._report_db = get_db_manager()
        except Exception as e:
            logger.warning(f"[REPORT_DB] DB 매니저 초기화 실패 (성과 적재 비활성): {e}")
            self._report_db = None
        self._report_table_columns: Dict[str, set] = {}
        try:
            self._report_mode = get_db_namespace_mode()
        except Exception:
            self._report_mode = (
                "REAL" if self.trading_mode in ("REAL", "LIVE")
                else ("DRY_RUN" if self.trading_mode == "CBT" else "PAPER")
            )
        self._report_snapshot_interval_sec = max(
            int(getattr(settings, "REPORT_SNAPSHOT_INTERVAL_SEC", 300)),
            30,
        )
        self._last_report_snapshot_at: Optional[datetime] = None
        self._risk_start_capital_sync_date: Optional[str] = None
        self._risk_start_capital_synced: bool = False
        
        # ★ 신규: 주문 동기화 컴포넌트 (감사 보고서 지적 해결)
        self.market_checker = get_market_checker()
        self.order_synchronizer = OrderSynchronizer(
            api=self.api,
            market_checker=self.market_checker,
            execution_timeout=getattr(settings, 'ORDER_EXECUTION_TIMEOUT', 45)
        )
        resync_mode = self._resolve_resync_mode(
            trading_mode=self.trading_mode,
            api_obj=self.api,
            api_was_injected=api is not None,
        )
        if resync_mode != self.trading_mode:
            logger.warning(
                "[RESYNC] 모드 보정 적용: trading_mode=%s, api_is_paper=%s → resync_mode=%s",
                self.trading_mode,
                getattr(self.api, "is_paper_trading", None),
                resync_mode,
            )
        self.position_resync = PositionResynchronizer(
            api=self.api,
            position_store=self.position_store,
            db_repository=self.db_position_repo,
            trading_mode=resync_mode,
            target_symbol=self.stock_code,
        )
        
        # 실행 상태
        self.is_running = False
        
        # ★ 신규: 동적 실행 간격 (감사 보고서 지적 해결)
        self._current_interval = getattr(settings, 'DEFAULT_EXECUTION_INTERVAL', 60)
        self._near_sl_interval = getattr(settings, 'NEAR_STOPLOSS_EXECUTION_INTERVAL', 15)
        self._near_sl_threshold = getattr(settings, 'NEAR_STOPLOSS_THRESHOLD_PCT', 70.0)
        
        # 알림 추적 (중복 방지)
        self._last_near_sl_alert = None
        self._last_near_tp_alert = None
        self._last_trailing_update = None
        self._last_market_closed_skip_log_at: Optional[datetime] = None
        self._daily_signal_cache: Optional[DailySignalSnapshotCache] = None
        self._daily_fetch_count: int = 0
        self._last_fast_risk_sync_at: Optional[datetime] = None
        self._pullback_pipeline_stop_event: Optional[threading.Event] = None
        self._pullback_candidate_store: Optional[ArmedCandidateStore] = None
        self._pullback_daily_context_store: Optional[DailyContextStore] = None
        self._pullback_account_risk_store: Optional[AccountRiskStore] = None
        self._pullback_dirty_symbols: Optional[DirtySymbolSet] = None
        self._pullback_entry_queue: Optional[EntryIntentQueue] = None
        self._pullback_daily_refresh_worker: Optional[DailyRefreshThread] = None
        self._pullback_risk_snapshot_worker: Optional[RiskSnapshotThread] = None
        self._pullback_setup_worker: Optional[PullbackSetupWorker] = None
        self._pullback_timing_worker: Optional[PullbackTimingWorker] = None
        self._pullback_order_worker: Optional[OrderExecutionWorker] = None
        self._pullback_quote_unsubscribe: Optional[Any] = None
        self._strategy_pipeline_registry: Optional[Any] = None
        self._strategy_pipeline_enabled_tags: tuple[str, ...] = ()
        self._pullback_threaded_context_version: str = ""
        self._pullback_daily_context_version: str = ""
        self._pullback_latest_quote_snapshot: Dict[str, Any] = {}
        self._threaded_pullback_pipeline_disabled: bool = False
        self._daily_context_refresh_ms: float = 0.0
        self._daily_context_refresh_count: int = 0
        self._daily_context_store_size: int = 0
        self._pullback_setup_eval_ms: float = 0.0
        self._pullback_setup_skip_reason: str = ""
        self._pullback_timing_eval_ms: float = 0.0
        self._pullback_intent_queue_depth: int = 0
        self._pullback_end_to_end_latency_ms: float = 0.0
        self._pullback_timing_skip_reason: str = ""
        self._strategy_setup_eval_ms: float = 0.0
        self._strategy_timing_eval_ms: float = 0.0
        self._candidate_store_size_by_strategy: Dict[str, int] = {}
        self._intent_queue_depth_by_strategy: Dict[str, int] = {}
        self._strategy_end_to_end_latency_ms: float = 0.0
        self._strategy_regime_snapshot_state_used: str = "absent"
        self._risk_snapshot_refresh_ms: float = 0.0
        self._risk_snapshot_refresh_count: int = 0
        self._holdings_snapshot_refresh_count: int = 0
        self._risk_snapshot_stale: bool = False
        self._order_final_validation_ms: float = 0.0
        self._risk_snapshot_last_success_age_sec: float = -1.0
        self._risk_snapshot_refresh_fail_count: int = 0
        
        # 일별 거래 기록
        self._daily_trades = []
        self._pending_exit_backoff_minutes = int(
            getattr(settings, "PENDING_EXIT_BACKOFF_MINUTES", 5)
        )
        self._pending_exit_max_age_hours = max(
            int(getattr(settings, "PENDING_EXIT_MAX_AGE_HOURS", 72)),
            1,
        )
        self._entry_allowed: bool = True
        self._entry_block_reason: str = ""
        self._entry_block_sticky: bool = False
        self._pending_exit_state: Optional[Dict[str, Any]] = self._sanitize_loaded_pending_exit(
            self.position_store.load_pending_exit()
        )
        if self._pending_exit_state:
            logger.info(
                f"[PENDING_EXIT] 복원: symbol={self._pending_exit_state.get('stock_code')}, "
                f"exit_reason={self._pending_exit_state.get('exit_reason')}, "
                f"next_retry_at={self._pending_exit_state.get('next_retry_at')}"
            )
        
        # ★ 신규: 초기 자본금 기록 (누적 드로다운 계산용)
        self._initial_capital = getattr(settings, 'BACKTEST_INITIAL_CAPITAL', 10_000_000)
        
        # 시그널 핸들러 등록 (종료 시 포지션 저장)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info(
            f"멀티데이 실행 엔진 초기화: "
            f"모드={self.trading_mode}, 종목={self.stock_code}, "
            f"수량={self.order_quantity}"
        )

        # 리스크 상태 출력 전 계좌 평가 스냅샷 동기화
        self._sync_risk_account_snapshot()

        # 리스크 매니저 상태 출력 (공유 매니저 기준 1회만 출력)
        self._maybe_print_risk_status()

    def _maybe_print_risk_status(self) -> None:
        """공유 RiskManager 인스턴스 기준으로 상태를 1회만 출력합니다."""
        if self.risk_manager is None:
            return
        if getattr(self.risk_manager, "_startup_status_printed", False):
            return
        self.risk_manager.print_status()
        try:
            setattr(self.risk_manager, "_startup_status_printed", True)
        except Exception:
            pass

    def set_entry_control(self, allow_entry: bool, reason: str = "", force: bool = False) -> None:
        """외부 정책(유니버스/보유 상한)에 따른 신규 진입 허용 여부 설정."""
        if getattr(self, "_entry_block_sticky", False) and not force:
            if allow_entry:
                return
            self._entry_allowed = False
            if reason and "blocked by reconcile" in reason:
                self._entry_block_reason = reason
            return

        self._entry_allowed = bool(allow_entry)
        self._entry_block_reason = reason or ""
        if force and allow_entry:
            self._entry_block_sticky = False

    def set_market_regime_snapshot(
        self,
        snapshot: Optional[MarketRegimeSnapshot],
    ) -> None:
        self.market_regime_snapshot = snapshot

    def set_market_phase_context(
        self,
        market_phase: Optional[Any],
        venue: Optional[Any] = "KRX",
    ) -> None:
        self.market_phase_context = market_phase
        self.market_venue_context = str(getattr(venue, "value", venue) or "KRX").strip().upper() or "KRX"

    def set_reconcile_entry_block(self, reason: str) -> None:
        """재동기화 실패 시 신규 진입 차단을 고정(sticky) 설정."""
        self._entry_block_sticky = True
        self._entry_allowed = False
        self._entry_block_reason = reason or "[ENTRY] blocked by reconcile: reconcile_failed"

    def is_entry_block_sticky(self) -> bool:
        return bool(self._entry_block_sticky)

    def get_entry_block_reason(self) -> str:
        return self._entry_block_reason or ""

    def _threaded_pipeline_enabled_strategy_tags(self) -> tuple[str, ...]:
        raw_value = str(getattr(settings, "THREADED_PIPELINE_ENABLED_STRATEGIES", "") or "")
        tags = []
        for token in raw_value.split(","):
            normalized = str(token or "").strip()
            if normalized and normalized not in tags:
                tags.append(normalized)
        return tuple(tags)

    def _is_multi_strategy_threaded_pipeline_enabled(self) -> bool:
        if not bool(getattr(settings, "ENABLE_MULTI_STRATEGY_THREADED_PIPELINE", False)):
            return False
        if not bool(getattr(settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False)):
            return False
        if bool(getattr(self, "_threaded_pullback_pipeline_disabled", False)):
            return False
        return "pullback_rebreakout" in self._threaded_pipeline_enabled_strategy_tags()

    def _is_threaded_pullback_pipeline_enabled(self) -> bool:
        if self._is_multi_strategy_threaded_pipeline_enabled():
            return False
        return (
            bool(getattr(settings, "ENABLE_THREADED_PULLBACK_PIPELINE", False))
            and bool(getattr(settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False))
            and not bool(getattr(self, "_threaded_pullback_pipeline_disabled", False))
        )

    def _is_any_threaded_pullback_pipeline_enabled(self) -> bool:
        return (
            self._is_multi_strategy_threaded_pipeline_enabled()
            or self._is_threaded_pullback_pipeline_enabled()
        )

    def _should_defer_pullback_buy_to_threaded_pipeline(self) -> bool:
        if self._is_multi_strategy_threaded_pipeline_enabled():
            return "pullback_rebreakout" in self._threaded_pipeline_enabled_strategy_tags()
        return self._is_threaded_pullback_pipeline_enabled()

    def _is_pullback_daily_refresh_enabled(self) -> bool:
        return self._is_any_threaded_pullback_pipeline_enabled() and bool(
            getattr(settings, "ENABLE_PULLBACK_DAILY_REFRESH_THREAD", False)
        )

    def _is_pullback_risk_snapshot_enabled(self) -> bool:
        return self._is_any_threaded_pullback_pipeline_enabled() and bool(
            getattr(settings, "ENABLE_RISK_SNAPSHOT_THREAD", False)
        )

    def _is_threaded_pullback_pipeline_running(self) -> bool:
        stop_event = getattr(self, "_pullback_pipeline_stop_event", None)
        workers = (
            getattr(self, "_pullback_risk_snapshot_worker", None),
            getattr(self, "_pullback_setup_worker", None),
            getattr(self, "_pullback_timing_worker", None),
            getattr(self, "_pullback_order_worker", None),
        )
        return bool(
            stop_event is not None
            and not stop_event.is_set()
            and any(worker is not None and worker.is_alive() for worker in workers)
        )

    def _handle_pullback_pipeline_worker_error(self, worker_name: str, exc: Exception) -> None:
        logger.error("[PULLBACK_PIPELINE] worker=%s err=%s", worker_name, exc)
        self._threaded_pullback_pipeline_disabled = True
        stop_event = getattr(self, "_pullback_pipeline_stop_event", None)
        if stop_event is not None:
            stop_event.set()

    def _ensure_threaded_pullback_pipeline_started(self) -> None:
        if not self._is_any_threaded_pullback_pipeline_enabled():
            return
        if self._is_threaded_pullback_pipeline_running():
            return
        self._start_threaded_pullback_pipeline()

    def _start_threaded_pullback_pipeline(self) -> None:
        if not self._is_any_threaded_pullback_pipeline_enabled():
            return
        self._stop_threaded_pullback_pipeline()
        self._pullback_pipeline_stop_event = threading.Event()
        self._pullback_candidate_store = ArmedCandidateStore()
        self._pullback_daily_context_store = DailyContextStore(
            max_symbols=max(int(getattr(settings, "DAILY_CONTEXT_STORE_MAX_SYMBOLS", 256) or 256), 1)
        )
        self._pullback_account_risk_store = AccountRiskStore()
        self._pullback_dirty_symbols = DirtySymbolSet()
        self._pullback_entry_queue = EntryIntentQueue(
            maxsize=max(int(getattr(settings, "PULLBACK_ENTRY_INTENT_QUEUE_MAXSIZE", 256) or 256), 1)
        )
        self._strategy_pipeline_enabled_tags = self._threaded_pipeline_enabled_strategy_tags()
        self._strategy_pipeline_registry = None
        if self._is_multi_strategy_threaded_pipeline_enabled():
            self._strategy_pipeline_registry = build_default_strategy_registry(
                pullback_strategy=self.strategy.pullback_strategy
            )
        if self._is_pullback_daily_refresh_enabled():
            self._pullback_daily_refresh_worker = DailyRefreshThread(
                executor=self,
                daily_context_store=self._pullback_daily_context_store,
                stop_event=self._pullback_pipeline_stop_event,
                on_error=self._handle_pullback_pipeline_worker_error,
            )
        if self._is_pullback_risk_snapshot_enabled():
            self._pullback_risk_snapshot_worker = RiskSnapshotThread(
                executor=self,
                account_risk_store=self._pullback_account_risk_store,
                stop_event=self._pullback_pipeline_stop_event,
                on_error=self._handle_pullback_pipeline_worker_error,
            )
        self._pullback_setup_worker = PullbackSetupWorker(
            executor=self,
            candidate_store=self._pullback_candidate_store,
            daily_context_store=self._pullback_daily_context_store,
            dirty_symbols=self._pullback_dirty_symbols,
            strategy_registry=self._strategy_pipeline_registry,
            enabled_strategy_tags=self._strategy_pipeline_enabled_tags,
            stop_event=self._pullback_pipeline_stop_event,
            on_error=self._handle_pullback_pipeline_worker_error,
        )
        self._pullback_timing_worker = PullbackTimingWorker(
            executor=self,
            candidate_store=self._pullback_candidate_store,
            dirty_symbols=self._pullback_dirty_symbols,
            entry_queue=self._pullback_entry_queue,
            strategy_registry=self._strategy_pipeline_registry,
            enabled_strategy_tags=self._strategy_pipeline_enabled_tags,
            stop_event=self._pullback_pipeline_stop_event,
            on_error=self._handle_pullback_pipeline_worker_error,
        )
        self._pullback_order_worker = OrderExecutionWorker(
            executor=self,
            candidate_store=self._pullback_candidate_store,
            entry_queue=self._pullback_entry_queue,
            stop_event=self._pullback_pipeline_stop_event,
            on_error=self._handle_pullback_pipeline_worker_error,
        )
        subscribe_quotes = getattr(self.market_data_provider, "subscribe_quotes", None)
        if callable(subscribe_quotes):
            try:
                def _on_quote(symbol: str, _snapshot: dict) -> None:
                    dirty_symbols = getattr(self, "_pullback_dirty_symbols", None)
                    if dirty_symbols is None:
                        return
                    if str(symbol).zfill(6) != str(self.stock_code).zfill(6):
                        return
                    self.update_pullback_quote_snapshot(
                        {
                            "stock_code": str(symbol).zfill(6),
                            **dict(_snapshot or {}),
                            "data_feed": "ws",
                            "source": str((_snapshot or {}).get("source") or "ws_tick"),
                            "ws_connected": True,
                        }
                    )
                    dirty_symbols.mark(symbol)

                self._pullback_quote_unsubscribe = subscribe_quotes(_on_quote)
            except Exception as exc:
                logger.warning("[PULLBACK_PIPELINE] quote subscription unavailable: %s", exc)
                self._pullback_quote_unsubscribe = None

        try:
            self.update_pullback_quote_snapshot(self.fetch_quote_snapshot())
        except Exception:
            pass

        if self._pullback_daily_refresh_worker is not None:
            self._pullback_daily_refresh_worker.start()
        if self._pullback_risk_snapshot_worker is not None:
            self._pullback_risk_snapshot_worker.start()
        self._pullback_setup_worker.start()
        self._pullback_timing_worker.start()
        self._pullback_order_worker.start()
        logger.info("[PULLBACK_PIPELINE] started symbol=%s", self.stock_code)

    def _stop_threaded_pullback_pipeline(self) -> None:
        unsubscribe = getattr(self, "_pullback_quote_unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                pass
        self._pullback_quote_unsubscribe = None

        stop_event = getattr(self, "_pullback_pipeline_stop_event", None)
        if stop_event is not None:
            stop_event.set()

        current_thread = threading.current_thread()
        for attr_name in (
            "_pullback_daily_refresh_worker",
            "_pullback_risk_snapshot_worker",
            "_pullback_setup_worker",
            "_pullback_timing_worker",
            "_pullback_order_worker",
        ):
            worker = getattr(self, attr_name, None)
            if worker is None:
                continue
            if worker.is_alive() and worker is not current_thread:
                worker.join(timeout=2.0)
            setattr(self, attr_name, None)

        self._pullback_pipeline_stop_event = None
        self._pullback_entry_queue = None
        self._pullback_dirty_symbols = None
        self._pullback_candidate_store = None
        self._pullback_daily_context_store = None
        self._pullback_account_risk_store = None
        self._strategy_pipeline_registry = None
        self._strategy_pipeline_enabled_tags = ()
        self._candidate_store_size_by_strategy = {}
        self._intent_queue_depth_by_strategy = {}
        self._pullback_threaded_context_version = ""
        self._pullback_daily_context_version = ""

    def fetch_cached_intraday_bars_if_available(self, n: int = 120) -> list[dict]:
        provider = getattr(self, "market_data_provider", None)
        if provider is None:
            return []
        is_ws_connected = getattr(provider, "is_ws_connected", None)
        if not callable(is_ws_connected):
            return []
        try:
            if not bool(is_ws_connected()):
                return []
        except Exception:
            return []
        try:
            bars = provider.get_recent_bars(
                stock_code=self.stock_code,
                n=max(int(n), 1),
                timeframe="1m",
            )
        except Exception as exc:
            logger.debug("[PULLBACK_PIPELINE] cached intraday unavailable: symbol=%s err=%s", self.stock_code, exc)
            return []
        normalized = [bar for bar in list(bars or []) if isinstance(bar, dict)]
        if not normalized:
            return []
        if all(float(bar.get("volume", 0.0) or 0.0) <= 0.0 for bar in normalized):
            return []
        return normalized

    def _pullback_pipeline_metrics(self) -> Dict[str, Any]:
        return {
            "daily_context_refresh_ms": float(getattr(self, "_daily_context_refresh_ms", 0.0) or 0.0),
            "daily_context_refresh_count": int(getattr(self, "_daily_context_refresh_count", 0) or 0),
            "daily_context_store_size": int(getattr(self, "_daily_context_store_size", 0) or 0),
            "pullback_setup_eval_ms": float(getattr(self, "_pullback_setup_eval_ms", 0.0) or 0.0),
            "pullback_setup_skip_reason": str(getattr(self, "_pullback_setup_skip_reason", "") or ""),
            "pullback_timing_eval_ms": float(getattr(self, "_pullback_timing_eval_ms", 0.0) or 0.0),
            "pullback_intent_queue_depth": int(getattr(self, "_pullback_intent_queue_depth", 0) or 0),
            "pullback_end_to_end_latency_ms": float(
                getattr(self, "_pullback_end_to_end_latency_ms", 0.0) or 0.0
            ),
            "pullback_candidate_store_size": (
                int(self._pullback_candidate_store.size())
                if self._pullback_candidate_store is not None
                else 0
            ),
            "pullback_timing_skip_reason": str(getattr(self, "_pullback_timing_skip_reason", "") or ""),
            "strategy_setup_eval_ms": float(getattr(self, "_strategy_setup_eval_ms", 0.0) or 0.0),
            "strategy_timing_eval_ms": float(getattr(self, "_strategy_timing_eval_ms", 0.0) or 0.0),
            "candidate_store_size_by_strategy": dict(
                getattr(self, "_candidate_store_size_by_strategy", {}) or {}
            ),
            "intent_queue_depth_by_strategy": dict(
                getattr(self, "_intent_queue_depth_by_strategy", {}) or {}
            ),
            "strategy_end_to_end_latency_ms": float(
                getattr(self, "_strategy_end_to_end_latency_ms", 0.0) or 0.0
            ),
            "strategy_regime_snapshot_state_used": str(
                getattr(self, "_strategy_regime_snapshot_state_used", "absent") or "absent"
            ),
            "risk_snapshot_refresh_ms": float(getattr(self, "_risk_snapshot_refresh_ms", 0.0) or 0.0),
            "risk_snapshot_refresh_count": int(getattr(self, "_risk_snapshot_refresh_count", 0) or 0),
            "holdings_snapshot_refresh_count": int(getattr(self, "_holdings_snapshot_refresh_count", 0) or 0),
            "risk_snapshot_stale": bool(getattr(self, "_risk_snapshot_stale", False)),
            "order_final_validation_ms": float(getattr(self, "_order_final_validation_ms", 0.0) or 0.0),
            "risk_snapshot_last_success_age_sec": float(
                getattr(self, "_risk_snapshot_last_success_age_sec", -1.0) or -1.0
            ),
            "risk_snapshot_refresh_fail_count": int(
                getattr(self, "_risk_snapshot_refresh_fail_count", 0) or 0
            ),
        }

    def update_pullback_quote_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        if not isinstance(snapshot, dict) or not snapshot:
            return
        current = dict(getattr(self, "_pullback_latest_quote_snapshot", {}) or {})
        merged = dict(current)
        for key, value in snapshot.items():
            if value is not None:
                merged[key] = value
        normalized_stock_code = self._normalize_pullback_refresh_symbol(self.stock_code)
        if normalized_stock_code:
            merged.setdefault("stock_code", normalized_stock_code)
        self._pullback_latest_quote_snapshot = merged

    def get_cached_pullback_quote_snapshot(self) -> Dict[str, Any]:
        return dict(getattr(self, "_pullback_latest_quote_snapshot", {}) or {})

    @staticmethod
    def _normalize_pullback_refresh_symbol(raw_symbol: Any) -> str:
        symbol = str(raw_symbol or "").strip()
        if not symbol:
            return ""
        if symbol == "000000":
            return ""
        if not symbol.isdigit():
            return ""
        normalized = symbol.zfill(6)
        return "" if normalized == "000000" else normalized

    def get_pullback_daily_refresh_symbols(self) -> List[str]:
        symbols: List[str] = []
        primary = self._normalize_pullback_refresh_symbol(self.stock_code)
        if primary:
            symbols.append(primary)
        position = getattr(self.strategy, "position", None)
        held_symbol = self._normalize_pullback_refresh_symbol(getattr(position, "symbol", ""))
        if held_symbol and held_symbol not in symbols:
            symbols.append(held_symbol)
        return symbols

    def retry_entry_unblock_via_resync(self) -> bool:
        """재동기화 재시도로 sticky 차단 해제를 시도."""
        if not getattr(self, "_entry_block_sticky", False):
            return self._entry_allowed

        sync_result = self.position_resync.synchronize_on_startup()
        for warning in sync_result.get("warnings", []) or []:
            logger.warning(f"[RESYNC][RETRY] {warning}")
        for recovery in sync_result.get("recoveries", []) or []:
            logger.info(f"[RESYNC][RETRY][AUTO] {recovery}")

        if not sync_result.get("allow_new_entries", True):
            reason = (
                sync_result.get("action")
                or "; ".join(sync_result.get("warnings", []) or [])
                or "reconcile_failed"
            )
            self.set_reconcile_entry_block(f"[ENTRY] blocked by reconcile: {reason}")
            return False

        self.set_entry_control(True, "", force=True)
        logger.info("[ENTRY] reconcile retry succeeded - sticky block released")
        return True
    
    def _signal_handler(self, signum, frame):
        """종료 시그널 핸들러"""
        logger.info(f"종료 시그널 수신: {signum}")
        self._save_position_on_exit()
        sys.exit(0)

    def _parse_iso_datetime(self, raw: Any) -> Optional[datetime]:
        if raw in (None, ""):
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            return KST.localize(dt)
        return dt.astimezone(KST)

    def _sanitize_loaded_pending_exit(
        self,
        pending: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        시작 시 로드한 pending_exit를 검증/정리합니다.

        stale 데이터는 전략 의사결정을 왜곡하지 않도록 즉시 제거합니다.
        """
        if not isinstance(pending, dict):
            return None

        pending_symbol = str(pending.get("stock_code") or "").zfill(6)
        current_symbol = str(self.stock_code or "").zfill(6)
        if pending_symbol and pending_symbol != current_symbol:
            logger.warning(
                "[PENDING_EXIT] 무시: symbol 불일치 stored=%s current=%s",
                pending_symbol,
                current_symbol,
            )
            self.position_store.clear_pending_exit()
            return None

        next_retry_raw = pending.get("next_retry_at")
        updated_raw = pending.get("updated_at") or next_retry_raw
        if next_retry_raw and self._parse_iso_datetime(next_retry_raw) is None:
            logger.warning(
                "[PENDING_EXIT] 무시: next_retry_at 파싱 실패 value=%s",
                next_retry_raw,
            )
            self.position_store.clear_pending_exit()
            return None

        updated_at = self._parse_iso_datetime(updated_raw)
        if updated_at is None:
            logger.warning(
                "[PENDING_EXIT] 무시: updated_at 파싱 실패 value=%s",
                updated_raw,
            )
            self.position_store.clear_pending_exit()
            return None

        now = datetime.now(KST)
        max_age = timedelta(hours=self._pending_exit_max_age_hours)
        if now - updated_at > max_age:
            logger.warning(
                "[PENDING_EXIT] stale 상태 정리: symbol=%s age_hours=%.1f max_age_hours=%s",
                current_symbol,
                (now - updated_at).total_seconds() / 3600.0,
                self._pending_exit_max_age_hours,
            )
            self.position_store.clear_pending_exit()
            return None

        return pending

    def _drop_pending_exit_state(self, reason: str) -> None:
        if not self._pending_exit_state:
            return
        previous = self._pending_exit_state
        self._pending_exit_state = None
        self.position_store.clear_pending_exit()
        logger.info(
            "[PENDING_EXIT] startup 정리: symbol=%s reason=%s prev_retry_key=%s",
            self.stock_code,
            reason,
            previous.get("retry_key"),
        )

    def _build_dry_run_virtual_snapshot(self) -> Dict[str, Any]:
        """DRY_RUN 모드용 가상 계좌 스냅샷을 생성합니다."""
        fallback_capital = float(getattr(settings, "BACKTEST_INITIAL_CAPITAL", 10_000_000))
        snapshot: Dict[str, Any] = {
            "success": True,
            "total_eval": fallback_capital,
            "cash_balance": fallback_capital,
            "total_pnl": 0.0,
            "holdings": [],
        }
        try:
            from cbt.virtual_account import VirtualAccount

            account = VirtualAccount(load_existing=True)
            summary = account.get_account_summary()
            total_equity = float(summary.get("total_equity") or 0.0)
            cash_balance = float(summary.get("cash") or 0.0)
            total_pnl = float(summary.get("total_pnl") or 0.0)
            has_position = bool(summary.get("has_position"))

            if total_equity > 0:
                snapshot["total_eval"] = total_equity
            if cash_balance >= 0:
                snapshot["cash_balance"] = cash_balance
            snapshot["total_pnl"] = total_pnl
            snapshot["holdings"] = [{}] if has_position else []
        except Exception as e:
            logger.warning(f"[RISK][DRY_RUN] 가상 계좌 스냅샷 로드 실패(기본값 사용): {e}")

        return snapshot

    def _sync_risk_starting_capital_from_equity(
        self,
        total_equity: float,
        source: str,
    ) -> None:
        """당일 시작 자본금을 1회 동기화합니다."""
        try:
            equity = float(total_equity)
        except (TypeError, ValueError):
            return
        if equity <= 0:
            return

        today = datetime.now(KST).date().isoformat()
        if getattr(self, "_risk_start_capital_sync_date", None) != today:
            self._risk_start_capital_sync_date = today
            self._risk_start_capital_synced = False

        if getattr(self, "_risk_start_capital_synced", False):
            return

        self.risk_manager.set_starting_capital(equity)
        self._risk_start_capital_synced = True
        logger.info(
            f"[RISK] 당일 시작 자본금 동기화: {equity:,.0f}원 "
            f"(source={source}, mode={getattr(self, '_report_mode', 'PAPER')})"
        )

    @staticmethod
    def _normalize_holdings_rows(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            rows = payload.get("holdings", [])
        else:
            rows = payload
        return [dict(row) for row in list(rows or []) if isinstance(row, dict)]

    def _build_account_risk_snapshot(
        self,
        raw_snapshot: Dict[str, Any],
        *,
        source: str,
        fetched_at: Optional[datetime] = None,
    ) -> Optional[AccountRiskSnapshot]:
        if not isinstance(raw_snapshot, dict) or not raw_snapshot:
            return None
        if not bool(raw_snapshot.get("success", True)):
            return None
        fetched = fetched_at or datetime.now(KST)
        holdings_rows = tuple(self._normalize_holdings_rows(raw_snapshot))
        total_eval = float(raw_snapshot.get("total_eval") or raw_snapshot.get("total_equity") or 0.0)
        cash_balance = float(raw_snapshot.get("cash_balance") or raw_snapshot.get("cash") or 0.0)
        total_pnl = float(raw_snapshot.get("total_pnl") or 0.0)
        version = hashlib.sha1(
            (
                f"{fetched.isoformat()}|{total_eval:.4f}|{cash_balance:.4f}|"
                f"{total_pnl:.4f}|{len(holdings_rows)}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        return AccountRiskSnapshot(
            fetched_at=fetched,
            total_eval=total_eval,
            cash_balance=cash_balance,
            total_pnl=total_pnl,
            holdings=holdings_rows,
            source=str(source or "sync_fallback"),
            success=True,
            stale=False,
            version=version,
        )

    def _build_holdings_risk_snapshot(
        self,
        holdings_payload: Any,
        *,
        source: str,
        fetched_at: Optional[datetime] = None,
    ) -> HoldingsRiskSnapshot:
        fetched = fetched_at or datetime.now(KST)
        holdings_rows = tuple(self._normalize_holdings_rows(holdings_payload))
        total_qty = sum(self._extract_holding_qty(row) for row in holdings_rows)
        version = hashlib.sha1(
            f"{fetched.isoformat()}|{len(holdings_rows)}|{int(total_qty)}".encode("utf-8")
        ).hexdigest()[:16]
        return HoldingsRiskSnapshot(
            fetched_at=fetched,
            holdings=holdings_rows,
            source=str(source or "sync_fallback"),
            success=True,
            stale=False,
            version=version,
        )

    @staticmethod
    def _account_snapshot_to_dict(snapshot: AccountRiskSnapshot) -> Dict[str, Any]:
        return {
            "success": bool(snapshot.success),
            "total_eval": float(snapshot.total_eval or 0.0),
            "cash_balance": float(snapshot.cash_balance or 0.0),
            "total_pnl": float(snapshot.total_pnl or 0.0),
            "holdings": [dict(row) for row in snapshot.holdings],
            "source": snapshot.source,
            "version": snapshot.version,
        }

    def refresh_account_risk_snapshot_sync(self, source: str = "sync_fallback") -> Optional[AccountRiskSnapshot]:
        report_mode = str(getattr(self, "_report_mode", "PAPER")).upper()
        fetched_at = datetime.now(KST)
        try:
            if report_mode == "DRY_RUN":
                raw_snapshot = self._build_dry_run_virtual_snapshot()
            else:
                raw_snapshot = self.api.get_account_balance()
                self.__class__._shared_account_snapshot_fetch_count += 1
        except Exception as err:
            logger.warning("[PULLBACK_RISK] account snapshot fetch failed: source=%s err=%s", source, err)
            return None

        snapshot = self._build_account_risk_snapshot(
            raw_snapshot,
            source=source,
            fetched_at=fetched_at,
        )
        if snapshot is None:
            logger.warning("[PULLBACK_RISK] account snapshot empty: source=%s", source)
            return None
        store = getattr(self, "_pullback_account_risk_store", None)
        if store is not None:
            store.replace_account_snapshot(snapshot)
        return snapshot

    def refresh_holdings_risk_snapshot_sync(self, source: str = "sync_fallback") -> Optional[HoldingsRiskSnapshot]:
        report_mode = str(getattr(self, "_report_mode", "PAPER")).upper()
        fetched_at = datetime.now(KST)
        try:
            if report_mode == "DRY_RUN":
                payload = self._build_dry_run_virtual_snapshot().get("holdings", [])
            elif hasattr(self.api, "get_holdings"):
                payload = self.api.get_holdings()
            else:
                balance = self.api.get_account_balance()
                self.__class__._shared_account_snapshot_fetch_count += 1
                if not isinstance(balance, dict) or not balance.get("success"):
                    logger.warning("[PULLBACK_RISK] holdings fallback balance unavailable: source=%s", source)
                    return None
                payload = balance.get("holdings", [])
        except Exception as err:
            logger.warning("[PULLBACK_RISK] holdings snapshot fetch failed: source=%s err=%s", source, err)
            return None

        snapshot = self._build_holdings_risk_snapshot(
            payload,
            source=source,
            fetched_at=fetched_at,
        )
        store = getattr(self, "_pullback_account_risk_store", None)
        if store is not None:
            store.replace_holdings_snapshot(snapshot)
        return snapshot

    def get_account_risk_snapshot_state(
        self,
        *,
        ttl_sec: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> tuple[Optional[AccountRiskSnapshot], str]:
        store = getattr(self, "_pullback_account_risk_store", None)
        if store is None:
            return None, "absent"
        effective_ttl = float(
            ttl_sec
            if ttl_sec is not None
            else float(getattr(settings, "RISK_SNAPSHOT_TTL_SEC", 60) or 60.0)
        )
        return store.get_account_state(ttl_sec=effective_ttl, now=now)

    def get_holdings_risk_snapshot_state(
        self,
        *,
        ttl_sec: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> tuple[Optional[HoldingsRiskSnapshot], str]:
        store = getattr(self, "_pullback_account_risk_store", None)
        if store is None:
            return None, "absent"
        effective_ttl = float(
            ttl_sec
            if ttl_sec is not None
            else float(getattr(settings, "HOLDINGS_SNAPSHOT_TTL_SEC", 30) or 30.0)
        )
        return store.get_holdings_state(ttl_sec=effective_ttl, now=now)

    def cached_account_has_holding(self, stock_code: str) -> bool:
        snapshot, state = self.get_holdings_risk_snapshot_state()
        if snapshot is None or state != "fresh":
            return False
        return self._find_holding_row(snapshot.holdings, stock_code) is not None

    def _find_holding_row(self, holdings_rows: Any, stock_code: str) -> Optional[Dict[str, Any]]:
        target = str(stock_code or "").strip()
        for raw_holding in self._normalize_holdings_rows(holdings_rows):
            symbol = str(
                raw_holding.get("stock_code")
                or raw_holding.get("pdno")
                or raw_holding.get("symbol")
                or ""
            ).strip()
            if symbol != target:
                continue
            if self._extract_holding_qty(raw_holding) <= 0:
                continue
            return raw_holding
        return None

    def _sync_risk_account_snapshot_legacy(self) -> None:
        """리스크 패널용 계좌 스냅샷 동기화 (짧은 TTL 캐시 적용)."""
        ttl_sec = int(getattr(settings, "RISK_ACCOUNT_SNAPSHOT_TTL_SEC", 60))
        now = datetime.now(KST)
        report_mode = str(getattr(self, "_report_mode", "PAPER")).upper()

        cached_snapshot = self.__class__._shared_account_snapshot
        cached_ts = self.__class__._shared_account_snapshot_ts
        if (
            cached_snapshot is not None
            and cached_ts is not None
            and (now - cached_ts).total_seconds() < ttl_sec
        ):
            self.risk_manager.update_account_snapshot(cached_snapshot)
            cached_total_equity = float(
                cached_snapshot.get("total_eval")
                or cached_snapshot.get("total_equity")
                or 0.0
            )
            self._sync_risk_starting_capital_from_equity(
                cached_total_equity,
                source="SNAPSHOT_CACHE",
            )
            logger.info(
                f"[RISK] 계좌 스냅샷 캐시 사용: age={(now - cached_ts).total_seconds():.1f}s"
            )
            return

        if report_mode == "DRY_RUN":
            snapshot = self._build_dry_run_virtual_snapshot()
            self.__class__._shared_account_snapshot_fetch_count += 1
        else:
            try:
                snapshot = self.api.get_account_balance()
                self.__class__._shared_account_snapshot_fetch_count += 1
            except Exception as e:
                logger.warning(f"[RISK] 계좌 스냅샷 조회 실패: {e}")
                return

        if not snapshot or not snapshot.get("success"):
            logger.warning("[RISK] 계좌 스냅샷 조회 결과가 비어있어 상태 반영을 건너뜁니다.")
            return

        self.__class__._shared_account_snapshot = snapshot
        self.__class__._shared_account_snapshot_ts = now
        self.risk_manager.update_account_snapshot(snapshot)
        total_equity = float(snapshot.get("total_eval") or snapshot.get("total_equity") or 0.0)
        self._sync_risk_starting_capital_from_equity(
            total_equity,
            source="LIVE_SNAPSHOT" if report_mode != "DRY_RUN" else "DRY_RUN_VIRTUAL",
        )
        total_pnl = float(snapshot.get("total_pnl", 0.0))
        logger.info(
            "[RISK] 계좌 스냅샷 반영: "
            f"holdings={len(snapshot.get('holdings', []))}, total_pnl={total_pnl:+,.0f}원"
        )

    def _sync_risk_account_snapshot(self) -> None:
        now = datetime.now(KST)
        if self._is_pullback_risk_snapshot_enabled():
            snapshot, state = self.get_account_risk_snapshot_state(now=now)
            store = getattr(self, "_pullback_account_risk_store", None)
            if store is not None:
                last_success_age = store.get_last_account_success_age_sec(now=now)
                self._risk_snapshot_last_success_age_sec = (
                    float(last_success_age) if last_success_age is not None else -1.0
                )
            self._risk_snapshot_stale = state != "fresh"
            if snapshot is not None:
                self.risk_manager.update_account_snapshot(self._account_snapshot_to_dict(snapshot))
                self._sync_risk_starting_capital_from_equity(
                    float(snapshot.total_eval or 0.0),
                    source=str(snapshot.source or "BACKGROUND_REFRESH").upper(),
                )
                logger.info(
                    "[RISK] 계좌 스냅샷 반영(source=%s state=%s): holdings=%s total_pnl=%+.0f원",
                    snapshot.source,
                    state,
                    len(snapshot.holdings),
                    float(snapshot.total_pnl or 0.0),
                )
                return
        self._sync_risk_account_snapshot_legacy()

    # ════════════════════════════════════════════════════════════════
    # 리포트 DB 적재 (매매 로직과 분리된 보조 기능)
    # ════════════════════════════════════════════════════════════════

    def _report_db_available(self) -> bool:
        return getattr(self, "_report_db", None) is not None

    def _to_db_datetime(self, value: Optional[datetime] = None) -> datetime:
        dt = value or datetime.now(KST)
        if dt.tzinfo is not None:
            dt = dt.astimezone(KST).replace(tzinfo=None)
        return dt

    def _get_report_table_columns(self, table_name: str) -> set:
        cached = self._report_table_columns.get(table_name)
        if cached is not None:
            return cached

        if not self._report_db_available():
            self._report_table_columns[table_name] = set()
            return set()

        try:
            rows = self._report_db.execute_query(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (self._report_db.config.database, table_name),
            )
            cols = {
                str(row.get("column_name") or row.get("COLUMN_NAME") or "").lower()
                for row in (rows or [])
            }
            self._report_table_columns[table_name] = cols
            return cols
        except Exception as err:
            logger.warning(f"[REPORT_DB] 컬럼 조회 실패: table={table_name}, err={err}")
            self._report_table_columns[table_name] = set()
            return set()

    @staticmethod
    def _pick_first_column(columns: set, *candidates: str) -> Optional[str]:
        for candidate in candidates:
            if candidate in columns:
                return candidate
        return None

    def _persist_trade_record(
        self,
        *,
        side: str,
        price: float,
        quantity: int,
        order_no: Optional[str],
        reason: Optional[str] = None,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        entry_price: Optional[float] = None,
        holding_days: Optional[int] = None,
        executed_at: Optional[datetime] = None,
    ) -> None:
        if not self._report_db_available():
            return

        columns = self._get_report_table_columns("trades")
        if not columns:
            return

        symbol_col = self._pick_first_column(columns, "symbol", "stock_code")
        if not symbol_col or "side" not in columns or "price" not in columns:
            return

        executed_time = self._to_db_datetime(executed_at)
        col_values: Dict[str, Any] = {
            symbol_col: self.stock_code,
            "side": side.upper(),
            "price": float(price),
        }
        if "quantity" in columns:
            col_values["quantity"] = int(quantity)
        if "executed_at" in columns:
            col_values["executed_at"] = executed_time
        if "reason" in columns and reason is not None:
            col_values["reason"] = str(reason)
        if "pnl" in columns and pnl is not None:
            col_values["pnl"] = float(pnl)
        if "pnl_percent" in columns and pnl_pct is not None:
            col_values["pnl_percent"] = float(pnl_pct)
        if "entry_price" in columns and entry_price is not None:
            col_values["entry_price"] = float(entry_price)
        if "holding_days" in columns and holding_days is not None:
            col_values["holding_days"] = int(holding_days)
        if "order_no" in columns and order_no:
            col_values["order_no"] = str(order_no)
        if "mode" in columns:
            col_values["mode"] = self._report_mode

        has_idempotency = "idempotency_key" in columns
        if has_idempotency:
            idem_source = (
                f"{self._report_mode}|{side.upper()}|{self.stock_code}|{price:.4f}|"
                f"{int(quantity)}|{order_no or ''}|{executed_time.isoformat()}|{reason or ''}"
            )
            col_values["idempotency_key"] = hashlib.sha256(
                idem_source.encode("utf-8")
            ).hexdigest()

        insert_columns = list(col_values.keys())
        placeholders = ", ".join(["%s"] * len(insert_columns))
        col_sql = ", ".join(insert_columns)
        sql = f"INSERT INTO trades ({col_sql}) VALUES ({placeholders})"
        if has_idempotency:
            sql += " ON DUPLICATE KEY UPDATE idempotency_key = VALUES(idempotency_key)"

        try:
            self._report_db.execute_command(
                sql,
                tuple(col_values[column] for column in insert_columns),
            )
        except QueryError as err:
            logger.warning(
                f"[REPORT_DB] 거래 적재 실패(무시): side={side}, symbol={self.stock_code}, err={err}"
            )
        except Exception as err:
            logger.warning(
                f"[REPORT_DB] 거래 적재 예외(무시): side={side}, symbol={self.stock_code}, err={err}"
            )

    def _parse_fill_executed_at(self, raw: Any) -> datetime:
        if isinstance(raw, datetime):
            return raw
        if raw in (None, ""):
            return datetime.now(KST)
        try:
            dt = datetime.fromisoformat(str(raw))
            if dt.tzinfo is None:
                return KST.localize(dt)
            return dt.astimezone(KST)
        except Exception:
            return datetime.now(KST)

    def _build_fill_idempotency_key(
        self,
        *,
        side: str,
        order_no: str,
        exec_id: Optional[str],
        executed_at: datetime,
        price: float,
        quantity: int,
    ) -> str:
        if exec_id:
            seed = (
                f"FILL|{self._report_mode}|{side.upper()}|{self.stock_code}|"
                f"{order_no}|{exec_id}"
            )
        else:
            seed = (
                f"FILL|{self._report_mode}|{side.upper()}|{self.stock_code}|"
                f"{order_no}|{executed_at.isoformat()}|{price:.2f}|{quantity}"
            )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def _extract_execution_fills(self, sync_result: Any, side: str) -> list[Dict[str, Any]]:
        fills: list[Dict[str, Any]] = []
        raw_fills = getattr(sync_result, "fills", None) or []
        for raw in raw_fills:
            if not isinstance(raw, dict):
                continue
            qty = int(raw.get("qty") or 0)
            price_raw = raw.get("price")
            if qty <= 0 or price_raw in (None, ""):
                continue
            price_dec = quantize_price(Decimal(str(price_raw)))
            if price_dec <= 0:
                continue
            executed_at = self._parse_fill_executed_at(raw.get("executed_at"))
            fills.append(
                {
                    "order_no": str(raw.get("order_no") or getattr(sync_result, "order_no", "") or ""),
                    "exec_id": (str(raw.get("exec_id")).strip() if raw.get("exec_id") not in (None, "") else None),
                    "executed_at": executed_at,
                    "price": float(price_dec),
                    "qty": qty,
                    "side": str(raw.get("side") or side).upper(),
                }
            )

        if not fills:
            qty = int(getattr(sync_result, "exec_qty", 0) or 0)
            price = float(getattr(sync_result, "exec_price", 0.0) or 0.0)
            if qty > 0 and price > 0:
                price_dec = quantize_price(Decimal(str(price)))
                fills.append(
                    {
                        "order_no": str(getattr(sync_result, "order_no", "") or ""),
                        "exec_id": None,
                        "executed_at": datetime.now(KST),
                        "price": float(price_dec),
                        "qty": qty,
                        "side": side.upper(),
                    }
                )
        return fills

    def _record_execution_fill(
        self,
        *,
        side: str,
        fill: Dict[str, Any],
        reason: Optional[str] = None,
        entry_price: Optional[float] = None,
        holding_days: Optional[int] = None,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ) -> bool:
        order_no = str(fill.get("order_no") or "")
        exec_id = fill.get("exec_id")
        executed_at = self._parse_fill_executed_at(fill.get("executed_at"))
        price = float(fill.get("price") or 0.0)
        quantity = int(fill.get("qty") or 0)
        if quantity <= 0 or price <= 0:
            return False

        idem_key = self._build_fill_idempotency_key(
            side=side,
            order_no=order_no,
            exec_id=str(exec_id) if exec_id else None,
            executed_at=executed_at,
            price=price,
            quantity=quantity,
        )

        if self.db_trade_repo is not None:
            _record, created = self.db_trade_repo.save_execution_fill(
                symbol=self.stock_code,
                side=side,
                price=price,
                quantity=quantity,
                executed_at=executed_at,
                order_no=order_no,
                exec_id=str(exec_id) if exec_id else None,
                reason=reason,
                entry_price=entry_price,
                holding_days=holding_days,
                pnl=pnl,
                pnl_percent=pnl_pct,
                idempotency_key=idem_key,
            )
            if created:
                return True
            return False

        # DB repository가 없으면 기존 적재 경로로 폴백(중복 방지는 제한적)
        self._persist_trade_record(
            side=side,
            price=price,
            quantity=quantity,
            order_no=order_no or None,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_price=entry_price,
            holding_days=holding_days,
            executed_at=executed_at,
        )
        return True

    def _sync_db_position_from_strategy(self) -> None:
        if self.db_position_repo is None:
            return
        try:
            if not self.strategy.has_position:
                self.db_position_repo.close_position(self.stock_code)
                return
            pos = self.strategy.position
            entry_price = float(quantize_price(Decimal(str(pos.entry_price))))
            self.db_position_repo.upsert_from_account_holding(
                symbol=self.stock_code,
                entry_price=entry_price,
                quantity=int(pos.quantity),
                atr_at_entry=float(pos.atr_at_entry),
                stop_price=float(pos.stop_loss),
                take_profit_price=float(pos.take_profit) if pos.take_profit is not None else None,
                trailing_stop=float(pos.trailing_stop),
                highest_price=float(pos.highest_price),
                entry_time=datetime.now(KST),
            )
        except Exception as err:
            logger.warning(f"[REPO] 포지션 동기화 실패(무시): {err}")

    def _persist_account_snapshot(self, force: bool = False) -> None:
        if not self._report_db_available():
            return

        now = datetime.now(KST)
        if (
            not force
            and self._last_report_snapshot_at is not None
            and (now - self._last_report_snapshot_at).total_seconds()
            < self._report_snapshot_interval_sec
        ):
            return

        columns = self._get_report_table_columns("account_snapshots")
        if not columns or "snapshot_time" not in columns:
            return

        if self._report_mode == "DRY_RUN":
            snapshot = self._build_dry_run_virtual_snapshot()
        else:
            try:
                snapshot = self.api.get_account_balance()
            except Exception as err:
                logger.warning(f"[REPORT_DB] 계좌 스냅샷 조회 실패(무시): {err}")
                return

        if not snapshot or not snapshot.get("success"):
            return

        holdings = snapshot.get("holdings") or []
        total_equity = float(snapshot.get("total_eval") or snapshot.get("total_equity") or 0.0)
        self._sync_risk_starting_capital_from_equity(
            total_equity,
            source="REPORT_DB_SNAPSHOT" if self._report_mode != "DRY_RUN" else "DRY_RUN_VIRTUAL",
        )
        cash_balance = float(snapshot.get("cash_balance") or snapshot.get("cash") or 0.0)
        unrealized_pnl = float(
            sum(float(item.get("pnl_amount") or 0.0) for item in holdings)
        )
        realized_pnl = 0.0
        try:
            pnl_summary = self.risk_manager.get_daily_pnl_summary()
            realized_pnl = float((pnl_summary or {}).get("realized_pnl") or 0.0)
        except Exception:
            realized_pnl = 0.0

        col_values: Dict[str, Any] = {
            "snapshot_time": self._to_db_datetime(now),
        }
        if "total_equity" in columns:
            col_values["total_equity"] = total_equity
        if "cash" in columns:
            col_values["cash"] = cash_balance
        if "unrealized_pnl" in columns:
            col_values["unrealized_pnl"] = unrealized_pnl
        if "realized_pnl" in columns:
            col_values["realized_pnl"] = realized_pnl
        if "position_count" in columns:
            col_values["position_count"] = int(len(holdings))
        if "mode" in columns:
            col_values["mode"] = self._report_mode

        insert_columns = list(col_values.keys())
        placeholders = ", ".join(["%s"] * len(insert_columns))
        col_sql = ", ".join(insert_columns)
        update_candidates = [
            "total_equity",
            "cash",
            "unrealized_pnl",
            "realized_pnl",
            "position_count",
            "mode",
        ]
        update_sql = ", ".join(
            f"{name} = VALUES({name})" for name in update_candidates if name in col_values
        )
        if update_sql:
            sql = (
                f"INSERT INTO account_snapshots ({col_sql}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_sql}"
            )
        else:
            sql = f"INSERT INTO account_snapshots ({col_sql}) VALUES ({placeholders})"

        try:
            self._report_db.execute_command(
                sql,
                tuple(col_values[column] for column in insert_columns),
            )
            self._last_report_snapshot_at = now
        except QueryError as err:
            logger.warning(f"[REPORT_DB] 계좌 스냅샷 적재 실패(무시): {err}")
        except Exception as err:
            logger.warning(f"[REPORT_DB] 계좌 스냅샷 적재 예외(무시): {err}")
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 영속화
    # ════════════════════════════════════════════════════════════════
    
    def _save_position_on_exit(self) -> None:
        """
        프로그램 종료 시 포지션 저장
        
        ★ 포지션 보유 중이면 저장
        ★ 포지션 없으면 저장 파일 클리어
        """
        if self.strategy.has_position:
            pos = self.strategy.position
            stored = StoredPosition.from_multiday_position(pos)
            self.position_store.save_position(stored)
            if self._pending_exit_state is not None:
                self.position_store.save_pending_exit(self._pending_exit_state)
            logger.info(f"포지션 저장 완료: {pos.symbol}")
        else:
            self.position_store.clear_position()
            self._pending_exit_state = None
            logger.info("포지션 없음 - 저장 파일 클리어")
    
    def restore_position_on_start(self) -> bool:
        """
        프로그램 시작 시 포지션 복원
        
        ★ 감사 보고서 해결: API 기준 재동기화로 불일치 방지
        
        ★ 순서:
            1. API 기준 재동기화 (실제 보유 확인)
            2. 저장된 데이터와 비교
            3. 불일치 해결
            4. 전략에 복원
            5. 텔레그램 알림
        
        Returns:
            bool: 복원 성공 여부
        """
        logger.info("=" * 50)
        logger.info("포지션 재동기화 프로세스 시작")
        logger.info("=" * 50)

        if not self.__class__._pending_recovery_done:
            pending_orders = self.order_synchronizer.recover_pending_orders()
            self.__class__._pending_recovery_done = True
            self.__class__._pending_recovery_count = len(pending_orders)
            if pending_orders:
                logger.warning(
                    f"[RESYNC] DB 기준 미종결 주문 {len(pending_orders)}건 발견 "
                    "(open_orders/pending_orders/partial_fills 복구 필요)"
                )
        elif self.__class__._pending_recovery_count:
            logger.info(
                f"[RESYNC] 미종결 주문 점검은 이미 수행됨 "
                f"(count={self.__class__._pending_recovery_count})"
            )
        
        # ★ API 기준 재동기화 (감사 보고서 지적 해결)
        sync_result = self.position_resync.synchronize_on_startup()
        if not sync_result.get("allow_new_entries", True):
            reason = (
                sync_result.get("action")
                or "; ".join(sync_result.get("warnings", []) or [])
                or "reconcile_failed"
            )
            self.set_reconcile_entry_block(f"[ENTRY] blocked by reconcile: {reason}")
        elif getattr(self, "_entry_block_sticky", False):
            self.set_entry_control(True, "", force=True)

        # 경고 메시지 출력
        for warning in sync_result.get("warnings", []):
            logger.warning(f"[RESYNC] {warning}")
            self.telegram.notify_warning(f"포지션 동기화: {warning}")

        # 자동복구 메시지 출력/알림
        for recovery in sync_result.get("recoveries", []):
            logger.info(f"[RESYNC][AUTO] {recovery}")
            self.telegram.notify_info(f"포지션 자동복구 완료: {recovery}")

        summary = sync_result.get("summary") or {}
        holdings = sync_result.get("holdings") or []
        if summary and not self.__class__._startup_resync_summary_notified:
            summary_msg = (
                "복원 완료: "
                f"{summary.get('total_holdings', 0)}종목 / "
                f"업데이트 {summary.get('updated', 0)} / "
                f"신규생성 {summary.get('created', 0)} / "
                f"좀비정리 {summary.get('zombies', 0)}"
            )
            logger.info(f"[RESYNC] {summary_msg}")
            if holdings:
                detail_lines = []
                for item in holdings:
                    code = str(item.get("stock_code") or "").strip()
                    qty = int(item.get("qty") or 0)
                    raw_avg = item.get("avg_price")
                    try:
                        avg_val = quantize_price(Decimal(str(raw_avg or "0")))
                    except Exception:
                        avg_val = Decimal("0.00")
                    avg = f"{avg_val:,.2f}원"
                    if code and qty > 0:
                        detail_lines.append(f"- {code}: qty={qty}, avg={avg}")
                if detail_lines:
                    self.telegram.notify_info(summary_msg + "\n" + "\n".join(detail_lines))
            self.__class__._startup_resync_summary_notified = True
        
        action = sync_result.get("action", "")
        sync_warnings = sync_result.get("warnings", []) or []
        sync_recoveries = sync_result.get("recoveries", []) or []
        sync_detail_lines = []
        if sync_warnings:
            sync_detail_lines.append(f"warnings={sync_warnings}")
        if sync_recoveries:
            sync_detail_lines.append(f"recoveries={sync_recoveries}")
        sync_detail_lines.append(f"action={action}")
        sync_error_detail = "\n".join(sync_detail_lines)
        
        if action in ("NO_POSITION", "AUTO_RECOVERED_CLEARED"):
            self._drop_pending_exit_state("no_position")
            logger.info("포지션 없음 확인")
            return False
        
        elif action == "API_FAILED":
            self._drop_pending_exit_state("api_failed")
            logger.error("포지션 동기화 실패(API) - 신규 진입 차단 유지")
            self.telegram.notify_error(
                "포지션 동기화 실패",
                "잔고조회 실패로 신규 진입을 차단했습니다.",
                error_detail=sync_error_detail,
            )
            return False
        
        elif action == "UNTRACKED_HOLDING":
            self._drop_pending_exit_state("untracked_holding")
            # 미기록 보유 발견 - 위험 상황
            logger.error("미기록 보유 발견 - 수동 확인 필요")
            self.telegram.notify_error(
                "미기록 보유 발견",
                "저장된 포지션 없이 실제 보유가 발견되었습니다.\n"
                "수동으로 확인하고 처리하세요.",
                error_detail=sync_error_detail,
            )
            return False
        
        elif action == "STORED_INVALID":
            self._drop_pending_exit_state("stored_invalid")
            # 저장 데이터 무효 - 이미 삭제됨
            logger.warning("저장된 포지션이 무효하여 삭제됨")
            return False
        
        elif action == "CRITICAL_MISMATCH":
            self._drop_pending_exit_state("critical_mismatch")
            # 심각한 불일치 - 킬 스위치 권장
            logger.error("심각한 포지션 불일치 - 수동 확인 필요")
            self.telegram.notify_error(
                "심각한 포지션 불일치",
                "저장된 포지션과 실제 보유가 다릅니다.\n"
                "즉시 확인하세요!",
                error_detail=sync_error_detail,
            )
            # 안전을 위해 킬 스위치 발동 고려
            return False
        
        elif action in (
            "MATCHED",
            "QTY_ADJUSTED",
            "AUTO_RECOVERED_FROM_API",
            "AUTO_RECOVERED_REPLACED",
        ):
            # 정상 또는 수량 조정됨
            stored = sync_result.get("position")
            
            if stored is None:
                logger.error("동기화 성공했으나 포지션 데이터 없음")
                return False

            stored = self._apply_stop_loss_guard_to_stored_position(
                stored,
                context=f"restore:{action}",
            )
            
            logger.info(
                f"포지션 동기화 완료: {stored.stock_code} @ {stored.entry_price:,.0f}원, "
                f"수량={stored.quantity}주, ATR={stored.atr_at_entry:,.0f} (고정)"
            )
            
            # 전략에 복원
            multiday_pos = stored.to_multiday_position()
            self.strategy.restore_position(multiday_pos)
            
            # 보유 일수 계산
            holding_days = self._calculate_holding_days(stored.entry_date)
            
            # 텔레그램 알림
            self.telegram.notify_position_restored(
                stock_code=stored.stock_code,
                entry_price=stored.entry_price,
                quantity=stored.quantity,
                entry_date=stored.entry_date,
                holding_days=holding_days,
                stop_loss=stored.stop_loss,
                take_profit=stored.take_profit,
                trailing_stop=stored.trailing_stop,
                atr_at_entry=stored.atr_at_entry
            )
            
            logger.info(
                f"포지션 복원 완료: 보유 {holding_days}일째, "
                f"Exit 조건 감시 재개"
            )
            
            return True
        
        else:
            logger.warning(f"알 수 없는 동기화 결과: {action}")
            self.telegram.notify_error(
                "알 수 없는 포지션 동기화 결과",
                "포지션 복원 동기화 결과를 해석하지 못했습니다.",
                error_detail=sync_error_detail,
            )
            return False
    
    def _calculate_holding_days(self, entry_date: str) -> int:
        """보유 일수 계산"""
        try:
            entry = datetime.strptime(entry_date, "%Y-%m-%d").date()
            return (datetime.now(KST).date() - entry).days + 1
        except ValueError:
            return 0
    
    # ════════════════════════════════════════════════════════════════
    # 데이터 조회
    # ════════════════════════════════════════════════════════════════
    
    def fetch_market_data_for_symbol(self, stock_code: str) -> pd.DataFrame:
        """지정 종목의 일봉 시장 데이터 조회"""
        try:
            if self.market_data_provider is not None:
                bars = self.market_data_provider.get_recent_bars(
                    stock_code=stock_code,
                    n=100,
                    timeframe="D",
                )
                if not bars:
                    logger.warning(f"시장 데이터 없음(provider): {stock_code}")
                    return pd.DataFrame()
                df = pd.DataFrame(bars)
                if "date" in df.columns:
                    df = df.sort_values("date").reset_index(drop=True)
                return df

            self._daily_fetch_count += 1
            df = self.api.get_daily_ohlcv(
                stock_code=stock_code,
                period_type="D"
            )
            
            if df.empty:
                logger.warning(f"시장 데이터 없음: {stock_code}")
            
            return df
            
        except _KIS_API_ERROR_TYPES as e:
            logger.error(f"시장 데이터 조회 실패: {e}")
            return pd.DataFrame()

    def fetch_market_data(self) -> pd.DataFrame:
        """시장 데이터 조회"""
        return self.fetch_market_data_for_symbol(self.stock_code)

    @staticmethod
    def _normalize_market_data_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or getattr(df, "empty", True):
            return pd.DataFrame()
        normalized = df.copy()
        if "date" in normalized.columns:
            normalized = normalized.sort_values("date").reset_index(drop=True)
        for column in ("open", "high", "low", "close", "volume"):
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0.0)
        return normalized

    @staticmethod
    def _trade_date_key(check_time: Optional[datetime] = None) -> str:
        return (check_time or datetime.now(KST)).astimezone(KST).date().isoformat()

    @staticmethod
    def _extract_market_data_trade_date(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        try:
            return datetime.fromisoformat(str(value)).date().isoformat()
        except Exception:
            raw = str(value).strip()
            if not raw:
                return None
            return raw[:10]

    def _capture_execution_metrics(self) -> Dict[str, int]:
        provider = getattr(self, "market_data_provider", None)
        provider_metrics: Dict[str, Any] = {}
        metrics_fn = getattr(provider, "metrics", None) if provider is not None else None
        if callable(metrics_fn):
            try:
                provider_metrics = dict(metrics_fn() or {})
            except Exception:
                provider_metrics = {}
        return {
            "daily_fetch_calls": int(
                provider_metrics.get("daily_fetch_calls", 0)
                or provider_metrics.get("rest_daily_fetch_calls", 0)
                or 0
            ) + int(getattr(self, "_daily_fetch_count", 0) or 0),
            "rest_quote_calls": int(provider_metrics.get("rest_quote_calls", 0) or 0),
            "account_snapshot_calls": int(self.__class__._shared_account_snapshot_fetch_count),
            "ws_reconnect_count": int(provider_metrics.get("ws_reconnect_count", 0) or 0),
            "ws_fallback_count": int(provider_metrics.get("ws_fallback_count", 0) or 0),
        }

    @staticmethod
    def _metrics_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
        delta: Dict[str, int] = {}
        for key in set(before) | set(after):
            delta[key] = max(int(after.get(key, 0)) - int(before.get(key, 0)), 0)
        return delta

    def metrics(self) -> Dict[str, int]:
        return self._capture_execution_metrics()

    def sync_account_and_risk_if_due(
        self,
        *,
        force: bool = False,
        min_interval_sec: Optional[float] = None,
    ) -> bool:
        now = datetime.now(KST)
        if not force and min_interval_sec is not None and self._last_fast_risk_sync_at is not None:
            if (now - self._last_fast_risk_sync_at).total_seconds() < float(min_interval_sec):
                return False
        self._sync_risk_account_snapshot()
        self._last_fast_risk_sync_at = now
        return True

    def _build_live_daily_frame_from_cache(
        self,
        *,
        quote_snapshot: Dict[str, Any],
        check_time: datetime,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        trade_date = self._trade_date_key(check_time)
        cache = self._daily_signal_cache
        refresh_interval_sec = max(
            float(getattr(settings, "FAST_EVAL_DAILY_REFRESH_INTERVAL_SEC", 300.0) or 300.0),
            0.0,
        )
        refresh_required = (
            force_refresh
            or cache is None
            or cache.trade_date != trade_date
            or (
                refresh_interval_sec > 0
                and cache is not None
                and (check_time - cache.last_refreshed_at).total_seconds() >= refresh_interval_sec
            )
        )
        if refresh_required:
            frame = self.fetch_market_data()
            normalized = self._normalize_market_data_frame(frame)
            if normalized.empty:
                return normalized
            cache = DailySignalSnapshotCache(
                trade_date=trade_date,
                source_frame=normalized,
                last_refreshed_at=check_time,
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
            )
            self._daily_signal_cache = cache

        cache = self._daily_signal_cache
        if cache is None or cache.source_frame.empty:
            return pd.DataFrame()

        live_df = cache.source_frame.copy()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", cache.open_price) or cache.open_price or 0.0)
        session_high = float(quote_snapshot.get("session_high", 0.0) or 0.0)
        session_low = float(quote_snapshot.get("session_low", 0.0) or 0.0)

        if current_price > 0 and session_high <= 0:
            session_high = current_price
        if current_price > 0 and session_low <= 0:
            session_low = current_price
        if open_price > 0 and session_high <= 0:
            session_high = open_price
        if open_price > 0 and session_low <= 0:
            session_low = open_price

        last_trade_date = self._extract_market_data_trade_date(
            live_df.iloc[-1].get("date") if len(live_df) > 0 else None
        )
        if last_trade_date == trade_date:
            row_idx = live_df.index[-1]
            existing_high = float(live_df.at[row_idx, "high"] or 0.0)
            existing_low = float(live_df.at[row_idx, "low"] or 0.0)
            existing_open = float(live_df.at[row_idx, "open"] or 0.0)
            if open_price > 0:
                live_df.at[row_idx, "open"] = open_price
            elif existing_open > 0:
                open_price = existing_open
            if session_high > 0:
                live_df.at[row_idx, "high"] = max(existing_high, session_high)
            if session_low > 0:
                live_df.at[row_idx, "low"] = (
                    min(existing_low, session_low)
                    if existing_low > 0
                    else session_low
                )
            if current_price > 0:
                live_df.at[row_idx, "close"] = current_price
        else:
            seed_price = float(current_price or open_price or 0.0)
            live_df = pd.concat(
                [
                    live_df,
                    pd.DataFrame(
                        [
                            {
                                "date": check_time,
                                "open": float(open_price or seed_price),
                                "high": float(session_high or seed_price),
                                "low": float(session_low or seed_price),
                                "close": float(seed_price),
                                "volume": 0.0,
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )

        live_df = self._normalize_market_data_frame(live_df)
        indicator_df = self.strategy.add_indicators(live_df)
        if indicator_df.empty:
            return indicator_df

        latest = indicator_df.iloc[-1]
        cache.stock_name = str(quote_snapshot.get("stock_name") or cache.stock_name or "")
        cache.open_price = open_price
        cache.prev_high = float(latest.get("prev_high", 0.0) or 0.0)
        cache.prev_close = float(latest.get("prev_close", 0.0) or 0.0)
        cache.atr = float(latest.get("atr", 0.0) or 0.0)
        cache.adx = float(latest.get("adx", 0.0) or 0.0)
        cache.trend = str(self.strategy.get_trend(indicator_df).value)
        return indicator_df

    def prepare_market_context(
        self,
        *,
        use_cached_daily: bool = False,
        force_daily_refresh: bool = False,
    ) -> Optional[PreparedEvaluationContext]:
        decision_time = datetime.now(KST)
        quote_snapshot = self.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
        if current_price <= 0:
            fallback_current, fallback_open = self.fetch_current_price()
            current_price = float(fallback_current or 0.0)
            open_price = float(fallback_open or 0.0)
            quote_snapshot = dict(quote_snapshot)
            quote_snapshot["current_price"] = current_price
            quote_snapshot["open_price"] = open_price
        if current_price <= 0:
            return None

        if hasattr(self.api, "is_network_disconnected_for") and self.api.is_network_disconnected_for(60):
            return None

        if use_cached_daily:
            indicator_df = self._build_live_daily_frame_from_cache(
                quote_snapshot=quote_snapshot,
                check_time=decision_time,
                force_refresh=force_daily_refresh,
            )
            df = indicator_df if not indicator_df.empty else pd.DataFrame()
        else:
            df = self.fetch_market_data()
        if df.empty:
            return None

        intraday_bars: List[dict] = []
        if bool(getattr(settings, "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY", False)):
            intraday_lookback = max(
                int(getattr(settings, "ORB_ENTRY_CUTOFF_MINUTES", 90) or 90)
                + int(getattr(settings, "ORB_OPENING_RANGE_MINUTES", 5) or 5)
                + 5,
                30,
            )
            intraday_bars = self.fetch_intraday_bars(n=intraday_lookback)

        has_pending_order = (
            self._has_active_pending_buy_order()
            if (
                bool(getattr(settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False))
                or bool(getattr(settings, "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY", False))
            )
            and not self.strategy.has_position
            else False
        )

        return PreparedEvaluationContext(
            decision_time=decision_time,
            df=df,
            quote_snapshot=quote_snapshot,
            current_price=current_price,
            open_price=open_price,
            intraday_bars=intraday_bars,
            has_pending_order=has_pending_order,
            used_cached_daily=bool(use_cached_daily),
        )

    def evaluate_signal_from_context(self, context: PreparedEvaluationContext) -> TradingSignal:
        signal = self.strategy.generate_signal(
            df=context.df,
            current_price=context.current_price,
            open_price=context.open_price,
            stock_code=self.stock_code,
            stock_name=str(context.quote_snapshot.get("stock_name") or ""),
            check_time=context.decision_time,
            market_phase=getattr(self, "market_phase_context", None),
            market_venue=getattr(self, "market_venue_context", "KRX"),
            has_pending_order=context.has_pending_order,
            market_regime_snapshot=getattr(self, "market_regime_snapshot", None),
            intraday_bars=context.intraday_bars,
            defer_pullback_buy=self._should_defer_pullback_buy_to_threaded_pipeline(),
        )
        signal = self._apply_stale_quote_guard(signal, context.quote_snapshot)
        signal.meta = dict(getattr(signal, "meta", {}) or {})
        signal.meta.setdefault(
            "signal_time",
            (
                context.quote_snapshot.get("received_at").isoformat()
                if isinstance(context.quote_snapshot.get("received_at"), datetime)
                else context.decision_time.isoformat()
            ),
        )
        signal.meta.setdefault("decision_time", context.decision_time.isoformat())
        signal.meta.setdefault("current_price_at_signal", context.current_price)
        signal.meta.setdefault("quote_age_sec", context.quote_snapshot.get("quote_age_sec"))
        signal.meta.setdefault("data_feed_source", context.quote_snapshot.get("source"))
        signal.meta.setdefault("order_style", self._resolve_entry_order_style())
        return signal

    def fetch_intraday_bars(self, n: int = 120) -> list[dict]:
        """장중 1분봉 조회. 실시간 분봉이 없거나 synthetic bar만 있으면 빈 리스트를 반환합니다."""
        provider = getattr(self, "market_data_provider", None)
        if provider is None:
            return []
        try:
            bars = provider.get_recent_bars(
                stock_code=self.stock_code,
                n=max(int(n), 1),
                timeframe="1m",
            )
        except Exception as exc:
            logger.debug("[ORB] intraday bars unavailable: symbol=%s err=%s", self.stock_code, exc)
            return []

        normalized = [bar for bar in list(bars or []) if isinstance(bar, dict)]
        if not normalized:
            return []
        # REST fallback minute bars are synthetic and carry zero volume only.
        if all(float(bar.get("volume", 0.0) or 0.0) <= 0.0 for bar in normalized):
            return []
        return normalized

    def fetch_current_price(self) -> tuple:
        """
        현재가 및 시가 조회
        
        Returns:
            tuple: (현재가, 시가)
        """
        try:
            quote_snapshot = self.fetch_quote_snapshot()
            current = float(quote_snapshot.get("current_price", 0.0) or 0.0)
            open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
            return current, open_price
        except Exception as e:
            logger.error(f"현재가 조회 실패: {e}")
            return 0.0, 0.0

    def fetch_quote_snapshot(self) -> Dict[str, Any]:
        """현재가/시가/호가/수신시각을 포함한 quote snapshot을 반환합니다."""
        try:
            snapshot: Dict[str, Any]
            market_data_provider = getattr(self, "market_data_provider", None)
            if market_data_provider is not None:
                snapshot_fn = getattr(market_data_provider, "get_quote_snapshot", None)
                if callable(snapshot_fn):
                    snapshot = snapshot_fn(self.stock_code) or {}
                    if snapshot:
                        self.update_pullback_quote_snapshot(snapshot)
                        return snapshot

                quote_fn = getattr(market_data_provider, "get_latest_price_with_open", None)
                if callable(quote_fn):
                    current, open_price = quote_fn(self.stock_code)
                    snapshot = {
                        "stock_code": self.stock_code,
                        "stock_name": None,
                        "current_price": float(current or 0.0),
                        "open_price": float(open_price or 0.0),
                        "best_ask": None,
                        "best_bid": None,
                        "received_at": datetime.now(KST),
                        "quote_age_sec": 0.0,
                        "source": "provider_quote",
                        "data_feed": "provider",
                        "ws_connected": False,
                    }
                    self.update_pullback_quote_snapshot(snapshot)
                    return snapshot

                current = float(market_data_provider.get_latest_price(self.stock_code) or 0.0)
                price_data = self.api.get_current_price(self.stock_code)
                snapshot = {
                    "stock_code": self.stock_code,
                    "stock_name": price_data.get("stock_name"),
                    "current_price": current,
                    "open_price": float(price_data.get("open_price", 0.0) or 0.0),
                    "best_ask": None,
                    "best_bid": None,
                    "received_at": datetime.now(KST),
                    "quote_age_sec": 0.0,
                    "source": "provider_price_plus_rest_open",
                    "data_feed": "provider",
                    "ws_connected": False,
                }
                self.update_pullback_quote_snapshot(snapshot)
                return snapshot

            price_data = self.api.get_current_price(self.stock_code)
            snapshot = {
                "stock_code": self.stock_code,
                "stock_name": price_data.get("stock_name"),
                "current_price": float(price_data.get("current_price", 0.0) or 0.0),
                "open_price": float(price_data.get("open_price", 0.0) or 0.0),
                "best_ask": None,
                "best_bid": None,
                "received_at": datetime.now(KST),
                "quote_age_sec": 0.0,
                "source": "rest_quote",
                "data_feed": "rest",
                "ws_connected": False,
            }
            self.update_pullback_quote_snapshot(snapshot)
            return snapshot
        except Exception as e:
            logger.error(f"호가 스냅샷 조회 실패: {e}")
            snapshot = {
                "stock_code": self.stock_code,
                "stock_name": None,
                "current_price": 0.0,
                "open_price": 0.0,
                "best_ask": None,
                "best_bid": None,
                "received_at": None,
                "quote_age_sec": float("inf"),
                "source": "quote_error",
                "data_feed": "unknown",
                "ws_connected": False,
            }
            self.update_pullback_quote_snapshot(snapshot)
            return snapshot

    @staticmethod
    def _format_entry_log_value(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value)

    def _log_entry_event(self, prefix: str, **payload: Any) -> None:
        details = " ".join(
            f"{key}={self._format_entry_log_value(value)}"
            for key, value in payload.items()
            if value is not None
        )
        logger.info(f"{prefix} {details}".rstrip())

    @staticmethod
    def _strategy_tag(signal: Optional[TradingSignal]) -> str:
        meta = dict(getattr(signal, "meta", {}) or {})
        return str(meta.get("strategy_tag") or "trend_atr")

    def _asset_type_max_pct(self, asset_type: str, etf_value: float, stock_value: float) -> float:
        if str(asset_type or "").upper() == ASSET_TYPE_ETF:
            return max(float(etf_value or 0.0), 0.0)
        return max(float(stock_value or 0.0), 0.0)

    def _apply_stale_quote_guard(
        self,
        signal: TradingSignal,
        quote_snapshot: Dict[str, Any],
    ) -> TradingSignal:
        signal_type_value = self._signal_type_value(getattr(signal, "signal_type", SignalType.BUY.value))
        if signal_type_value != SignalType.BUY.value:
            return signal
        if not bool(getattr(settings, "ENABLE_STALE_QUOTE_GUARD", False)):
            return signal

        data_feed = str(quote_snapshot.get("data_feed") or "").lower()
        if data_feed != "ws":
            return signal

        max_age_sec = max(float(getattr(settings, "QUOTE_MAX_AGE_SEC", 0.0) or 0.0), 0.0)
        raw_quote_age = quote_snapshot.get("quote_age_sec", float("inf"))
        try:
            quote_age_sec = float(raw_quote_age)
        except (TypeError, ValueError):
            quote_age_sec = float("inf")
        source = str(quote_snapshot.get("source") or "")
        ws_connected = bool(quote_snapshot.get("ws_connected"))

        is_stale = (source != "ws_tick") or (not ws_connected) or (quote_age_sec > max_age_sec)
        if not is_stale:
            return signal

        self._log_entry_event(
            "[ENTRY_BLOCK]",
            reason="stale_quote",
            symbol=self.stock_code,
            quote_age_sec=quote_age_sec,
            max_age_sec=max_age_sec,
            data_feed=data_feed,
            ws_connected=ws_connected,
            source=source,
        )
        meta = dict(getattr(signal, "meta", {}) or {})
        meta.update(
            {
                "reason_code": "stale_quote",
                "quote_age_sec": quote_age_sec,
                "max_age_sec": max_age_sec,
                "data_feed_source": source or data_feed,
                "ws_connected": ws_connected,
            }
        )
        return TradingSignal(
            signal_type=SignalType.HOLD,
            price=float(signal.price or 0.0),
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            trailing_stop=signal.trailing_stop,
            reason="WS quote stale - 신규 BUY 차단",
            atr=signal.atr,
            trend=signal.trend,
            reason_code="stale_quote",
            meta=meta,
        )

    def _get_market_regime_snapshot(
        self,
        check_time: Optional[datetime] = None,
    ) -> Optional[MarketRegimeSnapshot]:
        return materialize_market_regime_snapshot(
            getattr(self, "market_regime_snapshot", None),
            check_time,
        )

    def _annotate_signal_with_market_regime(
        self,
        signal: TradingSignal,
        snapshot: Optional[MarketRegimeSnapshot],
    ) -> None:
        signal.meta = dict(getattr(signal, "meta", {}) or {})
        if snapshot is None:
            signal.meta.update({"market_regime_source": "main_loop_cache"})
            return

        signal.meta.update(
            {
                "market_regime": snapshot.regime.value,
                "market_regime_reason": snapshot.reason,
                "market_regime_as_of": snapshot.as_of.isoformat(),
                "market_regime_is_stale": snapshot.is_stale,
                "market_regime_source": snapshot.source,
                "market_regime_kospi_symbol": snapshot.kospi_symbol,
                "market_regime_kosdaq_symbol": snapshot.kosdaq_symbol,
            }
        )

    def _apply_market_regime_guard(
        self,
        signal: TradingSignal,
        check_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        signal_type_value = self._signal_type_value(getattr(signal, "signal_type", SignalType.BUY.value))
        if signal_type_value != SignalType.BUY.value:
            return {"blocked": False, "snapshot": None, "message": ""}
        if not bool(getattr(settings, "ENABLE_MARKET_REGIME_FILTER", False)):
            return {"blocked": False, "snapshot": None, "message": ""}

        snapshot = self._get_market_regime_snapshot(check_time=check_time)
        self._annotate_signal_with_market_regime(signal, snapshot)

        if snapshot is None or snapshot.is_stale:
            fail_mode = get_market_regime_fail_mode()
            as_of = snapshot.as_of.isoformat() if snapshot is not None else "none"
            stale_age_sec = (
                f"{snapshot.stale_age_sec(check_time):.1f}"
                if snapshot is not None
                else "none"
            )
            logger.warning(
                "[MARKET_REGIME] snapshot_stale as_of=%s stale_age_sec=%s fail_mode=%s",
                as_of,
                stale_age_sec,
                fail_mode,
            )
            if fail_mode == "open":
                return {
                    "blocked": False,
                    "snapshot": snapshot,
                    "message": "",
                }

            self._log_entry_event(
                "[ENTRY_BLOCK]",
                reason="market_regime_stale",
                fail_mode=fail_mode,
                symbol=self.stock_code,
            )
            return {
                "blocked": True,
                "snapshot": snapshot,
                "message": "시장 레짐 snapshot stale - 신규 BUY 차단",
            }

        regime_value = snapshot.regime.value

        if (
            snapshot.regime == MarketRegime.BAD
            and bool(getattr(settings, "MARKET_REGIME_BAD_BLOCK_NEW_BUY", True))
        ):
            self._log_entry_event(
                "[ENTRY_BLOCK]",
                reason="market_regime_bad",
                regime=regime_value,
                regime_reason=snapshot.reason,
                symbol=self.stock_code,
            )
            return {
                "blocked": True,
                "snapshot": snapshot,
                "message": "시장 레짐 BAD - 신규 BUY 차단",
            }

        if (
            snapshot.regime == MarketRegime.NEUTRAL
            and not bool(getattr(settings, "MARKET_REGIME_NEUTRAL_ALLOW_BUY", True))
        ):
            self._log_entry_event(
                "[ENTRY_BLOCK]",
                reason="market_regime_neutral",
                regime=regime_value,
                regime_reason=snapshot.reason,
                symbol=self.stock_code,
            )
            return {
                "blocked": True,
                "snapshot": snapshot,
                "message": "시장 레짐 NEUTRAL - 신규 BUY 차단",
            }

        self._log_entry_event(
            "[ENTRY_TRACE]",
            market_regime=regime_value,
            as_of=snapshot.as_of.isoformat(),
            stale=snapshot.is_stale,
            symbol=self.stock_code,
        )
        if snapshot.regime == MarketRegime.NEUTRAL:
            logger.info(
                "[MARKET_REGIME] entry_allowed regime=%s reason=%s symbol=%s neutral_allow_buy=%s position_scale=%.6f",
                regime_value,
                snapshot.reason,
                self.stock_code,
                bool(getattr(settings, "MARKET_REGIME_NEUTRAL_ALLOW_BUY", True)),
                float(getattr(settings, "MARKET_REGIME_NEUTRAL_POSITION_SCALE", 1.0) or 1.0),
            )

        return {
            "blocked": False,
            "snapshot": snapshot,
            "message": "",
        }

    def _resolve_entry_order_style(self) -> str:
        style = str(getattr(settings, "ENTRY_ORDER_STYLE", "market") or "market").strip().lower()
        return style if style in ("market", "protected_limit") else "market"

    def _build_entry_order_plan(
        self,
        signal: TradingSignal,
        quote_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        asset_type = str(
            (getattr(signal, "meta", {}) or {}).get("asset_type")
            or detect_asset_type(self.stock_code, str(quote_snapshot.get("stock_name") or ""))
        ).upper()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        best_ask_raw = quote_snapshot.get("best_ask")
        best_ask = float(best_ask_raw) if best_ask_raw not in (None, "", 0, 0.0) else None
        meta = dict(getattr(signal, "meta", {}) or {})
        prev_high = float(meta.get("prev_high") or 0.0)
        entry_reference_price = float(meta.get("entry_reference_price") or prev_high or 0.0)
        entry_reference_label = str(meta.get("entry_reference_label") or "prev_high")
        style = self._resolve_entry_order_style()

        if style != "protected_limit":
            return {
                "blocked": False,
                "style": "market",
                "order_type": "01",
                "price": 0.0,
                "asset_type": asset_type,
                "best_ask": best_ask,
                "current_price": current_price,
                "prev_high": prev_high,
                "entry_reference_price": entry_reference_price,
                "entry_reference_label": entry_reference_label,
                "limit_price": 0.0,
                "slippage_cap_pct": float(getattr(settings, "ENTRY_MAX_SLIPPAGE_PCT", 0.0) or 0.0),
                "extension_pct_at_order": compute_extension_pct(current_price, entry_reference_price),
            }

        protect_ticks = (
            max(int(getattr(settings, "ENTRY_PROTECT_TICKS_ETF", 0) or 0), 0)
            if asset_type == ASSET_TYPE_ETF
            else max(int(getattr(settings, "ENTRY_PROTECT_TICKS_STOCK", 0) or 0), 0)
        )
        base_price = float(best_ask if best_ask is not None and best_ask > 0 else current_price)
        tick_size = get_tick_size(base_price, asset_type)
        working_price = base_price + (tick_size * protect_ticks)
        limit_price = align_price_to_tick(working_price, asset_type, direction="up")

        breakout_cap_pct = max(float(meta.get("max_allowed_pct") or 0.0), 0.0)
        if breakout_cap_pct <= 0 and bool(getattr(settings, "ENABLE_BREAKOUT_EXTENSION_CAP", False)):
            breakout_cap_pct = self._asset_type_max_pct(
                asset_type,
                getattr(settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.0),
                getattr(settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.0),
            )
        slippage_cap_pct = max(float(getattr(settings, "ENTRY_MAX_SLIPPAGE_PCT", 0.0) or 0.0), 0.0)

        limit_extension_pct = compute_extension_pct(limit_price, entry_reference_price)
        extension_pct_at_order = compute_extension_pct(current_price, entry_reference_price)
        limit_slippage_pct = compute_extension_pct(limit_price, current_price)

        blocked = False
        block_reason = ""
        if breakout_cap_pct > 0 and entry_reference_price > 0 and limit_extension_pct > breakout_cap_pct:
            blocked = True
            block_reason = "protected_limit_exceeds_cap"
        if not blocked and slippage_cap_pct > 0 and current_price > 0 and limit_slippage_pct > slippage_cap_pct:
            blocked = True
            block_reason = "protected_limit_exceeds_cap"

        return {
            "blocked": blocked,
            "reason_code": block_reason,
            "style": "protected_limit",
            "order_type": "00",
            "price": float(limit_price or 0.0),
            "asset_type": asset_type,
            "best_ask": best_ask,
            "current_price": current_price,
            "prev_high": prev_high,
            "entry_reference_price": entry_reference_price,
            "entry_reference_label": entry_reference_label,
            "protect_ticks": protect_ticks,
            "tick_size": tick_size,
            "limit_price": float(limit_price or 0.0),
            "breakout_cap_pct": breakout_cap_pct,
            "slippage_cap_pct": slippage_cap_pct,
            "extension_pct_at_order": extension_pct_at_order,
            "limit_extension_pct": limit_extension_pct,
            "limit_slippage_pct": limit_slippage_pct,
        }

    def _log_entry_trace(self, stage: str, signal: TradingSignal, payload: Dict[str, Any]) -> None:
        meta = dict(getattr(signal, "meta", {}) or {})
        base = {
            "stage": stage,
            "strategy_tag": meta.get("strategy_tag"),
            "symbol": self.stock_code,
            "signal_time": meta.get("signal_time"),
            "decision_time": meta.get("decision_time"),
            "order_submit_time": payload.get("order_submit_time"),
            "fill_time": payload.get("fill_time"),
            "prev_high": meta.get("prev_high"),
            "current_price_at_signal": meta.get("current_price_at_signal"),
            "current_price_at_order": payload.get("current_price_at_order"),
            "fill_price": payload.get("fill_price"),
            "extension_pct_at_signal": meta.get("extension_pct"),
            "extension_pct_at_order": payload.get("extension_pct_at_order"),
            "quote_age_sec": payload.get("quote_age_sec", meta.get("quote_age_sec")),
            "data_feed_source": payload.get("data_feed_source", meta.get("data_feed_source")),
            "order_style": payload.get("order_style", meta.get("order_style")),
            "best_ask": payload.get("best_ask"),
            "limit_price": payload.get("limit_price"),
        }
        self._log_entry_event("[ENTRY_TRACE]", **base)

    def _has_active_pending_buy_order(self) -> bool:
        syncer = getattr(self, "order_synchronizer", None)
        if syncer is None:
            return False
        checker = getattr(syncer, "has_open_order_for_symbol", None)
        if callable(checker):
            try:
                return bool(checker(self.stock_code, side="BUY"))
            except Exception as exc:
                logger.debug("[SYNC] pending buy order check failed: symbol=%s err=%s", self.stock_code, exc)
        return False

    def _resolve_holdings_snapshot_for_final_validation(self) -> tuple[Optional[HoldingsRiskSnapshot], str]:
        report_mode = str(getattr(self, "_report_mode", "PAPER")).upper()
        snapshot, state = self.get_holdings_risk_snapshot_state(now=datetime.now(KST))
        if snapshot is not None and state == "fresh":
            return snapshot, "background_refresh"
        if snapshot is not None and state == "stale" and report_mode in ("PAPER", "DRY_RUN"):
            return snapshot, "last_known_good"

        fallback = self.refresh_holdings_risk_snapshot_sync(source="final_validation")
        if fallback is not None:
            return fallback, "final_validation"
        if snapshot is not None and report_mode in ("PAPER", "DRY_RUN"):
            return snapshot, "last_known_good"
        return None, state or "absent"

    def _resolve_account_snapshot_for_final_validation(self) -> tuple[Optional[AccountRiskSnapshot], str]:
        report_mode = str(getattr(self, "_report_mode", "PAPER")).upper()
        snapshot, state = self.get_account_risk_snapshot_state(now=datetime.now(KST))
        if snapshot is not None and state == "fresh":
            return snapshot, "background_refresh"
        if snapshot is not None and state == "stale" and report_mode in ("PAPER", "DRY_RUN"):
            return snapshot, "last_known_good"

        fallback = self.refresh_account_risk_snapshot_sync(source="final_validation")
        if fallback is not None:
            return fallback, "final_validation"
        if snapshot is not None and report_mode in ("PAPER", "DRY_RUN"):
            return snapshot, "last_known_good"
        return None, state or "absent"

    def _run_order_final_validation(self, signal: TradingSignal) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            strategy_tag = self._strategy_tag(signal)
            if strategy_tag != "pullback_rebreakout" or not self._is_pullback_risk_snapshot_enabled():
                return {"allowed": True, "mode": "disabled"}
            if str(getattr(settings, "ORDER_FINAL_VALIDATION_MODE", "light") or "light").strip().lower() != "light":
                return {"allowed": True, "mode": "bypass"}

            holdings_snapshot, holdings_source = self._resolve_holdings_snapshot_for_final_validation()
            if holdings_snapshot is None:
                if str(getattr(self, "_report_mode", "PAPER")).upper() == "REAL":
                    return {
                        "allowed": False,
                        "reason_code": "holdings_snapshot_unavailable",
                        "message": "보유 스냅샷 부재로 pullback 신규 BUY 차단",
                    }
            elif self._find_holding_row(holdings_snapshot.holdings, self.stock_code) is not None:
                return {
                    "allowed": False,
                    "reason_code": "existing_position_snapshot",
                    "message": "계좌 보유 확인 - pullback 신규 BUY 차단",
                }

            account_snapshot, account_source = self._resolve_account_snapshot_for_final_validation()
            if account_snapshot is None:
                if str(getattr(self, "_report_mode", "PAPER")).upper() == "REAL":
                    return {
                        "allowed": False,
                        "reason_code": "account_snapshot_unavailable",
                        "message": "계좌 스냅샷 부재로 pullback 신규 BUY 차단",
                    }
            else:
                estimated_cost = float(getattr(signal, "price", 0.0) or 0.0) * max(int(self.order_quantity or 0), 1)
                cash_balance = float(account_snapshot.cash_balance or 0.0)
                if estimated_cost > 0 and cash_balance > 0 and estimated_cost > cash_balance:
                    return {
                        "allowed": False,
                        "reason_code": "insufficient_cash_snapshot",
                        "message": "예수금 부족 스냅샷으로 pullback 신규 BUY 차단",
                    }

            signal.meta = dict(getattr(signal, "meta", {}) or {})
            signal.meta["risk_snapshot_account_source"] = account_source
            signal.meta["risk_snapshot_holdings_source"] = holdings_source
            return {"allowed": True, "mode": "light"}
        finally:
            setattr(
                self,
                "_order_final_validation_ms",
                (time.perf_counter() - started) * 1000.0,
            )

    # ════════════════════════════════════════════════════════════════
    # 주문 실행 (모드별)
    # ════════════════════════════════════════════════════════════════
    
    def _can_place_orders(self) -> bool:
        """실제 주문 가능 여부"""
        return self.trading_mode in ("LIVE", "REAL", "PAPER")

    @staticmethod
    def _signal_type_value(signal_type: Any) -> str:
        """
        시그널 타입을 문자열로 정규화합니다.

        모듈 네임스페이스가 다른 Enum 인스턴스(kis_trend_atr_trading.strategy.* vs strategy.*)
        가 섞여도 value 기준으로 안정적으로 분기하기 위해 사용합니다.
        """
        raw_value = getattr(signal_type, "value", signal_type)
        return str(raw_value).upper().strip()

    def _build_exit_retry_key(self, signal: TradingSignal) -> str:
        exit_reason = signal.exit_reason.value if signal.exit_reason else ExitReason.MANUAL_EXIT.value
        reason_code = signal.reason_code or "NO_REASON_CODE"
        return f"{self.stock_code}:{exit_reason}:{reason_code}"

    @staticmethod
    def _is_market_unavailable_error(message: str) -> bool:
        lower = (message or "").lower()
        keywords = [
            "장종료",
            "장 종료",
            "장마감",
            "폐장",
            "주문불가",
            "주문 불가",
            "market closed",
            "market is closed",
        ]
        return any(k in lower for k in keywords)

    @staticmethod
    def _is_no_holding_error(message: str) -> bool:
        """매도 주문 실패가 계좌 무보유(잔고 없음) 케이스인지 판별합니다."""
        lower = (message or "").lower()
        keywords = [
            "잔고내역이 없습니다",
            "잔고 내역이 없습니다",
            "보유내역이 없습니다",
            "보유 내역이 없습니다",
            "no holding",
            "no holdings",
            "insufficient holding",
        ]
        return any(k in lower for k in keywords)

    def _finalize_evaluation_result(
        self,
        *,
        signal: TradingSignal,
        context: PreparedEvaluationContext,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        signal_type_value = self._signal_type_value(signal.signal_type)
        if signal_type_value == SignalType.BUY.value:
            self._log_entry_trace(
                "signal",
                signal,
                {
                    "current_price_at_order": None,
                    "extension_pct_at_order": None,
                    "quote_age_sec": context.quote_snapshot.get("quote_age_sec"),
                    "data_feed_source": context.quote_snapshot.get("source"),
                    "order_style": self._resolve_entry_order_style(),
                    "best_ask": context.quote_snapshot.get("best_ask"),
                    "limit_price": None,
                },
            )

        result["signal"] = {
            "type": signal_type_value,
            "strategy_tag": self._strategy_tag(signal),
            "price": signal.price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "trailing_stop": signal.trailing_stop,
            "exit_reason": signal.exit_reason.value if signal.exit_reason else None,
            "reason": signal.reason,
            "reason_code": getattr(signal, "reason_code", ""),
            "atr": signal.atr,
            "trend": signal.trend.value,
            "quote_age_sec": context.quote_snapshot.get("quote_age_sec"),
            "data_feed_source": context.quote_snapshot.get("source"),
        }

        logger.info(
            f"시그널: {signal_type_value} | "
            f"전략: {self._strategy_tag(signal)} | "
            f"가격: {context.current_price:,.0f}원 | "
            f"추세: {signal.trend.value} | "
            f"사유: {signal.reason}"
        )

        if signal_type_value == SignalType.BUY.value:
            if not self._entry_allowed:
                block_msg = self._entry_block_reason or f"[ENTRY] blocked: symbol={self.stock_code}"
                logger.info(block_msg)
                result["order_result"] = {
                    "success": False,
                    "skipped": True,
                    "message": block_msg,
                }
            else:
                result["order_result"] = self.execute_buy(signal)
        elif signal_type_value == SignalType.SELL.value:
            result["order_result"] = self._execute_exit_with_pending_control(signal)
        elif signal_type_value == SignalType.HOLD.value:
            self._check_and_send_alerts(signal, context.current_price)

        if self.strategy.has_position:
            pos = self.strategy.position
            pnl, pnl_pct = pos.get_pnl(context.current_price)
            result["position"] = {
                "symbol": pos.symbol,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "trailing_stop": pos.trailing_stop,
                "highest_price": pos.highest_price,
                "atr_at_entry": pos.atr_at_entry,
                "current_price": context.current_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "entry_date": pos.entry_date,
            }
            logger.info(
                f"포지션: {pos.symbol} | "
                f"진입: {pos.entry_price:,.0f}원 | "
                f"현재: {context.current_price:,.0f}원 | "
                f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)"
            )
        else:
            logger.info("포지션: 없음")

        return result

    @staticmethod
    def _extract_holding_qty(holding: Dict[str, Any]) -> int:
        raw_values = (
            holding.get("qty"),
            holding.get("quantity"),
            holding.get("holding_qty"),
            holding.get("sellable_qty"),
            holding.get("hldg_qty"),
            holding.get("ord_psbl_qty"),
        )
        for raw in raw_values:
            if raw is None:
                continue
            try:
                parsed = int(float(str(raw).replace(",", "").strip()))
            except (TypeError, ValueError):
                continue
            return max(parsed, 0)
        return 0

    @staticmethod
    def _extract_holding_avg_price(holding: Dict[str, Any]) -> float:
        raw_values = (
            holding.get("avg_price"),
            holding.get("pchs_avg_pric"),
            holding.get("avg_buy_price"),
            holding.get("pchs_avrg_pric"),
            holding.get("entry_price"),
        )
        for raw in raw_values:
            if raw is None:
                continue
            try:
                parsed = float(str(raw).replace(",", "").strip())
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0.0

    def _compute_entry_exit_prices(self, entry_price: float, atr: float) -> tuple[float, float, float]:
        """
        체결가/복구가 기준으로 진입 ATR, 손절가, 익절가를 일관 계산합니다.

        - 손절가는 전략 공용 함수(calculate_stop_loss)를 사용해 MAX_LOSS_PCT 캡을 반드시 반영합니다.
        - ATR 누락 시 보수적 폴백(진입가의 1%)을 사용합니다.
        """
        entry = float(entry_price or 0.0)
        resolved_atr = float(atr or 0.0)
        if entry <= 0:
            return 0.0, 0.0, 0.0
        if resolved_atr <= 0:
            resolved_atr = max(entry * 0.01, 1.0)

        calc_stop_loss = getattr(self.strategy, "calculate_stop_loss", None)
        calc_take_profit = getattr(self.strategy, "calculate_take_profit", None)
        if callable(calc_stop_loss):
            stop_loss = float(calc_stop_loss(entry, resolved_atr))
        else:
            stop_loss = max(entry - (resolved_atr * 2.0), 0.0)
        if callable(calc_take_profit):
            take_profit = float(calc_take_profit(entry, resolved_atr))
        else:
            take_profit = entry + (resolved_atr * 3.0)
        return resolved_atr, stop_loss, take_profit

    def _apply_stop_loss_guard_to_stored_position(self, stored: StoredPosition, context: str) -> StoredPosition:
        """
        저장/복구 포지션의 손절가가 전략 기준보다 과도하게 느슨하면 즉시 보정합니다.
        """
        if stored is None:
            return stored

        entry_price = float(getattr(stored, "entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            return stored

        base_atr = float(getattr(stored, "atr_at_entry", 0.0) or 0.0)
        resolved_atr, min_stop_loss, _ = self._compute_entry_exit_prices(entry_price, base_atr)
        if min_stop_loss <= 0:
            return stored

        if base_atr <= 0:
            stored.atr_at_entry = resolved_atr

        current_stop_loss = float(getattr(stored, "stop_loss", 0.0) or 0.0)
        if current_stop_loss <= 0 or current_stop_loss < min_stop_loss:
            logger.warning(
                "[RISK][SL_GUARD] context=%s symbol=%s stop_loss adjusted: old=%.2f -> new=%.2f "
                "(entry=%.2f, atr=%.2f, max_loss_pct=%.2f)",
                context,
                getattr(stored, "stock_code", "UNKNOWN"),
                current_stop_loss,
                min_stop_loss,
                entry_price,
                resolved_atr,
                float(getattr(settings, "MAX_LOSS_PCT", 0.0) or 0.0),
            )
            stored.stop_loss = min_stop_loss
            trailing_stop = float(getattr(stored, "trailing_stop", 0.0) or 0.0)
            if trailing_stop < min_stop_loss:
                stored.trailing_stop = min_stop_loss

        return stored

    def _get_account_holding(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """API 계좌 기준 특정 종목 보유 스냅샷을 반환합니다."""
        holdings_snapshot, holdings_state = self.get_holdings_risk_snapshot_state()
        if holdings_snapshot is not None and holdings_state == "fresh":
            raw_holding = self._find_holding_row(holdings_snapshot.holdings, stock_code)
            if raw_holding is not None:
                return {
                    "stock_code": str(stock_code).strip(),
                    "qty": self._extract_holding_qty(raw_holding),
                    "avg_price": self._extract_holding_avg_price(raw_holding),
                    "current_price": float(raw_holding.get("current_price") or raw_holding.get("prpr") or 0.0),
                }

        if holdings_snapshot is not None and holdings_state == "stale" and str(getattr(self, "_report_mode", "PAPER")).upper() == "DRY_RUN":
            raw_holding = self._find_holding_row(holdings_snapshot.holdings, stock_code)
            if raw_holding is not None:
                return {
                    "stock_code": str(stock_code).strip(),
                    "qty": self._extract_holding_qty(raw_holding),
                    "avg_price": self._extract_holding_avg_price(raw_holding),
                    "current_price": float(raw_holding.get("current_price") or raw_holding.get("prpr") or 0.0),
                }

        snapshot = self.refresh_holdings_risk_snapshot_sync(source="sync_fallback")
        if snapshot is None:
            return None
        raw_holding = self._find_holding_row(snapshot.holdings, stock_code)
        if raw_holding is not None:
            return {
                "stock_code": str(stock_code).strip(),
                "qty": self._extract_holding_qty(raw_holding),
                "avg_price": self._extract_holding_avg_price(raw_holding),
                "current_price": float(raw_holding.get("current_price") or raw_holding.get("prpr") or 0.0),
            }
        return None

    def _account_has_holding(self, stock_code: str) -> Optional[bool]:
        """API 계좌 기준으로 특정 종목 보유 여부를 확인합니다."""
        holding = self._get_account_holding(stock_code)
        if holding is None:
            return None
        return int(holding.get("qty") or 0) > 0

    def _auto_reconcile_stale_position_after_sell_failure(self, error_message: str) -> bool:
        """
        매도 실패가 '계좌 무보유' 원인으로 확인되면 로컬 포지션을 자동 정리합니다.
        """
        lower = (error_message or "").lower()
        is_timeout_like = ("타임아웃" in lower) or ("timeout" in lower)
        is_cancel_like = ("미체결로 주문 취소" in lower) or ("cancelled" in lower)
        should_reconcile = (
            self._is_no_holding_error(error_message)
            or is_timeout_like
            or is_cancel_like
        )
        if not should_reconcile:
            return False
        if self._is_market_unavailable_error(error_message):
            return False

        has_holding = self._account_has_holding(self.stock_code)
        if has_holding is None or has_holding:
            return False

        try:
            self.strategy.reset_to_wait()
        except Exception as e:
            logger.warning(
                "[AUTO_RECOVER] 전략 포지션 정리 실패: symbol=%s, err=%s",
                self.stock_code,
                e,
            )
            return False

        self.position_store.clear_position()
        self._clear_pending_exit("api_holding_missing_after_sell_failure")
        self._sync_db_position_from_strategy()
        self._persist_account_snapshot(force=True)

        logger.warning(
            "[AUTO_RECOVER] 계좌 무보유 확인으로 로컬 포지션 자동 정리: symbol=%s, error=%s",
            self.stock_code,
            error_message,
        )
        try:
            self.telegram.notify_warning(
                f"포지션 자동정리\n종목: {self.stock_code}\n사유: API 계좌 무보유 확인"
            )
        except Exception:
            pass
        return True

    def _auto_reconcile_stale_position_after_buy_failure(
        self,
        signal: TradingSignal,
        error_message: str,
    ) -> bool:
        """
        매수 실패가 타임아웃/취소 성격이고 계좌 보유가 확인되면 로컬 포지션을 자동 복구합니다.
        """
        lower = (error_message or "").lower()
        is_timeout_like = ("타임아웃" in lower) or ("timeout" in lower)
        is_cancel_like = ("미체결로 주문 취소" in lower) or ("cancelled" in lower)
        should_reconcile = is_timeout_like or is_cancel_like
        if not should_reconcile:
            return False
        if self._is_market_unavailable_error(error_message):
            return False

        has_holding = self._account_has_holding(self.stock_code)
        if has_holding is None or not has_holding:
            return False

        holding = self._get_account_holding(self.stock_code) or {}
        recovered_qty = int(holding.get("qty") or self.order_quantity or 0)
        recovered_avg = float(holding.get("avg_price") or 0.0)
        if recovered_avg <= 0:
            recovered_avg = float(getattr(signal, "price", 0.0) or 0.0)
        if recovered_qty <= 0 or recovered_avg <= 0:
            return False

        atr = float(getattr(signal, "atr", 0.0) or 0.0)
        atr, stop_loss, take_profit = self._compute_entry_exit_prices(recovered_avg, atr)
        if atr <= 0 or stop_loss <= 0:
            return False

        try:
            self.strategy.open_position(
                symbol=self.stock_code,
                entry_price=recovered_avg,
                quantity=recovered_qty,
                atr=atr,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        except Exception as e:
            logger.warning(
                "[AUTO_RECOVER] 매수 자동복구 실패: symbol=%s, err=%s",
                self.stock_code,
                e,
            )
            return False

        self._save_position_on_exit()
        self._sync_db_position_from_strategy()
        self._persist_account_snapshot(force=True)

        logger.warning(
            "[AUTO_RECOVER] 계좌 보유 확인으로 로컬 포지션 자동 복구: symbol=%s, qty=%s, avg=%s, error=%s",
            self.stock_code,
            recovered_qty,
            recovered_avg,
            error_message,
        )
        try:
            self.telegram.notify_warning(
                "포지션 자동복구\n"
                f"종목: {self.stock_code}\n"
                f"수량: {recovered_qty}주\n"
                f"평단가: {recovered_avg:,.2f}원\n"
                "사유: 체결조회 타임아웃/취소 후 API 계좌 보유 확인"
            )
        except Exception:
            pass
        return True

    def _activate_pending_exit(self, signal: TradingSignal, error_message: str) -> None:
        now = datetime.now(KST)
        retry_key = self._build_exit_retry_key(signal)
        next_retry_at = now + timedelta(minutes=max(self._pending_exit_backoff_minutes, 1))
        pending = {
            "status": "pending",
            "stock_code": self.stock_code,
            "retry_key": retry_key,
            "exit_reason": signal.exit_reason.value if signal.exit_reason else ExitReason.MANUAL_EXIT.value,
            "reason_code": signal.reason_code or "",
            "next_retry_at": next_retry_at.isoformat(),
            "last_error": error_message,
            "updated_at": now.isoformat(),
        }
        prev = self._pending_exit_state or {}
        self._pending_exit_state = pending
        self.position_store.save_pending_exit(pending)
        is_first_transition = (
            prev.get("status") != "pending" or prev.get("retry_key") != retry_key
        )
        logger.warning(
            f"[PENDING_EXIT] 전환: symbol={self.stock_code}, retry_key={retry_key}, "
            f"next_retry_at={pending['next_retry_at']}, error={error_message}"
        )
        if is_first_transition:
            self.telegram.notify_warning(
                f"청산 보류(PENDING_EXIT)\n"
                f"종목: {self.stock_code}\n"
                f"사유: {pending['exit_reason']} / {pending['reason_code']}\n"
                f"재시도 예정: {pending['next_retry_at']}\n"
                f"원인: {error_message}"
            )

    def _clear_pending_exit(self, clear_reason: str) -> None:
        if not self._pending_exit_state:
            return
        prev = self._pending_exit_state
        self._pending_exit_state = None
        self.position_store.clear_pending_exit()
        logger.info(
            f"[PENDING_EXIT] 해제: symbol={self.stock_code}, reason={clear_reason}, "
            f"prev_retry_key={prev.get('retry_key')}"
        )
        self.telegram.notify_info(
            f"청산 보류 해제\n종목: {self.stock_code}\n사유: {clear_reason}"
        )

    def _should_attempt_exit_order(self, signal: TradingSignal) -> tuple[bool, str]:
        pending = self._pending_exit_state
        if not pending:
            return True, "no_pending_exit"

        retry_key = self._build_exit_retry_key(signal)
        if pending.get("retry_key") != retry_key:
            self._clear_pending_exit("exit_reason_changed")
            return True, "reason_changed"

        next_retry_raw = pending.get("next_retry_at")
        try:
            next_retry = datetime.fromisoformat(next_retry_raw) if next_retry_raw else None
        except ValueError:
            next_retry = None

        now = datetime.now(KST)
        if next_retry and now < next_retry:
            return False, f"backoff_until={next_retry.isoformat()}"

        tradeable, market_reason = self.market_checker.is_tradeable()
        if not tradeable:
            next_retry = now + timedelta(minutes=max(self._pending_exit_backoff_minutes, 1))
            pending["next_retry_at"] = next_retry.isoformat()
            pending["updated_at"] = now.isoformat()
            self._pending_exit_state = pending
            self.position_store.save_pending_exit(pending)
            return False, f"market_unavailable={market_reason}"

        return True, "retry_due"

    def _execute_exit_with_pending_control(self, signal: TradingSignal) -> Dict[str, Any]:
        can_attempt, reason = self._should_attempt_exit_order(signal)
        if not can_attempt:
            logger.info(
                f"[PENDING_EXIT] 재시도 스킵: symbol={self.stock_code}, "
                f"reason={reason}, exit_reason={signal.exit_reason.value if signal.exit_reason else 'UNKNOWN'}"
            )
            return {"success": False, "pending_exit": True, "message": reason}

        order_result = self.execute_sell(signal)
        if order_result.get("success"):
            self._clear_pending_exit("order_success")
            return order_result

        error_message = str(order_result.get("message", ""))
        if self._is_market_unavailable_error(error_message):
            self._activate_pending_exit(signal, error_message)

        return order_result
    
    def execute_buy(self, signal: TradingSignal) -> Dict[str, Any]:
        """
        매수 주문 실행
        
        ★ 모드별 처리:
            - REAL/PAPER/LIVE: 실제 주문 (동기화 체결 확인 포함)
            - CBT: 텔레그램 알림만
        
        ★ 감사 보고서 해결:
            - 체결 확인 후에만 포지션 상태 갱신
            - 장 운영시간 체크
        """
        strategy_tag = self._strategy_tag(signal)
        # 리스크 체크
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=False)
        if not risk_check.passed:
            logger.warning(f"리스크 체크 실패: {risk_check.reason}")
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        # 이미 포지션 보유
        if self.strategy.has_position:
            return {"success": False, "message": "이미 포지션 보유 중"}

        if (
            strategy_tag == "pullback_rebreakout"
            and bool(getattr(settings, "PULLBACK_BLOCK_IF_PENDING_ORDER", True))
            and self._has_active_pending_buy_order()
        ):
            self._log_entry_event(
                "[ENTRY_BLOCK]",
                reason="pullback_pending_order",
                symbol=self.stock_code,
                strategy_tag=strategy_tag,
            )
            return {
                "success": False,
                "skipped": True,
                "message": "미종결 주문 존재 - Pullback 신규 진입 차단",
            }

        final_validation = self._run_order_final_validation(signal)
        if not final_validation.get("allowed", True):
            return {
                "success": False,
                "skipped": True,
                "message": final_validation.get("message") or "최종 주문 검증 실패",
            }

        # ★ 장 운영시간 체크 (감사 보고서 지적 해결)
        if self._can_place_orders():
            tradeable, reason = self.market_checker.is_tradeable()
            if not tradeable:
                logger.warning(f"매수 불가: {reason}")
                return {"success": False, "message": reason}

        market_regime_guard = self._apply_market_regime_guard(
            signal,
            check_time=datetime.now(KST),
        )
        if market_regime_guard.get("blocked"):
            return {
                "success": False,
                "skipped": True,
                "message": market_regime_guard.get("message") or "시장 레짐 필터로 신규 BUY 차단",
            }

        # CBT 모드: 알림만
        if self.trading_mode == "CBT":
            logger.info(f"[CBT] 매수 시그널: {self.stock_code} @ {signal.price:,.0f}원")
            
            self.telegram.notify_cbt_signal(
                signal_type="📈 매수 (BUY)",
                stock_code=self.stock_code,
                price=signal.price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                atr=signal.atr,
                trend=signal.trend.value,
                reason=signal.reason
            )
            
            # 가상 포지션 오픈 (추적용)
            self.strategy.open_position(
                symbol=self.stock_code,
                entry_price=signal.price,
                quantity=self.order_quantity,
                atr=signal.atr,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit
            )
            # 단발 실행/비정상 종료에도 복원 가능하도록 매수 직후 체크포인트 저장
            self._save_position_on_exit()
            self._persist_trade_record(
                side="BUY",
                price=signal.price,
                quantity=self.order_quantity,
                order_no="CBT-VIRTUAL",
                reason=f"CBT_VIRTUAL_BUY:{strategy_tag}",
            )
            self._persist_account_snapshot(force=True)
            
            return {"success": True, "message": "[CBT] 가상 매수", "order_no": "CBT-VIRTUAL"}
        
        # ★ REAL/PAPER/LIVE: 동기화 주문 실행 (감사 보고서 지적 해결)
        try:
            order_quote = self.fetch_quote_snapshot()
            guarded_signal = self._apply_stale_quote_guard(signal, order_quote)
            if self._signal_type_value(getattr(guarded_signal, "signal_type", SignalType.BUY.value)) != SignalType.BUY.value:
                return {
                    "success": False,
                    "skipped": True,
                    "message": guarded_signal.reason,
                }

            order_plan = self._build_entry_order_plan(signal, order_quote)
            order_quote_age = order_quote.get("quote_age_sec")
            if order_plan.get("blocked"):
                self._log_entry_event(
                    "[ENTRY_BLOCK]",
                    reason=order_plan.get("reason_code") or "protected_limit_exceeds_cap",
                    strategy_tag=strategy_tag,
                    symbol=self.stock_code,
                    asset_type=order_plan.get("asset_type"),
                    prev_high=order_plan.get("prev_high"),
                    current_price=order_plan.get("current_price"),
                    best_ask=order_plan.get("best_ask"),
                    limit_price=order_plan.get("limit_price"),
                    extension_pct_at_order=order_plan.get("extension_pct_at_order"),
                    limit_extension_pct=order_plan.get("limit_extension_pct"),
                    limit_slippage_pct=order_plan.get("limit_slippage_pct"),
                    breakout_cap_pct=order_plan.get("breakout_cap_pct"),
                    slippage_cap_pct=order_plan.get("slippage_cap_pct"),
                )
                return {
                    "success": False,
                    "skipped": True,
                    "message": "보호형 지정가가 허용 상한을 초과하여 신규 BUY 차단",
                }

            signal.meta = dict(getattr(signal, "meta", {}) or {})
            signal.meta.update(
                {
                    "asset_type": order_plan.get("asset_type"),
                    "current_price_at_order": order_plan.get("current_price"),
                    "extension_pct_at_order": order_plan.get("extension_pct_at_order"),
                    "order_style": order_plan.get("style"),
                }
            )
            self._log_entry_event(
                "[ENTRY_ORDER]",
                style=order_plan.get("style"),
                strategy_tag=strategy_tag,
                symbol=self.stock_code,
                asset_type=order_plan.get("asset_type"),
                current_price=order_plan.get("current_price"),
                best_ask=order_plan.get("best_ask"),
                prev_high=order_plan.get("prev_high"),
                limit_price=order_plan.get("limit_price"),
                slippage_cap_pct=order_plan.get("slippage_cap_pct"),
            )

            # 동기화 주문 - 체결 확인 후에만 성공 반환
            sync_result = self.order_synchronizer.execute_buy_order(
                stock_code=self.stock_code,
                quantity=self.order_quantity,
                signal_id=(
                    f"{self.stock_code}:{strategy_tag}:BUY:{signal.price:.2f}:"
                    f"{datetime.now(KST).strftime('%Y%m%d%H%M')}"
                ),
                skip_market_check=True,  # 위에서 이미 체크함
                price=float(order_plan.get("price") or 0.0),
                order_type=str(order_plan.get("order_type") or "01"),
            )
            submitted_at = getattr(sync_result, "submitted_at", None)
            self._log_entry_trace(
                "order_submit",
                signal,
                {
                    "order_submit_time": submitted_at.isoformat() if submitted_at else None,
                    "current_price_at_order": order_plan.get("current_price"),
                    "extension_pct_at_order": order_plan.get("extension_pct_at_order"),
                    "quote_age_sec": order_quote_age,
                    "data_feed_source": order_quote.get("source"),
                    "order_style": order_plan.get("style"),
                    "best_ask": order_plan.get("best_ask"),
                    "limit_price": order_plan.get("limit_price"),
                },
            )
            
            if sync_result.success:
                fills = self._extract_execution_fills(sync_result, "BUY")
                applied_qty = 0

                for fill in fills:
                    if not self._record_execution_fill(
                        side="BUY",
                        fill=fill,
                        reason="BUY_FILLED",
                    ):
                        continue

                    fill_price = float(fill["price"])
                    fill_qty = int(fill["qty"])
                    if not self.strategy.has_position:
                        entry_atr, actual_stop_loss, actual_take_profit = self._compute_entry_exit_prices(
                            fill_price,
                            float(getattr(signal, "atr", 0.0) or 0.0),
                        )
                        self.strategy.open_position(
                            symbol=self.stock_code,
                            entry_price=fill_price,
                            quantity=fill_qty,
                            atr=entry_atr,
                            stop_loss=actual_stop_loss,
                            take_profit=actual_take_profit,
                        )
                    else:
                        pos = self.strategy.position
                        new_avg = calc_weighted_avg(
                            Decimal(str(pos.entry_price)),
                            int(pos.quantity),
                            Decimal(str(fill_price)),
                            fill_qty,
                        )
                        pos.entry_price = float(new_avg)
                        pos.quantity = int(pos.quantity) + fill_qty
                        entry_atr, recalced_stop_loss, recalced_take_profit = self._compute_entry_exit_prices(
                            float(pos.entry_price),
                            float(getattr(pos, "atr_at_entry", 0.0) or 0.0),
                        )
                        pos.atr_at_entry = entry_atr
                        pos.stop_loss = recalced_stop_loss
                        pos.take_profit = recalced_take_profit
                        if float(getattr(pos, "trailing_stop", 0.0) or 0.0) < recalced_stop_loss:
                            pos.trailing_stop = recalced_stop_loss
                        pos.highest_price = max(float(pos.highest_price or 0.0), fill_price)
                    applied_qty += fill_qty

                if applied_qty <= 0:
                    logger.warning(
                        "[IDEMPOTENT] 매수 체결 반영 스킵(중복 체결): order_no=%s",
                        sync_result.order_no,
                    )
                    return {
                        "success": False,
                        "order_no": sync_result.order_no,
                        "exec_qty": 0,
                        "message": "중복 체결 반영 스킵",
                    }

                self._save_position_on_exit()
                self._sync_db_position_from_strategy()
                pos = self.strategy.position
                actual_price = float(pos.entry_price)
                actual_qty = int(pos.quantity)
                actual_stop_loss = float(pos.stop_loss)
                actual_take_profit = float(pos.take_profit) if pos.take_profit is not None else 0.0
                self._daily_trades.append({
                    "time": datetime.now(KST).isoformat(),
                    "type": "BUY",
                    "strategy_tag": strategy_tag,
                    "price": actual_price,
                    "quantity": applied_qty,
                    "order_no": sync_result.order_no,
                    "signal_price": signal.price,
                })
                self._persist_account_snapshot(force=True)

                try:
                    self.telegram.notify_buy_order(
                        stock_code=self.stock_code,
                        price=actual_price,
                        quantity=applied_qty,
                        stop_loss=actual_stop_loss,
                        take_profit=actual_take_profit,
                    )
                    self.telegram.notify_info(
                        "BUY 체결 반영\n"
                        f"종목: {self.stock_code}\n"
                        f"체결수량: {applied_qty}주\n"
                        f"갱신 평단가: {actual_price:,.2f}원\n"
                        f"총보유수량: {actual_qty}주"
                    )
                except Exception as notify_err:
                    logger.warning(f"매수 알림 전송 실패(주문은 성공): {notify_err}")

                logger.info(
                    "매수 체결 완료: %s qty=%s avg=%s strategy_tag=%s",
                    sync_result.order_no,
                    applied_qty,
                    actual_price,
                    strategy_tag,
                )
                first_fill = fills[0] if fills else None
                fill_time = None
                if isinstance(first_fill, dict) and first_fill.get("executed_at") is not None:
                    try:
                        fill_time = self._parse_fill_executed_at(first_fill.get("executed_at")).isoformat()
                    except Exception:
                        fill_time = None
                self._log_entry_trace(
                    "fill",
                    signal,
                    {
                        "order_submit_time": submitted_at.isoformat() if submitted_at else None,
                        "fill_time": fill_time,
                        "current_price_at_order": order_plan.get("current_price"),
                        "fill_price": actual_price,
                        "extension_pct_at_order": order_plan.get("extension_pct_at_order"),
                        "quote_age_sec": order_quote_age,
                        "data_feed_source": order_quote.get("source"),
                        "order_style": order_plan.get("style"),
                        "best_ask": order_plan.get("best_ask"),
                        "limit_price": order_plan.get("limit_price"),
                    },
                )

                return {
                    "success": True,
                    "order_no": sync_result.order_no,
                    "exec_price": actual_price,
                    "exec_qty": applied_qty,
                    "message": sync_result.message,
                    "strategy_tag": strategy_tag,
                }
            
            elif sync_result.result_type == OrderExecutionResult.PARTIAL:
                fills = self._extract_execution_fills(sync_result, "BUY")
                applied_qty = 0
                for fill in fills:
                    if not self._record_execution_fill(
                        side="BUY",
                        fill=fill,
                        reason="BUY_PARTIAL",
                    ):
                        continue
                    fill_price = float(fill["price"])
                    fill_qty = int(fill["qty"])
                    if not self.strategy.has_position:
                        entry_atr, partial_stop_loss, partial_take_profit = self._compute_entry_exit_prices(
                            fill_price,
                            float(getattr(signal, "atr", 0.0) or 0.0),
                        )
                        self.strategy.open_position(
                            symbol=self.stock_code,
                            entry_price=fill_price,
                            quantity=fill_qty,
                            atr=entry_atr,
                            stop_loss=partial_stop_loss,
                            take_profit=partial_take_profit,
                        )
                    else:
                        pos = self.strategy.position
                        new_avg = calc_weighted_avg(
                            Decimal(str(pos.entry_price)),
                            int(pos.quantity),
                            Decimal(str(fill_price)),
                            fill_qty,
                        )
                        pos.entry_price = float(new_avg)
                        pos.quantity = int(pos.quantity) + fill_qty
                        entry_atr, recalced_stop_loss, recalced_take_profit = self._compute_entry_exit_prices(
                            float(pos.entry_price),
                            float(getattr(pos, "atr_at_entry", 0.0) or 0.0),
                        )
                        pos.atr_at_entry = entry_atr
                        pos.stop_loss = recalced_stop_loss
                        pos.take_profit = recalced_take_profit
                        if float(getattr(pos, "trailing_stop", 0.0) or 0.0) < recalced_stop_loss:
                            pos.trailing_stop = recalced_stop_loss
                    applied_qty += fill_qty

                if applied_qty > 0:
                    self._save_position_on_exit()
                    self._sync_db_position_from_strategy()
                    self._persist_account_snapshot(force=True)
                    pos = self.strategy.position
                    try:
                        self.telegram.notify_warning(
                            f"부분 체결: {self.stock_code} {applied_qty}/{self.order_quantity}주\n"
                            f"평단가: {pos.entry_price:,.2f}원 | 총보유: {pos.quantity}주"
                        )
                    except Exception as notify_err:
                        logger.warning(f"부분체결 알림 전송 실패: {notify_err}")
                    logger.warning(f"부분 체결: {applied_qty}/{self.order_quantity}주")
                    first_fill = fills[0] if fills else None
                    fill_time = None
                    if isinstance(first_fill, dict) and first_fill.get("executed_at") is not None:
                        try:
                            fill_time = self._parse_fill_executed_at(first_fill.get("executed_at")).isoformat()
                        except Exception:
                            fill_time = None
                    self._log_entry_trace(
                        "fill",
                        signal,
                        {
                            "order_submit_time": submitted_at.isoformat() if submitted_at else None,
                            "fill_time": fill_time,
                            "current_price_at_order": order_plan.get("current_price"),
                            "fill_price": float(pos.entry_price),
                            "extension_pct_at_order": order_plan.get("extension_pct_at_order"),
                            "quote_age_sec": order_quote_age,
                            "data_feed_source": order_quote.get("source"),
                            "order_style": order_plan.get("style"),
                            "best_ask": order_plan.get("best_ask"),
                            "limit_price": order_plan.get("limit_price"),
                        },
                    )
                
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "exec_qty": applied_qty,
                    "message": sync_result.message,
                    "strategy_tag": strategy_tag,
                }
            
            else:
                # 완전 실패 - 포지션 상태 변경 없음
                if self._auto_reconcile_stale_position_after_buy_failure(signal, sync_result.message):
                    reconciled_pos = self.strategy.position if self.strategy.has_position else None
                    return {
                        "success": True,
                        "reconciled": True,
                        "order_no": sync_result.order_no,
                        "exec_qty": int(getattr(reconciled_pos, "quantity", 0) or 0),
                        "exec_price": float(getattr(reconciled_pos, "entry_price", 0.0) or 0.0),
                        "message": (
                            "API 계좌 보유 확인으로 로컬 포지션을 자동 복구했습니다."
                        ),
                        "strategy_tag": strategy_tag,
                    }
                logger.error(f"매수 실패: {sync_result.message}")
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "message": sync_result.message,
                    "strategy_tag": strategy_tag,
                }
            
        except Exception as e:
            logger.exception(f"매수 주문 에러: {e}")
            self.telegram.notify_error("매수 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def execute_sell(self, signal: TradingSignal) -> Dict[str, Any]:
        """
        매도 주문 실행 (청산)
        
        ★ 허용된 Exit 사유만 처리
        ★ EOD 청산은 절대 불가
        ★ 감사 보고서 해결: 체결 확인 후에만 포지션 상태 갱신
        """
        # 리스크 체크 (청산은 항상 허용)
        risk_check = self.risk_manager.check_order_allowed(is_closing_position=True)
        if not risk_check.passed:
            logger.warning(f"리스크 체크 실패 (청산): {risk_check.reason}")
            if risk_check.should_exit:
                safe_exit_with_message(risk_check.reason)
            return {"success": False, "message": risk_check.reason}
        
        if not self.strategy.has_position:
            return {"success": False, "message": "청산할 포지션 없음"}
        
        pos = self.strategy.position
        exit_reason = signal.exit_reason or ExitReason.MANUAL_EXIT
        
        # 손절 여부 판단 (긴급 청산 플래그)
        is_emergency = exit_reason in (
            ExitReason.ATR_STOP_LOSS,
            ExitReason.GAP_PROTECTION,
            ExitReason.KILL_SWITCH
        )
        
        # CBT 모드: 알림만
        if self.trading_mode == "CBT":
            logger.info(
                f"[CBT] 매도 시그널: {self.stock_code} @ {signal.price:,.0f}원, "
                f"사유={exit_reason.value}"
            )
            
            self.telegram.notify_cbt_signal(
                signal_type=f"📉 매도 ({exit_reason.value})",
                stock_code=self.stock_code,
                price=signal.price,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                atr=pos.atr_at_entry,
                trend=signal.trend.value,
                reason=signal.reason
            )
            
            # 가상 포지션 청산
            result = self.strategy.close_position(signal.price, exit_reason)
            
            if result:
                # 리스크 매니저에 손익 기록
                self.risk_manager.record_trade_pnl(result["pnl"])
                self._persist_trade_record(
                    side="SELL",
                    price=signal.price,
                    quantity=int(result.get("quantity") or pos.quantity or 0),
                    order_no="CBT-VIRTUAL",
                    reason=exit_reason.value,
                    pnl=float(result.get("pnl") or 0.0),
                    pnl_pct=float(result.get("pnl_pct") or 0.0),
                    entry_price=float(result.get("entry_price") or pos.entry_price),
                    holding_days=int(result.get("holding_days") or 0),
                )
                self._persist_account_snapshot(force=True)
            # 포지션/보류상태를 저장소와 동기화
            self._save_position_on_exit()
            
            return {"success": True, "message": "[CBT] 가상 청산", "order_no": "CBT-VIRTUAL"}
        
        # ★ REAL/PAPER/LIVE: 동기화 주문 실행 (감사 보고서 지적 해결)
        try:
            # 동기화 주문 - 체결 확인 후에만 성공 반환
            sync_result = self.order_synchronizer.execute_sell_order(
                stock_code=self.stock_code,
                quantity=pos.quantity,
                signal_id=(
                    f"{self.stock_code}:SELL:{signal.price:.2f}:"
                    f"{datetime.now(KST).strftime('%Y%m%d%H%M')}"
                ),
                is_emergency=is_emergency
            )
            
            if sync_result.success:
                fills = self._extract_execution_fills(sync_result, "SELL")
                starting_qty = int(pos.quantity)
                entry_price = float(pos.entry_price)
                applied_qty = 0
                applied_pnl = 0.0
                last_exec_price = float(sync_result.exec_price or signal.price)

                for fill in fills:
                    fill_qty = int(fill.get("qty") or 0)
                    if fill_qty <= 0:
                        continue
                    remaining = max(starting_qty - applied_qty, 0)
                    if remaining <= 0:
                        break
                    if fill_qty > remaining:
                        fill_qty = remaining
                    fill_price = float(fill.get("price") or 0.0)
                    fill["qty"] = fill_qty
                    fill["price"] = fill_price
                    partial_pnl = (fill_price - entry_price) * fill_qty
                    partial_pnl_pct = (
                        ((fill_price / entry_price) - 1.0) * 100.0
                        if entry_price > 0
                        else 0.0
                    )
                    if not self._record_execution_fill(
                        side="SELL",
                        fill=fill,
                        reason=exit_reason.value,
                        entry_price=entry_price,
                        pnl=partial_pnl,
                        pnl_pct=partial_pnl_pct,
                    ):
                        continue
                    applied_qty += fill_qty
                    applied_pnl += partial_pnl
                    last_exec_price = fill_price

                if applied_qty <= 0:
                    logger.warning(
                        "[IDEMPOTENT] 매도 체결 반영 스킵(중복 체결): order_no=%s",
                        sync_result.order_no,
                    )
                    return {
                        "success": False,
                        "order_no": sync_result.order_no,
                        "exec_qty": 0,
                        "message": "중복 체결 반영 스킵",
                    }

                if applied_qty >= starting_qty:
                    close_result = self.strategy.close_position(last_exec_price, exit_reason)
                    self.position_store.clear_position()
                    self._sync_db_position_from_strategy()
                    if close_result:
                        self.risk_manager.record_trade_pnl(close_result["pnl"])
                        self._daily_trades.append({
                            "time": datetime.now(KST).isoformat(),
                            "type": "SELL",
                            "price": last_exec_price,
                            "quantity": applied_qty,
                            "order_no": sync_result.order_no,
                            "pnl": close_result["pnl"],
                            "pnl_pct": close_result["pnl_pct"],
                            "exit_reason": exit_reason.value,
                            "signal_price": signal.price,
                        })
                        self._send_exit_notification(
                            exit_reason,
                            pos,
                            last_exec_price,
                            close_result,
                            signal,
                        )
                    self._persist_account_snapshot(force=True)
                    logger.info(f"매도 체결 완료: {sync_result.order_no} @ {last_exec_price:,.0f}원")
                    return {
                        "success": True,
                        "order_no": sync_result.order_no,
                        "exec_price": last_exec_price,
                        "exec_qty": applied_qty,
                        "pnl": close_result["pnl"] if close_result else applied_pnl,
                        "message": sync_result.message,
                    }

                # 안전장치: API 성공이더라도 반영수량이 전량 미만이면 부분청산 처리
                remaining_qty = reduce_quantity_after_sell(starting_qty, applied_qty)
                pos.quantity = remaining_qty
                self._save_position_on_exit()
                self._sync_db_position_from_strategy()
                self._persist_account_snapshot(force=True)
                self.risk_manager.record_trade_pnl(applied_pnl)
                self.telegram.notify_warning(
                    f"부분 청산: {self.stock_code} {applied_qty}/{starting_qty}주\n"
                    f"손익: {applied_pnl:+,.0f}원\n"
                    f"평단가(유지): {entry_price:,.2f}원 | 잔여: {remaining_qty}주"
                )
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "exec_price": last_exec_price,
                    "exec_qty": applied_qty,
                    "message": "부분 체결(중복 skip 포함)로 잔여 포지션 유지",
                }
            
            elif sync_result.result_type == OrderExecutionResult.PARTIAL:
                fills = self._extract_execution_fills(sync_result, "SELL")
                starting_qty = int(pos.quantity)
                entry_price = float(pos.entry_price)
                applied_qty = 0
                applied_pnl = 0.0
                last_exec_price = float(sync_result.exec_price or signal.price)

                for fill in fills:
                    fill_qty = int(fill.get("qty") or 0)
                    if fill_qty <= 0:
                        continue
                    remaining = max(starting_qty - applied_qty, 0)
                    if remaining <= 0:
                        break
                    if fill_qty > remaining:
                        fill_qty = remaining
                    fill_price = float(fill.get("price") or 0.0)
                    fill["qty"] = fill_qty
                    fill["price"] = fill_price
                    partial_pnl = (fill_price - entry_price) * fill_qty
                    partial_pnl_pct = (
                        ((fill_price / entry_price) - 1.0) * 100.0
                        if entry_price > 0
                        else 0.0
                    )
                    if not self._record_execution_fill(
                        side="SELL",
                        fill=fill,
                        reason=f"{exit_reason.value}_PARTIAL",
                        entry_price=entry_price,
                        pnl=partial_pnl,
                        pnl_pct=partial_pnl_pct,
                    ):
                        continue
                    applied_qty += fill_qty
                    applied_pnl += partial_pnl
                    last_exec_price = fill_price

                if applied_qty <= 0:
                    return {
                        "success": False,
                        "order_no": sync_result.order_no,
                        "exec_qty": 0,
                        "message": "중복 체결 반영 스킵",
                    }

                remaining_qty = reduce_quantity_after_sell(starting_qty, applied_qty)
                if remaining_qty > 0:
                    pos.quantity = remaining_qty
                    self._save_position_on_exit()
                    self._sync_db_position_from_strategy()
                    self.risk_manager.record_trade_pnl(applied_pnl)
                    self.telegram.notify_warning(
                        f"부분 청산: {self.stock_code} {applied_qty}/{starting_qty}주\n"
                        f"손익: {applied_pnl:+,.0f}원\n"
                        f"평단가(유지): {entry_price:,.2f}원 | 잔여: {remaining_qty}주 보유 중"
                    )
                else:
                    close_result = self.strategy.close_position(last_exec_price, exit_reason)
                    self.position_store.clear_position()
                    self._sync_db_position_from_strategy()
                    if close_result:
                        self.risk_manager.record_trade_pnl(close_result["pnl"])
                self._persist_account_snapshot(force=True)
                logger.warning(f"부분 청산: {applied_qty}/{starting_qty}주")

                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "exec_price": last_exec_price,
                    "exec_qty": applied_qty,
                    "message": sync_result.message,
                }
            
            else:
                # 완전 실패 - 포지션 상태 변경 없음 (매우 위험!)
                if self._auto_reconcile_stale_position_after_sell_failure(sync_result.message):
                    return {
                        "success": True,
                        "reconciled": True,
                        "order_no": sync_result.order_no,
                        "message": (
                            "API 계좌 무보유 확인으로 로컬 포지션을 자동 정리했습니다."
                        ),
                    }

                market_unavailable = self._is_market_unavailable_error(sync_result.message)
                if market_unavailable:
                    logger.warning(f"매도 실패(주문불가/장종료): {sync_result.message}")
                else:
                    logger.error(f"매도 실패 (포지션 유지됨): {sync_result.message}")
                
                # 긴급 손절 실패 시 킬 스위치 발동
                if is_emergency and not market_unavailable:
                    if exit_reason == ExitReason.GAP_PROTECTION:
                        logger.error(
                            f"[{GAP_REASON_FALLBACK}] 갭 보호 청산 주문 실패: "
                            f"order_no={sync_result.order_no}, reason={sync_result.message}"
                        )
                    self.telegram.notify_error(
                        "긴급 청산 실패",
                        f"종목: {self.stock_code}\n"
                        f"사유: {exit_reason.value}\n"
                        f"오류: {sync_result.message}\n"
                        f"⚠️ 수동 청산 필요!"
                    )
                
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "message": f"청산 실패 - {sync_result.message}"
                }
            
        except Exception as e:
            logger.error(f"매도 주문 에러: {e}")
            self.telegram.notify_error("매도 주문 실패", str(e))
            return {"success": False, "message": str(e)}
    
    def _send_exit_notification(
        self,
        exit_reason: ExitReason,
        position: MultidayPosition,
        exit_price: float,
        close_result: Dict,
        signal: TradingSignal,
    ) -> None:
        """청산 유형별 텔레그램 알림"""
        if exit_reason == ExitReason.ATR_STOP_LOSS:
            self.telegram.notify_stop_loss(
                stock_code=position.symbol,
                entry_price=position.entry_price,
                exit_price=exit_price,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        elif exit_reason == ExitReason.ATR_TAKE_PROFIT:
            self.telegram.notify_take_profit(
                stock_code=position.symbol,
                entry_price=position.entry_price,
                exit_price=exit_price,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        elif exit_reason == ExitReason.TRAILING_STOP:
            self.telegram.notify_sell_order(
                stock_code=position.symbol,
                price=exit_price,
                quantity=position.quantity,
                reason="트레일링 스탑",
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        elif exit_reason == ExitReason.GAP_PROTECTION:
            gap_raw_pct = signal.gap_raw_pct if signal.gap_raw_pct is not None else 0.0
            gap_display_pct = signal.gap_display_pct if signal.gap_display_pct is not None else round(gap_raw_pct, 3)
            gap_open_price = (
                signal.gap_open_price
                if signal.gap_open_price is not None
                else exit_price
            )
            gap_reference_price = (
                signal.gap_reference_price
                if signal.gap_reference_price is not None
                else position.entry_price
            )
            gap_reference_type = signal.gap_reference or "entry"
            reason_code = signal.reason_code or GAP_REASON_OTHER
            logger.info(
                f"[GAP_EXIT] symbol={position.symbol}, open={float(gap_open_price):.6f}, "
                f"base_label={gap_reference_type}, base_price={float(gap_reference_price):.6f}, "
                f"gap_pct={gap_raw_pct:.6f}, threshold={self.strategy.gap_threshold_pct}, "
                f"triggered=True, reason={reason_code}"
            )
            self.telegram.notify_gap_protection(
                stock_code=position.symbol,
                open_price=gap_open_price,
                stop_loss=position.stop_loss,
                entry_price=position.entry_price,
                gap_loss_pct=gap_display_pct,
                raw_gap_pct=gap_raw_pct,
                reference_price=gap_reference_price,
                reference_type=gap_reference_type,
                reason_code=reason_code,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
        else:
            self.telegram.notify_sell_order(
                stock_code=position.symbol,
                price=exit_price,
                quantity=position.quantity,
                reason=exit_reason.value,
                pnl=close_result["pnl"],
                pnl_pct=close_result["pnl_pct"]
            )
    
    # ════════════════════════════════════════════════════════════════
    # 근접 알림
    # ════════════════════════════════════════════════════════════════
    
    def _check_and_send_alerts(self, signal: TradingSignal, current_price: float) -> None:
        """손절/익절 근접 알림 체크 및 전송"""
        if not self.strategy.has_position:
            return
        
        pos = self.strategy.position
        pnl, pnl_pct = pos.get_pnl(current_price)
        
        # 손절선 근접 알림
        if signal.near_stop_loss_pct >= settings.ALERT_NEAR_STOPLOSS_PCT:
            alert_key = f"SL_{pos.symbol}_{int(signal.near_stop_loss_pct)}"
            
            if self._last_near_sl_alert != alert_key:
                self.telegram.notify_near_stop_loss(
                    stock_code=pos.symbol,
                    current_price=current_price,
                    entry_price=pos.entry_price,
                    stop_loss=pos.stop_loss,
                    progress=signal.near_stop_loss_pct,
                    pnl=pnl,
                    pnl_pct=pnl_pct
                )
                self._last_near_sl_alert = alert_key
        
        # 익절선 근접 알림
        if signal.near_take_profit_pct >= settings.ALERT_NEAR_TAKEPROFIT_PCT:
            alert_key = f"TP_{pos.symbol}_{int(signal.near_take_profit_pct)}"
            
            if self._last_near_tp_alert != alert_key:
                self.telegram.notify_near_take_profit(
                    stock_code=pos.symbol,
                    current_price=current_price,
                    entry_price=pos.entry_price,
                    take_profit=pos.take_profit,
                    progress=signal.near_take_profit_pct,
                    pnl=pnl,
                    pnl_pct=pnl_pct
                )
                self._last_near_tp_alert = alert_key
        
        # 트레일링 스탑 갱신 알림
        if settings.ENABLE_TRAILING_STOP and pos.trailing_stop > 0:
            trailing_key = f"TS_{pos.symbol}_{int(pos.trailing_stop)}"
            
            if (self._last_trailing_update != trailing_key and 
                pos.trailing_stop > pos.stop_loss):
                self.telegram.notify_trailing_stop_updated(
                    stock_code=pos.symbol,
                    highest_price=pos.highest_price,
                    trailing_stop=pos.trailing_stop,
                    entry_price=pos.entry_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct
                )
                self._last_trailing_update = trailing_key
    
    # ════════════════════════════════════════════════════════════════
    # 메인 실행 로직
    # ════════════════════════════════════════════════════════════════
    
    def run_once(self) -> Dict[str, Any]:
        """
        전략 1회 실행
        
        ★ EOD 청산 로직 없음
        ★ Exit는 오직 가격 조건으로만 발생
        """
        logger.info("=" * 50)
        logger.info(f"[{self.trading_mode}] 전략 실행")
        self._ensure_threaded_pullback_pipeline_started()

        metrics_before = self._capture_execution_metrics()

        # 리스크 패널 및 당일 시작 자본금 기준 동기화
        self.sync_account_and_risk_if_due(force=True)
        
        # 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                self._save_position_on_exit()
                safe_exit_with_message(kill_check.reason)
        
        result = {
            "timestamp": datetime.now(KST).isoformat(),
            "mode": self.trading_mode,
            "stock_code": self.stock_code,
            "signal": None,
            "order_result": None,
            "position": None,
            "error": None
        }

        # 리포트용 계좌 스냅샷은 주기적으로만 저장합니다.
        self._persist_account_snapshot(force=False)

        try:
            tradeable_now, market_reason = self.market_checker.is_tradeable()
            if not self.strategy.has_position and not tradeable_now:
                now = datetime.now(KST)
                if (
                    self._last_market_closed_skip_log_at is None
                    or (now - self._last_market_closed_skip_log_at).total_seconds() >= 300
                ):
                    logger.info(
                        f"[{self.stock_code}] 장외로 신규 시그널 계산 스킵: {market_reason}"
                    )
                    self._last_market_closed_skip_log_at = now
                result["error"] = f"market_closed_skip:{market_reason}"
                return result

            # 1. 시장 데이터 조회
            df = self.fetch_market_data()
            if df.empty:
                result["error"] = "시장 데이터 없음"
                return result
            
            # 2. 현재가/시가/호가 스냅샷 조회
            decision_time = datetime.now(KST)
            quote_snapshot = self.fetch_quote_snapshot()
            current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
            open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
            if current_price <= 0:
                fallback_current, fallback_open = self.fetch_current_price()
                current_price = float(fallback_current or 0.0)
                open_price = float(fallback_open or 0.0)
                quote_snapshot = dict(quote_snapshot)
                quote_snapshot["current_price"] = current_price
                quote_snapshot["open_price"] = open_price
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                return result

            if hasattr(self.api, "is_network_disconnected_for") and self.api.is_network_disconnected_for(60):
                result["error"] = "네트워크 단절 60초 이상 지속 - 안전모드로 거래 중단"
                logger.error(result["error"])
                return result

            intraday_bars: list[dict] = []
            if bool(getattr(settings, "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY", False)):
                intraday_lookback = max(
                    int(getattr(settings, "ORB_ENTRY_CUTOFF_MINUTES", 90) or 90)
                    + int(getattr(settings, "ORB_OPENING_RANGE_MINUTES", 5) or 5)
                    + 5,
                    30,
                )
                intraday_bars = self.fetch_intraday_bars(n=intraday_lookback)

            has_pending_order = (
                self._has_active_pending_buy_order()
                if (
                    bool(getattr(settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False))
                    or bool(getattr(settings, "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY", False))
                )
                and not self.strategy.has_position
                else False
            )

            context = PreparedEvaluationContext(
                decision_time=decision_time,
                df=df,
                quote_snapshot=quote_snapshot,
                current_price=current_price,
                open_price=open_price,
                intraday_bars=intraday_bars,
                has_pending_order=has_pending_order,
            )
            signal = self.evaluate_signal_from_context(context)
            result = self._finalize_evaluation_result(
                signal=signal,
                context=context,
                result=result,
            )
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"전략 실행 오류: {e}")
            notifier = getattr(self, "telegram", None)
            if notifier is not None and hasattr(notifier, "notify_error"):
                notifier.notify_error("전략 실행 오류", str(e))

        result["metrics"] = self._metrics_delta(metrics_before, self._capture_execution_metrics())
        result["metrics"].update(self._pullback_pipeline_metrics())
        logger.info("=" * 50)
        return result

    def run_fast_cycle(self) -> Dict[str, Any]:
        """WS quote-event fast path. Legacy strategy/order semantics are preserved."""
        logger.info("=" * 50)
        logger.info(f"[{self.trading_mode}] 전략 실행 (FAST_EVAL)")
        self._ensure_threaded_pullback_pipeline_started()

        metrics_before = self._capture_execution_metrics()
        self.sync_account_and_risk_if_due(
            force=False,
            min_interval_sec=float(getattr(settings, "FAST_EVAL_RISK_SYNC_INTERVAL_SEC", 30.0) or 30.0),
        )

        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                self._save_position_on_exit()
                safe_exit_with_message(kill_check.reason)

        result = {
            "timestamp": datetime.now(KST).isoformat(),
            "mode": self.trading_mode,
            "stock_code": self.stock_code,
            "signal": None,
            "order_result": None,
            "position": None,
            "error": None,
            "fast_path": True,
        }

        self._persist_account_snapshot(force=False)

        try:
            tradeable_now, market_reason = self.market_checker.is_tradeable()
            if not self.strategy.has_position and not tradeable_now:
                now = datetime.now(KST)
                if (
                    self._last_market_closed_skip_log_at is None
                    or (now - self._last_market_closed_skip_log_at).total_seconds() >= 300
                ):
                    logger.info(
                        f"[{self.stock_code}] 장외로 신규 시그널 계산 스킵: {market_reason}"
                    )
                    self._last_market_closed_skip_log_at = now
                result["error"] = f"market_closed_skip:{market_reason}"
                return result

            context = self.prepare_market_context(
                use_cached_daily=True,
                force_daily_refresh=False,
            )
            if context is None:
                result["error"] = "fast_eval_prepare_failed"
                return result

            signal = self.evaluate_signal_from_context(context)
            result = self._finalize_evaluation_result(
                signal=signal,
                context=context,
                result=result,
            )
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"FAST_EVAL 전략 실행 오류: {e}")
            notifier = getattr(self, "telegram", None)
            if notifier is not None and hasattr(notifier, "notify_error"):
                notifier.notify_error("FAST_EVAL 전략 실행 오류", str(e))

        result["metrics"] = self._metrics_delta(metrics_before, self._capture_execution_metrics())
        result["metrics"].update(self._pullback_pipeline_metrics())
        logger.info("=" * 50)
        return result
    
    def _calculate_dynamic_interval(self) -> int:
        """
        동적 실행 간격 계산
        
        ★ 감사 보고서 해결: 손절선 근접 시 실행 간격 단축
        
        Returns:
            int: 적용할 실행 간격 (초)
        """
        if not self.strategy.has_position:
            return self._current_interval
        
        pos = self.strategy.position
        
        # 현재가 조회
        try:
            current_price, _ = self.fetch_current_price()
            if current_price <= 0:
                return self._current_interval
        except Exception:
            return self._current_interval
        
        # 손절선까지의 거리 계산
        near_sl_pct = pos.get_distance_to_stop_loss(current_price)
        
        if near_sl_pct >= self._near_sl_threshold:
            # 손절선 근접 - 간격 단축
            logger.info(f"손절선 근접 ({near_sl_pct:.1f}%) - 실행 간격 {self._near_sl_interval}초로 단축")
            return self._near_sl_interval
        
        return self._current_interval
    
    def run(self, interval_seconds: int = 60, max_iterations: int = None) -> None:
        """
        전략 연속 실행
        
        ★ EOD 청산 로직 없음
        ★ 프로그램 종료 시에도 포지션 유지
        ★ 감사 보고서 해결: 동적 실행 간격 적용
        
        Args:
            interval_seconds: 기본 실행 간격 (초)
            max_iterations: 최대 반복 횟수 (None = 무한)
        """
        # 킬 스위치 체크
        kill_check = self.risk_manager.check_kill_switch()
        if not kill_check.passed:
            logger.error(kill_check.reason)
            if kill_check.should_exit:
                safe_exit_with_message(kill_check.reason)
            return
        
        # 기본 간격 설정 (최소 15초 허용 - 손절 감시용)
        min_interval = self._near_sl_interval
        if interval_seconds < min_interval:
            logger.warning(f"실행 간격이 {min_interval}초 미만입니다. {min_interval}초로 조정합니다.")
            interval_seconds = min_interval
        
        self._current_interval = interval_seconds
        self.is_running = True
        iteration = 0
        self._ensure_threaded_pullback_pipeline_started()
        
        logger.info(f"멀티데이 거래 시작 (모드: {self.trading_mode}, 기본 간격: {interval_seconds}초)")
        
        # 시작 알림
        mode_display = {
            "REAL": "🔴 실계좌",
            "LIVE": "🔴 실계좌",
            "CBT": "🟡 종이매매",
            "PAPER": "🟢 모의투자"
        }.get(self.trading_mode, self.trading_mode)
        
        self.telegram.notify_system_start(
            stock_code=self.stock_code,
            order_quantity=self.order_quantity,
            interval=interval_seconds,
            mode=mode_display
        )
        
        stop_reason = "정상 종료"
        
        try:
            while self.is_running:
                iteration += 1
                
                # ★ 동적 실행 간격 계산 (감사 보고서 해결)
                current_interval = self._calculate_dynamic_interval()
                
                logger.info(f"[반복 #{iteration}] (간격: {current_interval}초)")
                
                self.run_once()
                
                # 최대 반복 체크
                if max_iterations and iteration >= max_iterations:
                    logger.info(f"최대 반복 도달: {max_iterations}")
                    break
                
                # ★ 장 상태 체크 (선택적 대기)
                market_status = self.market_checker.get_market_status()
                if market_status.value == "CLOSED":
                    # 폐장 시 장 시작까지 대기 시간 계산
                    wait_time = min(current_interval, 300)  # 최대 5분
                    logger.info(f"폐장 중 - {wait_time}초 대기")
                    time.sleep(wait_time)
                else:
                    logger.info(f"다음 실행까지 {current_interval}초 대기...")
                    time.sleep(current_interval)
                
        except KeyboardInterrupt:
            logger.info("사용자 중단")
            stop_reason = "사용자 중단"
        except Exception as e:
            logger.error(f"예기치 않은 오류: {e}")
            stop_reason = f"오류: {str(e)}"
            self.telegram.notify_error("시스템 오류", str(e))
        finally:
            self.is_running = False
            self._stop_threaded_pullback_pipeline()
            
            # 포지션 저장
            self._save_position_on_exit()
            
            # ★ 인스턴스 락 해제
            try:
                lock = get_instance_lock()
                if lock.is_acquired:
                    lock.release()
            except Exception:
                pass
            
            # 종료 알림
            summary = self.get_daily_summary()
            self.telegram.notify_system_stop(
                reason=stop_reason,
                total_trades=summary["total_trades"],
                daily_pnl=summary["total_pnl"]
            )
            
            logger.info("멀티데이 거래 종료")
    
    def stop(self) -> None:
        """거래 중지"""
        logger.info("거래 중지 요청")
        self.is_running = False
        self._stop_threaded_pullback_pipeline()
    
    def get_daily_summary(self) -> Dict[str, Any]:
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
