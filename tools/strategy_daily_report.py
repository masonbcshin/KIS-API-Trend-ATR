#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
for _path in (APP_ROOT, PROJECT_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from analytics.materializer import StrategyAnalyticsMaterializer


def _load_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", APP_ROOT / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _parse_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strategy analytics daily report")
    parser.add_argument("--date", required=True, type=_parse_date, help="대상 거래일 (YYYY-MM-DD)")
    return parser


def _funnel_index(rows):
    index = {}
    for row in list(rows or []):
        strategy_tag = str(row.get("strategy_tag") or "")
        slice_key = str(row.get("slice_key") or "")
        slice_value = str(row.get("slice_value") or "")
        stage_name = str(row.get("stage_name") or "")
        index.setdefault(strategy_tag, {}).setdefault(slice_key, {}).setdefault(slice_value, {})[stage_name] = dict(row)
    return index


def _group_top_reason_groups(rows, *, strategy_tag: str, slice_key: str = "overall", slice_value: str = "all"):
    counts = {}
    for row in list(rows or []):
        if str(row.get("strategy_tag") or "") != strategy_tag:
            continue
        if str(row.get("slice_key") or "") != slice_key or str(row.get("slice_value") or "") != slice_value:
            continue
        reason_group = str(row.get("reason_group") or "")
        counts[reason_group] = counts.get(reason_group, 0) + int(row.get("count", 0) or 0)
    return sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0])))


def _diagnostic_counts(rows, *, strategy_tag: str):
    wanted = {
        "tie_break_applied": 0,
        "tie_break_loser": 0,
        "degraded_rejected": 0,
        "recovery_duplicate_prevented": 0,
        "authoritative_queue_rejected": 0,
    }
    for reason_group, count in _group_top_reason_groups(rows, strategy_tag=strategy_tag):
        if reason_group in wanted:
            wanted[reason_group] = int(count)
    return wanted


def _format_rate(value):
    return f"{float(value or 0.0):.2f}"


def _format_slice_funnel(funnel_by_slice, *, stage_names=("candidate_created", "filled")):
    tokens = []
    for slice_value, stages in sorted(funnel_by_slice.items()):
        counts = [f"{stage_name.split('_')[0]}={int((stages.get(stage_name) or {}).get('stage_count', 0) or 0)}" for stage_name in stage_names]
        if not any("=0" not in token for token in counts):
            continue
        tokens.append(f"{slice_value}({', '.join(counts)})")
    return "; ".join(tokens[:6])


def main() -> int:
    _load_env()
    args = _build_parser().parse_args()
    materializer = StrategyAnalyticsMaterializer()
    result = materializer.materialize_trade_date(trade_date=args.date, persist=False)
    funnel_index = _funnel_index(result.get("funnel_rows", []))
    attribution_rows = result.get("attribution_rows", [])
    print(f"[STRATEGY_ANALYTICS] trade_date={args.date} events={result['event_count']}")
    for row in result.get("summary_rows", []):
        strategy_tag = str(row["strategy_tag"])
        strategy_funnel = funnel_index.get(strategy_tag, {})
        overall = ((strategy_funnel.get("overall") or {}).get("all") or {})
        print(f"- {strategy_tag}")
        print(
            f"  summary candidates={row['candidate_count']} timing={row['timing_confirm_count']} "
            f"ingress={row['authoritative_ingress_count']} submitted={row['submitted_count']} "
            f"filled={row['filled_count']} exit={row['exit_count']} fill_rate={row['fill_rate']:.2f} "
            f"avg_3m={row['avg_markout_3m_bps']} avg_5m={row['avg_markout_5m_bps']}"
        )
        print(
            "  funnel "
            f"candidate={int((overall.get('candidate_created') or {}).get('stage_count', 0) or 0)} "
            f"timing={int((overall.get('timing_confirmed') or {}).get('stage_count', 0) or 0)}({ _format_rate((overall.get('timing_confirmed') or {}).get('conversion_rate')) }) "
            f"ingress={int((overall.get('authoritative_ingress') or {}).get('stage_count', 0) or 0)}({ _format_rate((overall.get('authoritative_ingress') or {}).get('conversion_rate')) }) "
            f"submitted={int((overall.get('submitted') or {}).get('stage_count', 0) or 0)}({ _format_rate((overall.get('submitted') or {}).get('conversion_rate')) }) "
            f"filled={int((overall.get('filled') or {}).get('stage_count', 0) or 0)}({ _format_rate((overall.get('filled') or {}).get('conversion_rate')) }) "
            f"exit={int((overall.get('exit') or {}).get('stage_count', 0) or 0)}({ _format_rate((overall.get('exit') or {}).get('conversion_rate')) })"
        )
        rejects = row.get("top_reject_reason_json") or []
        if rejects:
            print(f"  raw_top_rejects={rejects[:3]}")
        top_reason_groups = _group_top_reason_groups(attribution_rows, strategy_tag=strategy_tag)
        if top_reason_groups:
            print(f"  top_reason_groups={top_reason_groups[:5]}")
        diagnostics = _diagnostic_counts(attribution_rows, strategy_tag=strategy_tag)
        print(
            f"  diagnostics tie_break_hits={diagnostics['tie_break_applied']} "
            f"tie_break_losers={diagnostics['tie_break_loser']} "
            f"degraded={diagnostics['degraded_rejected']} "
            f"recovery={diagnostics['recovery_duplicate_prevented']} "
            f"queue_reject={diagnostics['authoritative_queue_rejected']}"
        )
        for slice_key, label in (
            ("regime_state", "regime"),
            ("session_bucket", "session"),
            ("source_state", "source"),
            ("degraded_mode", "degraded"),
        ):
            formatted = _format_slice_funnel(strategy_funnel.get(slice_key, {}))
            if formatted:
                print(f"  {label}_slice={formatted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
