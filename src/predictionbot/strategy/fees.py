"""Kalshi trading fee model.

Series with `fee_type` "quadratic*" use the General Trading Fee schedule:
    fee = round_up( 0.07 × C × P × (1 − P) × fee_multiplier )
for taker fills (C contracts at price P). We round UP to the next cent per
order, which is never cheaper than Kalshi's finer rounding — i.e. our EV is
conservative. Maker fees (quadratic_with_maker_fees) use 0.0175; the strategy
only takes liquidity, so taker fees apply to every simulated fill.
"""

from __future__ import annotations

from decimal import Decimal

from predictionbot.fixedpoint import CENT, ZERO, ceil_to

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")


def taker_fee(
    contracts: Decimal, price: Decimal, multiplier: Decimal = Decimal("1"), fee_type: str = "quadratic"
) -> Decimal:
    if contracts <= ZERO:
        return ZERO
    if fee_type == "flat":
        # Flat-fee series are not part of the supported allowlist; be conservative.
        return ceil_to(TAKER_RATE * contracts * Decimal("0.25") * multiplier, CENT)
    raw = TAKER_RATE * contracts * price * (1 - price) * multiplier
    return ceil_to(raw, CENT)


def maker_fee(contracts: Decimal, price: Decimal, multiplier: Decimal = Decimal("1")) -> Decimal:
    if contracts <= ZERO:
        return ZERO
    return ceil_to(MAKER_RATE * contracts * price * (1 - price) * multiplier, CENT)
