"""Opportunity evaluation: fair probability vs EXECUTABLE price → adjusted EV.

Separation of concerns:
  DIRECTIONAL FORECAST / FAIR PROBABILITY  → model.probability.Prediction
  MARKET MISPRICING (raw edge)             → p_raw(side) − best ask(side)
  EXPECTED VALUE (adjusted)                → p_cons(side) − limit price − fee/contract
  TRADE DECISION                           → thresholds here, then sizing + RiskGovernor

YES = "UP" (the reference finishes at/above the strike for greater-type
markets), NO = "DOWN". The strategy only takes liquidity with
immediate-or-cancel limit orders; the limit sits `latency_ticks` above the
current best ask so a small adverse move between decision and arrival is
paid for *in the EV*, and a larger one results in no fill rather than a
worse price.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from predictionbot.fixedpoint import ZERO, D
from predictionbot.market.models import MarketInfo
from predictionbot.market.orderbook import BookState, OrderBook
from predictionbot.model.probability import Prediction
from predictionbot.risk.profiles import RiskProfile
from predictionbot.strategy.fees import taker_fee
from predictionbot.strategy.reasons import Reason


@dataclass(frozen=True)
class ExecutionAssumptions:
    latency_ticks: int = 1  # adverse ticks paid for between decision and arrival
    depth_haircut: Decimal = Decimal("0.5")  # assume only half of displayed depth is really ours
    reference_size: Decimal = Decimal("10")  # contracts used to evaluate fees/slippage pre-sizing
    book_stale_s: float = 45.0
    min_seconds_after_open: float = 5.0


@dataclass
class SideAnalysis:
    outcome: str  # "yes" | "no"
    best_ask: Decimal | None = None
    limit_price: Decimal | None = None
    depth_at_limit: Decimal = ZERO
    p_raw: float | None = None
    p_cons: float | None = None
    raw_edge: float | None = None
    fee_per_contract: Decimal | None = None
    slippage_per_contract: Decimal | None = None
    adjusted_ev: Decimal | None = None
    edge_to_cost: Decimal | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return not self.reasons and self.adjusted_ev is not None


@dataclass
class Evaluation:
    ticker: str
    ts: float
    decision: str  # TRADE_UP | TRADE_DOWN | NO_TRADE
    reasons: list[str]
    yes: SideAnalysis | None = None
    no: SideAnalysis | None = None
    chosen: SideAnalysis | None = None
    profile: str = ""
    spread: Decimal | None = None
    seconds_to_close: float | None = None

    @property
    def primary_reason(self) -> str:
        return self.reasons[0] if self.reasons else self.decision

    def public(self) -> dict[str, Any]:
        def side(s: SideAnalysis | None) -> dict[str, Any] | None:
            if s is None:
                return None
            return {
                "outcome": s.outcome,
                "best_ask": _s(s.best_ask),
                "limit_price": _s(s.limit_price),
                "depth_at_limit": _s(s.depth_at_limit),
                "p_raw": s.p_raw,
                "p_cons": s.p_cons,
                "raw_edge": s.raw_edge,
                "fee_per_contract": _s(s.fee_per_contract),
                "adjusted_ev": _s(s.adjusted_ev),
                "edge_to_cost": _s(s.edge_to_cost),
                "reasons": s.reasons,
            }

        return {
            "ticker": self.ticker,
            "ts": self.ts,
            "decision": self.decision,
            "reasons": self.reasons,
            "primary_reason": self.primary_reason,
            "profile": self.profile,
            "spread": _s(self.spread),
            "seconds_to_close": self.seconds_to_close,
            "yes": side(self.yes),
            "no": side(self.no),
        }


def _s(x: Decimal | None) -> str | None:
    return str(x) if x is not None else None


def _model_gate(pred: Prediction | None) -> list[str]:
    if pred is None:
        return [Reason.MODEL_WARMING_UP.value]
    if pred.state == "READY":
        return []
    detail = ";".join(pred.missing) if pred.missing else ""
    if pred.state == "FAILED":
        return [f"{Reason.MODEL_FAILED.value}:{detail}"]
    if pred.state == "DEGRADED":
        code = Reason.CF_STALE.value if "CF_STALE" in detail else Reason.MODEL_DEGRADED.value
        return [f"{code}:{detail}"]
    if detail.startswith(Reason.INSUFFICIENT_HISTORY.value):
        return [detail]
    if "CF_NO_DATA" in detail or "FINAL_WINDOW" in detail:
        return [f"{Reason.MODEL_INPUT_MISSING.value}:{detail}"]
    return [f"{Reason.MODEL_WARMING_UP.value}:{detail}"]


def _book_gate(book: OrderBook | None, now: float, stale_s: float) -> list[str]:
    if book is None:
        return [Reason.ORDERBOOK_MISSING.value]
    if book.state == BookState.AWAITING_SNAPSHOT:
        return [f"{Reason.ORDERBOOK_MISSING.value}:awaiting snapshot"]
    if book.state == BookState.INVALID:
        if book.invalid_reason == "SEQUENCE_GAP":
            return [f"{Reason.ORDERBOOK_SEQUENCE_GAP.value}:resyncing"]
        return [f"{Reason.ORDERBOOK_INVALID.value}:{book.invalid_reason}"]
    age = book.age(now)
    if age is None or age > stale_s:
        return [f"{Reason.ORDERBOOK_STALE.value}:age={age}"]
    if book.source != "ws":
        return [f"{Reason.ORDERBOOK_STALE.value}:source={book.source} (realtime book required)"]
    return []


def analyze_side(
    outcome: str,
    market: MarketInfo,
    book: OrderBook,
    pred: Prediction,
    profile: RiskProfile,
    assumptions: ExecutionAssumptions,
) -> SideAnalysis:
    sa = SideAnalysis(outcome=outcome)
    sa.p_raw = (
        pred.raw_prob_yes if outcome == "yes" else (1.0 - pred.raw_prob_yes if pred.raw_prob_yes is not None else None)
    )
    sa.p_cons = pred.cons_prob_yes if outcome == "yes" else pred.cons_prob_no
    ask = book.yes_ask() if outcome == "yes" else book.no_ask()
    sa.best_ask = ask
    if ask is None or sa.p_raw is None or sa.p_cons is None:
        sa.reasons.append(f"{Reason.INSUFFICIENT_LIQUIDITY.value}:no asks for {outcome}")
        return sa
    sa.raw_edge = round(sa.p_raw - float(ask), 6)
    tick = market.tick_at(ask)
    limit = market.round_price(ask + tick * assumptions.latency_ticks, up=True)
    if limit is None:
        limit = ask
    sa.limit_price = limit
    if limit > profile.max_entry_price or ask < profile.min_entry_price:
        sa.reasons.append(f"{Reason.PRICE_OUT_OF_BOUNDS.value}:ask={ask} limit={limit}")
    depth = book.depth_at_or_better(outcome, limit) * assumptions.depth_haircut
    sa.depth_at_limit = depth
    if depth < profile.minimum_liquidity:
        sa.reasons.append(f"{Reason.INSUFFICIENT_LIQUIDITY.value}:depth={depth} < {profile.minimum_liquidity}")
    ref = min(assumptions.reference_size, max(depth, D("1")))
    ref = ref.quantize(D("1")) if ref >= 1 else D("1")
    fee = taker_fee(ref, limit, market.fee_multiplier, market.fee_type) / ref
    sa.fee_per_contract = fee.quantize(D("0.000001"))
    sa.slippage_per_contract = limit - ask
    p_cons = D(repr(round(sa.p_cons, 6)))
    all_in = limit + fee
    sa.adjusted_ev = (p_cons - all_in).quantize(D("0.000001"))
    sa.edge_to_cost = (sa.adjusted_ev / all_in).quantize(D("0.0001")) if all_in > 0 else None
    if sa.adjusted_ev <= ZERO:
        sa.reasons.append(f"{Reason.NO_POSITIVE_ADJUSTED_EV.value}:{sa.adjusted_ev}")
    elif sa.adjusted_ev < profile.min_adjusted_edge or (
        sa.edge_to_cost is not None and sa.edge_to_cost < profile.min_edge_to_cost
    ):
        sa.reasons.append(
            f"{Reason.EDGE_TOO_LOW.value}:ev={sa.adjusted_ev} < {profile.min_adjusted_edge} "
            f"or ev/cost={sa.edge_to_cost} < {profile.min_edge_to_cost}"
        )
    return sa


def evaluate(
    market: MarketInfo,
    book: OrderBook | None,
    pred: Prediction | None,
    profile: RiskProfile,
    *,
    verified: bool,
    cf_fresh: bool,
    assumptions: ExecutionAssumptions | None = None,
    now: float | None = None,
) -> Evaluation:
    a = assumptions or ExecutionAssumptions()
    now = time.time() if now is None else now
    ev = Evaluation(ticker=market.ticker, ts=now, decision="NO_TRADE", reasons=[], profile=profile.name.value)
    tau = market.seconds_to_close(now)
    ev.seconds_to_close = round(tau, 1)
    gates: list[str] = []
    if not market.is_open_now(now):
        gates.append(f"{Reason.INVALID_MARKET_STATE.value}:status={market.status}")
    if not verified:
        gates.append(Reason.MARKET_NOT_VERIFIED.value)
    if not market.cf_index_id:
        gates.append(Reason.CF_MAPPING_MISSING.value)
    elif not cf_fresh:
        gates.append(f"{Reason.CF_STALE.value}:{market.cf_index_id}")
    if tau < profile.min_seconds_to_close:
        gates.append(f"{Reason.TOO_CLOSE_TO_CLOSE.value}:{tau:.0f}s<{profile.min_seconds_to_close:.0f}s")
    if tau > profile.max_seconds_to_close or now - market.open_ts < a.min_seconds_after_open:
        gates.append(f"{Reason.TOO_EARLY_IN_MARKET.value}:{tau:.0f}s to close")
    gates.extend(_book_gate(book, now, a.book_stale_s))
    gates.extend(_model_gate(pred))
    if gates:
        ev.reasons = gates
        if book is not None:
            ev.spread = book.spread()
        return ev
    assert book is not None and pred is not None
    ev.spread = book.spread()
    model_reasons: list[str] = []
    if pred.disagreement is not None and pred.disagreement > profile.maximum_model_disagreement:
        model_reasons.append(f"{Reason.MODEL_DISAGREEMENT.value}:{pred.disagreement:.3f}")
    if pred.uncertainty is not None and pred.uncertainty > profile.maximum_model_uncertainty:
        model_reasons.append(f"{Reason.MODEL_UNCERTAINTY_TOO_HIGH.value}:{pred.uncertainty:.3f}")
    if ev.spread is None or ev.spread > profile.maximum_spread:
        model_reasons.append(f"{Reason.SPREAD_TOO_WIDE.value}:{ev.spread}")
    ev.yes = analyze_side("yes", market, book, pred, profile, a)
    ev.no = analyze_side("no", market, book, pred, profile, a)
    if model_reasons:
        ev.reasons = model_reasons
        return ev
    candidates = [s for s in (ev.yes, ev.no) if s.passes]
    if not candidates:
        best = max((ev.yes, ev.no), key=lambda s: s.adjusted_ev if s.adjusted_ev is not None else D("-9"))
        ev.reasons = best.reasons or [Reason.NO_POSITIVE_ADJUSTED_EV.value]
        return ev
    chosen = max(candidates, key=lambda s: s.adjusted_ev or ZERO)
    ev.chosen = chosen
    ev.decision = Reason.TRADE_UP.value if chosen.outcome == "yes" else Reason.TRADE_DOWN.value
    ev.reasons = []
    return ev
