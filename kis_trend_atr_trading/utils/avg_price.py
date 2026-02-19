"""Average-price helpers for execution and reconciliation layers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


PRICE_SCALE = Decimal("0.01")


def _to_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError(f"{field_name} is required")

    raw = str(value).strip().replace(",", "")
    if not raw:
        raise ValueError(f"{field_name} is empty")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal: {value}") from exc


def quantize_price(d: Decimal) -> Decimal:
    """Round price to scale=2 using ROUND_HALF_UP."""
    dec = _to_decimal(d, "price")
    return dec.quantize(PRICE_SCALE, rounding=ROUND_HALF_UP)


def calc_weighted_avg(
    existing_avg: Any,
    existing_qty: int,
    fill_price: Any,
    fill_qty: int,
) -> Decimal:
    """
    Calculate weighted average price.

    Formula:
      (existing_avg*existing_qty + fill_price*fill_qty) / (existing_qty + fill_qty)
    """
    if fill_qty <= 0:
        raise ValueError(f"fill_qty must be > 0, got {fill_qty}")
    if existing_qty < 0:
        raise ValueError(f"existing_qty must be >= 0, got {existing_qty}")

    fill_price_dec = quantize_price(_to_decimal(fill_price, "fill_price"))
    if existing_qty == 0:
        return fill_price_dec

    existing_avg_dec = quantize_price(_to_decimal(existing_avg, "existing_avg"))
    total_qty = existing_qty + fill_qty
    weighted = (
        (existing_avg_dec * Decimal(existing_qty))
        + (fill_price_dec * Decimal(fill_qty))
    ) / Decimal(total_qty)
    return quantize_price(weighted)


def reduce_quantity_after_sell(existing_qty: int, sell_qty: int) -> int:
    """Return remaining quantity after a sell fill."""
    if existing_qty < 0:
        raise ValueError(f"existing_qty must be >= 0, got {existing_qty}")
    if sell_qty <= 0:
        raise ValueError(f"sell_qty must be > 0, got {sell_qty}")
    remaining = existing_qty - sell_qty
    if remaining < 0:
        raise ValueError(
            f"sell_qty exceeds existing quantity: existing={existing_qty}, sell={sell_qty}"
        )
    return remaining
