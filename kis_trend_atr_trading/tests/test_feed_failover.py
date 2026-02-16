from __future__ import annotations

from datetime import datetime

import pytz

from adapters.kis_ws.bar_aggregator import MarketTick, MinuteBarAggregator
from engine.runtime_state_machine import FeedStatus, RuntimeConfig, RuntimeOverlay, RuntimeStateMachine
from utils.market_hours import MarketSessionState


KST = pytz.timezone("Asia/Seoul")


def _kst(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return KST.localize(datetime(y, m, d, hh, mm, ss))


def test_in_session_stale_forces_degraded_and_rest_active_feed():
    config = RuntimeConfig(
        data_feed_default="ws",
        ws_start_grace_sec=0,
        ws_stale_sec=60,
        ws_min_normal_sec=0,
    )
    machine = RuntimeStateMachine(config=config, start_ts=_kst(2026, 2, 13, 9, 0, 0))

    decision = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 1, 1),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=120.0,
        ),
        risk_stop=False,
    )

    assert decision.overlay == RuntimeOverlay.DEGRADED_FEED
    assert decision.policy.active_feed_mode == "rest"
    assert decision.policy.ws_should_run is True


def test_off_session_stale_does_not_trigger_degraded_transition():
    config = RuntimeConfig(
        data_feed_default="ws",
        ws_start_grace_sec=0,
        ws_stale_sec=60,
        ws_min_normal_sec=0,
    )
    machine = RuntimeStateMachine(config=config, start_ts=_kst(2026, 2, 13, 4, 0, 0))

    decision = machine.evaluate(
        now=_kst(2026, 2, 13, 6, 0, 0),
        market_state=MarketSessionState.OFF_SESSION,
        market_reason="off_session_before_prewarm",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=999.0,
        ),
        risk_stop=False,
    )

    assert decision.overlay == RuntimeOverlay.NORMAL
    assert decision.overlay_transition is None


def test_ws_stabilized_recovers_degraded_to_normal():
    config = RuntimeConfig(
        data_feed_default="ws",
        ws_start_grace_sec=0,
        ws_stale_sec=60,
        ws_min_normal_sec=0,
        ws_min_degraded_sec=0,
        ws_recover_policy="auto",
        ws_recover_stable_sec=1,
        ws_recover_required_bars=2,
    )
    machine = RuntimeStateMachine(config=config, start_ts=_kst(2026, 2, 13, 9, 0, 0))

    machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 0),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=False,
            ws_last_message_age_sec=120.0,
        ),
        risk_stop=False,
    )
    assert machine.overlay == RuntimeOverlay.DEGRADED_FEED

    machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 2),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=True,
            ws_last_message_age_sec=0.1,
            ws_last_bar_ts=_kst(2026, 2, 13, 9, 0, 0),
        ),
        risk_stop=False,
    )
    recovered = machine.evaluate(
        now=_kst(2026, 2, 13, 9, 0, 3),
        market_state=MarketSessionState.IN_SESSION,
        market_reason="regular_session_open",
        feed_status=FeedStatus(
            ws_enabled=True,
            ws_connected=True,
            ws_last_message_age_sec=0.1,
            ws_last_bar_ts=_kst(2026, 2, 13, 9, 1, 0),
        ),
        risk_stop=False,
    )

    assert recovered.overlay == RuntimeOverlay.NORMAL
    assert recovered.overlay_transition == (
        RuntimeOverlay.DEGRADED_FEED,
        RuntimeOverlay.NORMAL,
    )


def test_minute_bar_aggregator_emits_completed_event_from_tick_sequence():
    agg = MinuteBarAggregator(timeframe="1m")
    t1 = MarketTick("005930", 100.0, 10.0, datetime(2026, 2, 13, 9, 0, 1))
    t2 = MarketTick("005930", 101.0, 3.0, datetime(2026, 2, 13, 9, 0, 50))
    t3 = MarketTick("005930", 102.0, 5.0, datetime(2026, 2, 13, 9, 1, 2))

    assert agg.add_tick(t1) is None
    assert agg.add_tick(t2) is None
    completed = agg.add_tick(t3)

    assert completed is not None
    assert completed.start_at == datetime(2026, 2, 13, 9, 0, 0)
    assert completed.end_at == datetime(2026, 2, 13, 9, 1, 0)
    assert completed.open == 100.0
    assert completed.high == 101.0
    assert completed.close == 101.0
