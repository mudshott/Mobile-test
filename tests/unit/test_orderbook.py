from __future__ import annotations

from decimal import Decimal

from predictionbot.market.orderbook import BookState, OrderBook
from tests.conftest import make_book

D = Decimal


def test_complement_pricing() -> None:
    b = make_book("T", [("0.4000", "10.00"), ("0.3900", "5.00")], [("0.5200", "4.00"), ("0.5000", "7.00")])
    assert b.best_yes_bid() == D("0.4000")
    assert b.best_no_bid() == D("0.5200")
    assert b.yes_ask() == D("0.4800")  # 1 - best NO bid
    assert b.no_ask() == D("0.6000")  # 1 - best YES bid
    assert b.spread() == D("0.0800")
    assert b.ask_levels("yes") == [(D("0.4800"), D("4.00")), (D("0.5000"), D("7.00"))]


def test_walk_buy_partial_and_limit() -> None:
    b = make_book("T", [("0.40", "10")], [("0.52", "4"), ("0.50", "7")])
    w = b.walk_buy("yes", D("20"), D("0.49"))
    assert w.count == D("4") and w.worst_price == D("0.48")
    w2 = b.walk_buy("yes", D("20"), D("0.50"))
    assert w2.count == D("11") and w2.cost == D("4") * D("0.48") + D("7") * D("0.50")
    w3 = b.walk_buy("yes", D("20"), D("0.50"), depth_haircut=D("0.5"))
    assert w3.count == D("5.50")


def test_delta_apply_and_remove() -> None:
    b = make_book("T", [("0.40", "10")], [("0.52", "4")])
    assert b.apply_delta({"price_dollars": "0.4000", "delta_fp": "2.00", "side": "yes"}, seq=2).ok
    assert b.yes[D("0.4000")] == D("12.00")
    assert b.apply_delta({"price_dollars": "0.4000", "delta_fp": "-12.00", "side": "yes"}, seq=3).ok
    assert D("0.4000") not in b.yes


def test_negative_level_invalidates() -> None:
    b = make_book("T", [("0.40", "10")], [("0.52", "4")])
    r = b.apply_delta({"price_dollars": "0.40", "delta_fp": "-11", "side": "yes"}, seq=2)
    assert not r.ok and r.need_resync and b.state == BookState.INVALID


def test_gap_invalidates_then_snapshot_recovers() -> None:
    b = make_book("T", [("0.40", "10")], [("0.52", "4")])
    r = b.apply_delta({"price_dollars": "0.40", "delta_fp": "1", "side": "yes"}, seq=5, gap=True)
    assert r.need_resync and b.invalid_reason == "SEQUENCE_GAP"
    # deltas ignored until snapshot
    assert not b.apply_delta({"price_dollars": "0.40", "delta_fp": "1", "side": "yes"}, seq=6).ok
    assert b.apply_snapshot({"yes_dollars_fp": [["0.41", "3"]], "no_dollars_fp": []}, seq=7).ok
    assert b.state == BookState.VALID and b.best_yes_bid() == D("0.41")


def test_malformed_and_crossed() -> None:
    b = OrderBook("T")
    r = b.apply_snapshot({"yes_dollars_fp": [["abc", "1"]], "no_dollars_fp": []}, seq=1)
    assert not r.ok and b.state == BookState.INVALID
    r = b.apply_snapshot({"yes_dollars_fp": [["0.60", "1"]], "no_dollars_fp": [["0.50", "1"]]}, seq=1)
    assert not r.ok and b.invalid_reason == "CROSSED_BOOK"
    b2 = make_book("T", [("0.40", "10")], [("0.52", "4")])
    assert not b2.apply_delta({"price_dollars": "1.20", "delta_fp": "1", "side": "yes"}, seq=2).ok
    assert b2.state == BookState.INVALID


def test_delta_before_snapshot_ignored() -> None:
    b = OrderBook("T")
    r = b.apply_delta({"price_dollars": "0.40", "delta_fp": "1", "side": "yes"}, seq=1)
    assert not r.ok and b.state == BookState.AWAITING_SNAPSHOT


def test_empty_book_has_no_prices() -> None:
    b = make_book("T", [], [])
    assert b.yes_ask() is None and b.no_ask() is None and b.spread() is None
