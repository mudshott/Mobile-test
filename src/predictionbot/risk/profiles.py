"""Risk profiles: identical data/model/execution, different risk envelope only.

All fractions are of the *current reconciled* bankroll. `max_single_order_pct`
is a CEILING, never a target; sizing normally lands well below it via
fractional Kelly on the conservative probability, depth and exposure limits.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

HARD_MAX_SINGLE_ORDER_PCT = Decimal("0.30")  # absolute ceiling for any profile


class ProfileName(StrEnum):
    SAFE = "SAFE_RETURNS"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE_RESEARCHED"


@dataclass(frozen=True)
class RiskProfile:
    name: ProfileName
    min_adjusted_edge: Decimal  # minimum adjusted EV per contract (dollars per $1 payout)
    min_edge_to_cost: Decimal  # minimum adjusted EV relative to all-in cost (return on risk)
    kelly_fraction: Decimal
    max_single_order_pct: Decimal
    max_single_market_pct: Decimal
    max_total_exposure_pct: Decimal
    max_asset_exposure_pct: Decimal
    max_correlated_exposure_pct: Decimal  # same-direction exposure across all crypto assets
    min_cash_reserve_pct: Decimal
    max_session_drawdown_pct: Decimal
    max_daily_loss_pct: Decimal
    minimum_liquidity: Decimal  # contracts available at/better than the limit price
    maximum_spread: Decimal
    maximum_model_disagreement: float
    maximum_model_uncertainty: float
    min_seconds_to_close: float
    max_seconds_to_close: float
    max_entry_price: Decimal  # never pay more than this per contract
    min_entry_price: Decimal
    max_orders_per_market: int
    consecutive_loss_downgrade: int  # after N consecutive losses, downgrade one level
    high_confidence_override_enabled: bool = False
    high_confidence_min_probability: float = 0.90
    high_confidence_max_pct: Decimal = HARD_MAX_SINGLE_ORDER_PCT

    def __post_init__(self) -> None:
        if self.max_single_order_pct > HARD_MAX_SINGLE_ORDER_PCT:
            raise ValueError("max_single_order_pct may not exceed the 30% hard ceiling")
        if self.high_confidence_max_pct > HARD_MAX_SINGLE_ORDER_PCT:
            raise ValueError("high-confidence size may not exceed the 30% hard ceiling")
        if not (Decimal(0) < self.kelly_fraction <= Decimal("0.5")):
            raise ValueError("kelly_fraction must be in (0, 0.5]: full Kelly is not allowed")

    def public(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


SAFE = RiskProfile(
    name=ProfileName.SAFE,
    min_adjusted_edge=Decimal("0.05"),
    min_edge_to_cost=Decimal("0.10"),
    kelly_fraction=Decimal("0.10"),
    max_single_order_pct=Decimal("0.05"),
    max_single_market_pct=Decimal("0.08"),
    max_total_exposure_pct=Decimal("0.20"),
    max_asset_exposure_pct=Decimal("0.10"),
    max_correlated_exposure_pct=Decimal("0.15"),
    min_cash_reserve_pct=Decimal("0.60"),
    max_session_drawdown_pct=Decimal("0.05"),
    max_daily_loss_pct=Decimal("0.05"),
    minimum_liquidity=Decimal("10"),
    maximum_spread=Decimal("0.05"),
    maximum_model_disagreement=0.04,
    maximum_model_uncertainty=0.10,
    min_seconds_to_close=45.0,
    max_seconds_to_close=840.0,
    max_entry_price=Decimal("0.90"),
    min_entry_price=Decimal("0.05"),
    max_orders_per_market=1,
    consecutive_loss_downgrade=3,
)

BALANCED = RiskProfile(
    name=ProfileName.BALANCED,
    min_adjusted_edge=Decimal("0.03"),
    min_edge_to_cost=Decimal("0.06"),
    kelly_fraction=Decimal("0.20"),
    max_single_order_pct=Decimal("0.12"),
    max_single_market_pct=Decimal("0.15"),
    max_total_exposure_pct=Decimal("0.40"),
    max_asset_exposure_pct=Decimal("0.20"),
    max_correlated_exposure_pct=Decimal("0.30"),
    min_cash_reserve_pct=Decimal("0.35"),
    max_session_drawdown_pct=Decimal("0.10"),
    max_daily_loss_pct=Decimal("0.10"),
    minimum_liquidity=Decimal("5"),
    maximum_spread=Decimal("0.08"),
    maximum_model_disagreement=0.06,
    maximum_model_uncertainty=0.15,
    min_seconds_to_close=30.0,
    max_seconds_to_close=870.0,
    max_entry_price=Decimal("0.93"),
    min_entry_price=Decimal("0.04"),
    max_orders_per_market=2,
    consecutive_loss_downgrade=4,
)

AGGRESSIVE = RiskProfile(
    name=ProfileName.AGGRESSIVE,
    min_adjusted_edge=Decimal("0.02"),
    min_edge_to_cost=Decimal("0.04"),
    kelly_fraction=Decimal("0.33"),
    max_single_order_pct=Decimal("0.30"),
    max_single_market_pct=Decimal("0.30"),
    max_total_exposure_pct=Decimal("0.60"),
    max_asset_exposure_pct=Decimal("0.35"),
    max_correlated_exposure_pct=Decimal("0.50"),
    min_cash_reserve_pct=Decimal("0.20"),
    max_session_drawdown_pct=Decimal("0.20"),
    max_daily_loss_pct=Decimal("0.20"),
    minimum_liquidity=Decimal("3"),
    maximum_spread=Decimal("0.10"),
    maximum_model_disagreement=0.08,
    maximum_model_uncertainty=0.20,
    min_seconds_to_close=20.0,
    max_seconds_to_close=880.0,
    max_entry_price=Decimal("0.95"),
    min_entry_price=Decimal("0.03"),
    max_orders_per_market=3,
    consecutive_loss_downgrade=5,
)

PROFILES: dict[ProfileName, RiskProfile] = {p.name: p for p in (SAFE, BALANCED, AGGRESSIVE)}
ORDER: tuple[ProfileName, ...] = (ProfileName.SAFE, ProfileName.BALANCED, ProfileName.AGGRESSIVE)


def downgrade(name: ProfileName) -> ProfileName:
    i = ORDER.index(name)
    return ORDER[max(0, i - 1)]


def with_high_confidence(profile: RiskProfile, enabled: bool) -> RiskProfile:
    """Only the validation system may enable the override (after calibration evidence)."""
    return replace(profile, high_confidence_override_enabled=enabled)
