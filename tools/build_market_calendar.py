#!/usr/bin/env python3
"""
Build KRX market holiday calendar JSON for runtime use.

Output schema:
{
  "market": "KRX",
  "version": "YYYY.MM",
  "generated_at": "...",
  "coverage_from": "YYYY-01-01",
  "coverage_to": "YYYY-12-31",
  "source": "python-holidays|builtin_seed",
  "holidays": ["YYYY-MM-DD", ...]
}
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Set, Tuple

KST = timezone(timedelta(hours=9))

# Seed fallback used when python-holidays package is unavailable.
_DEFAULT_SEED_HOLIDAYS = {
    # 2024
    date(2024, 1, 1),
    date(2024, 2, 9),
    date(2024, 2, 10),
    date(2024, 2, 11),
    date(2024, 2, 12),
    date(2024, 3, 1),
    date(2024, 4, 10),
    date(2024, 5, 1),
    date(2024, 5, 6),
    date(2024, 5, 15),
    date(2024, 6, 6),
    date(2024, 8, 15),
    date(2024, 9, 16),
    date(2024, 9, 17),
    date(2024, 9, 18),
    date(2024, 10, 3),
    date(2024, 10, 9),
    date(2024, 12, 25),
    date(2024, 12, 31),
    # 2025
    date(2025, 1, 1),
    date(2025, 1, 28),
    date(2025, 1, 29),
    date(2025, 1, 30),
    date(2025, 3, 1),
    date(2025, 5, 1),
    date(2025, 5, 5),
    date(2025, 5, 6),
    date(2025, 6, 6),
    date(2025, 8, 15),
    date(2025, 10, 3),
    date(2025, 10, 5),
    date(2025, 10, 6),
    date(2025, 10, 7),
    date(2025, 10, 8),
    date(2025, 10, 9),
    date(2025, 12, 25),
    date(2025, 12, 31),
    # 2026
    date(2026, 1, 1),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 3, 1),
    date(2026, 3, 2),
    date(2026, 5, 1),
    date(2026, 5, 5),
    date(2026, 5, 24),
    date(2026, 5, 25),
    date(2026, 6, 6),
    date(2026, 8, 15),
    date(2026, 8, 17),
    date(2026, 9, 24),
    date(2026, 9, 25),
    date(2026, 9, 26),
    date(2026, 10, 3),
    date(2026, 10, 5),
    date(2026, 10, 9),
    date(2026, 12, 25),
    date(2026, 12, 31),
}


def _parse_date_tokens(tokens: Iterable[str]) -> Set[date]:
    out: Set[date] = set()
    for token in tokens:
        raw = str(token or "").strip()
        if not raw:
            continue
        out.add(datetime.strptime(raw, "%Y-%m-%d").date())
    return out


def _year_end_and_worker_day(year_from: int, year_to: int) -> Set[date]:
    out: Set[date] = set()
    for year in range(year_from, year_to + 1):
        out.add(date(year, 5, 1))
        out.add(date(year, 12, 31))
    return out


def _build_with_python_holidays(year_from: int, year_to: int) -> Tuple[Set[date], str]:
    try:
        import holidays  # type: ignore
    except Exception:
        return set(), "unavailable"

    years = list(range(year_from, year_to + 1))
    base = holidays.country_holidays("KR", years=years)
    out = {d for d in base.keys() if isinstance(d, date)}
    out.update(_year_end_and_worker_day(year_from, year_to))
    return out, "python-holidays"


def _build_with_builtin_seed(year_from: int, year_to: int) -> Tuple[Set[date], str]:
    out = {d for d in _DEFAULT_SEED_HOLIDAYS if year_from <= d.year <= year_to}
    out.update(_year_end_and_worker_day(year_from, year_to))
    return out, "builtin_seed"


def _apply_overrides(holidays_set: Set[date], override_file: Path) -> Set[date]:
    if not override_file.exists():
        return holidays_set

    payload = json.loads(override_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("override payload must be a JSON object")

    extra = _parse_date_tokens(payload.get("extra_holidays") or [])
    remove = _parse_date_tokens(payload.get("remove_holidays") or [])
    out = set(holidays_set)
    out.update(extra)
    out.difference_update(remove)
    return out


def _build_payload(
    year_from: int,
    year_to: int,
    source_name: str,
    holidays_set: Set[date],
) -> dict:
    holidays_sorted: List[str] = [d.isoformat() for d in sorted(holidays_set)]
    return {
        "market": "KRX",
        "version": datetime.now(KST).strftime("%Y.%m"),
        "generated_at": datetime.now(KST).isoformat(),
        "coverage_from": date(year_from, 1, 1).isoformat(),
        "coverage_to": date(year_to, 12, 31).isoformat(),
        "source": source_name,
        "holidays": holidays_sorted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build KRX holiday calendar JSON")
    this_year = datetime.now(KST).year
    parser.add_argument("--year-from", type=int, default=this_year - 1)
    parser.add_argument("--year-to", type=int, default=this_year + 3)
    parser.add_argument(
        "--output",
        type=str,
        default="kis_trend_atr_trading/data/market_calendar_krx.json",
    )
    parser.add_argument(
        "--override-file",
        type=str,
        default="kis_trend_atr_trading/config/market_calendar_overrides_krx.json",
    )
    args = parser.parse_args()

    if args.year_to < args.year_from:
        raise ValueError("year-to must be >= year-from")

    holidays_set, source_name = _build_with_python_holidays(args.year_from, args.year_to)
    if not holidays_set:
        holidays_set, source_name = _build_with_builtin_seed(args.year_from, args.year_to)

    override_file = Path(args.override_file)
    holidays_set = _apply_overrides(holidays_set, override_file)
    payload = _build_payload(args.year_from, args.year_to, source_name, holidays_set)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[calendar] written",
        f"path={output}",
        f"source={source_name}",
        f"count={len(payload['holidays'])}",
        f"coverage={payload['coverage_from']}..{payload['coverage_to']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
