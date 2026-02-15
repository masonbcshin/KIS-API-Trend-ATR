"""
Daily report service backed by MySQL.

This module is intentionally isolated from trading execution logic so that
it can run as an external scheduler task (cron/systemd timer).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db.mysql import MySQLManager, get_db_manager
from env import get_trading_mode
from utils.logger import get_logger
from utils.symbol_resolver import SymbolResolver, get_symbol_resolver
from utils.telegram_notifier import TelegramNotifier, get_telegram_notifier

logger = get_logger("daily_report_service")

_RISK_REASON_LABELS = {
    "DAILY_LOSS_LIMIT": "일일손실 제한 발동",
    "KILL_SWITCH": "킬스위치 발동",
    "ORDER_BLOCKED": "주문차단",
    "RISK_BLOCK": "리스크 차단",
}


@dataclass
class ReportSymbolSummary:
    """Per-symbol realized PnL summary for the report."""

    symbol: str
    display_symbol: str
    realized_pnl: float
    sell_count: int = 0


@dataclass
class DailyReportResult:
    """Computed daily report payload."""

    trade_date: date
    mode: str
    source: str
    realized_pnl: float
    realized_pnl_pct: Optional[float]
    total_trades: int
    buy_count: int
    sell_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_win: Optional[float]
    avg_loss: Optional[float]
    start_equity: Optional[float]
    end_equity: Optional[float]
    unrealized_pnl: Optional[float]
    top_symbols: List[ReportSymbolSummary] = field(default_factory=list)
    risk_events: List[str] = field(default_factory=list)


class DailyReportService:
    """Builds and sends a DB-backed daily report."""

    def __init__(
        self,
        db: Optional[MySQLManager] = None,
        notifier: Optional[TelegramNotifier] = None,
        symbol_resolver: Optional[SymbolResolver] = None,
        mode: Optional[str] = None,
    ):
        self._db = db or get_db_manager()
        self._notifier = notifier or get_telegram_notifier()
        self._symbol_resolver = symbol_resolver or get_symbol_resolver()
        self._mode = mode or get_trading_mode()

    @staticmethod
    def calculate_trade_metrics(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate realized PnL and trade stats from raw trades."""
        normalized: List[Dict[str, Any]] = []
        for row in trades:
            reason = str(row.get("reason") or "").upper()
            if reason == "SIGNAL_ONLY":
                continue
            side = str(row.get("side") or "").upper()
            pnl_raw = row.get("pnl")
            pnl = float(pnl_raw) if pnl_raw is not None else None
            normalized.append(
                {
                    "symbol": str(row.get("symbol") or ""),
                    "side": side,
                    "pnl": pnl,
                }
            )

        buy_count = sum(1 for row in normalized if row["side"] == "BUY")
        sell_rows = [row for row in normalized if row["side"] == "SELL"]
        sell_count = len(sell_rows)
        pnl_values = [row["pnl"] for row in sell_rows if row["pnl"] is not None]

        win_values = [p for p in pnl_values if p > 0]
        loss_values = [p for p in pnl_values if p < 0]

        realized_pnl = sum(pnl_values)
        win_count = len(win_values)
        loss_count = len(loss_values)
        win_rate = (win_count / sell_count * 100.0) if sell_count > 0 else 0.0

        return {
            "total_trades": len(normalized),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "realized_pnl": realized_pnl,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "avg_win": (sum(win_values) / len(win_values)) if win_values else None,
            "avg_loss": (sum(loss_values) / len(loss_values)) if loss_values else None,
            "max_profit": max(pnl_values) if pnl_values else 0.0,
            "max_loss": min(pnl_values) if pnl_values else 0.0,
        }

    def build_report(self, trade_date: date, persist_daily_summary: bool = True) -> DailyReportResult:
        """Build a report payload from DB."""
        trades = self._load_trades(trade_date)
        computed = self.calculate_trade_metrics(trades)
        start_equity, end_equity = self._get_equity_bounds(trade_date)

        source = "trades"
        if persist_daily_summary and self._daily_summary_exists():
            self._upsert_daily_summary(
                trade_date=trade_date,
                metrics=computed,
                start_equity=start_equity,
                end_equity=end_equity,
            )
            summary_row = self._get_daily_summary_row(trade_date)
            if summary_row:
                source = "daily_summary"
                computed = self._merge_metrics_from_daily_summary(summary_row, computed)

        realized_pnl = float(computed["realized_pnl"])
        realized_pnl_pct = None
        if start_equity and start_equity != 0:
            realized_pnl_pct = (realized_pnl / start_equity) * 100.0

        return DailyReportResult(
            trade_date=trade_date,
            mode=self._mode,
            source=source,
            realized_pnl=realized_pnl,
            realized_pnl_pct=realized_pnl_pct,
            total_trades=int(computed["total_trades"]),
            buy_count=int(computed["buy_count"]),
            sell_count=int(computed["sell_count"]),
            win_count=int(computed["win_count"]),
            loss_count=int(computed["loss_count"]),
            win_rate=float(computed["win_rate"]),
            avg_win=self._to_float_or_none(computed.get("avg_win")),
            avg_loss=self._to_float_or_none(computed.get("avg_loss")),
            start_equity=self._to_float_or_none(start_equity),
            end_equity=self._to_float_or_none(end_equity),
            unrealized_pnl=self._get_unrealized_pnl(),
            top_symbols=self._build_symbol_summaries(trades),
            risk_events=self._load_risk_events(trade_date),
        )

    def render_message(
        self,
        report: DailyReportResult,
        top_symbol_limit: int = 3,
        risk_event_limit: int = 5,
    ) -> str:
        """Render the report as Telegram-safe plain text."""
        lines = [
            f"[일일 자동 리포트] {report.trade_date.isoformat()} ({report.mode})",
            "",
            f"실현손익: {self._fmt_won(report.realized_pnl)} ({self._fmt_percent(report.realized_pnl_pct)})",
            f"거래 횟수: 총 {report.total_trades}회 (매수 {report.buy_count} / 매도 {report.sell_count})",
            (
                f"승률(매도 기준): {report.win_rate:.1f}% "
                f"(승 {report.win_count} / 패 {report.loss_count})"
            ),
            (
                "평균 이익/손실: "
                f"{self._fmt_optional_won(report.avg_win)} / {self._fmt_optional_won(report.avg_loss)}"
            ),
            (
                "계좌 스냅샷: "
                f"{self._fmt_optional_won(report.start_equity, with_sign=False)}"
                f" -> {self._fmt_optional_won(report.end_equity, with_sign=False)}"
            ),
            f"미실현손익: {self._fmt_optional_won(report.unrealized_pnl)}",
            "",
            "종목별 요약 Top3:",
        ]

        if report.top_symbols:
            limited = report.top_symbols[:max(top_symbol_limit, 1)]
            for idx, row in enumerate(limited, start=1):
                lines.append(f"{idx}. {row.display_symbol}: {self._fmt_won(row.realized_pnl)}")
            remaining = len(report.top_symbols) - len(limited)
            if remaining > 0:
                lines.append(f"+ 나머지 {remaining}종목")
        else:
            lines.append("- 데이터 없음")

        lines.append("")
        lines.append("리스크 이벤트:")
        if report.risk_events:
            limited_events = report.risk_events[: max(risk_event_limit, 1)]
            for event in limited_events:
                lines.append(f"- {event}")
            remaining_events = len(report.risk_events) - len(limited_events)
            if remaining_events > 0:
                lines.append(f"- 기타 {remaining_events}건")
        else:
            lines.append("- 없음 또는 근거 부족")

        lines.append("")
        lines.append(f"(source: {report.source})")
        return "\n".join(lines)

    def send_report_message(self, message: str) -> bool:
        """Send report message through existing Telegram notifier."""
        return self._notifier.send_message(message, parse_mode=None)

    def send_report(self, report: DailyReportResult) -> bool:
        """Render and send report."""
        return self.send_report_message(self.render_message(report))

    def test_telegram_connection(self) -> bool:
        """Run Telegram connectivity test via existing notifier."""
        return self._notifier.test_connection()

    def _load_trades(self, trade_date: date) -> List[Dict[str, Any]]:
        rows = self._db.execute_query(
            """
            SELECT symbol, side, pnl, reason, executed_at
            FROM trades
            WHERE DATE(executed_at) = %s AND mode = %s
            ORDER BY executed_at
            """,
            (trade_date, self._mode),
        )
        return rows or []

    def _build_symbol_summaries(self, trades: Iterable[Dict[str, Any]]) -> List[ReportSymbolSummary]:
        by_symbol: Dict[str, Dict[str, float]] = {}
        for row in trades:
            reason = str(row.get("reason") or "").upper()
            if reason == "SIGNAL_ONLY":
                continue
            if str(row.get("side") or "").upper() != "SELL":
                continue
            pnl_raw = row.get("pnl")
            if pnl_raw is None:
                continue

            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            pnl = float(pnl_raw)

            bucket = by_symbol.setdefault(symbol, {"pnl": 0.0, "sell_count": 0.0})
            bucket["pnl"] += pnl
            bucket["sell_count"] += 1.0

        summaries = [
            ReportSymbolSummary(
                symbol=symbol,
                display_symbol=self._safe_format_symbol(symbol),
                realized_pnl=values["pnl"],
                sell_count=int(values["sell_count"]),
            )
            for symbol, values in by_symbol.items()
        ]
        summaries.sort(key=lambda item: item.realized_pnl, reverse=True)
        return summaries

    def _safe_format_symbol(self, symbol: str) -> str:
        try:
            return self._symbol_resolver.format_symbol(symbol, refresh=False)
        except TypeError:
            # Backward compatibility if resolver signature differs.
            try:
                return self._symbol_resolver.format_symbol(symbol)
            except Exception as err:
                logger.warning(f"[REPORT] symbol format fallback failed: {symbol}, {err}")
                return f"UNKNOWN({symbol})"
        except Exception as err:
            logger.warning(f"[REPORT] symbol format failed: {symbol}, {err}")
            return f"UNKNOWN({symbol})"

    def _get_equity_bounds(self, trade_date: date) -> Tuple[Optional[float], Optional[float]]:
        first = self._db.execute_query(
            """
            SELECT total_equity
            FROM account_snapshots
            WHERE DATE(snapshot_time) = %s AND mode = %s
            ORDER BY snapshot_time ASC
            LIMIT 1
            """,
            (trade_date, self._mode),
            fetch_one=True,
        )
        last = self._db.execute_query(
            """
            SELECT total_equity
            FROM account_snapshots
            WHERE DATE(snapshot_time) = %s AND mode = %s
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            (trade_date, self._mode),
            fetch_one=True,
        )

        return self._to_float_or_none((first or {}).get("total_equity")), self._to_float_or_none(
            (last or {}).get("total_equity")
        )

    def _get_unrealized_pnl(self) -> Optional[float]:
        if not self._db.table_exists("positions"):
            return None
        if not self._positions_has_unrealized_columns():
            return None

        rows = self._db.execute_query(
            """
            SELECT symbol, unrealized_pnl
            FROM positions
            WHERE status IN ('OPEN', 'ENTERED') AND mode = %s
            """,
            (self._mode,),
        )
        rows = rows or []
        if not rows:
            return 0.0

        values = [self._to_float_or_none(row.get("unrealized_pnl")) for row in rows]
        values = [value for value in values if value is not None]
        if not values:
            return None
        return float(sum(values))

    def _positions_has_unrealized_columns(self) -> bool:
        db_name = self._get_db_name()
        result = self._db.execute_query(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = 'positions'
              AND column_name IN ('current_price', 'unrealized_pnl')
            """,
            (db_name,),
            fetch_one=True,
        )
        return int((result or {}).get("cnt", 0) or 0) >= 2

    def _load_risk_events(self, trade_date: date) -> List[str]:
        events: List[str] = []
        events.extend(self._load_risk_reason_events(trade_date))
        order_state_event = self._load_order_state_event(trade_date)
        if order_state_event:
            events.append(order_state_event)
        return events

    def _load_risk_reason_events(self, trade_date: date) -> List[str]:
        reasons = tuple(_RISK_REASON_LABELS.keys())
        placeholders = ", ".join(["%s"] * len(reasons))
        rows = self._db.execute_query(
            f"""
            SELECT reason, COUNT(*) AS cnt
            FROM trades
            WHERE DATE(executed_at) = %s
              AND mode = %s
              AND reason IN ({placeholders})
            GROUP BY reason
            ORDER BY cnt DESC
            """,
            (trade_date, self._mode, *reasons),
        )
        events = []
        for row in rows or []:
            reason = str(row.get("reason") or "").upper()
            cnt = int(row.get("cnt") or 0)
            if cnt <= 0:
                continue
            label = _RISK_REASON_LABELS.get(reason, reason)
            events.append(f"{label}: {cnt}건")
        return events

    def _load_order_state_event(self, trade_date: date) -> Optional[str]:
        if not self._db.table_exists("order_state"):
            return None

        row = self._db.execute_query(
            """
            SELECT
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled_count
            FROM order_state
            WHERE DATE(requested_at) = %s AND mode = %s
            """,
            (trade_date, self._mode),
            fetch_one=True,
        )
        failed_count = int((row or {}).get("failed_count", 0) or 0)
        cancelled_count = int((row or {}).get("cancelled_count", 0) or 0)
        total = failed_count + cancelled_count
        if total <= 0:
            return None
        return (
            "주문차단/실패: "
            f"{total}건 (FAILED {failed_count}건, CANCELLED {cancelled_count}건)"
        )

    def _daily_summary_exists(self) -> bool:
        return self._db.table_exists("daily_summary")

    def _get_daily_summary_row(self, trade_date: date) -> Optional[Dict[str, Any]]:
        return self._db.execute_query(
            """
            SELECT trade_date, total_trades, buy_count, sell_count,
                   realized_pnl, win_count, loss_count, win_rate,
                   max_profit, max_loss, start_equity, end_equity
            FROM daily_summary
            WHERE trade_date = %s
            """,
            (trade_date,),
            fetch_one=True,
        )

    def _upsert_daily_summary(
        self,
        trade_date: date,
        metrics: Dict[str, Any],
        start_equity: Optional[float],
        end_equity: Optional[float],
    ) -> None:
        self._db.execute_command(
            """
            INSERT INTO daily_summary (
                trade_date, total_trades, buy_count, sell_count,
                realized_pnl, win_count, loss_count, win_rate,
                max_profit, max_loss, start_equity, end_equity
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_trades = VALUES(total_trades),
                buy_count = VALUES(buy_count),
                sell_count = VALUES(sell_count),
                realized_pnl = VALUES(realized_pnl),
                win_count = VALUES(win_count),
                loss_count = VALUES(loss_count),
                win_rate = VALUES(win_rate),
                max_profit = VALUES(max_profit),
                max_loss = VALUES(max_loss),
                start_equity = VALUES(start_equity),
                end_equity = VALUES(end_equity)
            """,
            (
                trade_date,
                int(metrics.get("total_trades", 0) or 0),
                int(metrics.get("buy_count", 0) or 0),
                int(metrics.get("sell_count", 0) or 0),
                float(metrics.get("realized_pnl", 0.0) or 0.0),
                int(metrics.get("win_count", 0) or 0),
                int(metrics.get("loss_count", 0) or 0),
                float(metrics.get("win_rate", 0.0) or 0.0),
                float(metrics.get("max_profit", 0.0) or 0.0),
                float(metrics.get("max_loss", 0.0) or 0.0),
                self._to_float_or_none(start_equity),
                self._to_float_or_none(end_equity),
            ),
        )

    def _merge_metrics_from_daily_summary(
        self,
        row: Dict[str, Any],
        fallback: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(fallback)
        merged.update(
            {
                "total_trades": int(row.get("total_trades", fallback.get("total_trades", 0)) or 0),
                "buy_count": int(row.get("buy_count", fallback.get("buy_count", 0)) or 0),
                "sell_count": int(row.get("sell_count", fallback.get("sell_count", 0)) or 0),
                "realized_pnl": float(row.get("realized_pnl", fallback.get("realized_pnl", 0.0)) or 0.0),
                "win_count": int(row.get("win_count", fallback.get("win_count", 0)) or 0),
                "loss_count": int(row.get("loss_count", fallback.get("loss_count", 0)) or 0),
                "win_rate": float(row.get("win_rate", fallback.get("win_rate", 0.0)) or 0.0),
                "max_profit": float(row.get("max_profit", fallback.get("max_profit", 0.0)) or 0.0),
                "max_loss": float(row.get("max_loss", fallback.get("max_loss", 0.0)) or 0.0),
            }
        )
        return merged

    @staticmethod
    def _to_float_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fmt_won(value: float, with_sign: bool = True) -> str:
        if with_sign:
            return f"{value:+,.0f}원"
        return f"{value:,.0f}원"

    @classmethod
    def _fmt_optional_won(cls, value: Optional[float], with_sign: bool = True) -> str:
        if value is None:
            return "N/A (근거 부족)"
        return cls._fmt_won(value, with_sign=with_sign)

    @staticmethod
    def _fmt_percent(value: Optional[float]) -> str:
        if value is None:
            return "N/A (근거 부족)"
        return f"{value:+.2f}%"

    def _get_db_name(self) -> str:
        configured = getattr(getattr(self._db, "config", None), "database", None)
        return configured or os.getenv("DB_NAME", "kis_trading")
