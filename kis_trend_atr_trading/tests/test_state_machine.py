from __future__ import annotations

from datetime import datetime, timedelta

import pytz

from engine.runtime_state_machine import FeedStatus, RuntimeConfig, RuntimeOverlay, RuntimeStateMachine
from utils.market_hours import MarketSessionState, get_market_session_state


KST = pytz.timezone("Asia/Seoul")


def _kst(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return KST.localize(datetime(y, m, d, hh, mm, ss))


def test_market_session_state_transition_fixed_times():
    weekend_state, _ = get_market_session_state(now=_kst(2026, 2, 14, 10, 0))
    assert weekend_state == MarketSessionState.OFF_SESSION

    preopen_state, _ = get_market_session_state(
        now=_kst(2026, 2, 13, 8, 55),
        preopen_warmup_min=10,
    )
    assert preopen_state == MarketSessionState.PREOPEN_WARMUP

    in_session_state, _ = get_market_session_state(now=_kst(2026, 2, 13, 10, 0))
    assert in_session_state == MarketSessionState.IN_SESSION


def test_stale_evaluated_only_in_session_with_start_grace():
    config = RuntimeConfig(
        data_feed_default="ws",
        ws_start_grace_sec=30,
        ws_stale_sec=60,
        ws_min_normal_sec=0,
    )
    machine = RuntimeStateMachine(config=config, start_ts=_kst(2026, 2, 13, 9, 0, 0))

    in_grace = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 20),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=999.0,
        ),
        risk_stop=False,
    )
    assert in_grace.overlay == RuntimeOverlay.NORMAL

    after_grace = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 1, 0),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=999.0,
        ),
        risk_stop=False,
    )
    assert after_grace.overlay == RuntimeOverlay.DEGRADED_FEED

    off_session_machine = RuntimeStateMachine(config=config, start_ts=_kst(2026, 2, 13, 6, 0, 0))
    off_session = off_session_machine.evaluate(
        now=_kst(2026, 2, 13, 7, 0, 0),
        market_state=MarketSessionState.OFF_SESSION,
        market_reason="off_session_before_prewarm",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=999.0,
        ),
        risk_stop=False,
    )
    assert off_session.overlay == RuntimeOverlay.NORMAL
    assert off_session.overlay_transition is None


def test_ws_recover_policy_next_session():
    config = RuntimeConfig(
        data_feed_default="ws",
        ws_start_grace_sec=0,
        ws_stale_sec=60,
        ws_min_normal_sec=0,
        ws_min_degraded_sec=0,
        ws_recover_policy="next_session",
        ws_recover_stable_sec=1,
        ws_recover_required_bars=2,
    )
    machine = RuntimeStateMachine(config=config, start_ts=_kst(2026, 2, 13, 9, 0, 0))

    degraded = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 0),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=999.0,
        ),
        risk_stop=False,
    )
    assert degraded.overlay == RuntimeOverlay.DEGRADED_FEED

    same_session_stable_1 = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 2),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=True,
            ws_last_message_age_sec=0.5,
            ws_last_bar_ts=_kst(2026, 2, 13, 9, 0, 0),
        ),
        risk_stop=False,
    )
    same_session_stable_2 = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 3),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=True,
            ws_last_message_age_sec=0.5,
            ws_last_bar_ts=_kst(2026, 2, 13, 9, 1, 0),
        ),
        risk_stop=False,
    )
    assert same_session_stable_1.overlay == RuntimeOverlay.DEGRADED_FEED
    assert same_session_stable_2.overlay == RuntimeOverlay.DEGRADED_FEED

    machine.evaluate(
        now=_kst(2026, 2, 13, 15, 40, 0),
        market_state=MarketSessionState.OFF_SESSION,
        market_reason="off_session_after_postclose",
        feed_status=FeedStatus(ws_enabled=True),
        risk_stop=False,
    )
    recovered = machine.evaluate(
        now=_kst(2026, 2, 17, 9, 0, 2),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=True,
            ws_last_message_age_sec=0.1,
            ws_last_bar_ts=_kst(2026, 2, 17, 9, 0, 0),
        ),
        risk_stop=False,
    )
    recovered = machine.evaluate(
        now=_kst(2026, 2, 17, 9, 0, 3),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=True,
            ws_last_message_age_sec=0.1,
            ws_last_bar_ts=_kst(2026, 2, 17, 9, 1, 0),
        ),
        risk_stop=False,
    )
    assert recovered.overlay == RuntimeOverlay.NORMAL
    assert recovered.overlay_transition == (
        RuntimeOverlay.DEGRADED_FEED,
        RuntimeOverlay.NORMAL,
    )
