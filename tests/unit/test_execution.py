"""PAPER fill realism and the LIVE V2 order path (mocked HTTP; no real orders)."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest

from predictionbot.execution.base import OrderIntent
from predictionbot.execution.live import LiveExecutor, LiveNotArmedError, build_create_order_body, normalize_fill
from predictionbot.execution.paper import PaperExecutor, ShadowExecutor
from predictionbot.fixedpoint import ZERO
from predictionbot.kalshi.auth import KalshiSigner
from predictionbot.kalshi.rest import KalshiRest
from predictionbot.portfolio.ledger import Ledger
from tests.conftest import make_book, make_market

D = Decimal


def _intent(outcome: str = "yes", count: str = "20", price: str = "0.50") -> OrderIntent:
    return OrderIntent(make_market(), outcome, "UP" if outcome == "yes" else "DOWN", D(count), D(price))


# ------------------------------------------------------------------ PAPER
async def test_paper_partial_fill_with_haircut_and_fees(db) -> None:
    book = make_book(
        "KXBTC15M-TEST-00", [("0.45", "100")], [("0.52", "10"), ("0.50", "10")]
    )  # YES asks 0.48 x10, 0.50 x10
    led = Ledger("PAPER", db, D("1000"))
    ex = PaperExecutor(led, lambda t: book, db, latency_s=0.0)
    res = await ex.submit(_intent(count="20", price="0.50"))
    assert res.status == "PARTIALLY_FILLED" and res.filled_count == D("10")  # 50% haircut of 20 displayed
    assert res.avg_fill_price == D("0.49")
    fills = db.query("SELECT * FROM fills")
    assert len(fills) == 2 and all(f["mode"] == "PAPER" for f in fills)
    assert led.cash == D("1000") - D("5") * D("0.48") - D("5") * D("0.50") - res.fees
    assert led.reserved_total == ZERO
    order = db.one("SELECT * FROM orders")
    assert order["status"] == "PARTIALLY_FILLED" and order["client_order_id"].startswith("pcb-p-")


async def test_paper_rereads_book_after_latency(db) -> None:
    book = make_book("KXBTC15M-TEST-00", [("0.45", "100")], [("0.52", "100")])
    led = Ledger("PAPER", db, D("1000"))
    ex = PaperExecutor(led, lambda t: book, db, latency_s=0.05)
    import asyncio

    task = asyncio.create_task(ex.submit(_intent(count="10", price="0.48")))
    await asyncio.sleep(0.01)
    # liquidity disappears before our order "arrives"
    book.apply_delta({"price_dollars": "0.52", "delta_fp": "-100", "side": "no"}, seq=2)
    res = await task
    assert res.status == "NO_FILL" and res.filled_count == ZERO and led.cash == D("1000")


async def test_paper_no_fill_on_invalid_book(db) -> None:
    book = make_book("KXBTC15M-TEST-00", [("0.45", "100")], [("0.52", "100")])
    book.invalidate("SEQUENCE_GAP")
    ex = PaperExecutor(Ledger("PAPER", db, D("1000")), lambda t: book, db, latency_s=0)
    assert (await ex.submit(_intent(price="0.48"))).status == "NO_FILL"


async def test_shadow_moves_no_capital(db) -> None:
    book = make_book("KXBTC15M-TEST-00", [("0.45", "100")], [("0.52", "100")])
    ex = ShadowExecutor(lambda t: book, db)
    res = await ex.submit(_intent(price="0.48"))
    assert res.status == "SHADOW"
    assert db.query("SELECT * FROM fills") == []
    assert json.loads(db.one("SELECT request FROM orders")["request"])["hypothetical_fill"]["would_fill"] == "20"


# ------------------------------------------------------------------- LIVE
def test_v2_order_body_yes_and_no() -> None:
    yes = build_create_order_body(_intent("yes", "3", "0.56"), "pcb-l-1", None)
    assert yes == {
        "ticker": "KXBTC15M-TEST-00",
        "client_order_id": "pcb-l-1",
        "side": "bid",
        "count": "3.00",
        "price": "0.5600",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }
    no = build_create_order_body(_intent("no", "2", "0.30"), "pcb-l-2", 4)
    assert no["side"] == "ask" and no["price"] == "0.7000" and no["subaccount"] == 4  # buy NO@0.30 = sell YES@0.70
    with pytest.raises(ValueError):
        build_create_order_body(_intent("yes", "1", "0.565"), "x", None)  # off the 1¢ grid
    with pytest.raises(ValueError):
        build_create_order_body(_intent("yes", "1.005", "0.56"), "x", None)  # finer than 0.01 contracts


class Exchange:
    """Scripted Kalshi responses recorded via httpx.MockTransport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.create_behavior = "ok"
        self.orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        assert req.headers.get("KALSHI-ACCESS-SIGNATURE")
        if req.method == "POST" and req.url.path.endswith("/portfolio/events/orders"):
            if self.create_behavior == "timeout":
                # exchange accepted it, but the response is lost
                self.orders.append(
                    {
                        "order_id": "ex-1",
                        "client_order_id": body["client_order_id"],
                        "status": "executed",
                        "fill_count_fp": body["count"],
                        "initial_count_fp": body["count"],
                    }
                )
                self._fill(body)
                raise httpx.ReadTimeout("lost response")
            if self.create_behavior == "409":
                return httpx.Response(409, json={"error": {"code": "duplicate"}})
            if self.create_behavior == "400":
                return httpx.Response(400, json={"error": {"code": "invalid_price"}})
            self.orders.append(
                {
                    "order_id": "ex-2",
                    "client_order_id": body["client_order_id"],
                    "status": "executed",
                    "fill_count_fp": body["count"],
                    "initial_count_fp": body["count"],
                }
            )
            self._fill(body)
            return httpx.Response(
                201,
                json={
                    "order_id": "ex-2",
                    "client_order_id": body["client_order_id"],
                    "fill_count": body["count"],
                    "remaining_count": "0.00",
                    "average_fill_price": body["price"],
                    "ts_ms": 1,
                },
            )
        if req.url.path.endswith("/portfolio/orders"):
            return httpx.Response(200, json={"orders": self.orders, "cursor": ""})
        if req.url.path.endswith("/portfolio/fills"):
            oid = req.url.params.get("order_id")
            return httpx.Response(
                200, json={"fills": [f for f in self.fills if not oid or f["order_id"] == oid], "cursor": ""}
            )
        return httpx.Response(404, json={})

    def _fill(self, body: dict[str, Any]) -> None:
        oid = self.orders[-1]["order_id"]
        self.fills.append(
            {
                "fill_id": f"f-{oid}",
                "order_id": oid,
                "client_order_id": body["client_order_id"],
                "ticker": body["ticker"],
                "outcome_side": "yes" if body["side"] == "bid" else "no",
                "book_side": body["side"],
                "count_fp": body["count"],
                "yes_price_dollars": body["price"],
                "fee_cost": "0.0200",
                "is_taker": True,
                "created_time": "2026-10-11T00:00:00Z",
            }
        )

    def posts(self) -> int:
        return sum(1 for m, p, _ in self.calls if m == "POST")


