from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    from analytics.alerts import build_alert_rows as build_strategy_alert_rows
    from analytics.diagnostics import build_diagnostics_report
    from analytics.attribution import build_attribution_rows
    from analytics.event_logger import inspect_strategy_event_input, load_strategy_events, resolve_strategy_event_dir
    from analytics.parity import build_parity_rows as build_strategy_parity_rows
    from analytics.repository import (
        StrategyAlertsDailyRepository,
        StrategyAttributionDailyRepository,
        StrategyAnalyticsSummaryRepository,
        StrategyFunnelDailyRepository,
        StrategyParityDailyRepository,
        StrategyRejectReasonDailyRepository,
        TradeMarkoutRepository,
    )
    from analytics.summary_drilldown import build_funnel_rows
    from config import settings
    from db.mysql import get_db_manager
    from utils.logger import get_logger
except ImportError:
    from kis_trend_atr_trading.analytics.alerts import build_alert_rows as build_strategy_alert_rows
    from kis_trend_atr_trading.analytics.diagnostics import build_diagnostics_report
    from kis_trend_atr_trading.analytics.attribution import build_attribution_rows
    from kis_trend_atr_trading.analytics.event_logger import (
        inspect_strategy_event_input,
        load_strategy_events,
        resolve_strategy_event_dir,
    )
    from kis_trend_atr_trading.analytics.parity import build_parity_rows as build_strategy_parity_rows
    from kis_trend_atr_trading.analytics.repository import (
        StrategyAlertsDailyRepository,
        StrategyAttributionDailyRepository,
        StrategyAnalyticsSummaryRepository,
        StrategyFunnelDailyRepository,
        StrategyParityDailyRepository,
        StrategyRejectReasonDailyRepository,
        TradeMarkoutRepository,
    )
    from kis_trend_atr_trading.analytics.summary_drilldown import build_funnel_rows
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.db.mysql import get_db_manager
    from kis_trend_atr_trading.utils.logger import get_logger


logger = get_logger("strategy_analytics")


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


@dataclass(frozen=True)
class PriceObservation:
    ts: datetime
    symbol: str
    price: float
    source_type: str


