"""Tests for symbol_cache refresh batch helpers."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from db.repository import SymbolCacheRecord
from utils.market_hours import KST
from utils.symbol_cache_batch import (
    ConstituentArtifactRow,
    load_constituent_artifact,
    parse_constituent_artifact_lines,
    refresh_symbol_cache_from_constituents,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class InMemorySymbolCacheRepo:
    def __init__(self, initial=None):
        self._data = initial or {}
        self.upsert_calls = []

    def get(self, stock_code: str):
        return self._data.get(stock_code)

    def upsert(self, stock_code: str, stock_name: str, updated_at=None):
        ts = updated_at or datetime.now(KST)
        self._data[stock_code] = SymbolCacheRecord(
            stock_code=stock_code,
            stock_name=stock_name,
            updated_at=ts,
        )
        self.upsert_calls.append((stock_code, stock_name))
        return True


class FakeKisApi:
    def __init__(self, responses=None, errors=None):
        self._responses = responses or {}
        self._errors = errors or {}
        self.calls = []

    def get_current_price(self, stock_code: str):
        code = str(stock_code)
        self.calls.append(code)
        if code in self._errors:
            raise self._errors[code]
        return self._responses.get(code, {})


class FakeRateLimiter:
    def __init__(self):
        self.calls = 0

    def wait(self):
        self.calls += 1


def test_parse_constituent_artifact_lines_supports_member_and_auxiliary_rows():
    fixture_text = (FIXTURE_DIR / "sample_constituents.txt").read_text(encoding="utf-8")

    rows = parse_constituent_artifact_lines(fixture_text.splitlines(), index_name="kosdaq150")

    assert [row.code for row in rows] == ["005930", "0009K0", "KRD010010001"]
    assert rows[0].kind == "member"
    assert rows[2].kind == "auxiliary"


def test_load_constituent_artifact_raises_for_missing_file(tmp_path):
    missing = tmp_path / "missing_constituents.txt"

    try:
        load_constituent_artifact(missing, index_name="kospi200")
    except FileNotFoundError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected FileNotFoundError")

    assert "constituent artifact가 없습니다" in message


def test_refresh_symbol_cache_prefers_kis_name_and_falls_back_to_artifact():
    repo = InMemorySymbolCacheRepo()
    api_client = FakeKisApi(
        responses={"086520": {"stock_name": "에코프로"}},
        errors={"0009K0": RuntimeError("unsupported code")},
    )
    limiter = FakeRateLimiter()
    rows_by_index = {
        "kosdaq150": [
            ConstituentArtifactRow(index_name="kosdaq150", code="086520", name="아티팩트에코프로", kind="member"),
            ConstituentArtifactRow(index_name="kosdaq150", code="0009K0", name="에임드바이오", kind="member"),
        ]
    }

    summary = refresh_symbol_cache_from_constituents(
        rows_by_index=rows_by_index,
        cache_repo=repo,
        api_client=api_client,
        rate_limiter=limiter,
    )

    assert ("086520", "에코프로") in repo.upsert_calls
    assert ("0009K0", "에임드바이오") in repo.upsert_calls
    assert limiter.calls == 2
    assert summary["kis_api_call_count"] == 2
    assert summary["kis_name_hit_count"] == 1
    assert summary["artifact_fallback_count"] == 1


def test_auxiliary_row_is_upserted_and_duplicates_are_skipped_without_kis_lookup():
    repo = InMemorySymbolCacheRepo()
    api_client = FakeKisApi(
        responses={"005930": {"stock_name": "삼성전자"}},
    )
    limiter = FakeRateLimiter()
    rows_by_index = {
        "kospi200": [
            ConstituentArtifactRow(index_name="kospi200", code="005930", name="삼성전자", kind="member"),
            ConstituentArtifactRow(index_name="kospi200", code="KRD010010001", name="원화예금", kind="auxiliary"),
        ],
        "kosdaq150": [
            ConstituentArtifactRow(index_name="kosdaq150", code="005930", name="삼성전자", kind="member"),
        ],
    }

    summary = refresh_symbol_cache_from_constituents(
        rows_by_index=rows_by_index,
        cache_repo=repo,
        api_client=api_client,
        rate_limiter=limiter,
    )

    assert api_client.calls == ["005930"]
    assert limiter.calls == 1
    assert ("KRD010010001", "원화예금") in repo.upsert_calls
    assert summary["indexes"]["kospi200"]["auxiliary_rows"] == 1
    assert summary["indexes"]["kosdaq150"]["duplicates_skipped"] == 1


def test_refresh_symbol_cache_supports_artifact_only_fallback_when_kis_unavailable():
    repo = InMemorySymbolCacheRepo()
    rows_by_index = {
        "kospi200": [
            ConstituentArtifactRow(index_name="kospi200", code="0126Z0", name="삼성에피스홀딩스", kind="member"),
        ]
    }

    summary = refresh_symbol_cache_from_constituents(
        rows_by_index=rows_by_index,
        cache_repo=repo,
        api_client=None,
        api_init_error="kis init failed",
    )

    assert summary["api_client_initialized"] is False
    assert summary["api_init_error"] == "kis init failed"
    assert ("0126Z0", "삼성에피스홀딩스") in repo.upsert_calls
    assert summary["failed_count"] == 0
    assert summary["artifact_fallback_count"] == 1


def test_refresh_symbol_cache_skips_fresh_cache_without_kis_call():
    fresh_time = datetime.now(KST) - timedelta(days=1)
    repo = InMemorySymbolCacheRepo(
        initial={
            "086520": SymbolCacheRecord(
                stock_code="086520",
                stock_name="에코프로",
                updated_at=fresh_time,
            )
        }
    )
    api_client = FakeKisApi(
        responses={"086520": {"stock_name": "에코프로-신규"}},
    )

    summary = refresh_symbol_cache_from_constituents(
        rows_by_index={
            "kosdaq150": [
                ConstituentArtifactRow(index_name="kosdaq150", code="086520", name="에코프로", kind="member"),
            ]
        },
        cache_repo=repo,
        api_client=api_client,
        stale_days=30,
    )

    assert api_client.calls == []
    assert repo.upsert_calls == []
    assert summary["skipped_fresh_count"] == 1
    assert summary["upserted_count"] == 0


def test_refresh_symbol_cache_dry_run_reports_upserts_without_db_write():
    repo = InMemorySymbolCacheRepo()
    api_client = FakeKisApi(
        responses={"086520": {"stock_name": "에코프로"}},
    )

    summary = refresh_symbol_cache_from_constituents(
        rows_by_index={
            "kosdaq150": [
                ConstituentArtifactRow(index_name="kosdaq150", code="086520", name="에코프로", kind="member"),
            ]
        },
        cache_repo=repo,
        api_client=api_client,
        dry_run=True,
    )

    assert repo.upsert_calls == []
    assert summary["upserted_count"] == 1
    assert summary["dry_run"] is True

