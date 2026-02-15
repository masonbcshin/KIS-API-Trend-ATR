"""Tests for daily report generation and delivery."""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Add project root for local imports.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reporting.daily_report_service import DailyReportService, DailyReportResult, ReportSymbolSummary


class DummyDB:
    """Small query router used by unit tests."""

    def __init__(
        self,
        *,
        trades=None,
        snapshot_first=None,
        snapshot_last=None,
        daily_summary_row=None,
        risk_reason_rows=None,
        order_state_row=None,
        positions_column_count=0,
        positions_rows=None,
        position_columns=None,
        table_exists_map=None,
    ):
        self.trades = trades or []
        self.snapshot_first = snapshot_first
        self.snapshot_last = snapshot_last
        self.daily_summary_row = daily_summary_row
        self.risk_reason_rows = risk_reason_rows or []
        self.order_state_row = order_state_row
        self.positions_column_count = positions_column_count
        self.positions_rows = positions_rows or []
        self.position_columns = position_columns or []
        self.table_exists_map = table_exists_map or {}
        self.commands = []
        self.config = SimpleNamespace(database="kis_trading")

    def table_exists(self, table_name):
        return self.table_exists_map.get(table_name, False)

    def execute_query(self, query, params=None, fetch_one=False):
        q = " ".join(query.lower().split())

        if "from trades" in q and "order by executed_at" in q:
            result = self.trades
        elif "from account_snapshots" in q and "order by snapshot_time asc" in q:
            result = self.snapshot_first
        elif "from account_snapshots" in q and "order by snapshot_time desc" in q:
            result = self.snapshot_last
        elif "from account_snapshots" in q and "unrealized_pnl" in q:
            result = (
                {"unrealized_pnl": None}
                if self.snapshot_last is None
                else {"unrealized_pnl": self.snapshot_last.get("unrealized_pnl")}
            )
        elif "from daily_summary" in q:
            result = self.daily_summary_row
        elif "from information_schema.columns" in q and "table_name = 'positions'" in q:
            result = {"cnt": self.positions_column_count}
        elif "select column_name" in q and "from information_schema.columns" in q and "table_name = %s" in q:
            result = [{"column_name": name} for name in self.position_columns]
        elif "from positions" in q and "unrealized_pnl" in q:
            result = self.positions_rows
        elif "from trades" in q and "reason in" in q:
            result = self.risk_reason_rows
        elif "from order_state" in q:
            result = self.order_state_row or {"failed_count": 0, "cancelled_count": 0}
        else:
            raise AssertionError(f"Unexpected query in test: {query}")

        if fetch_one and isinstance(result, list):
            return result[0] if result else None
        return result

    def execute_command(self, command, params=None):
        self.commands.append((command, params))
        return 1


def _create_service(db: DummyDB) -> DailyReportService:
    notifier = MagicMock()
    notifier.send_message.return_value = True
    resolver = MagicMock()
    resolver.format_symbol.side_effect = (
        lambda symbol, refresh=False: f"삼성전자({symbol})" if symbol == "005930" else f"UNKNOWN({symbol})"
    )
    return DailyReportService(
        db=db,
        notifier=notifier,
        symbol_resolver=resolver,
        mode="PAPER",
    )


def test_calculate_trade_metrics_with_sample_trades():
    sample_trades = [
        {"symbol": "005930", "side": "BUY", "pnl": None, "reason": None},
        {"symbol": "005930", "side": "SELL", "pnl": 15000, "reason": "TAKE_PROFIT"},
        {"symbol": "000660", "side": "BUY", "pnl": None, "reason": None},
        {"symbol": "000660", "side": "SELL", "pnl": -5000, "reason": "ATR_STOP"},
    ]

    metrics = DailyReportService.calculate_trade_metrics(sample_trades)

    assert metrics["realized_pnl"] == 10000
    assert metrics["total_trades"] == 4
    assert metrics["buy_count"] == 2
    assert metrics["sell_count"] == 2
    assert metrics["win_rate"] == 50.0


def test_snapshot_missing_marks_na_in_message():
    db = DummyDB(
        trades=[{"symbol": "005930", "side": "SELL", "pnl": 10000, "reason": "TAKE_PROFIT"}],
        snapshot_first=None,
        snapshot_last=None,
        table_exists_map={"daily_summary": False, "positions": False, "order_state": False},
    )
    service = _create_service(db)

    report = service.build_report(date(2026, 2, 15))
    message = service.render_message(report)

    assert "계좌 스냅샷: N/A (근거 부족) -> N/A (근거 부족)" in message
    assert "미실현손익: N/A (근거 부족)" in message


def test_send_report_message_uses_mocked_notifier():
    db = DummyDB(table_exists_map={"daily_summary": False, "positions": False, "order_state": False})
    service = _create_service(db)

    sent = service.send_report_message("hello report")

    assert sent is True
    service._notifier.send_message.assert_called_once_with("hello report", parse_mode=None)


def test_render_message_contains_date_realized_pnl_and_symbol_display():
    report = DailyReportResult(
        trade_date=date(2026, 2, 15),
        mode="PAPER",
        source="trades",
        realized_pnl=12345,
        realized_pnl_pct=0.12,
        total_trades=2,
        buy_count=1,
        sell_count=1,
        win_count=1,
        loss_count=0,
        win_rate=100.0,
        avg_win=12345,
        avg_loss=None,
        start_equity=10_000_000,
        end_equity=10_012_345,
        unrealized_pnl=None,
        top_symbols=[ReportSymbolSummary(symbol="005930", display_symbol="삼성전자(005930)", realized_pnl=12345)],
        risk_events=[],
    )
    db = DummyDB(table_exists_map={"daily_summary": False, "positions": False, "order_state": False})
    service = _create_service(db)

    message = service.render_message(report)

    assert "2026-02-15" in message
    assert "실현손익: +12,345원" in message
    assert "삼성전자(005930)" in message


def test_unrealized_query_does_not_require_symbol_column():
    class SymbolSensitiveDB(DummyDB):
        def execute_query(self, query, params=None, fetch_one=False):
            q = " ".join(query.lower().split())
            if "from positions" in q and "symbol" in q:
                raise AssertionError("positions query must not require symbol column")
            return super().execute_query(query, params=params, fetch_one=fetch_one)

    db = SymbolSensitiveDB(
        trades=[{"symbol": "005930", "side": "SELL", "pnl": 10000, "reason": "TAKE_PROFIT"}],
        snapshot_first={"total_equity": 1_000_000},
        snapshot_last={"total_equity": 1_010_000},
        positions_column_count=2,
        position_columns=["status", "mode", "unrealized_pnl", "current_price"],
        positions_rows=[{"unrealized_pnl": 1234.5}],
        table_exists_map={"daily_summary": False, "positions": True, "order_state": False},
    )
    service = _create_service(db)

    report = service.build_report(date(2026, 2, 15))

    assert report.unrealized_pnl == 1234.5


def test_unrealized_falls_back_to_latest_account_snapshot():
    db = DummyDB(
        trades=[{"symbol": "005930", "side": "SELL", "pnl": 10000, "reason": "TAKE_PROFIT"}],
        snapshot_first={"total_equity": 1_000_000},
        snapshot_last={"total_equity": 1_010_000, "unrealized_pnl": 4321.0},
        positions_column_count=0,
        table_exists_map={
            "daily_summary": False,
            "positions": False,
            "order_state": False,
            "account_snapshots": True,
        },
    )
    service = _create_service(db)

    report = service.build_report(date(2026, 2, 15))

    assert report.unrealized_pnl == 4321.0