def _live(db, ex: Exchange, rsa_pem: bytes) -> tuple[LiveExecutor, Ledger]:
    rest = KalshiRest(
        "https://mock.invalid/trade-api/v2", KalshiSigner("kid", rsa_pem), transport=httpx.MockTransport(ex)
    )
    led = Ledger("LIVE", db, D("500"))
    led.cash_source = "exchange"
    le = LiveExecutor(rest, led, db)
    le.armed = True
    return le, led


async def test_live_requires_arming(db, rsa_pem: bytes) -> None:
    le, _ = _live(db, Exchange(), rsa_pem)
    le.armed = False
    with pytest.raises(LiveNotArmedError):
        await le.submit(_intent())


async def test_live_success_records_fills(db, rsa_pem: bytes) -> None:
    ex = Exchange()
    le, led = _live(db, ex, rsa_pem)
    res = await le.submit(_intent("no", "4", "0.30"))
    assert res.status == "FILLED" and res.exchange_order_id == "ex-2"
    assert ex.posts() == 1
    assert ex.calls[0][2]["side"] == "ask" and ex.calls[0][2]["price"] == "0.7000"
    pos = led.open_positions()
    assert len(pos) == 1 and pos[0].outcome == "no" and pos[0].count == D("4.00") and pos[0].cost == D("1.2000")
    assert db.one("SELECT status FROM orders")["status"] == "FILLED"


async def test_live_timeout_is_ambiguous_and_never_resubmitted(db, rsa_pem: bytes) -> None:
    ex = Exchange()
    ex.create_behavior = "timeout"
    le, led = _live(db, ex, rsa_pem)
    res = await le.submit(_intent("yes", "2", "0.50"))
    assert res.status == "AMBIGUOUS" and le.unresolved_count() == 1
    assert ex.posts() == 1  # NOT retried
    assert led.reserved_total > 0  # capital stays reserved until resolved
    await le.reconcile_ambiguous()
    assert ex.posts() == 1  # reconciliation is read-only
    assert le.unresolved_count() == 0 and led.reserved_total == ZERO
    row = db.one("SELECT status, exchange_order_id FROM orders")
    assert row["status"] == "FILLED" and row["exchange_order_id"] == "ex-1"
    assert len(led.open_positions()) == 1  # fill recovered from the exchange


