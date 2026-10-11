"""Exact decimal handling for Kalshi fixed-point fields.

Kalshi V2 uses strings everywhere: `*_dollars` prices ("0.5600") and `*_fp`
contract counts ("10.00", granularity 0.01). We never parse these as floats.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
COUNT_STEP = Decimal("0.01")
PRICE_QUANT = Decimal("0.0001")


class FixedPointError(ValueError):
    pass


def D(value: Any) -> Decimal:
    """Parse a Kalshi fixed-point value. Raises FixedPointError for junk."""
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, bool) or value is None:
        raise FixedPointError(f"not a number: {value!r}")
    elif isinstance(value, int):
        out = Decimal(value)
    elif isinstance(value, float):
        out = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            out = Decimal(value.strip())
        except InvalidOperation as exc:
            raise FixedPointError(f"not a number: {value!r}") from exc
    else:
        raise FixedPointError(f"unsupported type {type(value).__name__}")
    if not out.is_finite():
        raise FixedPointError(f"non-finite value: {value!r}")
    return out


def D_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return D(value)
    except FixedPointError:
        return None


def fmt_price(p: Decimal) -> str:
    """Request price string: 4 decimals (Kalshi accepts 2-4)."""
    if p <= ZERO or p >= ONE:
        raise FixedPointError(f"binary price must be inside (0,1): {p}")
    return str(p.quantize(PRICE_QUANT))


def fmt_count(c: Decimal) -> str:
    if c <= ZERO:
        raise FixedPointError(f"count must be positive: {c}")
    q = c.quantize(COUNT_STEP, rounding=ROUND_DOWN)
    if q != c:
        raise FixedPointError(f"count has finer than 0.01 granularity: {c}")
    return str(q)


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
