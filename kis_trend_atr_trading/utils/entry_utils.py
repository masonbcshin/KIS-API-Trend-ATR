"""Helpers for BUY entry classification, caps, and KRX price alignment."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Tuple

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


ASSET_TYPE_STOCK = "STOCK"
ASSET_TYPE_ETF = "ETF"

DEFAULT_ETF_NAME_KEYWORDS: tuple[str, ...] = (
    "KODEX",
    "TIGER",
    "KOSEF",
    "KBSTAR",
    "KINDEX",
    "HANARO",
    "ARIRANG",
    "ACE",
    "SOL",
    "PLUS",
    "TIMEFOLIO",
    "TREX",
    "FOCUS",
    "KIWOOM",
    "KIS",
    "WON",
)


def _normalize_tokens(items: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in items or ():
        token = str(item or "").strip().upper()
        if token:
            normalized.append(token)
    return tuple(normalized)


@lru_cache(maxsize=1)
def _load_etf_detection_config() -> Tuple[set[str], tuple[str, ...]]:
    symbols: set[str] = set()
    keywords = DEFAULT_ETF_NAME_KEYWORDS

    if yaml is None:
        return symbols, keywords

    config_path = Path(__file__).resolve().parents[1] / "config" / "universe.yaml"
    if not config_path.exists():
        return symbols, keywords

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except Exception:
        return symbols, keywords

    section = payload.get("universe") or {}
    raw_symbols = section.get("etf_symbols") or []
    raw_keywords = section.get("etf_name_keywords") or []

    symbols = {
        str(item or "").strip().zfill(6)
        for item in raw_symbols
        if str(item or "").strip()
    }
    parsed_keywords = _normalize_tokens(raw_keywords)
    if parsed_keywords:
        keywords = parsed_keywords
    return symbols, keywords


def detect_asset_type(stock_code: str, stock_name: str = "") -> str:
    code = str(stock_code or "").strip().zfill(6)
    name = str(stock_name or "").strip().upper()
    etf_symbols, etf_keywords = _load_etf_detection_config()

    if code and code in etf_symbols:
        return ASSET_TYPE_ETF

    if name:
        for keyword in etf_keywords:
            if keyword and keyword in name:
                return ASSET_TYPE_ETF

    return ASSET_TYPE_STOCK


def compute_extension_pct(price: float, reference_price: float) -> float:
    try:
        px = float(price or 0.0)
        ref = float(reference_price or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if px <= 0 or ref <= 0:
        return 0.0
    return (px / ref) - 1.0


def get_tick_size(price: float, asset_type: str) -> int:
    px = float(price or 0.0)
    asset = str(asset_type or ASSET_TYPE_STOCK).upper()

    if asset == ASSET_TYPE_ETF:
        return 1 if px < 2000 else 5

    if px < 2000:
        return 1
    if px < 5000:
        return 5
    if px < 20000:
        return 10
    if px < 50000:
        return 50
    if px < 200000:
        return 100
    if px < 500000:
        return 500
    return 1000


def align_price_to_tick(price: float, asset_type: str, direction: str = "up") -> float:
    px = Decimal(str(float(price or 0.0)))
    if px <= 0:
        return 0.0

    tick = Decimal(str(get_tick_size(float(px), asset_type)))
    if tick <= 0:
        return float(px)

    if direction == "down":
        aligned = (px / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    else:
        aligned = (px / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return float(aligned)
