#!/usr/bin/env python3
"""Sync KOSPI200/KOSDAQ150 constituent artifacts and publish symbol_cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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

from utils.index_constituent_sync import (
    DEFAULT_REFERENCE_OUTPUT_DIR,
    KodexProxyConstituentFetcher,
    TigerProxyConstituentFetcher,
    sync_index_constituents_proxy,
)
from utils.logger import get_logger

logger = get_logger("sync_index_constituents_proxy")


def _load_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", APP_ROOT / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ETF proxy source(TIGER+KODEX)를 cross-check해 constituent artifact를 동기화합니다.",
    )
    parser.add_argument(
        "--index",
        choices=("all", "kospi200", "kosdaq150"),
        default="all",
        help="동기화 대상 지수 (기본: all)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_REFERENCE_OUTPUT_DIR),
        help="artifact 출력 디렉터리 (기본: data/reference)",
    )
    parser.add_argument("--dry-run", action="store_true", help="파일/DB 반영 없이 검증만 수행")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser


def _selected_indexes(index_arg: str) -> list[str]:
    if index_arg == "all":
        return ["kospi200", "kosdaq150"]
    return [index_arg]


def main() -> int:
    _load_env()
    parser = _build_parser()
    args = parser.parse_args()

    indexes = _selected_indexes(args.index)
    output_dir = Path(args.output_dir)
    logger.info(
        "[INDEX_PROXY_SYNC] start indexes=%s output_dir=%s dry_run=%s",
        indexes,
        output_dir,
        args.dry_run,
    )

    try:
        summary = sync_index_constituents_proxy(
            index_names=indexes,
            output_dir=output_dir,
            tiger_fetcher=TigerProxyConstituentFetcher(),
            kodex_fetcher=KodexProxyConstituentFetcher(),
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logger.error("[INDEX_PROXY_SYNC] batch failed: %s", exc, exc_info=True)
        print(f"❌ constituent proxy sync 실패: {exc}")
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("[INDEX_PROXY_SYNC]")
        print(
            f"status={summary['status']} output_dir={summary['output_dir']} "
            f"dry_run={summary['dry_run']} synced_at={summary['synced_at']}"
        )
        for index_name, item in summary["indexes"].items():
            print(
                f"- {index_name} status={item['status']} member_count={item.get('member_count', 0)} "
                f"auxiliary_count={item.get('auxiliary_count', 0)} cross_check_match={item.get('cross_check_match')} "
                f"name_mismatch_count={item.get('name_mismatch_count', 0)} "
                f"symbol_cache_upserted={item.get('symbol_cache_sync', {}).get('upserted_count', 0)} "
                f"output_txt={item['output_txt']}"
            )
            if item["status"] == "failed":
                print(f"  error={item['error']}")

    return 0 if summary["status"] in ("ok", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
