"""Position sizing: fractional, capped Kelly on the CONSERVATIVE probability.

For a binary contract bought at all-in cost c (price + fee per contract)
paying $1 with probability p, the Kelly fraction of bankroll is
    f* = (p − c) / (1 − c).
We stake kelly_fraction · f* · bankroll (kelly_fraction ≤ 0.5, never full
Kelly), then cap by the profile's single-order ceiling (≤ 30%), and by
available depth. The 30% ceiling is a ceiling — typical sizes are far below it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from predictionbot.fixedpoint import ONE, ZERO
from predictionbot.risk.profiles import HARD_MAX_SINGLE_ORDER_PCT, RiskProfile


@dataclass
class SizeResult:
    count: Decimal
    kelly_fraction_full: Decimal
    stake: Decimal
    binding: str


def size_order(
    *,
    p_cons: Decimal,
    all_in_cost: Decimal,
    bankroll: Decimal,
    profile: RiskProfile,
    depth: Decimal,
    high_confidence_validated: bool = False,
) -> SizeResult:
    if bankroll <= ZERO or all_in_cost <= ZERO or all_in_cost >= ONE or p_cons <= all_in_cost:
        return SizeResult(ZERO, ZERO, ZERO, "no_edge_or_no_bankroll")
    f_star = (p_cons - all_in_cost) / (ONE - all_in_cost)
    kelly_stake = profile.kelly_fraction * f_star * bankroll
    cap_pct = profile.max_single_order_pct
    if (
        high_confidence_validated
        and profile.high_confidence_override_enabled
        and float(p_cons) >= profile.high_confidence_min_probability
    ):
        cap_pct = max(cap_pct, profile.high_confidence_max_pct)
    cap_pct = min(cap_pct, HARD_MAX_SINGLE_ORDER_PCT)
    cap_stake = cap_pct * bankroll
    stake = min(kelly_stake, cap_stake)
    binding = "kelly" if kelly_stake <= cap_stake else "single_order_cap"
    count = (stake / all_in_cost).to_integral_value(rounding=ROUND_FLOOR)
    depth_whole = depth.to_integral_value(rounding=ROUND_FLOOR)
    if count > depth_whole:
        count = depth_whole
        binding = "depth"
    return SizeResult(max(ZERO, count), f_star, stake, binding)