class StrategyAnalyticsMaterializer:
    def __init__(
        self,
        *,
        event_dir: Optional[str] = None,
        db_manager: Any = None,
        markout_horizons_sec: Optional[Sequence[int]] = None,
        enable_markouts: Optional[bool] = None,
    ) -> None:
        self._event_dir = event_dir
        self._db = db_manager if db_manager is not None else get_db_manager()
        self._last_event_input_diagnostics: Dict[str, Any] = {}
        self._enable_markouts = (
            bool(enable_markouts)
            if enable_markouts is not None
            else bool(getattr(settings, "ENABLE_STRATEGY_MARKOUTS", False))
        )
        if markout_horizons_sec is None:
            raw = str(getattr(settings, "STRATEGY_MARKOUT_HORIZONS_SEC", "60,180,300,600") or "60,180,300,600")
            markout_horizons_sec = [int(token.strip()) for token in raw.split(",") if token.strip()]
        self._markout_horizons_sec = tuple(sorted({max(int(value), 1) for value in markout_horizons_sec}))
        self._summary_repo = StrategyAnalyticsSummaryRepository(self._db)
        self._reject_repo = StrategyRejectReasonDailyRepository(self._db)
        self._funnel_repo = StrategyFunnelDailyRepository(self._db)
        self._attribution_repo = StrategyAttributionDailyRepository(self._db)
        self._alerts_repo = StrategyAlertsDailyRepository(self._db)
        self._parity_repo = StrategyParityDailyRepository(self._db)
        self._markout_repo = TradeMarkoutRepository(self._db)

    def _db_enabled_for_analytics(self) -> bool:
        return bool(getattr(settings, "DB_ENABLED", False)) and bool(getattr(self._db.config, "enabled", False))

    def _resolve_event_input_diagnostics(self, trade_date: str) -> Dict[str, Any]:
        diagnostics = inspect_strategy_event_input(event_dir=self._event_dir, trade_date=trade_date)
        configured_live_dir = resolve_strategy_event_dir(None)
        diagnostics["configured_live_event_dir"] = str(configured_live_dir)
        diagnostics["live_writer_expected"] = bool(
            getattr(settings, "ENABLE_STRATEGY_ANALYTICS", False)
        ) and str(diagnostics.get("resolved_event_dir") or "") == str(configured_live_dir)
        missing_state = str(diagnostics.get("missing_input_state") or "ok")
        likely_cause = ""
        if missing_state == "event_dir_missing":
            likely_cause = (
                "writer_never_initialized_or_live_emit_unwired"
                if bool(diagnostics.get("live_writer_expected"))
                else "event_dir_missing"
            )
        elif missing_state == "trade_date_file_missing":
            likely_cause = (
                "live_writer_attached_but_no_events_emitted_or_wiring_missing"
                if bool(diagnostics.get("live_writer_expected"))
                else "trade_date_file_missing"
            )
        elif missing_state == "trade_date_file_empty":
            likely_cause = "event_file_created_but_no_events_appended"
        elif missing_state == "event_dir_empty":
            likely_cause = "event_dir_exists_but_has_no_strategy_event_files"
        diagnostics["likely_cause"] = likely_cause
        return diagnostics

    def _log_event_input_diagnostics(self, diagnostics: Dict[str, Any]) -> None:
        missing_state = str(diagnostics.get("missing_input_state") or "ok")
        if missing_state == "ok":
            return
        logger.warning(
            "[STRATEGY_ANALYTICS][INPUT_DIAGNOSTICS] state=%s trade_date=%s event_dir=%s event_file=%s "
            "live_writer_expected=%s likely_cause=%s",
            missing_state,
            diagnostics.get("trade_date"),
            diagnostics.get("resolved_event_dir"),
            diagnostics.get("event_file"),
            bool(diagnostics.get("live_writer_expected")),
            diagnostics.get("likely_cause") or "",
        )

    def _load_events(self, trade_date: str) -> List[Dict[str, Any]]:
        diagnostics = self._resolve_event_input_diagnostics(trade_date)
        self._last_event_input_diagnostics = diagnostics
        self._log_event_input_diagnostics(diagnostics)
        return load_strategy_events(event_dir=self._event_dir, trade_date=trade_date)

    def _extract_price_observation(self, event: Dict[str, Any]) -> Optional[PriceObservation]:
        payload = dict(event.get("payload_json") or {})
        symbol = str(event.get("symbol") or "").zfill(6)
        ts = _parse_ts(event.get("event_ts"))
        if not symbol or ts is None:
            return None
        price_fields = (
            ("current_price", "quote"),
            ("mark_price", "quote"),
            ("fill_price", "quote"),
            ("exec_price", "quote"),
            ("price", "quote"),
            ("close_price", "close"),
        )
        for field_name, source_type in price_fields:
            price = _safe_float(payload.get(field_name))
            if price is not None and price > 0.0:
                return PriceObservation(ts=ts, symbol=symbol, price=price, source_type=source_type)
        return None

    def _build_price_index(self, events: Iterable[Dict[str, Any]]) -> Dict[str, List[PriceObservation]]:
        index: Dict[str, List[PriceObservation]] = defaultdict(list)
        for event in list(events or []):
            observation = self._extract_price_observation(event)
            if observation is None:
                continue
            index[observation.symbol].append(observation)
        for symbol in list(index.keys()):
            index[symbol].sort(key=lambda item: item.ts)
        return index

    def _next_observation(
        self,
        *,
        observations: Sequence[PriceObservation],
        target_ts: datetime,
    ) -> Optional[PriceObservation]:
        for observation in observations:
            if observation.ts >= target_ts:
                return observation
        return None

    def build_markout_rows(self, trade_date: str, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self._enable_markouts:
            return []
        price_index = self._build_price_index(events)
        rows: List[Dict[str, Any]] = []
        for event in list(events or []):
            if str(event.get("event_type") or "") != "order_filled":
                continue
            payload = dict(event.get("payload_json") or {})
            if str(payload.get("side") or "BUY").upper() != "BUY":
                continue
            entry_ts = _parse_ts(event.get("event_ts"))
            symbol = str(event.get("symbol") or "").zfill(6)
            ref_price = _safe_float(payload.get("fill_price") or payload.get("exec_price") or payload.get("price"))
            if entry_ts is None or not symbol or ref_price is None or ref_price <= 0.0:
                continue
            observations = price_index.get(symbol, [])
            for horizon_sec in self._markout_horizons_sec:
                observation = self._next_observation(
                    observations=observations,
                    target_ts=entry_ts + timedelta(seconds=int(horizon_sec)),
                )
                if observation is None:
                    rows.append(
                        {
                            "trade_date": trade_date,
                            "strategy_tag": str(event.get("strategy_tag") or ""),
                            "symbol": symbol,
                            "entry_ts": entry_ts,
                            "horizon_sec": int(horizon_sec),
                            "intent_id": str(event.get("intent_id") or ""),
                            "broker_order_id": str(event.get("broker_order_id") or ""),
                            "ref_price": float(ref_price),
                            "mark_price": None,
                            "markout_bps": None,
                            "source_type": "na",
                        }
                    )
                    continue
                rows.append(
                    {
                        "trade_date": trade_date,
                        "strategy_tag": str(event.get("strategy_tag") or ""),
                        "symbol": symbol,
                        "entry_ts": entry_ts,
                        "horizon_sec": int(horizon_sec),
                        "intent_id": str(event.get("intent_id") or ""),
                        "broker_order_id": str(event.get("broker_order_id") or ""),
                        "ref_price": float(ref_price),
                        "mark_price": float(observation.price),
                        "markout_bps": ((float(observation.price) / float(ref_price)) - 1.0) * 10000.0,
                        "source_type": str(observation.source_type or "quote"),
                    }
                )
        return rows

    def build_reject_reason_rows(self, trade_date: str, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        counts: Counter[tuple[str, str, str]] = Counter()
        for event in list(events or []):
            reason = str(event.get("reject_reason") or "").strip()
            if not reason:
                continue
            strategy_tag = str(event.get("strategy_tag") or "")
            stage = str(event.get("stage") or "")
            counts[(strategy_tag, stage, reason)] += 1
        rows: List[Dict[str, Any]] = []
        for (strategy_tag, reject_stage, reject_reason), count in sorted(counts.items()):
            rows.append(
                {
                    "trade_date": trade_date,
                    "strategy_tag": strategy_tag,
                    "reject_stage": reject_stage,
                    "reject_reason": reject_reason,
                    "count": int(count),
                }
            )
        return rows

    def build_daily_summary_rows(
        self,
        trade_date: str,
        events: Iterable[Dict[str, Any]],
        reject_rows: Iterable[Dict[str, Any]],
        markout_rows: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        summary: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "trade_date": trade_date,
                "strategy_tag": "",
                "candidate_count": 0,
                "timing_confirm_count": 0,
                "authoritative_ingress_count": 0,
                "precheck_reject_count": 0,
                "native_handoff_reject_count": 0,
                "submitted_count": 0,
                "filled_count": 0,
                "cancelled_count": 0,
                "exit_count": 0,
                "avg_markout_3m_bps": None,
                "avg_markout_5m_bps": None,
                "fill_rate": 0.0,
                "top_reject_reason_json": [],
                "degraded_event_count": 0,
                "recovery_duplicate_prevented_count": 0,
            }
        )
        for event in list(events or []):
            strategy_tag = str(event.get("strategy_tag") or "")
            row = summary[strategy_tag]
            row["strategy_tag"] = strategy_tag
            event_type = str(event.get("event_type") or "")
            decision = str(event.get("decision") or "")
            if event_type == "candidate_created":
                row["candidate_count"] += 1
            elif event_type == "timing_confirmed":
                row["timing_confirm_count"] += 1
            elif event_type == "intent_ingressed" and decision == "accepted":
                row["authoritative_ingress_count"] += 1
            elif event_type == "precheck_rejected":
                row["precheck_reject_count"] += 1
            elif event_type == "native_handoff_rejected":
                row["native_handoff_reject_count"] += 1
            elif event_type == "order_submitted":
                row["submitted_count"] += 1
            elif event_type == "order_filled":
                row["filled_count"] += 1
            elif event_type == "order_cancelled":
                row["cancelled_count"] += 1
            elif event_type == "exit_decision":
                row["exit_count"] += 1
            elif event_type == "recovery_duplicate_prevented":
                row["recovery_duplicate_prevented_count"] += 1
            if bool(event.get("degraded_mode")):
                row["degraded_event_count"] += 1

        reject_by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in list(reject_rows or []):
            reject_by_strategy[str(row.get("strategy_tag") or "")].append(dict(row))

        markouts_by_strategy_horizon: Dict[tuple[str, int], List[float]] = defaultdict(list)
        for row in list(markout_rows or []):
            if row.get("markout_bps") is None:
                continue
            markouts_by_strategy_horizon[(str(row.get("strategy_tag") or ""), int(row.get("horizon_sec", 0) or 0))].append(
                float(row.get("markout_bps") or 0.0)
            )

        for strategy_tag, row in summary.items():
            rejects = sorted(
                reject_by_strategy.get(strategy_tag, []),
                key=lambda item: (-int(item.get("count", 0) or 0), str(item.get("reject_reason") or "")),
            )
            row["top_reject_reason_json"] = rejects[:5]
            for horizon_sec, field_name in ((180, "avg_markout_3m_bps"), (300, "avg_markout_5m_bps")):
                values = markouts_by_strategy_horizon.get((strategy_tag, horizon_sec), [])
                if values:
                    row[field_name] = sum(values) / float(len(values))
            submitted = int(row.get("submitted_count", 0) or 0)
            row["fill_rate"] = (float(row.get("filled_count", 0) or 0) / float(submitted)) if submitted > 0 else 0.0
        return [summary[key] for key in sorted(summary.keys())]

    def build_funnel_rows(self, trade_date: str, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return build_funnel_rows(trade_date, events)

    def build_attribution_rows(self, trade_date: str, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return build_attribution_rows(trade_date, events)

    def _baseline_days(self) -> int:
        return max(int(getattr(settings, "STRATEGY_ALERT_BASELINE_DAYS", 5) or 5), 0)

    def load_baseline_summary_rows(self, trade_date: str, baseline_days: Optional[int] = None) -> List[Dict[str, Any]]:
        days = self._baseline_days() if baseline_days is None else max(int(baseline_days), 0)
        if days <= 0 or not self._db_enabled_for_analytics():
            return []
        try:
            return self._summary_repo.list_recent_before_trade_date(trade_date, limit=days * 8)
        except Exception:
            return []

    def load_baseline_attribution_rows(self, trade_date: str, baseline_days: Optional[int] = None) -> List[Dict[str, Any]]:
        days = self._baseline_days() if baseline_days is None else max(int(baseline_days), 0)
        if days <= 0 or not self._db_enabled_for_analytics():
            return []
        try:
            return self._attribution_repo.list_recent_before_trade_date(trade_date, limit=days * 64)
        except Exception:
            return []

    def build_diagnostics_report(
        self,
        *,
        trade_date: str,
        analytics_payload: Dict[str, Any],
        alert_rows: Optional[Iterable[Dict[str, Any]]] = None,
        parity_rows: Optional[Iterable[Dict[str, Any]]] = None,
        top_n: int = 5,
    ) -> Dict[str, Any]:
        return build_diagnostics_report(
            trade_date=trade_date,
            analytics_payload=analytics_payload,
            alert_rows=alert_rows,
            parity_rows=parity_rows,
            top_n=top_n,
        )

    def build_alert_rows(
        self,
        *,
        trade_date: str,
        analytics_payload: Dict[str, Any],
        baseline_summary_rows: Optional[Iterable[Dict[str, Any]]] = None,
        baseline_attribution_rows: Optional[Iterable[Dict[str, Any]]] = None,
        parity_rows: Optional[Iterable[Dict[str, Any]]] = None,
        thresholds: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        summary_rows = (
            list(baseline_summary_rows)
            if baseline_summary_rows is not None
            else self.load_baseline_summary_rows(trade_date)
        )
        attribution_rows = (
            list(baseline_attribution_rows)
            if baseline_attribution_rows is not None
            else self.load_baseline_attribution_rows(trade_date)
        )
        return build_strategy_alert_rows(
            trade_date,
            analytics_payload,
            baseline_summary_rows=summary_rows,
            baseline_attribution_rows=attribution_rows,
            parity_rows=parity_rows,
            thresholds=thresholds,
        )

    def build_parity_rows(
        self,
        *,
        trade_date: str,
        live_payload: Dict[str, Any],
        replay_payload: Dict[str, Any],
        thresholds: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return build_strategy_parity_rows(
            trade_date,
            live_payload,
            replay_payload,
            thresholds=thresholds,
        )

    def materialize_alerts_trade_date(
        self,
        *,
        trade_date: str,
        analytics_payload: Optional[Dict[str, Any]] = None,
        parity_rows: Optional[Iterable[Dict[str, Any]]] = None,
        baseline_summary_rows: Optional[Iterable[Dict[str, Any]]] = None,
        baseline_attribution_rows: Optional[Iterable[Dict[str, Any]]] = None,
        thresholds: Optional[Dict[str, Any]] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        payload = analytics_payload or self.materialize_trade_date(trade_date=trade_date, persist=False)
        alert_rows = self.build_alert_rows(
            trade_date=trade_date,
            analytics_payload=payload,
            baseline_summary_rows=baseline_summary_rows,
            baseline_attribution_rows=baseline_attribution_rows,
            parity_rows=parity_rows,
            thresholds=thresholds,
        )
        result = {
            "trade_date": trade_date,
            "alert_count": len(alert_rows),
            "alert_rows": alert_rows,
            "analytics_input_diagnostics": dict(payload.get("event_input_diagnostics") or {}),
        }
        if persist and self._db_enabled_for_analytics():
            self._alerts_repo.ensure_table()
            self._alerts_repo.replace_for_trade_date(trade_date, alert_rows)
        return result

    def materialize_parity_trade_date(
        self,
        *,
        trade_date: str,
        live_payload: Dict[str, Any],
        replay_payload: Dict[str, Any],
        thresholds: Optional[Dict[str, Any]] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        parity_rows = self.build_parity_rows(
            trade_date=trade_date,
            live_payload=live_payload,
            replay_payload=replay_payload,
            thresholds=thresholds,
        )
        result = {
            "trade_date": trade_date,
            "parity_count": len(parity_rows),
            "mismatch_count": sum(1 for row in parity_rows if bool(row.get("mismatch_flag"))),
            "parity_rows": parity_rows,
            "live_input_diagnostics": dict(live_payload.get("event_input_diagnostics") or {}),
            "replay_input_diagnostics": dict(replay_payload.get("event_input_diagnostics") or {}),
        }
        if persist and self._db_enabled_for_analytics():
            self._parity_repo.ensure_table()
            self._parity_repo.replace_for_trade_date(trade_date, parity_rows)
        return result

    def materialize_trade_date(
        self,
        *,
        trade_date: str,
        persist: bool = True,
    ) -> Dict[str, Any]:
        events = self._load_events(trade_date)
        reject_rows = self.build_reject_reason_rows(trade_date, events)
        funnel_rows = self.build_funnel_rows(trade_date, events)
        attribution_rows = self.build_attribution_rows(trade_date, events)
        markout_rows = self.build_markout_rows(trade_date, events)
        summary_rows = self.build_daily_summary_rows(trade_date, events, reject_rows, markout_rows)
        payload = {
            "trade_date": trade_date,
            "event_count": len(events),
            "event_input_diagnostics": dict(self._last_event_input_diagnostics or {}),
            "summary_rows": summary_rows,
            "reject_rows": reject_rows,
            "funnel_rows": funnel_rows,
            "attribution_rows": attribution_rows,
            "markout_rows": markout_rows,
        }
        if persist and self._db_enabled_for_analytics():
            self._summary_repo.ensure_table()
            self._reject_repo.ensure_table()
            self._funnel_repo.ensure_table()
            self._attribution_repo.ensure_table()
            self._markout_repo.ensure_table()
            self._summary_repo.replace_for_trade_date(trade_date, summary_rows)
            self._reject_repo.replace_for_trade_date(trade_date, reject_rows)
            self._funnel_repo.replace_for_trade_date(trade_date, funnel_rows)
            self._attribution_repo.replace_for_trade_date(trade_date, attribution_rows)
            self._markout_repo.replace_for_trade_date(trade_date, markout_rows)
        return payload
