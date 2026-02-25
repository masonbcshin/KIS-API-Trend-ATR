"""
Daily report service backed by MySQL.

This module is intentionally isolated from trading execution logic so that
it can run as an external scheduler task (cron/systemd timer).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db.mysql import MySQLManager, get_db_manager
from env import get_db_namespace_mode
from utils.logger import get_logger
from utils.market_hours import KST
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
        self._mode = mode or get_db_namespace_mode()

    @classmethod
    def _normalize_trade_row_for_report(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        side = str(row.get("side") or "").upper().strip()
        reason = str(row.get("reason") or "").upper().strip()
        quantity = int(row.get("quantity") or 0)
        price = cls._to_float_or_none(row.get("price")) or 0.0

        return {
            "symbol": str(row.get("symbol") or "").strip(),
            "side": side,
            "quantity": quantity,
            "price": float(price),
            "entry_price": cls._to_float_or_none(row.get("entry_price")),
            "pnl": cls._to_float_or_none(row.get("pnl")),
            "reason": reason,
            "order_no": cls._normalize_order_no(row.get("order_no")),
            "executed_at": row.get("executed_at"),
        }

    @staticmethod
    def _trade_row_priority(row: Dict[str, Any]) -> Tuple[int, int, int, int]:
        reason = str(row.get("reason") or "").upper().strip()
        return (
            0 if reason == "BROKER_RECONCILE" else 1,
            1 if row.get("pnl") is not None else 0,
            1 if row.get("entry_price") is not None else 0,
            1 if reason else 0,
        )

    @classmethod
    def _build_trade_execution_key(cls, row: Dict[str, Any]) -> Tuple[Any, ...]:
        side = str(row.get("side") or "").upper().strip()
        order_no = cls._normalize_order_no(row.get("order_no"))
        if order_no:
            return ("ORDER", side, order_no)

        executed_at = row.get("executed_at")
        if isinstance(executed_at, datetime):
            executed_at_key = executed_at.isoformat()
        else:
            executed_at_key = str(executed_at or "")

        return (
            "FALLBACK",
            side,
            str(row.get("symbol") or "").strip(),
            executed_at_key,
            int(row.get("quantity") or 0),
            round(float(row.get("price") or 0.0), 2),
        )

    @classmethod
    def _dedup_trades_for_report(cls, trades: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for raw in trades:
            normalized = cls._normalize_trade_row_for_report(raw)
            if normalized["reason"] == "SIGNAL_ONLY":
                continue

            key = cls._build_trade_execution_key(normalized)
            current = deduped.get(key)
            if current is None:
                deduped[key] = normalized
                continue

            if cls._trade_row_priority(normalized) > cls._trade_row_priority(current):
                deduped[key] = normalized

        return list(deduped.values())

    @classmethod
    def calculate_trade_metrics(cls, trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate realized PnL and trade stats from raw trades."""
        normalized = cls._dedup_trades_for_report(trades)

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
        if persist_daily_summary and self._daily_summary_mode_isolated():
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

        unrealized_pnl: Optional[float] = None
        try:
            unrealized_pnl = self._get_unrealized_pnl()
        except Exception as err:
            logger.warning(f"[REPORT] 미실현손익 조회 실패 - N/A 처리: {err}")

        risk_events: List[str] = []
        try:
            risk_events = self._load_risk_events(
                trade_date=trade_date,
                total_trades=int(computed.get("total_trades", 0) or 0),
            )
        except Exception as err:
            logger.warning(f"[REPORT] 리스크 이벤트 조회 실패 - 빈 목록 처리: {err}")

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
            unrealized_pnl=unrealized_pnl,
            top_symbols=self._build_symbol_summaries(trades),
            risk_events=risk_events,
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
        lines.append("- 집계기준: 거래/손익=trades(체결), 주문차단/실패=order_state(주문시도)")
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

    @staticmethod
    def _normalize_order_no(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return raw
        try:
            return str(int(digits))
        except Exception:
            return digits.lstrip("0") or "0"

    @classmethod
    def _build_broker_order_key(cls, row: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
        if not isinstance(row, dict):
            return None
        symbol = str(row.get("stock_code") or "").strip()
        side = str(row.get("side") or "").upper().strip()
        if not symbol or side not in ("BUY", "SELL"):
            return None

        order_no = cls._normalize_order_no(row.get("order_no"))
        exec_id = str(row.get("exec_id") or "").strip()
        if exec_id:
            return (symbol, side, order_no, exec_id)

        executed_at = str(row.get("executed_at") or "").strip()
        qty = int(row.get("exec_qty") or 0)
        price = float(row.get("exec_price") or 0.0)
        return (symbol, side, order_no, executed_at, qty, round(price, 2))

    @staticmethod
    def _parse_broker_executed_at(raw: Any, fallback_date: date) -> datetime:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else KST.localize(raw)

        if raw not in (None, ""):
            try:
                parsed = datetime.fromisoformat(str(raw))
                if parsed.tzinfo is None:
                    return KST.localize(parsed)
                return parsed.astimezone(KST)
            except Exception:
                pass

        return KST.localize(datetime.combine(fallback_date, datetime.min.time()))

    @classmethod
    def _build_order_no_match_clause(cls, order_no: Optional[str]) -> Tuple[str, List[Any]]:
        raw_order_no = str(order_no or "").strip()
        normalized_order_no = cls._normalize_order_no(raw_order_no)
        order_values: List[str] = []
        for value in (raw_order_no, normalized_order_no):
            if value and value not in order_values:
                order_values.append(value)

        if not order_values:
            return "1 = 0", []

        placeholders = ", ".join(["%s"] * len(order_values))
        clauses = [f"order_no IN ({placeholders})"]
        params: List[Any] = list(order_values)
        if normalized_order_no.isdigit():
            clauses.append("CAST(order_no AS UNSIGNED) = %s")
            params.append(int(normalized_order_no))
        return "(" + " OR ".join(clauses) + ")", params

    def _find_sell_backfill_source(
        self,
        *,
        symbol: str,
        order_no: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        order_match_sql, order_params = self._build_order_no_match_clause(order_no)
        if not order_params:
            return None

        try:
            return self._db.execute_query(
                f"""
                SELECT entry_price, pnl, pnl_percent, reason
                FROM trades
                WHERE mode = %s
                  AND symbol = %s
                  AND side = 'SELL'
                  AND order_no IS NOT NULL
                  AND {order_match_sql}
                  AND (entry_price IS NOT NULL OR pnl IS NOT NULL OR pnl_percent IS NOT NULL)
                ORDER BY
                    CASE WHEN UPPER(COALESCE(reason, '')) = 'BROKER_RECONCILE' THEN 1 ELSE 0 END ASC,
                    executed_at DESC,
                    id DESC
                LIMIT 1
                """,
                (self._mode, symbol, *order_params),
                fetch_one=True,
            )
        except Exception as err:
            logger.warning(
                "[REPORT_RECONCILE] sell 백필 소스 조회 실패: symbol=%s order_no=%s err=%s",
                symbol,
                order_no,
                err,
            )
            return None

    def _find_latest_buy_price(
        self,
        *,
        symbol: str,
        executed_at: datetime,
    ) -> Optional[float]:
        try:
            row = self._db.execute_query(
                """
                SELECT price
                FROM trades
                WHERE mode = %s
                  AND symbol = %s
                  AND side = 'BUY'
                  AND COALESCE(reason, '') != 'SIGNAL_ONLY'
                  AND executed_at <= %s
                ORDER BY executed_at DESC, id DESC
                LIMIT 1
                """,
                (self._mode, symbol, executed_at),
                fetch_one=True,
            )
        except Exception as err:
            logger.warning(
                "[REPORT_RECONCILE] 최근 BUY 조회 실패: symbol=%s executed_at=%s err=%s",
                symbol,
                executed_at,
                err,
            )
            return None
        return self._to_float_or_none((row or {}).get("price"))

    def _build_reconcile_sell_backfill(
        self,
        *,
        symbol: str,
        order_no: Optional[str],
        executed_at: datetime,
        sell_price: float,
        quantity: int,
    ) -> Dict[str, Optional[float]]:
        entry_price: Optional[float] = None
        pnl: Optional[float] = None
        pnl_percent: Optional[float] = None

        source = self._find_sell_backfill_source(symbol=symbol, order_no=order_no)
        if source:
            entry_price = self._to_float_or_none(source.get("entry_price"))
            pnl = self._to_float_or_none(source.get("pnl"))
            pnl_percent = self._to_float_or_none(source.get("pnl_percent"))

        if entry_price is None:
            entry_price = self._find_latest_buy_price(symbol=symbol, executed_at=executed_at)

        if pnl is None and entry_price is not None:
            pnl = (float(sell_price) - float(entry_price)) * int(quantity)
        if pnl_percent is None and entry_price not in (None, 0):
            pnl_percent = ((float(sell_price) / float(entry_price)) - 1.0) * 100.0

        return {
            "entry_price": entry_price,
            "pnl": pnl,
            "pnl_percent": pnl_percent,
        }

    def reconcile_trades_from_broker(
        self,
        trade_date: date,
        *,
        attempts: int = 2,
        interval_seconds: float = 1.0,
        api_client: Optional[Any] = None,
        trade_repo: Optional[Any] = None,
    ) -> Dict[str, int]:
        """
        주식일별주문체결조회 결과를 trades 테이블에 보강 반영합니다.

        목적:
            - 메인 프로세스 타임아웃/지연으로 누락된 체결을 cron 리포트 직전에 보강
            - idempotency_key 기반 중복 삽입 방지
        """
        stats = {
            "attempted_calls": 0,
            "fetched_orders": 0,
            "unique_orders": 0,
            "filled_orders": 0,
            "inserted_trades": 0,
            "duplicate_trades": 0,
            "skipped_orders": 0,
            "errors": 0,
            "skipped_mode": 0,
        }

        mode = str(self._mode or "").upper().strip()
        if mode not in ("PAPER", "REAL"):
            stats["skipped_mode"] = 1
            logger.info(
                "[REPORT_RECONCILE] skip mode=%s date=%s (broker reconcile disabled)",
                mode,
                trade_date,
            )
            return stats

        attempts = max(int(attempts or 1), 1)
        interval_seconds = max(float(interval_seconds or 0.0), 0.0)
        stats["attempted_calls"] = attempts

        if api_client is None:
            from api.kis_api import KISApi

            api_client = KISApi(is_paper_trading=(mode != "REAL"))

        if trade_repo is None:
            from db.repository import TradeRepository

            trade_repo = TradeRepository(db=self._db)
        if hasattr(trade_repo, "mode"):
            trade_repo.mode = mode

        deduped_orders: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for idx in range(attempts):
            try:
                payload = api_client.get_order_status(
                    order_no=None,
                    trade_date=trade_date,
                    end_date=trade_date,
                )
                orders = payload.get("orders") or []
                stats["fetched_orders"] += len(orders)
                for row in orders:
                    key = self._build_broker_order_key(row)
                    if key is None:
                        stats["skipped_orders"] += 1
                        continue
                    deduped_orders[key] = row
            except Exception as err:
                stats["errors"] += 1
                logger.warning(
                    "[REPORT_RECONCILE] broker fetch failed attempt=%s/%s date=%s err=%s",
                    idx + 1,
                    attempts,
                    trade_date,
                    err,
                )
            if idx + 1 < attempts and interval_seconds > 0:
                time.sleep(interval_seconds)

        stats["unique_orders"] = len(deduped_orders)

        for row in deduped_orders.values():
            symbol = str(row.get("stock_code") or "").strip()
            side = str(row.get("side") or "").upper().strip()
            qty = int(row.get("exec_qty") or 0)
            price = float(row.get("exec_price") or 0.0)
            order_no = str(row.get("order_no") or "").strip() or None
            exec_id = str(row.get("exec_id") or "").strip() or None

            if not symbol or side not in ("BUY", "SELL") or qty <= 0 or price <= 0:
                stats["skipped_orders"] += 1
                continue

            stats["filled_orders"] += 1
            executed_at = self._parse_broker_executed_at(row.get("executed_at"), trade_date)
            reason = "BROKER_RECONCILE" if side == "SELL" else None
            sell_backfill = {"entry_price": None, "pnl": None, "pnl_percent": None}
            if side == "SELL":
                sell_backfill = self._build_reconcile_sell_backfill(
                    symbol=symbol,
                    order_no=order_no,
                    executed_at=executed_at,
                    sell_price=price,
                    quantity=qty,
                )

            try:
                _, created = trade_repo.save_execution_fill(
                    symbol=symbol,
                    side=side,
                    price=price,
                    quantity=qty,
                    executed_at=executed_at,
                    order_no=order_no,
                    exec_id=exec_id,
                    reason=reason,
                    entry_price=sell_backfill["entry_price"],
                    pnl=sell_backfill["pnl"],
                    pnl_percent=sell_backfill["pnl_percent"],
                    dedup_on_order_no=True,
                    upsert_missing_fields=True,
                )
                if created:
                    stats["inserted_trades"] += 1
                else:
                    stats["duplicate_trades"] += 1
            except Exception as err:
                stats["errors"] += 1
                logger.warning(
                    "[REPORT_RECONCILE] trade save failed symbol=%s side=%s order_no=%s err=%s",
                    symbol,
                    side,
                    order_no,
                    err,
                )

        logger.info(
            "[REPORT_RECONCILE] done date=%s mode=%s fetched=%s unique=%s filled=%s inserted=%s duplicate=%s skipped=%s errors=%s",
            trade_date,
            mode,
            stats["fetched_orders"],
            stats["unique_orders"],
            stats["filled_orders"],
            stats["inserted_trades"],
            stats["duplicate_trades"],
            stats["skipped_orders"],
            stats["errors"],
        )
        return stats

    def _load_trades(self, trade_date: date) -> List[Dict[str, Any]]:
        rows = self._db.execute_query(
            """
            SELECT symbol, side, quantity, price, entry_price, pnl, reason, order_no, executed_at
            FROM trades
            WHERE DATE(executed_at) = %s AND mode = %s
            ORDER BY executed_at
            """,
            (trade_date, self._mode),
        )
        return rows or []

    def _build_symbol_summaries(self, trades: Iterable[Dict[str, Any]]) -> List[ReportSymbolSummary]:
        by_symbol: Dict[str, Dict[str, float]] = {}
        for row in self._dedup_trades_for_report(trades):
            if row["side"] != "SELL":
                continue
            pnl = self._to_float_or_none(row.get("pnl"))
            if pnl is None:
                continue

            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue

            bucket = by_symbol.setdefault(symbol, {"pnl": 0.0, "sell_count": 0.0})
            bucket["pnl"] += float(pnl)
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
            return self._get_unrealized_from_snapshots()
        if not self._positions_has_unrealized_columns():
            return self._get_unrealized_from_snapshots()

        position_columns = self._get_table_columns("positions")
        where_clauses: List[str] = []
        params: List[Any] = []

        # 구버전 테이블 호환: 컬럼이 있는 경우에만 조건을 추가합니다.
        if "status" in position_columns:
            where_clauses.append("status IN ('OPEN', 'ENTERED')")
        if "mode" in position_columns:
            where_clauses.append("mode = %s")
            params.append(self._mode)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        rows = self._db.execute_query(
            f"""
            SELECT unrealized_pnl
            FROM positions
            {where_sql}
            """,
            tuple(params) if params else None,
        )
        rows = rows or []
        if not rows:
            snapshot_value = self._get_unrealized_from_snapshots()
            return snapshot_value if snapshot_value is not None else 0.0

        values = [self._to_float_or_none(row.get("unrealized_pnl")) for row in rows]
        values = [value for value in values if value is not None]
        if not values:
            return self._get_unrealized_from_snapshots()
        return float(sum(values))

    def _get_unrealized_from_snapshots(self) -> Optional[float]:
        if not self._db.table_exists("account_snapshots"):
            return None
        snapshot_columns = self._get_table_columns("account_snapshots")
        if "mode" in snapshot_columns:
            row = self._db.execute_query(
                """
                SELECT unrealized_pnl
                FROM account_snapshots
                WHERE mode = %s
                ORDER BY snapshot_time DESC
                LIMIT 1
                """,
                (self._mode,),
                fetch_one=True,
            )
        else:
            row = self._db.execute_query(
                """
                SELECT unrealized_pnl
                FROM account_snapshots
                ORDER BY snapshot_time DESC
                LIMIT 1
                """,
                fetch_one=True,
            )
        return self._to_float_or_none((row or {}).get("unrealized_pnl"))

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

    def _get_table_columns(self, table_name: str) -> set:
        db_name = self._get_db_name()
        rows = self._db.execute_query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (db_name, table_name),
        )
        return {
            str(row.get("column_name") or row.get("COLUMN_NAME") or "").lower()
            for row in (rows or [])
        }

    def _load_risk_events(self, trade_date: date, total_trades: int) -> List[str]:
        events: List[str] = []
        events.extend(self._load_risk_reason_events(trade_date))
        order_state_stats = self._load_order_state_stats(trade_date)
        order_state_event = self._build_order_state_event(order_state_stats)
        if order_state_event:
            events.append(order_state_event)
        events.extend(
            self._build_order_state_anomaly_events(
                order_state_stats=order_state_stats,
                total_trades=total_trades,
            )
        )
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

    def _load_order_state_stats(self, trade_date: date) -> Dict[str, Any]:
        if not self._db.table_exists("order_state"):
            return {
                "failed_count": 0,
                "cancelled_count": 0,
                "total_count": 0,
                "unique_signal_count": 0,
            }

        try:
            row = self._db.execute_query(
                """
                SELECT
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled_count,
                    COUNT(
                        DISTINCT CASE
                            WHEN status IN ('FAILED', 'CANCELLED')
                            THEN COALESCE(NULLIF(signal_id, ''), idempotency_key)
                            ELSE NULL
                        END
                    ) AS unique_signal_count
                FROM order_state
                WHERE DATE(requested_at) = %s AND mode = %s
                """,
                (trade_date, self._mode),
                fetch_one=True,
            )
        except Exception:
            # 구스키마(order_state.signal_id 없음) 호환
            row = self._db.execute_query(
                """
                SELECT
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled_count,
                    COUNT(
                        DISTINCT CASE
                            WHEN status IN ('FAILED', 'CANCELLED')
                            THEN idempotency_key
                            ELSE NULL
                        END
                    ) AS unique_signal_count
                FROM order_state
                WHERE DATE(requested_at) = %s AND mode = %s
                """,
                (trade_date, self._mode),
                fetch_one=True,
            )

        failed_count = int((row or {}).get("failed_count", 0) or 0)
        cancelled_count = int((row or {}).get("cancelled_count", 0) or 0)
        unique_signal_count = int((row or {}).get("unique_signal_count", 0) or 0)
        total = failed_count + cancelled_count
        return {
            "failed_count": failed_count,
            "cancelled_count": cancelled_count,
            "total_count": total,
            "unique_signal_count": unique_signal_count,
        }

    @staticmethod
    def _build_order_state_event(order_state_stats: Dict[str, Any]) -> Optional[str]:
        failed_count = int(order_state_stats.get("failed_count", 0) or 0)
        cancelled_count = int(order_state_stats.get("cancelled_count", 0) or 0)
        total = failed_count + cancelled_count
        if total <= 0:
            return None
        return (
            "주문차단/실패(order_state, 주문 시도 기준): "
            f"{total}건 (FAILED {failed_count}건, CANCELLED {cancelled_count}건)"
        )

    @staticmethod
    def _build_order_state_anomaly_events(
        order_state_stats: Dict[str, Any],
        total_trades: int,
    ) -> List[str]:
        events: List[str] = []
        total = int(order_state_stats.get("total_count", 0) or 0)
        unique_signal_count = int(order_state_stats.get("unique_signal_count", 0) or 0)

        if total <= 0:
            return events

        if total_trades <= 0:
            events.append(
                f"체결 0건인데 주문 실패/취소 {total}건 발생 "
                "(체결지연/주문번호 불일치/반복시도 점검 권장)"
            )

        if unique_signal_count > 0 and total >= 20:
            retry_ratio = total / unique_signal_count
            if retry_ratio >= 3.0:
                events.append(
                    "주문 재시도 집중: "
                    f"{total}건 / 고유 신호 {unique_signal_count}건 "
                    f"(신호당 {retry_ratio:.1f}건)"
                )

        return events

    def _daily_summary_exists(self) -> bool:
        return self._db.table_exists("daily_summary")

    def _daily_summary_mode_isolated(self) -> bool:
        if not self._daily_summary_exists():
            return False

        columns = self._get_table_columns("daily_summary")
        if "mode" not in columns:
            return False

        db_name = self._get_db_name()
        pk_rows = self._db.execute_query(
            """
            SELECT column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s
              AND table_name = 'daily_summary'
              AND constraint_name = 'PRIMARY'
            ORDER BY ordinal_position
            """,
            (db_name,),
        )
        pk_columns = [
            str(row.get("column_name") or row.get("COLUMN_NAME") or "").lower()
            for row in (pk_rows or [])
        ]
        return pk_columns == ["trade_date", "mode"]

    def _get_daily_summary_row(self, trade_date: date) -> Optional[Dict[str, Any]]:
        return self._db.execute_query(
            """
            SELECT trade_date, mode, total_trades, buy_count, sell_count,
                   realized_pnl, win_count, loss_count, win_rate,
                   max_profit, max_loss, start_equity, end_equity
            FROM daily_summary
            WHERE trade_date = %s AND mode = %s
            """,
            (trade_date, self._mode),
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
                trade_date, mode, total_trades, buy_count, sell_count,
                realized_pnl, win_count, loss_count, win_rate,
                max_profit, max_loss, start_equity, end_equity
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                mode = VALUES(mode),
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
                self._mode,
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
