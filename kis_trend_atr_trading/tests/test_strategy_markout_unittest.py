from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from kis_trend_atr_trading.analytics.event_logger import StrategyAnalyticsEventLogger
from kis_trend_atr_trading.analytics.materializer import StrategyAnalyticsMaterializer


def test_markout_calculation_and_na_rows(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    entry_ts = datetime.fromisoformat("2026-03-11T09:00:00+09:00")
    logger.log_event(
        event_ts=entry_ts,
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        intent_id="intent-1",
        broker_order_id="ORD-1",
        event_type="order_filled",
        stage="order",
        decision="filled",
        source_component="unit_test",
        payload_json={"fill_price": 100.0, "exec_qty": 2, "side": "BUY"},
    )
    logger.log_event(
        event_ts=entry_ts + timedelta(seconds=181),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 101.0},
    )
    logger.log_event(
        event_ts=entry_ts + timedelta(seconds=301),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 102.0},
    )
    logger.close()

    materializer = StrategyAnalyticsMaterializer(
        event_dir=str(tmp_path),
        markout_horizons_sec=[60, 180, 300],
        enable_markouts=True,
    )
    result = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
    rows = {(row["horizon_sec"], row["source_type"]): row for row in result["markout_rows"]}
    assert rows[(60, "quote")]["mark_price"] == 101.0
    assert round(rows[(60, "quote")]["markout_bps"], 2) == 100.0
    assert rows[(180, "quote")]["mark_price"] == 101.0
    assert round(rows[(180, "quote")]["markout_bps"], 2) == 100.0
    assert rows[(300, "quote")]["mark_price"] == 102.0
    assert round(rows[(300, "quote")]["markout_bps"], 2) == 200.0
