from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategy.multiday_trend_atr as multiday_trend_atr
import strategy.opening_range_breakout as opening_range_breakout
import strategy.pullback_rebreakout as pullback_rebreakout
from tools.fast_eval_replay import main as fast_eval_main
from tools.threaded_pipeline_replay import main as threaded_replay_main
from tools.threaded_pipeline_replay_support import (
    ThreadedPipelineReplayRunner,
    build_replay_report,
    load_replay_events,
)
from utils.market_hours import KST


def _kst_dt(hour: int, minute: int) -> datetime:
    return KST.localize(datetime(2026, 3, 11, hour, minute, 0))


def _make_indicator_df() -> pd.DataFrame:
    dates = pd.date_range(end=datetime(2026, 3, 10), periods=60, freq="D")
    close = list(np.linspace(100.0, 165.0, 45))
    close += [170.0, 174.0, 179.0, 176.0, 174.0, 172.0, 171.0, 170.5, 171.0, 172.0]
    close += [173.2, 173.8, 173.6, 173.5, 173.4]
    high = [value + 1.2 for value in close]
    low = [value - 1.0 for value in close]
    open_price = [value - 0.3 for value in close]
    volume = [700000 for _ in close]

    high[47] = 180.5
    low[47] = 177.0
    high[-3:] = [174.2, 174.5, 174.3]
    low[-3:] = [172.5, 173.0, 172.9]

    df = pd.DataFrame(
        {
            "date": dates,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    df["atr"] = 2.0
    df["adx"] = 32.0
    df["ma"] = 160.0
    df["ma20"] = 171.0
    df["trend"] = "UPTREND"
    df["prev_high"] = df["high"].shift(1)
    df["prev_close"] = df["close"].shift(1)
    df.loc[df.index[-1], "prev_high"] = 176.5
    return df


def _make_daily_context_event(symbol: str = "005930") -> dict:
    df = _make_indicator_df()
    return {
        "event_type": "daily_context",
        "event_ts": _kst_dt(8, 59).isoformat(),
        "symbol": symbol,
        "trade_date": "2026-03-11",
        "context_version": "ctx-1",
        "recent_bars": df.to_dict(orient="records"),
        "prev_high": float(df.iloc[-1]["prev_high"]),
        "prev_close": float(df.iloc[-1]["prev_close"]),
        "atr": 2.0,
        "adx": 32.0,
        "trend": "UPTREND",
        "ma20": 171.0,
        "ma50": 160.0,
        "swing_high": float(df.tail(15)["high"].max()),
        "swing_low": float(df.tail(12)["low"].min()),
    }


def _make_risk_event() -> dict:
    return {
        "event_type": "risk_snapshot",
        "event_ts": _kst_dt(8, 59).isoformat(),
        "holdings": [],
        "pending_symbols": [],
    }


def _make_regime_event() -> dict:
    return {
        "event_type": "regime_snapshot",
        "event_ts": _kst_dt(8, 59).isoformat(),
        "regime": "GOOD",
        "is_stale": False,
    }


def _make_pullback_quote_event(symbol: str = "005930", *, minute: int = 30) -> dict:
    return {
        "event_type": "quote",
        "event_ts": _kst_dt(10, minute).isoformat(),
        "symbol": symbol,
        "stock_name": "삼성전자",
        "current_price": 177.0,
        "open_price": 173.4,
        "best_bid": 176.9,
        "best_ask": 177.0,
        "session_high": 177.1,
        "session_low": 173.0,
    }


def _make_trend_quote_event(symbol: str = "005930") -> dict:
    return {
        "event_type": "quote",
        "event_ts": _kst_dt(10, 15).isoformat(),
        "symbol": symbol,
        "stock_name": "삼성전자",
        "current_price": 178.0,
        "open_price": 176.8,
        "best_bid": 177.9,
        "best_ask": 178.0,
        "session_high": 178.2,
        "session_low": 176.5,
    }


def _make_orb_intraday_events(symbol: str = "005930") -> list[dict]:
    events: list[dict] = []
    market_open = _kst_dt(9, 0)
    opening_range_high = 177.4
    opening_range_low = 176.3
    for minute_idx in range(5):
        start_at = market_open + timedelta(minutes=minute_idx)
        close_price = 176.8 + (minute_idx * 0.05)
        events.append(
            {
                "event_type": "intraday_bar",
                "event_ts": start_at.isoformat(),
                "symbol": symbol,
                "start_at": start_at.isoformat(),
                "open": close_price - 0.1,
                "high": opening_range_high if minute_idx == 1 else close_price + 0.05,
                "low": opening_range_low if minute_idx == 0 else close_price - 0.15,
                "close": close_price,
                "volume": 1000 + minute_idx * 50,
                "provider_ready": True,
            }
        )
    for minute_idx in range(5, 32):
        start_at = market_open + timedelta(minutes=minute_idx)
        close_price = 177.0 if minute_idx < 30 else 177.8
        events.append(
            {
                "event_type": "intraday_bar",
                "event_ts": start_at.isoformat(),
                "symbol": symbol,
                "start_at": start_at.isoformat(),
                "open": close_price - 0.05,
                "high": close_price + 0.05,
                "low": close_price - 0.1,
                "close": close_price,
                "volume": 1200 + minute_idx * 10,
                "provider_ready": True,
            }
        )
    return events


def _make_orb_quote_event(symbol: str = "005930", *, minute: int = 31) -> dict:
    return {
        "event_type": "quote",
        "event_ts": _kst_dt(9, minute).isoformat(),
        "symbol": symbol,
        "stock_name": "삼성전자",
        "current_price": 177.9,
        "open_price": 176.8,
        "best_bid": 177.8,
        "best_ask": 177.9,
        "session_high": 178.0,
        "session_low": 176.5,
    }


def _write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for payload in events:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


@contextmanager
def _patched_replay_settings():
    import tools.threaded_pipeline_replay_support as replay_support
    import engine.strategy_pipeline_registry as strategy_pipeline_registry

    values = {
        "ENABLE_PULLBACK_REBREAKOUT_STRATEGY": True,
        "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY": True,
        "ENABLE_MULTI_STRATEGY_THREADED_PIPELINE": True,
        "THREADED_PIPELINE_ENABLED_STRATEGIES": "pullback_rebreakout,trend_atr,opening_range_breakout",
        "STRATEGY_CANDIDATE_MAX_AGE_SEC": 300,
        "PULLBACK_SETUP_REFRESH_SEC": 60,
        "PULLBACK_REBREAKOUT_LOOKBACK_BARS": 3,
        "PULLBACK_LOOKBACK_BARS": 12,
        "PULLBACK_SWING_LOOKBACK_BARS": 15,
        "PULLBACK_MIN_PULLBACK_PCT": 0.015,
        "PULLBACK_MAX_PULLBACK_PCT": 0.06,
        "PULLBACK_REQUIRE_ABOVE_MA20": True,
        "PULLBACK_USE_ADX_FILTER": True,
        "PULLBACK_MIN_ADX": 20.0,
        "PULLBACK_ONLY_MAIN_MARKET": True,
        "PULLBACK_ALLOWED_ENTRY_VENUES": "KRX",
        "PULLBACK_BLOCK_IF_EXISTING_POSITION": True,
        "PULLBACK_BLOCK_IF_PENDING_ORDER": True,
        "ENABLE_OPENING_NO_ENTRY_GUARD": False,
        "ENABLE_BREAKOUT_EXTENSION_CAP": False,
        "ENABLE_ENTRY_GAP_FILTER": False,
        "ORB_BLOCK_IF_PENDING_ORDER": True,
        "ORB_ONLY_MAIN_MARKET": True,
        "ORB_ALLOWED_ENTRY_VENUES": "KRX",
        "ORB_OPENING_RANGE_MINUTES": 5,
        "ORB_ENTRY_CUTOFF_MINUTES": 90,
        "MAX_PENDING_INTENTS_PER_SYMBOL": 1,
        "MAX_INTENT_QUEUE_DEPTH": 1024,
    }
    with ExitStack() as stack:
        for module in (
            replay_support.settings,
            multiday_trend_atr.settings,
            opening_range_breakout.settings,
            pullback_rebreakout.settings,
            strategy_pipeline_registry.settings,
        ):
            stack.enter_context(patch.multiple(module, **values))
        yield


def test_threaded_pipeline_replay_is_repeatable_for_same_input(tmp_path: Path):
    replay_path = tmp_path / "replay.jsonl"
    events = [_make_daily_context_event(), _make_risk_event(), _make_regime_event(), _make_trend_quote_event()]
    _write_events(replay_path, events)

    with _patched_replay_settings():
        loaded = load_replay_events(replay_path, strict=True)
        report1 = build_replay_report(loaded, strategy_filter=("trend_atr",), strict=True)
        report2 = build_replay_report(loaded, strategy_filter=("trend_atr",), strict=True)

    assert json.dumps(report1, sort_keys=True, default=str) == json.dumps(report2, sort_keys=True, default=str)
    assert report1["summary"]["orders_would_submit"] == 1


def test_threaded_pipeline_replay_runs_without_starting_threads(tmp_path: Path):
    replay_path = tmp_path / "replay.jsonl"
    _write_events(replay_path, [_make_daily_context_event(), _make_risk_event(), _make_trend_quote_event()])

    with _patched_replay_settings(), patch("threading.Thread.start", side_effect=AssertionError("threads forbidden")):
        loaded = load_replay_events(replay_path, strict=True)
        report = build_replay_report(loaded, strategy_filter=("trend_atr",), strict=True)

    assert report["summary"]["orders_would_submit"] == 1


def test_threaded_pipeline_replay_pullback_timing_path_creates_candidate_intent_and_order(tmp_path: Path):
    replay_path = tmp_path / "pullback.jsonl"
    _write_events(
        replay_path,
        [_make_daily_context_event(), _make_risk_event(), _make_regime_event(), _make_pullback_quote_event()],
    )

    with _patched_replay_settings():
        report = build_replay_report(load_replay_events(replay_path, strict=True), strategy_filter=("pullback_rebreakout",), strict=True)

    assert any(item["setup_candidate_created"] for item in report["candidate_timeline"])
    assert any(item["intent_emitted"] for item in report["intent_timeline"])
    assert any(item["order_decision"] == "order_would_submit" for item in report["order_timeline"])


def test_threaded_pipeline_replay_trend_atr_path_reuses_native_handoff(tmp_path: Path):
    replay_path = tmp_path / "trend.jsonl"
    _write_events(replay_path, [_make_daily_context_event(), _make_risk_event(), _make_trend_quote_event()])

    with _patched_replay_settings():
        report = build_replay_report(load_replay_events(replay_path, strict=True), strategy_filter=("trend_atr",), strict=True)

    assert report["summary"]["orders_would_submit"] == 1
    assert any(item["strategy_tag"] == "trend_atr" for item in report["order_timeline"])


def test_threaded_pipeline_replay_orb_with_intraday_bars_submits_order(tmp_path: Path):
    replay_path = tmp_path / "orb.jsonl"
    events = [_make_daily_context_event(), _make_risk_event(), _make_regime_event(), *_make_orb_intraday_events(), _make_orb_quote_event()]
    _write_events(replay_path, events)

    with _patched_replay_settings():
        report = build_replay_report(
            load_replay_events(replay_path, strict=True),
            strategy_filter=("opening_range_breakout",),
            strict=True,
        )

    assert report["summary"]["orders_would_submit"] == 1
    assert any(item["strategy_tag"] == "opening_range_breakout" for item in report["order_timeline"])


def test_threaded_pipeline_replay_orb_quote_only_is_partial_and_unsupported(tmp_path: Path):
    replay_path = tmp_path / "orb_quote_only.jsonl"
    _write_events(replay_path, [_make_daily_context_event(), _make_risk_event(), _make_regime_event(), _make_orb_quote_event()])

    with _patched_replay_settings():
        report = build_replay_report(
            load_replay_events(replay_path, strict=True),
            strategy_filter=("opening_range_breakout",),
            strict=True,
        )

    assert report["summary"]["orders_would_submit"] == 0
    assert "orb_intraday_unsupported" in report["summary"]["reject_reason_summary"]


def test_threaded_pipeline_replay_respects_orb_native_cutoff_expiry(tmp_path: Path):
    replay_path = tmp_path / "orb_expiry.jsonl"
    events = [_make_daily_context_event(), _make_risk_event(), *_make_orb_intraday_events(), _make_orb_quote_event(minute=31)]
    late_quote = _make_orb_quote_event(minute=31)
    late_quote["event_ts"] = _kst_dt(10, 45).isoformat()
    events.append(late_quote)
    _write_events(replay_path, events)

    with _patched_replay_settings():
        report = build_replay_report(
            load_replay_events(replay_path, strict=True),
            strategy_filter=("opening_range_breakout",),
            strict=True,
        )

    assert report["summary"]["orders_would_submit"] == 1


def test_threaded_pipeline_replay_degraded_state_blocks_new_ingress_without_threads(tmp_path: Path):
    replay_path = tmp_path / "degraded.jsonl"
    events = [
        _make_daily_context_event(),
        _make_risk_event(),
        {"event_type": "degraded_state_change", "event_ts": _kst_dt(10, 0).isoformat(), "is_degraded": True, "reason": "synthetic"},
        _make_trend_quote_event(),
    ]
    _write_events(replay_path, events)

    with _patched_replay_settings():
        report = build_replay_report(load_replay_events(replay_path, strict=True), strategy_filter=("trend_atr",), strict=True)

    assert report["summary"]["orders_would_submit"] == 0
    assert "degraded_mode" in report["summary"]["reject_reason_summary"]


def test_fast_eval_and_threaded_replay_tools_can_coexist():
    assert callable(fast_eval_main)
    assert callable(threaded_replay_main)
