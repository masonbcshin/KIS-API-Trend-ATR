"""Tests for ETF proxy constituent sync helpers."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.index_constituent_sync import (
    ConstituentRecord,
    SourceSnapshot,
    build_canonical_constituents,
    extract_tiger_as_of,
    parse_kodex_product_payload,
    parse_tiger_constituent_rows,
    sync_index_constituents_proxy,
)
from utils.market_hours import KST

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class DummyFetcher:
    def __init__(self, snapshots):
        self._snapshots = dict(snapshots)

    def fetch(self, *, index_name: str):
        return self._snapshots[index_name]


def _build_rows(expected_member_count: int, *, special_member_code: str, special_member_name: str):
    rows = [ConstituentRecord(code=special_member_code, name=special_member_name, kind="member")]
    for idx in range(1, expected_member_count):
        rows.append(
            ConstituentRecord(
                code=f"{idx:06d}",
                name=f"종목{idx:03d}",
                kind="member",
            )
        )
    rows.append(
        ConstituentRecord(
            code="KRD010010001",
            name="원화예금",
            kind="auxiliary",
        )
    )
    return rows


def test_parse_tiger_sample_supports_alphanumeric_member_and_auxiliary():
    html = (FIXTURE_DIR / "tiger_kosdaq_sample.html").read_text(encoding="utf-8")

    rows = parse_tiger_constituent_rows(html)
    as_of = extract_tiger_as_of(html)

    assert as_of == "2026-03-13"
    assert [row.code for row in rows] == ["086520", "0009K0", "KRD010010001"]
    assert rows[1].kind == "member"
    assert rows[2].kind == "auxiliary"


def test_parse_kodex_sample_supports_alphanumeric_member_and_auxiliary():
    payload = json.loads((FIXTURE_DIR / "kodex_kosdaq_sample.json").read_text(encoding="utf-8"))

    snapshot = parse_kodex_product_payload(payload, index_name="kosdaq150")

    assert snapshot.as_of == "2026-03-14"
    assert snapshot.detail_url == "https://www.samsungfund.com/excel_pdf.do?fId=2ETF54&gijunYMD=20260314"
    assert [row.code for row in snapshot.rows] == ["086520", "0009K0", "KRD010010001"]
    assert snapshot.rows[1].kind == "member"
    assert snapshot.rows[2].kind == "auxiliary"


def test_kospi200_cross_check_success_allows_alphanumeric_member():
    tiger = SourceSnapshot(
        index_name="kospi200",
        source="tiger",
        as_of="2026-03-13",
        rows=_build_rows(200, special_member_code="0126Z0", special_member_name="삼성에피스홀딩스"),
        source_url="tiger",
    )
    kodex = SourceSnapshot(
        index_name="kospi200",
        source="kodex",
        as_of="2026-03-14",
        rows=_build_rows(200, special_member_code="0126Z0", special_member_name="삼성에피스홀딩스"),
        source_url="kodex",
    )

    rows, mismatches = build_canonical_constituents(tiger_snapshot=tiger, kodex_snapshot=kodex)

    assert len(mismatches) == 0
    assert sum(1 for row in rows if row.kind == "member") == 200
    assert any(row.code == "0126Z0" and row.kind == "member" for row in rows)
    assert any(row.code == "KRD010010001" and row.kind == "auxiliary" for row in rows)


def test_kosdaq150_cross_check_success_allows_alphanumeric_member():
    tiger = SourceSnapshot(
        index_name="kosdaq150",
        source="tiger",
        as_of="2026-03-13",
        rows=_build_rows(150, special_member_code="0009K0", special_member_name="에임드바이오"),
        source_url="tiger",
    )
    kodex = SourceSnapshot(
        index_name="kosdaq150",
        source="kodex",
        as_of="2026-03-14",
        rows=_build_rows(150, special_member_code="0009K0", special_member_name="에임드바이오"),
        source_url="kodex",
    )

    rows, mismatches = build_canonical_constituents(tiger_snapshot=tiger, kodex_snapshot=kodex)

    assert len(mismatches) == 0
    assert sum(1 for row in rows if row.kind == "member") == 150
    assert any(row.code == "0009K0" and row.kind == "member" for row in rows)


def test_sync_fails_when_code_sets_differ(tmp_path):
    tiger_rows = _build_rows(200, special_member_code="0126Z0", special_member_name="삼성에피스홀딩스")
    kodex_rows = list(tiger_rows[:-2]) + [
        ConstituentRecord(code="999999", name="다른종목", kind="member"),
        tiger_rows[-1],
    ]

    summary = sync_index_constituents_proxy(
        index_names=["kospi200"],
        output_dir=tmp_path,
        tiger_fetcher=DummyFetcher(
            {"kospi200": SourceSnapshot("kospi200", "tiger", "2026-03-13", tiger_rows, "tiger")}
        ),
        kodex_fetcher=DummyFetcher(
            {"kospi200": SourceSnapshot("kospi200", "kodex", "2026-03-14", kodex_rows, "kodex")}
        ),
    )

    assert summary["status"] == "failed"
    assert summary["indexes"]["kospi200"]["status"] == "failed"
    assert "canonical code set mismatch" in summary["indexes"]["kospi200"]["error"]


def test_member_count_validation_fails_when_member_rows_missing(tmp_path):
    rows = _build_rows(200, special_member_code="0126Z0", special_member_name="삼성에피스홀딩스")
    bad_rows = list(rows[1:])  # member 1개 제거, auxiliary는 유지

    summary = sync_index_constituents_proxy(
        index_names=["kospi200"],
        output_dir=tmp_path,
        tiger_fetcher=DummyFetcher(
            {"kospi200": SourceSnapshot("kospi200", "tiger", "2026-03-13", bad_rows, "tiger")}
        ),
        kodex_fetcher=DummyFetcher(
            {"kospi200": SourceSnapshot("kospi200", "kodex", "2026-03-14", bad_rows, "kodex")}
        ),
    )

    assert summary["status"] == "failed"
    assert "member_count mismatch" in summary["indexes"]["kospi200"]["error"]


def test_name_mismatch_is_recorded_but_does_not_fail(tmp_path):
    tiger_rows = _build_rows(150, special_member_code="0009K0", special_member_name="에임드바이오")
    kodex_rows = list(tiger_rows)
    kodex_rows[10] = ConstituentRecord(code=kodex_rows[10].code, name="이름다름", kind="member")
    synced_at = KST.localize(datetime(2026, 3, 14, 8, 30))

    summary = sync_index_constituents_proxy(
        index_names=["kosdaq150"],
        output_dir=tmp_path,
        tiger_fetcher=DummyFetcher(
            {"kosdaq150": SourceSnapshot("kosdaq150", "tiger", "2026-03-13", tiger_rows, "tiger")}
        ),
        kodex_fetcher=DummyFetcher(
            {"kosdaq150": SourceSnapshot("kosdaq150", "kodex", "2026-03-14", kodex_rows, "kodex")}
        ),
        synced_at=synced_at,
    )

    meta = json.loads((tmp_path / "kosdaq150_constituents.meta.json").read_text(encoding="utf-8"))
    assert summary["status"] == "ok"
    assert meta["name_mismatch_count"] == 1
    assert meta["cross_check_match"] is True


def test_sync_dry_run_does_not_write_files(tmp_path):
    rows = _build_rows(150, special_member_code="0009K0", special_member_name="에임드바이오")

    summary = sync_index_constituents_proxy(
        index_names=["kosdaq150"],
        output_dir=tmp_path,
        tiger_fetcher=DummyFetcher(
            {"kosdaq150": SourceSnapshot("kosdaq150", "tiger", "2026-03-13", rows, "tiger")}
        ),
        kodex_fetcher=DummyFetcher(
            {"kosdaq150": SourceSnapshot("kosdaq150", "kodex", "2026-03-14", rows, "kodex")}
        ),
        dry_run=True,
    )

    assert summary["status"] == "dry_run"
    assert not (tmp_path / "kosdaq150_constituents.txt").exists()
    assert not (tmp_path / "kosdaq150_constituents.meta.json").exists()


def test_sync_preserves_last_known_good_on_failure(tmp_path):
    existing = tmp_path / "kospi200_constituents.txt"
    existing.write_text("# old\n005930\t삼성전자\tmember\n", encoding="utf-8")
    (tmp_path / "kospi200_constituents.meta.json").write_text('{"status":"ok"}\n', encoding="utf-8")

    summary = sync_index_constituents_proxy(
        index_names=["kospi200"],
        output_dir=tmp_path,
        tiger_fetcher=DummyFetcher(
            {"kospi200": SourceSnapshot("kospi200", "tiger", "2026-03-13", [], "tiger")}
        ),
        kodex_fetcher=DummyFetcher(
            {"kospi200": SourceSnapshot("kospi200", "kodex", "2026-03-14", [], "kodex")}
        ),
    )

    assert summary["status"] == "failed"
    assert summary["indexes"]["kospi200"]["last_known_good_exists"] is True
    assert existing.read_text(encoding="utf-8") == "# old\n005930\t삼성전자\tmember\n"
