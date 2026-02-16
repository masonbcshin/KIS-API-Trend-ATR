"""Runtime state machine for 24/365 market-session aware operation."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import pytz

from utils.market_hours import MarketSessionState


class RuntimeOverlay(Enum):
    """Runtime overlay layer on top of market session state."""

    NORMAL = "NORMAL"
    DEGRADED_FEED = "DEGRADED_FEED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


def _normalize_feed_mode(mode: str) -> str:
    raw = str(mode or "rest").strip().lower()
    return raw if raw in ("rest", "ws") else "rest"


def _normalize_recover_policy(policy: str) -> str:
    raw = str(policy or "auto").strip().lower()
    return raw if raw in ("auto", "next_session") else "auto"


def _ensure_tz(now: Optional[datetime], tz: str) -> datetime:
    tzinfo = pytz.timezone(tz)
    if now is None:
        return datetime.now(tzinfo)
    if now.tzinfo is None:
        return tzinfo.localize(now)
    return now.astimezone(tzinfo)


def completed_bar_ts_1m(now: Optional[datetime] = None, tz: str = "Asia/Seoul") -> datetime:
    """
    1m bar timestamp standard.

    NOTE:
      - bar_ts is fixed to minute-start of the completed bar in KST.
      - Example: now=10:01:12 -> completed bar_ts=10:00:00.
    """
    now_kst = _ensure_tz(now, tz)
    minute_floor = now_kst.replace(second=0, microsecond=0)
    return minute_floor - timedelta(minutes=1)


def is_consecutive_1m(prev_ts: datetime, next_ts: datetime) -> bool:
    return int((next_ts - prev_ts).total_seconds()) == 60


@dataclass
class RuntimeConfig:
    market_timezone: str = "Asia/Seoul"
    data_feed_default: str = "rest"
    timeframe: str = "1m"
    preopen_warmup_min: int = 10
    postclose_min: int = 10
    auction_guard_windows: List[str] = field(default_factory=list)
    allow_exit_in_auction: bool = True
    offsession_ws_enabled: bool = False

    ws_start_grace_sec: int = 30
    ws_stale_sec: int = 60
    ws_reconnect_max_attempts: int = 5
    ws_reconnect_backoff_base_sec: int = 1
    ws_recover_policy: str = "auto"
    ws_recover_stable_sec: int = 30
    ws_recover_required_bars: int = 2
    ws_min_degraded_sec: int = 120
    ws_min_normal_sec: int = 120
    telegram_transition_cooldown_sec: int = 600

    status_log_interval_sec: int = 300
    insession_sleep_sec: int = 60
    offsession_sleep_sec: int = 600

    def __post_init__(self) -> None:
        self.data_feed_default = _normalize_feed_mode(self.data_feed_default)
        self.ws_recover_policy = _normalize_recover_policy(self.ws_recover_policy)
        self.ws_reconnect_max_attempts = max(int(self.ws_reconnect_max_attempts), 1)
        self.ws_reconnect_backoff_base_sec = max(int(self.ws_reconnect_backoff_base_sec), 1)
        self.ws_stale_sec = max(int(self.ws_stale_sec), 1)
        self.ws_start_grace_sec = max(int(self.ws_start_grace_sec), 0)
        self.ws_recover_stable_sec = max(int(self.ws_recover_stable_sec), 1)
        self.ws_recover_required_bars = max(int(self.ws_recover_required_bars), 1)
        self.ws_min_degraded_sec = max(int(self.ws_min_degraded_sec), 0)
        self.ws_min_normal_sec = max(int(self.ws_min_normal_sec), 0)
        self.status_log_interval_sec = max(int(self.status_log_interval_sec), 60)
        self.insession_sleep_sec = max(int(self.insession_sleep_sec), 15)
        self.offsession_sleep_sec = min(max(int(self.offsession_sleep_sec), 300), 900)
        self.preopen_warmup_min = max(int(self.preopen_warmup_min), 0)
        self.postclose_min = max(int(self.postclose_min), 0)

    @classmethod
    def from_settings(cls, settings_obj) -> "RuntimeConfig":
        windows_raw = getattr(settings_obj, "AUCTION_GUARD_WINDOWS", [])
        if isinstance(windows_raw, str):
            windows = [t.strip() for t in windows_raw.split(",") if t.strip()]
        else:
            windows = [str(t).strip() for t in (windows_raw or []) if str(t).strip()]

        return cls(
            market_timezone=str(getattr(settings_obj, "MARKET_TIMEZONE", "Asia/Seoul")),
            data_feed_default=str(getattr(settings_obj, "DATA_FEED_DEFAULT", "rest")),
            timeframe=str(getattr(settings_obj, "RUNTIME_TIMEFRAME", "1m")),
            preopen_warmup_min=int(getattr(settings_obj, "PREOPEN_WARMUP_MIN", 10)),
            postclose_min=int(getattr(settings_obj, "POSTCLOSE_MIN", 10)),
            auction_guard_windows=windows,
            allow_exit_in_auction=bool(getattr(settings_obj, "ALLOW_EXIT_IN_AUCTION", True)),
            offsession_ws_enabled=bool(getattr(settings_obj, "OFFSESSION_WS_ENABLED", False)),
            ws_start_grace_sec=int(getattr(settings_obj, "WS_START_GRACE_SEC", 30)),
            ws_stale_sec=int(getattr(settings_obj, "WS_STALE_SEC", 60)),
            ws_reconnect_max_attempts=int(getattr(settings_obj, "WS_RECONNECT_MAX_ATTEMPTS", 5)),
            ws_reconnect_backoff_base_sec=int(
                getattr(settings_obj, "WS_RECONNECT_BACKOFF_BASE_SEC", 1)
            ),
            ws_recover_policy=str(getattr(settings_obj, "WS_RECOVER_POLICY", "auto")),
            ws_recover_stable_sec=int(getattr(settings_obj, "WS_RECOVER_STABLE_SEC", 30)),
            ws_recover_required_bars=int(
                getattr(settings_obj, "WS_RECOVER_REQUIRED_BARS", 2)
            ),
            ws_min_degraded_sec=int(getattr(settings_obj, "WS_MIN_DEGRADED_SEC", 120)),
            ws_min_normal_sec=int(getattr(settings_obj, "WS_MIN_NORMAL_SEC", 120)),
            telegram_transition_cooldown_sec=int(
                getattr(settings_obj, "TELEGRAM_TRANSITION_COOLDOWN_SEC", 600)
            ),
            status_log_interval_sec=int(getattr(settings_obj, "RUNTIME_STATUS_LOG_INTERVAL_SEC", 300)),
            insession_sleep_sec=int(getattr(settings_obj, "RUNTIME_INSESSION_SLEEP_SEC", 60)),
            offsession_sleep_sec=int(getattr(settings_obj, "RUNTIME_OFFSESSION_SLEEP_SEC", 600)),
        )


@dataclass
class FeedStatus:
    ws_enabled: bool = False
    ws_connected: bool = False
    ws_last_message_age_sec: float = math.inf
    ws_last_bar_ts: Optional[datetime] = None


@dataclass
class RuntimePolicy:
    allow_new_entries: bool
    allow_exit: bool
    run_strategy: bool
    active_feed_mode: str
    ws_should_run: bool
    sleep_sec: int


@dataclass
class RuntimeDecision:
    market_state: MarketSessionState
    market_reason: str
    overlay: RuntimeOverlay
    policy: RuntimePolicy
    feed_status: FeedStatus
    market_transition: Optional[Tuple[MarketSessionState, MarketSessionState]] = None
    overlay_transition: Optional[Tuple[RuntimeOverlay, RuntimeOverlay]] = None


class RuntimeStateMachine:
    """
    Runtime state machine.

    Rules:
      - stale/degraded transition is evaluated only during IN_SESSION.
      - DEGRADED_FEED keeps WS recovery in background while forcing active feed to REST.
      - recovery requires hysteresis (connected + healthy duration + consecutive bar growth).
    """

    def __init__(self, config: RuntimeConfig, start_ts: Optional[datetime] = None):
        self.config = config
        self.start_ts = _ensure_tz(start_ts, config.market_timezone)
        self.overlay = RuntimeOverlay.NORMAL
        self._overlay_changed_at = self.start_ts
        self._last_market_state: Optional[MarketSessionState] = None
        self._ws_healthy_since: Optional[datetime] = None
        self._recent_ws_bar_ts: Deque[datetime] = deque(maxlen=16)
        self._session_reentered_for_recover: bool = False

    def _reset_ws_stability(self) -> None:
        self._ws_healthy_since = None
        self._recent_ws_bar_ts.clear()

    def _append_ws_bar_ts(self, bar_ts: Optional[datetime]) -> None:
        if bar_ts is None:
            return
        if not self._recent_ws_bar_ts:
            self._recent_ws_bar_ts.append(bar_ts)
            return
        last_ts = self._recent_ws_bar_ts[-1]
        if bar_ts > last_ts:
            self._recent_ws_bar_ts.append(bar_ts)

    def _has_required_consecutive_ws_bars(self) -> bool:
        required = max(int(self.config.ws_recover_required_bars), 1)
        if len(self._recent_ws_bar_ts) < required:
            return False
        candidate = list(self._recent_ws_bar_ts)[-required:]
        for idx in range(1, len(candidate)):
            if not is_consecutive_1m(candidate[idx - 1], candidate[idx]):
                return False
        return True

    def _is_ws_recover_stable(self, now: datetime, feed_status: FeedStatus) -> bool:
        if not feed_status.ws_enabled or not feed_status.ws_connected:
            self._reset_ws_stability()
            return False

        if float(feed_status.ws_last_message_age_sec) > float(self.config.ws_stale_sec):
            self._reset_ws_stability()
            return False

        if self._ws_healthy_since is None:
            self._ws_healthy_since = now
        self._append_ws_bar_ts(feed_status.ws_last_bar_ts)

        healthy_elapsed = (now - self._ws_healthy_since).total_seconds()
        if healthy_elapsed < float(self.config.ws_recover_stable_sec):
            return False
        return self._has_required_consecutive_ws_bars()

    def _build_policy(self, market_state: MarketSessionState, feed_status: FeedStatus) -> RuntimePolicy:
        ws_available = bool(feed_status.ws_enabled)
        overlay = self.overlay

        if overlay == RuntimeOverlay.EMERGENCY_STOP:
            return RuntimePolicy(
                allow_new_entries=False,
                allow_exit=False,
                run_strategy=False,
                active_feed_mode="rest",
                ws_should_run=False,
                sleep_sec=max(self.config.insession_sleep_sec, 30),
            )

        if market_state == MarketSessionState.OFF_SESSION:
            return RuntimePolicy(
                allow_new_entries=False,
                allow_exit=False,
                run_strategy=False,
                active_feed_mode="rest",
                ws_should_run=ws_available and self.config.offsession_ws_enabled,
                sleep_sec=self.config.offsession_sleep_sec,
            )

        if market_state == MarketSessionState.PREOPEN_WARMUP:
            return RuntimePolicy(
                allow_new_entries=False,
                allow_exit=False,
                run_strategy=False,
                active_feed_mode="rest",
                ws_should_run=ws_available and (self.config.data_feed_default == "ws"),
                sleep_sec=30,
            )

        if market_state == MarketSessionState.POSTCLOSE:
            return RuntimePolicy(
                allow_new_entries=False,
                allow_exit=False,
                run_strategy=False,
                active_feed_mode="rest",
                ws_should_run=ws_available and self.config.offsession_ws_enabled,
                sleep_sec=120,
            )

        if market_state == MarketSessionState.AUCTION_GUARD:
            active_feed = "rest"
            if (
                overlay == RuntimeOverlay.NORMAL
                and self.config.data_feed_default == "ws"
                and ws_available
            ):
                active_feed = "ws"
            return RuntimePolicy(
                allow_new_entries=False,
                allow_exit=bool(self.config.allow_exit_in_auction),
                run_strategy=bool(self.config.allow_exit_in_auction),
                active_feed_mode=active_feed,
                ws_should_run=ws_available,
                sleep_sec=15,
            )

        # IN_SESSION
        active_feed = "rest"
        if (
            overlay == RuntimeOverlay.NORMAL
            and self.config.data_feed_default == "ws"
            and ws_available
        ):
            active_feed = "ws"

        return RuntimePolicy(
            allow_new_entries=True,
            allow_exit=True,
            run_strategy=True,
            active_feed_mode=active_feed,
            ws_should_run=ws_available,
            sleep_sec=self.config.insession_sleep_sec,
        )

    def evaluate(
        self,
        *,
        now: datetime,
        market_state: MarketSessionState,
        market_reason: str,
        feed_status: FeedStatus,
        risk_stop: bool,
    ) -> RuntimeDecision:
        now_kst = _ensure_tz(now, self.config.market_timezone)

        market_transition: Optional[Tuple[MarketSessionState, MarketSessionState]] = None
        if self._last_market_state is not None and self._last_market_state != market_state:
            market_transition = (self._last_market_state, market_state)
            if (
                self.overlay == RuntimeOverlay.DEGRADED_FEED
                and market_transition[0] != MarketSessionState.IN_SESSION
                and market_transition[1] == MarketSessionState.IN_SESSION
            ):
                self._session_reentered_for_recover = True
        self._last_market_state = market_state

        ws_stable = False
        if market_state == MarketSessionState.IN_SESSION:
            ws_stable = self._is_ws_recover_stable(now_kst, feed_status)
        else:
            self._reset_ws_stability()

        prev_overlay = self.overlay
        overlay_transition: Optional[Tuple[RuntimeOverlay, RuntimeOverlay]] = None

        if risk_stop and self.overlay != RuntimeOverlay.EMERGENCY_STOP:
            self.overlay = RuntimeOverlay.EMERGENCY_STOP
            self._overlay_changed_at = now_kst
        elif self.overlay != RuntimeOverlay.EMERGENCY_STOP:
            if market_state == MarketSessionState.IN_SESSION:
                stale_condition = (
                    feed_status.ws_enabled
                    and (now_kst - self.start_ts).total_seconds() >= self.config.ws_start_grace_sec
                    and float(feed_status.ws_last_message_age_sec) > float(self.config.ws_stale_sec)
                )

                if self.overlay == RuntimeOverlay.NORMAL:
                    normal_elapsed = (now_kst - self._overlay_changed_at).total_seconds()
                    if stale_condition and normal_elapsed >= float(self.config.ws_min_normal_sec):
                        self.overlay = RuntimeOverlay.DEGRADED_FEED
                        self._overlay_changed_at = now_kst
                        if self.config.ws_recover_policy == "next_session":
                            self._session_reentered_for_recover = False

                elif self.overlay == RuntimeOverlay.DEGRADED_FEED:
                    degraded_elapsed = (now_kst - self._overlay_changed_at).total_seconds()
                    can_leave_degraded = degraded_elapsed >= float(self.config.ws_min_degraded_sec)
                    if can_leave_degraded and ws_stable:
                        if self.config.ws_recover_policy == "auto":
                            self.overlay = RuntimeOverlay.NORMAL
                            self._overlay_changed_at = now_kst
                        elif self._session_reentered_for_recover:
                            self.overlay = RuntimeOverlay.NORMAL
                            self._overlay_changed_at = now_kst
                            self._session_reentered_for_recover = False

        if prev_overlay != self.overlay:
            overlay_transition = (prev_overlay, self.overlay)

        policy = self._build_policy(market_state, feed_status)
        if self.overlay == RuntimeOverlay.DEGRADED_FEED and market_state == MarketSessionState.IN_SESSION:
            policy = RuntimePolicy(
                allow_new_entries=policy.allow_new_entries,
                allow_exit=policy.allow_exit,
                run_strategy=policy.run_strategy,
                active_feed_mode="rest",
                ws_should_run=True,
                sleep_sec=policy.sleep_sec,
            )

        return RuntimeDecision(
            market_state=market_state,
            market_reason=market_reason,
            overlay=self.overlay,
            policy=policy,
            feed_status=feed_status,
            market_transition=market_transition,
            overlay_transition=overlay_transition,
        )


class SymbolBarGate:
    """Per-symbol completed-bar gate to enforce run_once exactly once per bar."""

    def __init__(self):
        self._last_processed: Dict[str, datetime] = {}

    def last_processed(self, symbol: str) -> Optional[datetime]:
        return self._last_processed.get(str(symbol).zfill(6))

    def should_run(self, symbol: str, bar_ts: Optional[datetime]) -> bool:
        if bar_ts is None:
            return False
        code = str(symbol).zfill(6)
        prev = self._last_processed.get(code)
        if prev is not None and bar_ts <= prev:
            return False
        return True

    def mark_processed(self, symbol: str, bar_ts: datetime) -> None:
        self._last_processed[str(symbol).zfill(6)] = bar_ts


class TransitionCooldown:
    """Cooldown helper for repeated transition notifications."""

    def __init__(self, cooldown_sec: int):
        self.cooldown_sec = max(int(cooldown_sec), 1)
        self._last_sent_ts: Dict[str, datetime] = {}

    def should_send(self, key: str, now: datetime) -> bool:
        previous = self._last_sent_ts.get(key)
        if previous is None:
            self._last_sent_ts[key] = now
            return True
        if (now - previous).total_seconds() >= float(self.cooldown_sec):
            self._last_sent_ts[key] = now
            return True
        return False
