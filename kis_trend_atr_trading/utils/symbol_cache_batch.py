"""Utilities for refreshing symbol_cache from constituent reference artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import time
from typing import Any, Dict, Iterable, Mapping, Optional

from utils.logger import get_logger
from utils.market_hours import KST

logger = get_logger("symbol_cache_batch")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONSTITUENT_FILES = {
    "kospi200": REPO_ROOT / "data" / "reference" / "kospi200_constituents.txt",
    "kosdaq150": REPO_ROOT / "data" / "reference" / "kosdaq150_constituents.txt",
}
DEFAULT_STALE_DAYS = 30
DEFAULT_MAX_RPS = 2.0


@dataclass(frozen=True)
class ConstituentArtifactRow:
    index_name: str
    code: str
    name: str
    kind: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class SymbolCacheRefreshStats:
    index_name: str
    rows_total: int = 0
    member_rows: int = 0
    auxiliary_rows: int = 0
    duplicates_skipped: int = 0
    skipped_fresh: int = 0
    kis_api_calls: int = 0
    kis_name_hits: int = 0
    kis_api_errors: int = 0
    artifact_fallbacks: int = 0
    upserted: int = 0
    failed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RequestRateLimiter:
    """Simple per-process rate limiter for best-effort batch API calls."""

    def __init__(
        self,
        max_rps: float,
        *,
        time_fn=time.monotonic,
        sleep_fn=time.sleep,
    ) -> None:
        self._max_rps = float(max_rps)
        self._min_interval = 1.0 / self._max_rps if self._max_rps > 0 else 0.0
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        self._last_call_at: float | None = None

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        now = float(self._time_fn())
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep_fn(remaining)
                now = float(self._time_fn())
        self._last_call_at = now


def _normalize_kind(kind: str) -> str:
    token = str(kind or "").strip().lower()
    return "auxiliary" if token == "auxiliary" else "member"


def parse_constituent_artifact_lines(
    lines: Iterable[str],
    *,
    index_name: str,
) -> list[ConstituentArtifactRow]:
    rows: list[ConstituentArtifactRow] = []
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise ValueError(f"{index_name} constituent line must contain code/name/kind: {line!r}")
        code = str(parts[0] or "").strip()
        name = str(parts[1] or "").strip()
        kind = _normalize_kind(parts[2])
        if not code or not name:
            raise ValueError(f"{index_name} constituent line missing code/name: {line!r}")
        rows.append(
            ConstituentArtifactRow(
                index_name=index_name,
                code=code,
                name=name,
                kind=kind,
            )
        )
    return rows


def load_constituent_artifact(path: Path, *, index_name: str) -> list[ConstituentArtifactRow]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(f"{index_name} constituent artifact가 없습니다: {artifact_path}")
    rows = parse_constituent_artifact_lines(
        artifact_path.read_text(encoding="utf-8").splitlines(),
        index_name=index_name,
    )
    if not rows:
        raise ValueError(f"{index_name} constituent artifact가 비어 있습니다: {artifact_path}")
    return rows


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        try:
            dt = datetime.fromisoformat(token)
        except ValueError:
            try:
                dt = datetime.strptime(token, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


def _is_fresh(updated_at: Any, *, now: datetime, stale_days: int) -> bool:
    stale_window = max(int(stale_days), 0)
    if stale_window <= 0:
        return False
    dt = _coerce_datetime(updated_at)
    if dt is None:
        return False
    return (now - dt) <= timedelta(days=stale_window)


def _extract_kis_stock_name(quote: Any) -> str | None:
    if not isinstance(quote, Mapping):
        return None
    stock_name = str(
        quote.get("stock_name")
        or quote.get("name")
        or quote.get("hts_kor_isnm")
        or quote.get("prdt_name")
        or quote.get("isnm_nm")
        or ""
    ).strip()
    return stock_name or None


def refresh_symbol_cache_from_constituents(
    *,
    rows_by_index: Mapping[str, Iterable[ConstituentArtifactRow]],
    cache_repo: Any,
    api_client: Any = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    max_rps: float = DEFAULT_MAX_RPS,
    rate_limiter: Any = None,
    api_init_error: str | None = None,
) -> Dict[str, Any]:
    now_kst = now or datetime.now(KST)
    resolved_stale_days = max(int(stale_days), 0)
    resolved_max_rps = float(max_rps)
    limiter = rate_limiter if rate_limiter is not None else (
        RequestRateLimiter(resolved_max_rps) if api_client is not None else None
    )

    stats_by_index: Dict[str, SymbolCacheRefreshStats] = {}
    seen_codes: set[str] = set()
    cache_lookup_errors: list[Dict[str, str]] = []

    for index_name, rows_iter in rows_by_index.items():
        rows = list(rows_iter)
        stats = SymbolCacheRefreshStats(index_name=index_name)
        stats_by_index[index_name] = stats

        for row in rows:
            stats.rows_total += 1
            if row.kind == "auxiliary":
                stats.auxiliary_rows += 1
            else:
                stats.member_rows += 1

            if row.code in seen_codes:
                stats.duplicates_skipped += 1
                continue
            seen_codes.add(row.code)

            cached = None
            try:
                cached = cache_repo.get(row.code)
            except Exception as exc:
                cache_lookup_errors.append({"code": row.code, "error": str(exc)})
                logger.warning("[SYMBOL_CACHE_BATCH] symbol_cache get failed code=%s err=%s", row.code, exc)

            if cached and _is_fresh(getattr(cached, "updated_at", None), now=now_kst, stale_days=resolved_stale_days):
                stats.skipped_fresh += 1
                continue

            stock_name = None
            if api_client is not None and row.kind != "auxiliary":
                stats.kis_api_calls += 1
                if limiter is not None:
                    limiter.wait()
                try:
                    quote = api_client.get_current_price(row.code)
                    stock_name = _extract_kis_stock_name(quote)
                    if stock_name:
                        stats.kis_name_hits += 1
                except Exception as exc:
                    stats.kis_api_errors += 1
                    logger.warning(
                        "[SYMBOL_CACHE_BATCH] KIS name lookup failed index=%s code=%s kind=%s err=%s",
                        index_name,
                        row.code,
                        row.kind,
                        exc,
                    )

            if not stock_name:
                stock_name = row.name
                stats.artifact_fallbacks += 1

            if dry_run:
                stats.upserted += 1
                continue

            if cache_repo.upsert(row.code, stock_name, updated_at=now_kst):
                stats.upserted += 1
            else:
                stats.failed += 1
                logger.warning(
                    "[SYMBOL_CACHE_BATCH] symbol_cache upsert failed index=%s code=%s name=%s kind=%s",
                    index_name,
                    row.code,
                    stock_name,
                    row.kind,
                )

    summary = {
        "name_source": "kis",
        "dry_run": bool(dry_run),
        "refreshed_at": now_kst.isoformat(),
        "stale_days": resolved_stale_days,
        "max_rps": resolved_max_rps,
        "api_client_initialized": bool(api_client is not None),
        "api_init_error": api_init_error,
        "cache_lookup_error_count": len(cache_lookup_errors),
        "cache_lookup_errors": cache_lookup_errors,
        "indexes": {name: stats.to_dict() for name, stats in stats_by_index.items()},
        "total_unique_codes": len(seen_codes),
        "skipped_fresh_count": sum(stats.skipped_fresh for stats in stats_by_index.values()),
        "kis_api_call_count": sum(stats.kis_api_calls for stats in stats_by_index.values()),
        "kis_name_hit_count": sum(stats.kis_name_hits for stats in stats_by_index.values()),
        "kis_api_error_count": sum(stats.kis_api_errors for stats in stats_by_index.values()),
        "artifact_fallback_count": sum(stats.artifact_fallbacks for stats in stats_by_index.values()),
        "upserted_count": sum(stats.upserted for stats in stats_by_index.values()),
        "failed_count": sum(stats.failed for stats in stats_by_index.values()),
    }
    logger.info("[SYMBOL_CACHE_BATCH] summary=%s", summary)
    return summary
