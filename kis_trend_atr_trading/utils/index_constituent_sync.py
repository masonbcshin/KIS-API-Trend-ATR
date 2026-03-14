"""ETF proxy constituent sync helpers for KOSPI200/KOSDAQ150."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import requests

from utils.logger import get_logger
from utils.market_hours import KST

logger = get_logger("index_constituent_sync")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REFERENCE_OUTPUT_DIR = REPO_ROOT / "data" / "reference"


@dataclass(frozen=True)
class ProxyIndexSpec:
    index_name: str
    expected_member_count: int
    tiger_fund_id: str
    tiger_page_url: str
    kodex_fund_id: str
    kodex_page_url: str


@dataclass(frozen=True)
class ConstituentRecord:
    code: str
    name: str
    kind: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SourceSnapshot:
    index_name: str
    source: str
    as_of: str
    rows: Sequence[ConstituentRecord]
    source_url: str
    detail_url: Optional[str] = None


INDEX_SPECS: Dict[str, ProxyIndexSpec] = {
    "kospi200": ProxyIndexSpec(
        index_name="kospi200",
        expected_member_count=200,
        tiger_fund_id="KR7102110004",
        tiger_page_url="https://investments.miraeasset.com/tigeretf/ko/product/search/detail/index.do?ksdFund=KR7102110004&otherPage=asset",
        kodex_fund_id="2ETF01",
        kodex_page_url="https://www.samsungfund.com/etf/product/view.do?id=2ETF01&isBanner=Y",
    ),
    "kosdaq150": ProxyIndexSpec(
        index_name="kosdaq150",
        expected_member_count=150,
        tiger_fund_id="KR7232080002",
        tiger_page_url="https://investments.miraeasset.com/tigeretf/ko/product/search/detail/index.do?ksdFund=KR7232080002&otherPage=asset",
        kodex_fund_id="2ETF54",
        kodex_page_url="https://www.samsungfund.com/etf/product/view.do?id=2ETF54",
    ),
}

TIGER_EXCEL_URL = "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/pdf/excel.do"
KODEX_PRODUCT_API_URL = "https://www.samsungfund.com/api/v1/kodex/product/{fund_id}.do"
KODEX_BASE_URL = "https://www.samsungfund.com"


class IndexConstituentSyncError(RuntimeError):
    """Base error for ETF proxy constituent sync failures."""


class ConstituentFetchError(IndexConstituentSyncError):
    """Raised when proxy source fetch fails."""


class ConstituentValidationError(IndexConstituentSyncError):
    """Raised when fetched proxy source data is unsafe to promote."""


class _TigerTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_cell = False
        self._current_cell: list[str] = []
        self._current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in ("td", "th"):
            self._in_cell = True
            self._current_cell = []
        elif tag == "tr":
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            value = _collapse_ws("".join(self._current_cell))
            self._current_row.append(value)
        elif tag == "tr" and self._current_row:
            self.rows.append(list(self._current_row))


def _collapse_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_date_token(raw: Any) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    digits = re.sub(r"[^0-9]", "", token)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return token


def classify_constituent_kind(code: str) -> str:
    return "auxiliary" if str(code or "").strip().upper().startswith("KRD") else "member"


def normalize_name_for_compare(name: str) -> str:
    return _collapse_ws(name)


def parse_tiger_constituent_rows(html: str) -> list[ConstituentRecord]:
    parser = _TigerTableParser()
    parser.feed(str(html or ""))
    rows: list[ConstituentRecord] = []
    for row in parser.rows:
        if len(row) < 3:
            continue
        if row[0].strip().lower() == "no":
            continue
        if not row[0].strip().isdigit():
            continue
        code = _collapse_ws(row[1])
        name = _collapse_ws(row[2])
        if not code or not name:
            continue
        rows.append(
            ConstituentRecord(
                code=code,
                name=name,
                kind=classify_constituent_kind(code),
            )
        )
    return rows


def extract_tiger_as_of(page_html: str) -> str:
    html = str(page_html or "")
    patterns = (
        r'<div class="sort-label">기준일</div>.*?name="endDate".*?value="(\d{4}\.\d{2}\.\d{2})"',
        r"기준일\s+(\d{4}\.\d{2}\.\d{2})\s+\d{2}:\d{2}:\d{2}",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.S)
        if match:
            return _normalize_date_token(match.group(1))
    raise ConstituentFetchError("TIGER constituent 기준일을 페이지에서 찾지 못했습니다.")


def parse_kodex_product_payload(payload: Mapping[str, Any], *, index_name: str) -> SourceSnapshot:
    if not isinstance(payload, Mapping):
        raise ConstituentValidationError(f"{index_name} KODEX payload must be a mapping")
    pdf = payload.get("pdf")
    if not isinstance(pdf, Mapping):
        raise ConstituentValidationError(f"{index_name} KODEX payload missing pdf section")
    rows_raw = pdf.get("list")
    if not isinstance(rows_raw, Sequence):
        raise ConstituentValidationError(f"{index_name} KODEX payload missing pdf.list rows")
    as_of = _normalize_date_token(pdf.get("gijunYMD"))
    if not as_of:
        raise ConstituentValidationError(f"{index_name} KODEX payload missing pdf.gijunYMD")
    rows: list[ConstituentRecord] = []
    for row in rows_raw:
        if not isinstance(row, Mapping):
            continue
        code = _collapse_ws(row.get("itmNo"))
        name = _collapse_ws(row.get("secNm"))
        if not code or not name:
            continue
        rows.append(
            ConstituentRecord(
                code=code,
                name=name,
                kind=classify_constituent_kind(code),
            )
        )
    detail_url = str(pdf.get("pdfExcelDownloadUrl") or "").strip()
    if detail_url.startswith("/"):
        detail_url = f"{KODEX_BASE_URL}{detail_url}"
    product = payload.get("info") or {}
    product_info = product.get("product") if isinstance(product, Mapping) else {}
    source_url = str(
        product_info.get("fId")
        or INDEX_SPECS[index_name].kodex_page_url
    ).strip()
    return SourceSnapshot(
        index_name=index_name,
        source="kodex",
        as_of=as_of,
        rows=rows,
        source_url=INDEX_SPECS[index_name].kodex_page_url,
        detail_url=detail_url or None,
    )


def _canonicalize_rows(
    rows: Iterable[ConstituentRecord],
    *,
    index_name: str,
    source: str,
) -> Dict[str, ConstituentRecord]:
    mapping: Dict[str, ConstituentRecord] = {}
    for row in rows:
        code = _collapse_ws(getattr(row, "code", ""))
        name = _collapse_ws(getattr(row, "name", ""))
        if not code or not name:
            raise ConstituentValidationError(f"{index_name} {source} row missing code/name")
        if code in mapping:
            raise ConstituentValidationError(f"{index_name} {source} duplicate code: {code}")
        mapping[code] = ConstituentRecord(
            code=code,
            name=name,
            kind=classify_constituent_kind(code),
        )
    return mapping


def build_canonical_constituents(
    *,
    tiger_snapshot: SourceSnapshot,
    kodex_snapshot: SourceSnapshot,
) -> tuple[list[ConstituentRecord], list[Dict[str, str]]]:
    index_name = tiger_snapshot.index_name
    if tiger_snapshot.index_name != kodex_snapshot.index_name:
        raise ConstituentValidationError(
            f"source snapshots index mismatch: tiger={tiger_snapshot.index_name} kodex={kodex_snapshot.index_name}"
        )
    tiger_rows = _canonicalize_rows(tiger_snapshot.rows, index_name=index_name, source="tiger")
    kodex_rows = _canonicalize_rows(kodex_snapshot.rows, index_name=index_name, source="kodex")

    tiger_codes = set(tiger_rows)
    kodex_codes = set(kodex_rows)
    if tiger_codes != kodex_codes:
        raise ConstituentValidationError(
            f"{index_name} canonical code set mismatch: "
            f"tiger_only={sorted(tiger_codes - kodex_codes)[:10]} "
            f"kodex_only={sorted(kodex_codes - tiger_codes)[:10]}"
        )

    name_mismatches: list[Dict[str, str]] = []
    ordered_codes = []
    seen_codes: set[str] = set()
    for row in tiger_snapshot.rows:
        code = _collapse_ws(row.code)
        if code and code not in seen_codes:
            ordered_codes.append(code)
            seen_codes.add(code)

    canonical_rows: list[ConstituentRecord] = []
    for code in ordered_codes:
        tiger_row = tiger_rows[code]
        kodex_row = kodex_rows[code]
        if normalize_name_for_compare(tiger_row.name) != normalize_name_for_compare(kodex_row.name):
            name_mismatches.append(
                {
                    "code": code,
                    "tiger_name": tiger_row.name,
                    "kodex_name": kodex_row.name,
                }
            )
            logger.warning(
                "[INDEX_PROXY_SYNC] name mismatch index=%s code=%s tiger=%s kodex=%s",
                index_name,
                code,
                tiger_row.name,
                kodex_row.name,
            )
        canonical_rows.append(
            ConstituentRecord(
                code=code,
                name=kodex_row.name or tiger_row.name,
                kind=classify_constituent_kind(code),
            )
        )

    expected_member_count = INDEX_SPECS[index_name].expected_member_count
    member_count = sum(1 for row in canonical_rows if row.kind == "member")
    if member_count != expected_member_count:
        raise ConstituentValidationError(
            f"{index_name} member_count mismatch: expected={expected_member_count} actual={member_count}"
        )
    return canonical_rows, name_mismatches


def render_constituent_txt(
    *,
    index_name: str,
    rows: Sequence[ConstituentRecord],
    synced_at: datetime,
    tiger_as_of: str,
    kodex_as_of: str,
) -> str:
    header = (
        f"# index={index_name} synced_at={synced_at.isoformat()} "
        f"tiger_as_of={tiger_as_of} kodex_as_of={kodex_as_of}"
    )
    body = "\n".join(
        f"{row.code}\t{row.name.replace(chr(9), ' ')}\t{row.kind}" for row in rows
    )
    return f"{header}\n{body}\n"


def build_metadata(
    *,
    index_name: str,
    rows: Sequence[ConstituentRecord],
    tiger_snapshot: SourceSnapshot,
    kodex_snapshot: SourceSnapshot,
    name_mismatches: Sequence[Mapping[str, str]],
    synced_at: datetime,
    output_path: Path,
) -> Dict[str, Any]:
    payload_bytes = "\n".join(
        f"{row.code}\t{row.name}\t{row.kind}" for row in rows
    ).encode("utf-8")
    member_count = sum(1 for row in rows if row.kind == "member")
    auxiliary_count = sum(1 for row in rows if row.kind == "auxiliary")
    tiger_member = sum(1 for row in tiger_snapshot.rows if row.kind == "member")
    tiger_aux = sum(1 for row in tiger_snapshot.rows if row.kind == "auxiliary")
    kodex_member = sum(1 for row in kodex_snapshot.rows if row.kind == "member")
    kodex_aux = sum(1 for row in kodex_snapshot.rows if row.kind == "auxiliary")
    return {
        "index_name": index_name,
        "status": "ok",
        "synced_at": synced_at.isoformat(),
        "source_dates": {
            "tiger_as_of": tiger_snapshot.as_of,
            "kodex_as_of": kodex_snapshot.as_of,
            "as_of_mismatch": tiger_snapshot.as_of != kodex_snapshot.as_of,
        },
        "source_counts": {
            "tiger": {
                "raw_rows": len(tiger_snapshot.rows),
                "member_count": tiger_member,
                "auxiliary_count": tiger_aux,
                "source_url": tiger_snapshot.source_url,
                "detail_url": tiger_snapshot.detail_url,
            },
            "kodex": {
                "raw_rows": len(kodex_snapshot.rows),
                "member_count": kodex_member,
                "auxiliary_count": kodex_aux,
                "source_url": kodex_snapshot.source_url,
                "detail_url": kodex_snapshot.detail_url,
            },
        },
        "cross_check_match": True,
        "member_count": member_count,
        "auxiliary_count": auxiliary_count,
        "name_mismatch_count": len(name_mismatches),
        "name_mismatches": list(name_mismatches),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "output_path": str(output_path),
    }


def artifact_paths(*, output_dir: Path, index_name: str) -> Dict[str, Path]:
    return {
        "txt": output_dir / f"{index_name}_constituents.txt",
        "meta": output_dir / f"{index_name}_constituents.meta.json",
    }


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


class TigerProxyConstituentFetcher:
    def __init__(self, *, session: Optional[requests.Session] = None, timeout: float = 20.0) -> None:
        self._session = session or requests.Session()
        self._timeout = float(timeout)
        self._headers = {"User-Agent": "Mozilla/5.0"}

    def fetch(self, *, index_name: str) -> SourceSnapshot:
        spec = INDEX_SPECS[index_name]
        logger.info("[INDEX_PROXY_SYNC] tiger fetch_start index=%s url=%s", index_name, spec.tiger_page_url)
        page_response = self._session.get(spec.tiger_page_url, headers=self._headers, timeout=self._timeout)
        page_response.raise_for_status()
        as_of = extract_tiger_as_of(page_response.text)
        download_response = self._session.post(
            TIGER_EXCEL_URL,
            headers={**self._headers, "Referer": spec.tiger_page_url},
            data={
                "ksdFund": spec.tiger_fund_id,
                "fixDate": as_of.replace("-", "."),
                "prfPrd": "Week01",
                "order": "SRD",
            },
            timeout=self._timeout,
        )
        download_response.raise_for_status()
        rows = parse_tiger_constituent_rows(download_response.text)
        return SourceSnapshot(
            index_name=index_name,
            source="tiger",
            as_of=as_of,
            rows=rows,
            source_url=spec.tiger_page_url,
            detail_url=TIGER_EXCEL_URL,
        )


class KodexProxyConstituentFetcher:
    def __init__(self, *, session: Optional[requests.Session] = None, timeout: float = 20.0) -> None:
        self._session = session or requests.Session()
        self._timeout = float(timeout)
        self._headers = {"User-Agent": "Mozilla/5.0"}

    def fetch(self, *, index_name: str) -> SourceSnapshot:
        spec = INDEX_SPECS[index_name]
        api_url = KODEX_PRODUCT_API_URL.format(fund_id=spec.kodex_fund_id)
        logger.info("[INDEX_PROXY_SYNC] kodex fetch_start index=%s url=%s", index_name, api_url)
        response = self._session.get(
            api_url,
            headers={**self._headers, "Referer": spec.kodex_page_url},
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return parse_kodex_product_payload(payload, index_name=index_name)


def sync_index_constituents_proxy(
    *,
    index_names: Sequence[str],
    output_dir: Path,
    tiger_fetcher: Any,
    kodex_fetcher: Any,
    dry_run: bool = False,
    synced_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    now_kst = synced_at or datetime.now(KST)
    summary: Dict[str, Any] = {
        "status": "ok",
        "output_dir": str(output_dir),
        "dry_run": bool(dry_run),
        "synced_at": now_kst.isoformat(),
        "indexes": {},
    }
    failed = False
    for index_name in index_names:
        paths = artifact_paths(output_dir=output_dir, index_name=index_name)
        try:
            tiger_snapshot = tiger_fetcher.fetch(index_name=index_name)
            kodex_snapshot = kodex_fetcher.fetch(index_name=index_name)
            rows, name_mismatches = build_canonical_constituents(
                tiger_snapshot=tiger_snapshot,
                kodex_snapshot=kodex_snapshot,
            )
            metadata = build_metadata(
                index_name=index_name,
                rows=rows,
                tiger_snapshot=tiger_snapshot,
                kodex_snapshot=kodex_snapshot,
                name_mismatches=name_mismatches,
                synced_at=now_kst,
                output_path=paths["txt"],
            )
            if not dry_run:
                write_text_atomic(
                    paths["txt"],
                    render_constituent_txt(
                        index_name=index_name,
                        rows=rows,
                        synced_at=now_kst,
                        tiger_as_of=tiger_snapshot.as_of,
                        kodex_as_of=kodex_snapshot.as_of,
                    ),
                )
                write_json_atomic(paths["meta"], metadata)
            summary["indexes"][index_name] = {
                "status": "dry_run" if dry_run else "ok",
                "output_txt": str(paths["txt"]),
                "output_meta": str(paths["meta"]),
                "replaced_last_known_good": not dry_run,
                "source_dates": metadata["source_dates"],
                "source_counts": metadata["source_counts"],
                "cross_check_match": metadata["cross_check_match"],
                "member_count": metadata["member_count"],
                "auxiliary_count": metadata["auxiliary_count"],
                "name_mismatch_count": metadata["name_mismatch_count"],
                "metadata": metadata,
            }
        except Exception as exc:
            failed = True
            summary["indexes"][index_name] = {
                "status": "failed",
                "error": str(exc),
                "output_txt": str(paths["txt"]),
                "output_meta": str(paths["meta"]),
                "replaced_last_known_good": False,
                "last_known_good_exists": paths["txt"].exists() or paths["meta"].exists(),
            }
            logger.error("[INDEX_PROXY_SYNC] sync_failed index=%s error=%s", index_name, exc)
    if failed:
        summary["status"] = "failed"
    elif dry_run:
        summary["status"] = "dry_run"
    return summary
