from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

try:
    from db.mysql import MySQLManager
except ImportError:
    from kis_trend_atr_trading.db.mysql import MySQLManager


class StrategyAnalyticsSummaryRepository:
    def __init__(self, db: MySQLManager) -> None:
        self._db = db

    def ensure_table(self) -> None:
        self._db.execute_command(
            """
            CREATE TABLE IF NOT EXISTS strategy_daily_summary (
                trade_date DATE NOT NULL,
                strategy_tag VARCHAR(64) NOT NULL,
                candidate_count INT NOT NULL DEFAULT 0,
                timing_confirm_count INT NOT NULL DEFAULT 0,
                authoritative_ingress_count INT NOT NULL DEFAULT 0,
                precheck_reject_count INT NOT NULL DEFAULT 0,
                native_handoff_reject_count INT NOT NULL DEFAULT 0,
                submitted_count INT NOT NULL DEFAULT 0,
                filled_count INT NOT NULL DEFAULT 0,
                cancelled_count INT NOT NULL DEFAULT 0,
                exit_count INT NOT NULL DEFAULT 0,
                avg_markout_3m_bps DOUBLE NULL,
                avg_markout_5m_bps DOUBLE NULL,
                fill_rate DOUBLE NULL,
                top_reject_reason_json LONGTEXT NULL,
                degraded_event_count INT NOT NULL DEFAULT 0,
                recovery_duplicate_prevented_count INT NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (trade_date, strategy_tag)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def replace_for_trade_date(self, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        prepared = list(rows or [])
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM strategy_daily_summary WHERE trade_date = %s", (trade_date,))
            if not prepared:
                return
            cursor.executemany(
                """
                INSERT INTO strategy_daily_summary (
                    trade_date,
                    strategy_tag,
                    candidate_count,
                    timing_confirm_count,
                    authoritative_ingress_count,
                    precheck_reject_count,
                    native_handoff_reject_count,
                    submitted_count,
                    filled_count,
                    cancelled_count,
                    exit_count,
                    avg_markout_3m_bps,
                    avg_markout_5m_bps,
                    fill_rate,
                    top_reject_reason_json,
                    degraded_event_count,
                    recovery_duplicate_prevented_count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        trade_date,
                        str(row.get("strategy_tag") or ""),
                        int(row.get("candidate_count", 0) or 0),
                        int(row.get("timing_confirm_count", 0) or 0),
                        int(row.get("authoritative_ingress_count", 0) or 0),
                        int(row.get("precheck_reject_count", 0) or 0),
                        int(row.get("native_handoff_reject_count", 0) or 0),
                        int(row.get("submitted_count", 0) or 0),
                        int(row.get("filled_count", 0) or 0),
                        int(row.get("cancelled_count", 0) or 0),
                        int(row.get("exit_count", 0) or 0),
                        row.get("avg_markout_3m_bps"),
                        row.get("avg_markout_5m_bps"),
                        row.get("fill_rate"),
                        json.dumps(row.get("top_reject_reason_json") or [], ensure_ascii=True, separators=(",", ":")),
                        int(row.get("degraded_event_count", 0) or 0),
                        int(row.get("recovery_duplicate_prevented_count", 0) or 0),
                    )
                    for row in prepared
                ],
            )

    def list_for_trade_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return list(
            self._db.execute_query(
                "SELECT * FROM strategy_daily_summary WHERE trade_date = %s ORDER BY strategy_tag",
                (trade_date,),
            )
            or []
        )


class StrategyRejectReasonDailyRepository:
    def __init__(self, db: MySQLManager) -> None:
        self._db = db

    def ensure_table(self) -> None:
        self._db.execute_command(
            """
            CREATE TABLE IF NOT EXISTS strategy_reject_reason_daily (
                trade_date DATE NOT NULL,
                strategy_tag VARCHAR(64) NOT NULL,
                reject_stage VARCHAR(64) NOT NULL,
                reject_reason VARCHAR(128) NOT NULL,
                count INT NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (trade_date, strategy_tag, reject_stage, reject_reason)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def replace_for_trade_date(self, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        prepared = list(rows or [])
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM strategy_reject_reason_daily WHERE trade_date = %s", (trade_date,))
            if not prepared:
                return
            cursor.executemany(
                """
                INSERT INTO strategy_reject_reason_daily (
                    trade_date,
                    strategy_tag,
                    reject_stage,
                    reject_reason,
                    count
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        trade_date,
                        str(row.get("strategy_tag") or ""),
                        str(row.get("reject_stage") or ""),
                        str(row.get("reject_reason") or ""),
                        int(row.get("count", 0) or 0),
                    )
                    for row in prepared
                ],
            )

    def list_for_trade_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return list(
            self._db.execute_query(
                """
                SELECT *
                FROM strategy_reject_reason_daily
                WHERE trade_date = %s
                ORDER BY strategy_tag, reject_stage, count DESC, reject_reason
                """,
                (trade_date,),
            )
            or []
        )


