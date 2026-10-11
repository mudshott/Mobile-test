"""Independent risk governor. The strategy proposes; the governor decides.

Verdicts: APPROVED, REDUCED (count lowered to fit limits) or REJECTED.
The governor never increases size and never raises the profile above the one
the operator selected. Poor conditions DOWNGRADE the effective profile.
There is deliberately no logic that increases risk after losses (no martingale).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from predictionbot.fixedpoint import ZERO
from predictionbot.portfolio.ledger import Ledger
from predictionbot.risk.profiles import HARD_MAX_SINGLE_ORDER_PCT, PROFILES, ProfileName, RiskProfile, downgrade
from predictionbot.strategy.fees import taker_fee


@dataclass
class Proposal:
    ticker: str
    asset: str
    outcome: str
    direction: str
    count: Decimal
    limit_price: Decimal
    fee_multiplier: Decimal = Decimal("1")
    fee_type: str = "quadratic"

    def cost(self, count: Decimal | None = None) -> Decimal:
        c = self.count if count is None else count
        return c * self.limit_price + taker_fee(c, self.limit_price, self.fee_multiplier, self.fee_type)


@dataclass
class OpsStatus:
    """Operational facts the governor needs; produced by the health monitor."""

    kill_switch: bool = False
    ws_healthy: bool = True
    portfolio_reconciled: bool = True
    unresolved_orders: int = 0
    db_healthy: bool = True
    api_healthy: bool = True
    orders_in_market: int = 0
    execution_reject_ratio: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class RiskDecision:
    verdict: str
    approved_count: Decimal
    requested_count: Decimal
    selected_profile: str
    effective_profile: str
    reasons: list[str]
    bankroll: Decimal
    snapshot: dict[str, Any]
    ts: float = field(default_factory=time.time)

    @property
    def approved(self) -> bool:
        return self.verdict in ("APPROVED", "REDUCED") and self.approved_count > 0


def effective_profile(selected: ProfileName, ledger: Ledger, ops: OpsStatus) -> tuple[RiskProfile, list[str]]:
    name = selected
    notes: list[str] = []
    prof = PROFILES[name]
    if ledger.drawdown_pct >= prof.max_session_drawdown_pct / 2:
        name = downgrade(name)
        notes.append(f"DOWNGRADE:drawdown {ledger.drawdown_pct:.3f}")
    if ledger.consecutive_losses >= PROFILES[selected].consecutive_loss_downgrade:
        name = downgrade(name)
        notes.append(f"DOWNGRADE:{ledger.consecutive_losses} consecutive losses")
    if ops.execution_reject_ratio > 0.5:
        name = downgrade(name)
        notes.append(f"DOWNGRADE:execution reject ratio {ops.execution_reject_ratio:.2f}")
    return PROFILES[name], notes


class RiskGovernor:
    def __init__(self) -> None:
        self.decisions = 0
        self.rejections = 0

    def evaluate(self, proposal: Proposal, ledger: Ledger, selected: ProfileName, ops: OpsStatus) -> RiskDecision:
        self.decisions += 1
        profile, notes = effective_profile(selected, ledger, ops)
        bankroll = ledger.equity
        reasons: list[str] = list(notes)
        snap: dict[str, Any] = {
            "equity": str(bankroll),
            "cash": str(ledger.cash),
            "available": str(ledger.available),
            "deployed": str(ledger.deployed),
            "reserved": str(ledger.reserved_total),
            "market_exposure": str(ledger.exposure_by_market(proposal.ticker)),
            "asset_exposure": str(ledger.exposure_by_asset(proposal.asset)),
            "direction_exposure": str(ledger.exposure_by_direction(proposal.direction)),
            "drawdown_pct": str(ledger.drawdown_pct),
            "session_loss_pct": str(ledger.session_loss_pct),
            "daily_loss_pct": str(ledger.daily_loss_pct),
        }

        def reject(code: str) -> RiskDecision:
            self.rejections += 1
            return RiskDecision(
                "REJECTED", ZERO, proposal.count, selected.value, profile.name.value, [*reasons, code], bankroll, snap
            )

        # ---- operational integrity: fail closed
        if ops.kill_switch:
            return reject("KILL_SWITCH_ACTIVE")
        if not ops.db_healthy:
            return reject("DATABASE_UNHEALTHY")
        if not ops.api_healthy:
            return reject("API_UNHEALTHY")
        if not ops.ws_healthy:
            return reject("WS_UNHEALTHY")
        if not ops.portfolio_reconciled:
            return reject("PORTFOLIO_NOT_RECONCILED")
        if ops.unresolved_orders > 0:
            return reject(f"UNRESOLVED_ORDERS:{ops.unresolved_orders}")
        if ops.orders_in_market >= profile.max_orders_per_market:
            return reject(f"MAX_ORDERS_PER_MARKET:{ops.orders_in_market}")
        # ---- loss limits (halt new risk)
        if ledger.session_loss_pct >= profile.max_session_drawdown_pct:
            return reject(f"SESSION_LOSS_LIMIT:{ledger.session_loss_pct:.4f}")
        if ledger.daily_loss_pct >= profile.max_daily_loss_pct:
            return reject(f"DAILY_LOSS_LIMIT:{ledger.daily_loss_pct:.4f}")
        if ledger.drawdown_pct >= profile.max_session_drawdown_pct:
            return reject(f"DRAWDOWN_LIMIT:{ledger.drawdown_pct:.4f}")
        if bankroll <= ZERO:
            return reject("NO_BANKROLL")
        if proposal.count <= ZERO:
            return reject("SIZE_ZERO")

        # ---- exposure budgets (dollars still available under each limit)
        single_pct = min(profile.max_single_order_pct, HARD_MAX_SINGLE_ORDER_PCT)
        budgets = {
            "single_order": single_pct * bankroll,
            "single_market": profile.max_single_market_pct * bankroll - ledger.exposure_by_market(proposal.ticker),
            "asset": profile.max_asset_exposure_pct * bankroll - ledger.exposure_by_asset(proposal.asset),
            "correlated": profile.max_correlated_exposure_pct * bankroll
            - ledger.exposure_by_direction(proposal.direction),
            "total": profile.max_total_exposure_pct * bankroll - ledger.deployed - ledger.reserved_total,
            "cash_reserve": ledger.available - profile.min_cash_reserve_pct * bankroll,
        }
        snap["budgets"] = {k: str(v.quantize(Decimal("0.01"))) for k, v in budgets.items()}
        binding = min(budgets, key=lambda k: budgets[k])
        budget = budgets[binding]
        if budget <= ZERO:
            return reject(f"LIMIT_EXHAUSTED:{binding}")
        count = proposal.count
        if proposal.cost(count) > budget:
            per = proposal.cost(Decimal(1))
            count = (budget / per).to_integral_value(rounding=ROUND_FLOOR)
            while count > 0 and proposal.cost(count) > budget:
                count -= 1
        if count <= ZERO:
            return reject(f"LIMIT_EXHAUSTED:{binding}")
        verdict = "APPROVED" if count == proposal.count else "REDUCED"
        if verdict == "REDUCED":
            reasons.append(f"REDUCED_BY:{binding}")
        return RiskDecision(verdict, count, proposal.count, selected.value, profile.name.value, reasons, bankroll, snap)
