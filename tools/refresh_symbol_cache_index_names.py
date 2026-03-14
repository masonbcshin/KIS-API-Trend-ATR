#!/usr/bin/env python3
"""Refresh symbol_cache from constituent reference artifacts using KIS stock-name lookups."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    def load_dotenv(*args, **kwargs):  # type: ignore[override]
        return False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
for _path in (APP_ROOT, PROJECT_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from api.kis_api import KISApi
from db.repository import get_symbol_cache_repository
from env import get_trading_mode
from utils.logger import get_logger
from utils.symbol_cache_batch import (
    DEFAULT_CONSTITUENT_FILES,
    DEFAULT_MAX_RPS,
    DEFAULT_STALE_DAYS,
    load_constituent_artifact,
    refresh_symbol_cache_from_constituents,
)

logger = get_logger("refresh_symbol_cache_index_names")


def _load_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", APP_ROOT / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="constituent reference artifact와 KIS 현재가 API를 이용해 symbol_cache를 갱신합니다.",
    )
    parser.add_argument(
        "--index",
        choices=("all", "kospi200", "kosdaq150"),
        default="all",
        help="갱신 대상 지수 (기본: all)",
    )
    parser.add_argument(
        "--kospi200-file",
        default=str(DEFAULT_CONSTITUENT_FILES["kospi200"]),
        help="KOSPI200 constituent artifact 경로",
    )
    parser.add_argument(
        "--kosdaq150-file",
        default=str(DEFAULT_CONSTITUENT_FILES["kosdaq150"]),
        help="KOSDAQ150 constituent artifact 경로",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help="symbol_cache.updated_at 기준 fresh TTL 일수 (기본: 30, 0이면 항상 재조회)",
    )
    parser.add_argument(
        "--max-rps",
        type=float,
        default=DEFAULT_MAX_RPS,
        help="KIS 현재가 조회 최대 초당 호출 수 (기본: 2.0, 0이면 제한 없음)",
    )
    parser.add_argument("--dry-run", action="store_true", help="DB upsert 없이 요약만 출력")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser


def _selected_index_paths(args: argparse.Namespace) -> Dict[str, Path]:
    selected: Dict[str, Path] = {}
    if args.index in ("all", "kospi200"):
        selected["kospi200"] = Path(args.kospi200_file)
    if args.index in ("all", "kosdaq150"):
        selected["kosdaq150"] = Path(args.kosdaq150_file)
    return selected


def _print_text_summary(summary: dict) -> None:
    print("[SYMBOL_CACHE_BATCH]")
    print(
        f"dry_run={summary['dry_run']} "
        f"stale_days={summary['stale_days']} "
        f"max_rps={summary['max_rps']} "
        f"api_client_initialized={summary['api_client_initialized']} "
        f"unique_codes={summary['total_unique_codes']} "
        f"skipped_fresh_count={summary['skipped_fresh_count']} "
        f"kis_api_call_count={summary['kis_api_call_count']} "
        f"kis_name_hit_count={summary['kis_name_hit_count']} "
        f"artifact_fallback_count={summary['artifact_fallback_count']} "
        f"upserted_count={summary['upserted_count']} "
        f"failed_count={summary['failed_count']}"
    )
    if summary.get("api_init_error"):
        print(f"- api_init_error={summary['api_init_error']}")
    for index_name, stats in summary["indexes"].items():
        print(
            f"- {index_name} rows_total={stats['rows_total']} member_rows={stats['member_rows']} "
            f"auxiliary_rows={stats['auxiliary_rows']} duplicates_skipped={stats['duplicates_skipped']} "
            f"skipped_fresh={stats['skipped_fresh']} kis_api_calls={stats['kis_api_calls']} "
            f"kis_name_hits={stats['kis_name_hits']} artifact_fallbacks={stats['artifact_fallbacks']} "
            f"upserted={stats['upserted']} failed={stats['failed']}"
        )


def main() -> int:
    _load_env()
    parser = _build_parser()
    args = parser.parse_args()

    try:
        rows_by_index = {
            index_name: load_constituent_artifact(path, index_name=index_name)
            for index_name, path in _selected_index_paths(args).items()
        }
    except Exception as exc:
        logger.error("[SYMBOL_CACHE_BATCH] input load failed: %s", exc)
        print(f"❌ 입력 로딩 실패: {exc}")
        return 2

    logger.info(
        "[SYMBOL_CACHE_BATCH] start indexes=%s dry_run=%s stale_days=%s max_rps=%s",
        list(rows_by_index.keys()),
        args.dry_run,
        args.stale_days,
        args.max_rps,
    )

    api_client = None
    api_init_error = None
    try:
        is_paper = get_trading_mode() != "REAL"
        api_client = KISApi(is_paper_trading=is_paper)
    except Exception as exc:
        api_init_error = str(exc)
        logger.warning("[SYMBOL_CACHE_BATCH] KIS API init failed, artifact fallback only: %s", exc)

    try:
        cache_repo = get_symbol_cache_repository()
        summary = refresh_symbol_cache_from_constituents(
            rows_by_index=rows_by_index,
            cache_repo=cache_repo,
            api_client=api_client,
            dry_run=args.dry_run,
            stale_days=args.stale_days,
            max_rps=args.max_rps,
            api_init_error=api_init_error,
        )
    except Exception as exc:
        logger.error("[SYMBOL_CACHE_BATCH] execution failed: %s", exc, exc_info=True)
        print(f"❌ symbol_cache 갱신 실패: {exc}")
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_text_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