class StrategyFunnelDailyRepository:
    def __init__(self, db: MySQLManager) -> None:
        self._db = db

    def ensure_table(self) -> None:
        self._db.execute_command(
            """
            CREATE TABLE IF NOT EXISTS strategy_funnel_daily (
                trade_date DATE NOT NULL,
                strategy_tag VARCHAR(64) NOT NULL,
                slice_key VARCHAR(32) NOT NULL,
                slice_value VARCHAR(64) NOT NULL,
                stage_name VARCHAR(64) NOT NULL,
                stage_order INT NOT NULL,
                stage_count INT NOT NULL DEFAULT 0,
                prev_stage_count INT NOT NULL DEFAULT 0,
                conversion_rate DOUBLE NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (trade_date, strategy_tag, slice_key, slice_value, stage_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def replace_for_trade_date(self, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        prepared = list(rows or [])
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM strategy_funnel_daily WHERE trade_date = %s", (trade_date,))
            if not prepared:
                return
            cursor.executemany(
                """
                INSERT INTO strategy_funnel_daily (
                    trade_date,
                    strategy_tag,
                    slice_key,
                    slice_value,
                    stage_name,
                    stage_order,
                    stage_count,
                    prev_stage_count,
                    conversion_rate
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        trade_date,
                        str(row.get("strategy_tag") or ""),
                        str(row.get("slice_key") or ""),
                        str(row.get("slice_value") or ""),
                        str(row.get("stage_name") or ""),
                        int(row.get("stage_order", 0) or 0),
                        int(row.get("stage_count", 0) or 0),
                        int(row.get("prev_stage_count", 0) or 0),
                        row.get("conversion_rate"),
                    )
                    for row in prepared
                ],
            )

    def list_for_trade_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return list(
            self._db.execute_query(
                """
                SELECT *
                FROM strategy_funnel_daily
                WHERE trade_date = %s
                ORDER BY strategy_tag, slice_key, slice_value, stage_order
                """,
                (trade_date,),
            )
            or []
        )


class StrategyAttributionDailyRepository:
    def __init__(self, db: MySQLManager) -> None:
        self._db = db

    def ensure_table(self) -> None:
        self._db.execute_command(
            """
            CREATE TABLE IF NOT EXISTS strategy_attribution_daily (
                trade_date DATE NOT NULL,
                strategy_tag VARCHAR(64) NOT NULL,
                slice_key VARCHAR(32) NOT NULL,
                slice_value VARCHAR(64) NOT NULL,
                reject_stage VARCHAR(64) NOT NULL,
                reject_reason VARCHAR(128) NOT NULL,
                reason_group VARCHAR(64) NOT NULL,
                outcome_class VARCHAR(16) NOT NULL,
                count INT NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    trade_date,
                    strategy_tag,
                    slice_key,
                    slice_value,
                    reject_stage,
                    reject_reason,
                    reason_group,
                    outcome_class
                )
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def replace_for_trade_date(self, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        prepared = list(rows or [])
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM strategy_attribution_daily WHERE trade_date = %s", (trade_date,))
            if not prepared:
                return
            cursor.executemany(
                """
                INSERT INTO strategy_attribution_daily (
                    trade_date,
                    strategy_tag,
                    slice_key,
                    slice_value,
                    reject_stage,
                    reject_reason,
                    reason_group,
                    outcome_class,
                    count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        trade_date,
                        str(row.get("strategy_tag") or ""),
                        str(row.get("slice_key") or ""),
                        str(row.get("slice_value") or ""),
                        str(row.get("reject_stage") or ""),
                        str(row.get("reject_reason") or ""),
                        str(row.get("reason_group") or ""),
                        str(row.get("outcome_class") or ""),
                        int(row.get("count", 0) or 0),
                    )
                    for row in prepared
                ],
            )

    def list_for_trade_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return list(
            self._db.execute_query(
                """
                SELECT *
                FROM strategy_attribution_daily
                WHERE trade_date = %s
                ORDER BY strategy_tag, slice_key, slice_value, count DESC, reason_group, reject_reason
                """,
                (trade_date,),
            )
            or []
        )


class TradeMarkoutRepository:
    def __init__(self, db: MySQLManager) -> None:
        self._db = db

    def ensure_table(self) -> None:
        self._db.execute_command(
            """
            CREATE TABLE IF NOT EXISTS trade_markouts (
                trade_date DATE NOT NULL,
                strategy_tag VARCHAR(64) NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                entry_ts DATETIME NOT NULL,
                horizon_sec INT NOT NULL,
                intent_id VARCHAR(64) NULL,
                broker_order_id VARCHAR(64) NULL,
                ref_price DOUBLE NOT NULL,
                mark_price DOUBLE NULL,
                markout_bps DOUBLE NULL,
                source_type VARCHAR(16) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (trade_date, strategy_tag, symbol, entry_ts, horizon_sec)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def replace_for_trade_date(self, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        prepared = list(rows or [])
        with self._db.transaction() as cursor:
            cursor.execute("DELETE FROM trade_markouts WHERE trade_date = %s", (trade_date,))
            if not prepared:
                return
            cursor.executemany(
                """
                INSERT INTO trade_markouts (
                    trade_date,
                    strategy_tag,
                    symbol,
                    entry_ts,
                    horizon_sec,
                    intent_id,
                    broker_order_id,
                    ref_price,
                    mark_price,
                    markout_bps,
                    source_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        trade_date,
                        str(row.get("strategy_tag") or ""),
                        str(row.get("symbol") or ""),
                        row.get("entry_ts"),
                        int(row.get("horizon_sec", 0) or 0),
                        str(row.get("intent_id") or ""),
                        str(row.get("broker_order_id") or ""),
                        float(row.get("ref_price", 0.0) or 0.0),
                        row.get("mark_price"),
                        row.get("markout_bps"),
                        str(row.get("source_type") or "na"),
                    )
                    for row in prepared
                ],
            )

    def list_for_trade_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return list(
            self._db.execute_query(
                """
                SELECT *
                FROM trade_markouts
                WHERE trade_date = %s
                ORDER BY strategy_tag, symbol, entry_ts, horizon_sec
                """,
                (trade_date,),
            )
            or []
        )