async def test_live_ambiguous_not_found_gives_up_after_window(db, rsa_pem: bytes, monkeypatch) -> None:
    ex = Exchange()
    ex.create_behavior = "timeout"
    le, _led = _live(db, ex, rsa_pem)
    await le.submit(_intent())
    ex.orders.clear()  # exchange never saw it
    ex.fills.clear()
    await le.reconcile_ambiguous()
    assert le.unresolved_count() == 1  # still waiting inside the window
    coid = next(iter(le.ambiguous))
    t, since, i = le.ambiguous[coid]
    le.ambiguous[coid] = (t, since - 1000, i)
    await le.reconcile_ambiguous()
    assert le.unresolved_count() == 0 and db.one("SELECT status FROM orders")["status"] == "NOT_FOUND"


async def test_live_duplicate_client_order_id_reconciles(db, rsa_pem: bytes) -> None:
    ex = Exchange()
    ex.create_behavior = "409"
    le, _ = _live(db, ex, rsa_pem)
    res = await le.submit(_intent())
    assert res.status == "AMBIGUOUS" and le.unresolved_count() == 1 and ex.posts() == 1


async def test_live_rejected_releases_capital(db, rsa_pem: bytes) -> None:
    ex = Exchange()
    ex.create_behavior = "400"
    le, led = _live(db, ex, rsa_pem)
    res = await le.submit(_intent())
    assert res.status == "REJECTED" and led.reserved_total == ZERO and le.unresolved_count() == 0


async def test_restart_recovers_unresolved(db, rsa_pem: bytes) -> None:
    ex = Exchange()
    ex.create_behavior = "timeout"
    le, _ = _live(db, ex, rsa_pem)
    await le.submit(_intent())
    # simulate process restart: new executor reading the same DB
    le2, _led2 = _live(db, ex, rsa_pem)
    assert le2.recover_unresolved_from_db() == 1 and le2.unresolved_count() == 1
    le2.load_known_fills()
    await le2.reconcile_ambiguous()
    assert le2.unresolved_count() == 0
    assert db.one("SELECT status FROM orders")["status"] == "FILLED"


def test_fill_normalization_ws_and_rest() -> None:
    ws = {
        "trade_id": "t1",
        "order_id": "o",
        "market_ticker": "X",
        "outcome_side": "no",
        "book_side": "ask",
        "yes_price_dollars": "0.6500",
        "count_fp": "3.00",
        "fee_cost": "0.0500",
        "ts_ms": 1700000000000,
        "action": "sell",
        "side": "no",
        "is_taker": True,
        "post_position_fp": "-3.00",
        "purchased_side": "no",
        "exchange_index": 0,
        "ts": 1,
    }
    nf = normalize_fill(ws)
    assert nf and nf.outcome == "no" and nf.price == D("0.3500") and nf.is_opening_buy and nf.ts == 1700000000.0
    rest = {
        "fill_id": "f",
        "order_id": "o",
        "ticker": "X",
        "outcome_side": "yes",
        "book_side": "bid",
        "count_fp": "1.00",
        "yes_price_dollars": "0.4000",
        "no_price_dollars": "0.6000",
        "fee_cost": "0.02",
        "created_time": "2026-10-11T00:00:00Z",
        "is_taker": True,
    }
    nf2 = normalize_fill(rest)
    assert nf2 and nf2.price == D("0.4000") and nf2.is_opening_buy
    assert normalize_fill({"fill_id": "x"}) is None


async def test_live_fill_dedup(db, rsa_pem: bytes) -> None:
    le, led = _live(db, Exchange(), rsa_pem)
    f = normalize_fill(
        {
            "fill_id": "dup",
            "ticker": "X",
            "outcome_side": "yes",
            "book_side": "bid",
            "count_fp": "1",
            "yes_price_dollars": "0.40",
            "fee_cost": "0.01",
        }
    )
    assert f is not None
    assert le.apply_fill(f) is True and le.apply_fill(f) is False
    assert led.open_positions()[0].count == D("1")
    _ = time


async def test_live_fill_debits_cash_provisionally(db, rsa_pem: bytes) -> None:
    le, led = _live(db, Exchange(), rsa_pem)
    await le.submit(_intent("yes", "4", "0.50"))
    assert led.cash == D("500") - D("4.00") * D("0.50") - D("0.0200")  # provisional until reconciliation
    led.replace_from_exchange(
        D("497.98"),
        [
            {
                "ticker": "KXBTC15M-TEST-00",
                "position_fp": "4.00",
                "market_exposure_dollars": "2.00",
                "fees_paid_dollars": "0.02",
            }
        ],
        {},
    )
    assert led.cash == D("497.98") and led.open_positions()[0].count == D("4.00")
