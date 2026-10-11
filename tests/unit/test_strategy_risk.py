"""Deterministic fixtures proving TRADE UP, TRADE DOWN and NO TRADE are all reachable,
plus risk-profile, sizing and governor invariants."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from predictionbot.fixedpoint import ZERO
from predictionbot.market.orderbook import OrderBook
from predictionbot.model.probability import SettlementModel
from predictionbot.portfolio.ledger import Ledger
from predictionbot.risk.governor import OpsStatus, Proposal, RiskGovernor, effective_profile
from predictionbot.risk.profiles import (
    AGGRESSIVE,
    BALANCED,
    HARD_MAX_SINGLE_ORDER_PCT,
    PROFILES,
    SAFE,
    ProfileName,
    RiskProfile,
)
from predictionbot.risk.sizing import size_order
from predictionbot.strategy.evaluator import evaluate
from predictionbot.strategy.fees import taker_fee
from tests.conftest import feed_with_history, make_book, make_market

D = Decimal


def _setup(strike: float, yes_bid: str, no_bid: str, depth: str = "200", seconds_to_close: float = 300):
    now = time.time()
    feed = feed_with_history(spot=100000, now=now)
    mk = make_market(now=now, strike=strike, seconds_to_close=seconds_to_close)
    pred = SettlementModel(feed).predict(mk, now)
    book = make_book(mk.ticker, [(yes_bid, depth)], [(no_bid, depth)])
    return mk, book, pred, now, feed


def test_trade_up_fixture() -> None:
    # Reference well above strike → model P(YES) ≈ 0.95+, market asks only 0.60 for YES.
    mk, book, pred, now, _feed = _setup(99700, "0.57", "0.40")
    ev = evaluate(mk, book, pred, BALANCED, verified=True, cf_fresh=True, now=now)
    assert pred.state == "READY" and pred.raw_prob_yes > 0.85
    assert ev.decision == "TRADE_UP", ev.reasons
    assert ev.chosen.outcome == "yes" and ev.chosen.adjusted_ev >= BALANCED.min_adjusted_edge


def test_trade_down_fixture() -> None:
    mk, book, pred, now, _feed = _setup(100300, "0.40", "0.57")
    ev = evaluate(mk, book, pred, BALANCED, verified=True, cf_fresh=True, now=now)
    assert ev.decision == "TRADE_DOWN", ev.reasons
    assert ev.chosen.outcome == "no"


def test_no_trade_when_fairly_priced() -> None:
    mk, book, pred, now, _feed = _setup(100000, "0.48", "0.49")
    ev = evaluate(mk, book, pred, BALANCED, verified=True, cf_fresh=True, now=now)
    assert ev.decision == "NO_TRADE"
    assert ev.reasons[0].split(":")[0] in ("NO_POSITIVE_ADJUSTED_EV", "EDGE_TOO_LOW")


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda c: c.update(book=None), "ORDERBOOK_MISSING"),
        (lambda c: c["book"].invalidate("SEQUENCE_GAP"), "ORDERBOOK_SEQUENCE_GAP"),
        (lambda c: setattr(c["book"], "last_confirmed_ts", time.time() - 600), "ORDERBOOK_STALE"),
        (lambda c: setattr(c["book"], "source", "rest"), "ORDERBOOK_STALE"),
        (lambda c: c.update(cf_fresh=False), "CF_STALE"),
        (lambda c: c.update(verified=False), "MARKET_NOT_VERIFIED"),
        (lambda c: setattr(c["mk"], "status", "closed"), "INVALID_MARKET_STATE"),
        (lambda c: c.update(pred=None), "MODEL_WARMING_UP"),
    ],
)
def test_no_trade_reasons(mutate, code: str) -> None:
    mk, book, pred, now, _feed = _setup(99700, "0.57", "0.40")
    ctx = {"mk": mk, "book": book, "pred": pred, "cf_fresh": True, "verified": True}
    mutate(ctx)
    ev = evaluate(
        ctx["mk"], ctx["book"], ctx["pred"], BALANCED, verified=ctx["verified"], cf_fresh=ctx["cf_fresh"], now=now
    )
    assert ev.decision == "NO_TRADE"
    assert any(r.startswith(code) for r in ev.reasons), ev.reasons


def test_wide_spread_and_thin_liquidity() -> None:
    mk, book, pred, now, _feed = _setup(99700, "0.30", "0.40")  # yes ask 0.60, bid 0.30 → spread 0.30
    ev = evaluate(mk, book, pred, BALANCED, verified=True, cf_fresh=True, now=now)
    assert any(r.startswith("SPREAD_TOO_WIDE") for r in ev.reasons)
    mk, book, pred, now, _feed = _setup(99700, "0.57", "0.40", depth="2")
    ev = evaluate(mk, book, pred, BALANCED, verified=True, cf_fresh=True, now=now)
    assert ev.decision == "NO_TRADE" and any("INSUFFICIENT_LIQUIDITY" in r for r in ev.reasons)


def test_too_close_to_close() -> None:
    mk, book, pred, now, _feed = _setup(99700, "0.57", "0.40", seconds_to_close=10)
    ev = evaluate(mk, book, pred, BALANCED, verified=True, cf_fresh=True, now=now)
    assert any(r.startswith("TOO_CLOSE_TO_CLOSE") for r in ev.reasons)


def test_profiles_share_everything_but_risk_envelope() -> None:
    assert SAFE.min_adjusted_edge > BALANCED.min_adjusted_edge > AGGRESSIVE.min_adjusted_edge > 0
    assert SAFE.kelly_fraction < BALANCED.kelly_fraction < AGGRESSIVE.kelly_fraction <= D("0.5")
    assert SAFE.min_cash_reserve_pct > BALANCED.min_cash_reserve_pct > AGGRESSIVE.min_cash_reserve_pct
    for p in PROFILES.values():
        assert p.max_single_order_pct <= HARD_MAX_SINGLE_ORDER_PCT
        assert p.high_confidence_override_enabled is False
    with pytest.raises(ValueError):
        RiskProfile(**{**BALANCED.__dict__, "max_single_order_pct": D("0.31")})
    with pytest.raises(ValueError):
        RiskProfile(**{**BALANCED.__dict__, "kelly_fraction": D("1.0")})


def test_sizing_caps_and_never_full_bankroll() -> None:
    r = size_order(p_cons=D("0.99"), all_in_cost=D("0.50"), bankroll=D("1000"), profile=AGGRESSIVE, depth=D("100000"))
    assert r.count * D("0.50") <= D("300")  # 30% ceiling
    assert r.binding == "single_order_cap"
    r = size_order(p_cons=D("0.62"), all_in_cost=D("0.55"), bankroll=D("1000"), profile=BALANCED, depth=D("100000"))
    assert r.binding == "kelly" and r.count * D("0.55") < D("120")
    r = size_order(p_cons=D("0.99"), all_in_cost=D("0.50"), bankroll=D("1000"), profile=AGGRESSIVE, depth=D("7.9"))
    assert r.count == D("7") and r.binding == "depth"
    assert (
        size_order(p_cons=D("0.5"), all_in_cost=D("0.55"), bankroll=D("1000"), profile=BALANCED, depth=D("50")).count
        == 0
    )
    # High-confidence override cannot exceed 30% even if "validated"
    r = size_order(
        p_cons=D("0.97"),
        all_in_cost=D("0.50"),
        bankroll=D("1000"),
        profile=AGGRESSIVE,
        depth=D("1e6"),
        high_confidence_validated=True,
    )
    assert r.count * D("0.50") <= D("300")


def _ledger(cash: str = "1000") -> Ledger:
    led = Ledger("PAPER", None, D(cash))
    led.start_session()
    return led


def _prop(
    count: str = "100", price: str = "0.50", ticker: str = "T1", asset: str = "BTC", direction: str = "UP"
) -> Proposal:
    return Proposal(ticker, asset, "yes", direction, D(count), D(price))


def test_governor_fail_closed() -> None:
    g = RiskGovernor()
    led = _ledger()
    for ops, code in [
        (OpsStatus(kill_switch=True), "KILL_SWITCH_ACTIVE"),
        (OpsStatus(ws_healthy=False), "WS_UNHEALTHY"),
        (OpsStatus(portfolio_reconciled=False), "PORTFOLIO_NOT_RECONCILED"),
        (OpsStatus(unresolved_orders=1), "UNRESOLVED_ORDERS"),
        (OpsStatus(db_healthy=False), "DATABASE_UNHEALTHY"),
        (OpsStatus(api_healthy=False), "API_UNHEALTHY"),
    ]:
        d = g.evaluate(_prop(), led, ProfileName.BALANCED, ops)
        assert d.verdict == "REJECTED" and any(r.startswith(code) for r in d.reasons)


def test_governor_reduces_to_limits() -> None:
    g = RiskGovernor()
    led = _ledger()
    d = g.evaluate(_prop("1000"), led, ProfileName.BALANCED, OpsStatus())
    assert d.verdict == "REDUCED"
    assert _prop("1000").cost(d.approved_count) <= D("1000") * BALANCED.max_single_order_pct


def test_correlated_exposure_limit() -> None:
    g = RiskGovernor()
    led = _ledger()
    led.apply_fill(ticker="ETH1", outcome="yes", asset="ETH", direction="UP", count=D("300"), price=D("0.50"), fee=ZERO)
    led.apply_fill(ticker="SOL1", outcome="yes", asset="SOL", direction="UP", count=D("300"), price=D("0.50"), fee=ZERO)
    d = g.evaluate(_prop("100", direction="UP"), led, ProfileName.BALANCED, OpsStatus())
    assert d.verdict == "REJECTED" and any("correlated" in r for r in d.reasons)
    d2 = g.evaluate(_prop("40", direction="DOWN"), led, ProfileName.BALANCED, OpsStatus())
    assert d2.approved


def test_loss_limits_halt_and_no_martingale() -> None:
    g = RiskGovernor()
    led = _ledger()
    before = g.evaluate(_prop("1000"), led, ProfileName.AGGRESSIVE, OpsStatus()).approved_count
    # lose 12% of equity across consecutive losses
    for i in range(3):
        led.apply_fill(
            ticker=f"L{i}", outcome="yes", asset="BTC", direction="UP", count=D("80"), price=D("0.50"), fee=ZERO
        )
        led.settle_market(f"L{i}", "no", source="test")
    assert led.consecutive_losses == 3
    after = g.evaluate(_prop("1000"), led, ProfileName.AGGRESSIVE, OpsStatus())
    assert after.approved_count <= before  # never sizes up after losses
    eff, notes = effective_profile(ProfileName.AGGRESSIVE, led, OpsStatus())
    assert eff.name != ProfileName.AGGRESSIVE and notes  # downgraded, never upgraded
    for i in range(3, 8):
        led.apply_fill(
            ticker=f"L{i}", outcome="yes", asset="BTC", direction="UP", count=D("80"), price=D("0.50"), fee=ZERO
        )
        led.settle_market(f"L{i}", "no", source="test")
    halted = g.evaluate(_prop("10"), led, ProfileName.SAFE, OpsStatus())
    assert halted.verdict == "REJECTED" and any("LIMIT" in r for r in halted.reasons)


def test_fee_model() -> None:
    assert taker_fee(D("100"), D("0.50")) == D("1.75")  # 0.07*100*.25
    assert taker_fee(D("1"), D("0.50")) == D("0.02")  # 0.0175 rounded up to the cent
    assert taker_fee(D("0"), D("0.50")) == ZERO


def test_orders_per_market_cap() -> None:
    g = RiskGovernor()
    d = g.evaluate(_prop(), _ledger(), ProfileName.SAFE, OpsStatus(orders_in_market=1))
    assert d.verdict == "REJECTED" and any(r.startswith("MAX_ORDERS_PER_MARKET") for r in d.reasons)


def test_book_unused_import_guard() -> None:
    assert OrderBook  # keep import used for type clarity in fixtures
