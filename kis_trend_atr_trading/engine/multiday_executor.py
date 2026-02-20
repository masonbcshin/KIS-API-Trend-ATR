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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Any
import pandas as pd

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
from utils.avg_price import calc_weighted_avg, quantize_price, reduce_quantity_after_sell
from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
from utils.logger import get_logger, TradeLogger
from utils.market_hours import KST
from env import get_db_namespace_mode

logger = get_logger("multiday_executor")
trade_logger = TradeLogger("multiday_executor")


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

        기본은 설정값(trading_mode)을 따르되, 외부에서 API 객체를 주입했고
        설정 모드가 CBT 계열이면 API의 실제 계좌 모드(PAPER/REAL)로 보정합니다.
        """
        normalized_mode = cls._normalize_mode_label(trading_mode)
        if not api_was_injected or normalized_mode in ("PAPER", "REAL"):
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

        # 리스크 매니저 상태 출력
        self.risk_manager.print_status()

    def set_entry_control(self, allow_entry: bool, reason: str = "") -> None:
        """외부 정책(유니버스/보유 상한)에 따른 신규 진입 허용 여부 설정."""
        self._entry_allowed = bool(allow_entry)
        self._entry_block_reason = reason or ""
    
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

    def _sync_risk_account_snapshot(self) -> None:
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
        else:
            try:
                snapshot = self.api.get_account_balance()
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
            self.set_entry_control(False, f"[ENTRY] blocked by reconcile: {reason}")

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
    
    def fetch_market_data(self) -> pd.DataFrame:
        """시장 데이터 조회"""
        try:
            if self.market_data_provider is not None:
                bars = self.market_data_provider.get_recent_bars(
                    stock_code=self.stock_code,
                    n=100,
                    timeframe="D",
                )
                if not bars:
                    logger.warning(f"시장 데이터 없음(provider): {self.stock_code}")
                    return pd.DataFrame()
                df = pd.DataFrame(bars)
                if "date" in df.columns:
                    df = df.sort_values("date").reset_index(drop=True)
                return df

            df = self.api.get_daily_ohlcv(
                stock_code=self.stock_code,
                period_type="D"
            )
            
            if df.empty:
                logger.warning(f"시장 데이터 없음: {self.stock_code}")
            
            return df
            
        except KISApiError as e:
            logger.error(f"시장 데이터 조회 실패: {e}")
            return pd.DataFrame()
    
    def fetch_current_price(self) -> tuple:
        """
        현재가 및 시가 조회
        
        Returns:
            tuple: (현재가, 시가)
        """
        try:
            if self.market_data_provider is not None:
                current = float(
                    self.market_data_provider.get_latest_price(self.stock_code) or 0.0
                )
                # 멀티데이 전략은 갭 보호를 위해 시가가 필요하므로 provider-only 모드에서는
                # REST 현재가 응답에서 시가를 보강합니다(전략/주문 파라미터 불변).
                price_data = self.api.get_current_price(self.stock_code)
                open_price = float(price_data.get("open_price", 0.0) or 0.0)
                return current, open_price

            price_data = self.api.get_current_price(self.stock_code)
            current = price_data.get("current_price", 0)
            open_price = price_data.get("open_price", 0)
            
            return current, open_price
            
        except KISApiError as e:
            logger.error(f"현재가 조회 실패: {e}")
            return 0.0, 0.0
    
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
        
        # ★ 장 운영시간 체크 (감사 보고서 지적 해결)
        if self._can_place_orders():
            tradeable, reason = self.market_checker.is_tradeable()
            if not tradeable:
                logger.warning(f"매수 불가: {reason}")
                return {"success": False, "message": reason}
        
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
            self._persist_trade_record(
                side="BUY",
                price=signal.price,
                quantity=self.order_quantity,
                order_no="CBT-VIRTUAL",
                reason="CBT_VIRTUAL_BUY",
            )
            self._persist_account_snapshot(force=True)
            
            return {"success": True, "message": "[CBT] 가상 매수", "order_no": "CBT-VIRTUAL"}
        
        # ★ REAL/PAPER/LIVE: 동기화 주문 실행 (감사 보고서 지적 해결)
        try:
            # 동기화 주문 - 체결 확인 후에만 성공 반환
            sync_result = self.order_synchronizer.execute_buy_order(
                stock_code=self.stock_code,
                quantity=self.order_quantity,
                signal_id=(
                    f"{self.stock_code}:BUY:{signal.price:.2f}:"
                    f"{datetime.now(KST).strftime('%Y%m%d%H%M')}"
                ),
                skip_market_check=True  # 위에서 이미 체크함
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
                        actual_stop_loss = fill_price - (signal.atr * settings.ATR_MULTIPLIER_SL)
                        actual_take_profit = fill_price + (signal.atr * settings.ATR_MULTIPLIER_TP)
                        self.strategy.open_position(
                            symbol=self.stock_code,
                            entry_price=fill_price,
                            quantity=fill_qty,
                            atr=signal.atr,
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
                    "매수 체결 완료: %s qty=%s avg=%s",
                    sync_result.order_no,
                    applied_qty,
                    actual_price,
                )

                return {
                    "success": True,
                    "order_no": sync_result.order_no,
                    "exec_price": actual_price,
                    "exec_qty": applied_qty,
                    "message": sync_result.message,
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
                        self.strategy.open_position(
                            symbol=self.stock_code,
                            entry_price=fill_price,
                            quantity=fill_qty,
                            atr=signal.atr,
                            stop_loss=fill_price - (signal.atr * settings.ATR_MULTIPLIER_SL),
                            take_profit=fill_price + (signal.atr * settings.ATR_MULTIPLIER_TP),
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
                
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "exec_qty": applied_qty,
                    "message": sync_result.message
                }
            
            else:
                # 완전 실패 - 포지션 상태 변경 없음
                logger.error(f"매수 실패: {sync_result.message}")
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "message": sync_result.message
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
            
            # 포지션 저장 파일 클리어
            self.position_store.clear_position()
            
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

        # 리스크 패널 및 당일 시작 자본금 기준 동기화
        self._sync_risk_account_snapshot()
        
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
            
            # 2. 현재가/시가 조회
            current_price, open_price = self.fetch_current_price()
            if current_price <= 0:
                result["error"] = "현재가 조회 실패"
                return result

            if hasattr(self.api, "is_network_disconnected_for") and self.api.is_network_disconnected_for(60):
                result["error"] = "네트워크 단절 60초 이상 지속 - 안전모드로 거래 중단"
                logger.error(result["error"])
                return result
            
            # 3. 시그널 생성
            signal = self.strategy.generate_signal(
                df=df,
                current_price=current_price,
                open_price=open_price,
                stock_code=self.stock_code
            )
            
            signal_type_value = self._signal_type_value(signal.signal_type)

            result["signal"] = {
                "type": signal_type_value,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "trailing_stop": signal.trailing_stop,
                "exit_reason": signal.exit_reason.value if signal.exit_reason else None,
                "reason": signal.reason,
                "atr": signal.atr,
                "trend": signal.trend.value
            }
            
            logger.info(
                f"시그널: {signal_type_value} | "
                f"가격: {current_price:,.0f}원 | "
                f"추세: {signal.trend.value} | "
                f"사유: {signal.reason}"
            )
            
            # 4. 시그널에 따른 주문 실행
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
                    order_result = self.execute_buy(signal)
                    result["order_result"] = order_result
                
            elif signal_type_value == SignalType.SELL.value:
                order_result = self._execute_exit_with_pending_control(signal)
                result["order_result"] = order_result
                
            elif signal_type_value == SignalType.HOLD.value:
                # 근접 알림 체크
                self._check_and_send_alerts(signal, current_price)
            
            # 5. 현재 포지션 정보
            if self.strategy.has_position:
                pos = self.strategy.position
                pnl, pnl_pct = pos.get_pnl(current_price)
                
                result["position"] = {
                    "symbol": pos.symbol,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                    "trailing_stop": pos.trailing_stop,
                    "highest_price": pos.highest_price,
                    "atr_at_entry": pos.atr_at_entry,
                    "current_price": current_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "entry_date": pos.entry_date
                }
                
                logger.info(
                    f"포지션: {pos.symbol} | "
                    f"진입: {pos.entry_price:,.0f}원 | "
                    f"현재: {current_price:,.0f}원 | "
                    f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)"
                )
            else:
                logger.info("포지션: 없음")
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"전략 실행 오류: {e}")
            self.telegram.notify_error("전략 실행 오류", str(e))
        
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
