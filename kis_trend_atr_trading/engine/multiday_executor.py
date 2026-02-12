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
from datetime import datetime, timedelta
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
from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier
from utils.logger import get_logger, TradeLogger
from utils.market_hours import KST

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
    
    def __init__(
        self,
        api: KISApi = None,
        strategy: MultidayTrendATRStrategy = None,
        stock_code: str = None,
        order_quantity: int = None,
        risk_manager: RiskManager = None,
        telegram: TelegramNotifier = None,
        position_store: PositionStore = None
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
        
        # 트레이딩 모드 확인
        self.trading_mode = settings.TRADING_MODE
        
        # API 클라이언트 (CBT 모드에서도 데이터 조회용으로 필요)
        is_paper = self.trading_mode != "LIVE"
        self.api = api or KISApi(is_paper_trading=is_paper)
        
        # 전략 초기화
        self.strategy = strategy or MultidayTrendATRStrategy()
        
        # 기본 설정
        self.stock_code = stock_code or settings.DEFAULT_STOCK_CODE
        self.order_quantity = order_quantity or settings.ORDER_QUANTITY
        
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
        
        # ★ 신규: 주문 동기화 컴포넌트 (감사 보고서 지적 해결)
        self.market_checker = get_market_checker()
        self.order_synchronizer = OrderSynchronizer(
            api=self.api,
            market_checker=self.market_checker,
            execution_timeout=getattr(settings, 'ORDER_EXECUTION_TIMEOUT', 45)
        )
        self.position_resync = PositionResynchronizer(
            api=self.api,
            position_store=self.position_store,
            db_repository=self.db_position_repo,
            trading_mode="REAL" if self.trading_mode == "LIVE" else self.trading_mode
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
        self._pending_exit_state: Optional[Dict[str, Any]] = self.position_store.load_pending_exit()
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
    
    def _signal_handler(self, signum, frame):
        """종료 시그널 핸들러"""
        logger.info(f"종료 시그널 수신: {signum}")
        self._save_position_on_exit()
        sys.exit(0)

    def _sync_risk_account_snapshot(self) -> None:
        """리스크 패널용 계좌 스냅샷 동기화 (짧은 TTL 캐시 적용)."""
        ttl_sec = int(getattr(settings, "RISK_ACCOUNT_SNAPSHOT_TTL_SEC", 60))
        now = datetime.now(KST)

        cached_snapshot = self.__class__._shared_account_snapshot
        cached_ts = self.__class__._shared_account_snapshot_ts
        if (
            cached_snapshot is not None
            and cached_ts is not None
            and (now - cached_ts).total_seconds() < ttl_sec
        ):
            self.risk_manager.update_account_snapshot(cached_snapshot)
            logger.info(
                f"[RISK] 계좌 스냅샷 캐시 사용: age={(now - cached_ts).total_seconds():.1f}s"
            )
            return

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
        total_pnl = float(snapshot.get("total_pnl", 0.0))
        logger.info(
            "[RISK] 계좌 스냅샷 반영: "
            f"holdings={len(snapshot.get('holdings', []))}, total_pnl={total_pnl:+,.0f}원"
        )
    
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
        
        # 경고 메시지 출력
        for warning in sync_result.get("warnings", []):
            logger.warning(f"[RESYNC] {warning}")
            self.telegram.notify_warning(f"포지션 동기화: {warning}")
        
        action = sync_result.get("action", "")
        
        if action == "NO_POSITION":
            logger.info("포지션 없음 확인")
            return False
        
        elif action == "UNTRACKED_HOLDING":
            # 미기록 보유 발견 - 위험 상황
            logger.error("미기록 보유 발견 - 수동 확인 필요")
            self.telegram.notify_error(
                "미기록 보유 발견",
                "저장된 포지션 없이 실제 보유가 발견되었습니다.\n"
                "수동으로 확인하고 처리하세요."
            )
            return False
        
        elif action == "STORED_INVALID":
            # 저장 데이터 무효 - 이미 삭제됨
            logger.warning("저장된 포지션이 무효하여 삭제됨")
            return False
        
        elif action == "CRITICAL_MISMATCH":
            # 심각한 불일치 - 킬 스위치 권장
            logger.error("심각한 포지션 불일치 - 수동 확인 필요")
            self.telegram.notify_error(
                "심각한 포지션 불일치",
                "저장된 포지션과 실제 보유가 다릅니다.\n"
                "즉시 확인하세요!"
            )
            # 안전을 위해 킬 스위치 발동 고려
            return False
        
        elif action in ("MATCHED", "QTY_ADJUSTED"):
            # 정상 또는 수량 조정됨
            stored = sync_result.get("position")
            
            if stored is None:
                logger.error("동기화 성공했으나 포지션 데이터 없음")
                return False
            
            logger.info(
                f"포지션 동기화 완료: {stored.stock_code} @ {stored.entry_price:,.0f}원, "
                f"ATR={stored.atr_at_entry:,.0f} (고정)"
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
        return self.trading_mode in ("LIVE", "PAPER")

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
            - LIVE/PAPER: 실제 주문 (동기화 체결 확인 포함)
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
            
            return {"success": True, "message": "[CBT] 가상 매수", "order_no": "CBT-VIRTUAL"}
        
        # ★ LIVE/PAPER: 동기화 주문 실행 (감사 보고서 지적 해결)
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
                # ★ 체결 확인됨 - 실제 체결가로 포지션 오픈
                actual_price = sync_result.exec_price if sync_result.exec_price > 0 else signal.price
                actual_qty = sync_result.exec_qty if sync_result.exec_qty > 0 else self.order_quantity
                
                # 실제 체결가 기준으로 손절/익절 재계산
                actual_stop_loss = actual_price - (signal.atr * settings.ATR_MULTIPLIER_SL)
                actual_take_profit = actual_price + (signal.atr * settings.ATR_MULTIPLIER_TP)
                
                self.strategy.open_position(
                    symbol=self.stock_code,
                    entry_price=actual_price,
                    quantity=actual_qty,
                    atr=signal.atr,
                    stop_loss=actual_stop_loss,
                    take_profit=actual_take_profit
                )
                
                # 포지션 저장
                self._save_position_on_exit()
                
                # 거래 기록 (실제 체결가 사용)
                self._daily_trades.append({
                    "time": datetime.now(KST).isoformat(),
                    "type": "BUY",
                    "price": actual_price,
                    "quantity": actual_qty,
                    "order_no": sync_result.order_no,
                    "signal_price": signal.price  # 신호가도 기록
                })
                
                # 텔레그램 알림 실패가 주문 성공 흐름을 깨지 않도록 분리
                try:
                    self.telegram.notify_buy_order(
                        stock_code=self.stock_code,
                        price=actual_price,
                        quantity=actual_qty,
                        stop_loss=actual_stop_loss,
                        take_profit=actual_take_profit
                    )
                except Exception as notify_err:
                    logger.warning(f"매수 알림 전송 실패(주문은 성공): {notify_err}")
                
                logger.info(f"매수 체결 완료: {sync_result.order_no} @ {actual_price:,.0f}원")
                
                return {
                    "success": True,
                    "order_no": sync_result.order_no,
                    "exec_price": actual_price,
                    "exec_qty": actual_qty,
                    "message": sync_result.message
                }
            
            elif sync_result.result_type == OrderExecutionResult.PARTIAL:
                # 부분 체결 - 체결된 수량만큼 포지션 오픈
                if sync_result.exec_qty > 0:
                    actual_price = sync_result.exec_price
                    
                    self.strategy.open_position(
                        symbol=self.stock_code,
                        entry_price=actual_price,
                        quantity=sync_result.exec_qty,
                        atr=signal.atr,
                        stop_loss=actual_price - (signal.atr * settings.ATR_MULTIPLIER_SL),
                        take_profit=actual_price + (signal.atr * settings.ATR_MULTIPLIER_TP)
                    )
                    
                    self._save_position_on_exit()
                    
                    try:
                        self.telegram.notify_warning(
                            f"부분 체결: {self.stock_code} {sync_result.exec_qty}/{self.order_quantity}주 @ {actual_price:,.0f}원"
                        )
                    except Exception as notify_err:
                        logger.warning(f"부분체결 알림 전송 실패: {notify_err}")
                    
                    logger.warning(f"부분 체결: {sync_result.exec_qty}/{self.order_quantity}주")
                
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "exec_qty": sync_result.exec_qty,
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
            
            # 포지션 저장 파일 클리어
            self.position_store.clear_position()
            
            return {"success": True, "message": "[CBT] 가상 청산", "order_no": "CBT-VIRTUAL"}
        
        # ★ LIVE/PAPER: 동기화 주문 실행 (감사 보고서 지적 해결)
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
                # ★ 체결 확인됨 - 실제 체결가로 청산 처리
                actual_price = sync_result.exec_price if sync_result.exec_price > 0 else signal.price
                
                # 포지션 청산 (실제 체결가 사용)
                close_result = self.strategy.close_position(actual_price, exit_reason)
                
                # 리스크 매니저에 손익 기록
                if close_result:
                    self.risk_manager.record_trade_pnl(close_result["pnl"])
                    
                    # 거래 기록 (실제 체결가)
                    self._daily_trades.append({
                        "time": datetime.now(KST).isoformat(),
                        "type": "SELL",
                        "price": actual_price,
                        "quantity": sync_result.exec_qty,
                        "order_no": sync_result.order_no,
                        "pnl": close_result["pnl"],
                        "pnl_pct": close_result["pnl_pct"],
                        "exit_reason": exit_reason.value,
                        "signal_price": signal.price  # 신호가도 기록
                    })
                    
                    # 텔레그램 알림 (청산 유형별)
                    self._send_exit_notification(
                        exit_reason,
                        pos,
                        actual_price,
                        close_result,
                        signal,
                    )
                
                # 포지션 저장 파일 클리어
                self.position_store.clear_position()
                
                logger.info(f"매도 체결 완료: {sync_result.order_no} @ {actual_price:,.0f}원")
                
                return {
                    "success": True,
                    "order_no": sync_result.order_no,
                    "exec_price": actual_price,
                    "exec_qty": sync_result.exec_qty,
                    "pnl": close_result["pnl"] if close_result else 0,
                    "message": sync_result.message
                }
            
            elif sync_result.result_type == OrderExecutionResult.PARTIAL:
                # 부분 체결 - 체결된 수량만큼만 청산 처리
                if sync_result.exec_qty > 0:
                    actual_price = sync_result.exec_price
                    
                    # 부분 청산 손익 계산
                    partial_pnl = (actual_price - pos.entry_price) * sync_result.exec_qty
                    partial_pnl_pct = (actual_price - pos.entry_price) / pos.entry_price * 100
                    
                    # 남은 수량으로 포지션 축소 (전략 상태는 유지)
                    remaining_qty = pos.quantity - sync_result.exec_qty
                    if remaining_qty > 0:
                        pos.quantity = remaining_qty
                        self._save_position_on_exit()
                        
                        self.telegram.notify_warning(
                            f"부분 청산: {self.stock_code} {sync_result.exec_qty}/{pos.quantity + sync_result.exec_qty}주\n"
                            f"손익: {partial_pnl:+,.0f}원 ({partial_pnl_pct:+.2f}%)\n"
                            f"잔여: {remaining_qty}주 보유 중"
                        )
                    else:
                        # 전량 청산된 경우
                        close_result = self.strategy.close_position(actual_price, exit_reason)
                        self.position_store.clear_position()
                        if close_result:
                            self.risk_manager.record_trade_pnl(close_result["pnl"])
                    
                    logger.warning(f"부분 청산: {sync_result.exec_qty}/{pos.quantity}주")
                
                return {
                    "success": False,
                    "order_no": sync_result.order_no,
                    "exec_qty": sync_result.exec_qty,
                    "message": sync_result.message
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
            
            result["signal"] = {
                "type": signal.signal_type.value,
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
                f"시그널: {signal.signal_type.value} | "
                f"가격: {current_price:,.0f}원 | "
                f"추세: {signal.trend.value} | "
                f"사유: {signal.reason}"
            )
            
            # 4. 시그널에 따른 주문 실행
            if signal.signal_type == SignalType.BUY:
                order_result = self.execute_buy(signal)
                result["order_result"] = order_result
                
            elif signal.signal_type == SignalType.SELL:
                order_result = self._execute_exit_with_pending_control(signal)
                result["order_result"] = order_result
                
            elif signal.signal_type == SignalType.HOLD:
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
