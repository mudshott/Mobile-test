"""PAPER execution: real market data, simulated orders/fills/capital.

Fill model (conservative):
* The order "arrives" after `latency_s`; it is matched against the book as it
  is THEN, not as it was at decision time.
* Immediate-or-cancel at the limit price: only liquidity at or better than
  the limit fills; the remainder is cancelled (partial fills are normal).
* Only `depth_haircut` (default 50%) of displayed size is assumed reachable,
  because queue position / competing takers are unknown.
* Fees: Kalshi taker fee per price level, rounded up to the cent.
* No fill at all if the book is not VALID and fresh on arrival.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal

from predictionbot.execution.base import (
    OrderIntent,
    OrderResult,
    new_client_order_id,
    persist_new_order,
    update_order,
)
from predictionbot.fixedpoint import ZERO
from predictionbot.market.orderbook import BookState, OrderBook
from predictionbot.persistence.db import Database
from predictionbot.portfolio.ledger import Ledger
from predictionbot.strategy.fees import taker_fee

BookLookup = Callable[[str], OrderBook | None]


class PaperExecutor:
    mode = "PAPER"

    def __init__(
        self,
        ledger: Ledger,
        books: BookLookup,
        db: Database | None,
        *,
        latency_s: float = 0.25,
        depth_haircut: Decimal = Decimal("0.5"),
        book_stale_s: float = 45.0,
    ) -> None:
        self.ledger = ledger
        self.books = books
        self.db = db
        self.latency_s = latency_s
        self.depth_haircut = depth_haircut
        self.book_stale_s = book_stale_s
        self.orders_submitted = 0

    def unresolved_count(self) -> int:
        return 0

    async def submit(self, intent: OrderIntent) -> OrderResult:
        coid = new_client_order_id(self.mode)
        persist_new_order(
            self.db,
            client_order_id=coid,
            mode=self.mode,
            intent=intent,
            book_side="bid" if intent.outcome == "yes" else "ask",
            status="PENDING_SUBMIT",
        )
        reserve = intent.count * intent.limit_price + taker_fee(
            intent.count, intent.limit_price, intent.market.fee_multiplier, intent.market.fee_type
        )
        self.ledger.reserve(coid, reserve)
        self.orders_submitted += 1
        try:
            await asyncio.sleep(self.latency_s)
            return self._match(coid, intent)
        finally:
            self.ledger.release(coid)

    def _match(self, coid: str, intent: OrderIntent) -> OrderResult:
        book = self.books(intent.ticker)
        now = time.time()
        if book is None or book.state != BookState.VALID or (book.age(now) or 1e9) > self.book_stale_s:
            update_order(self.db, coid, status="NO_FILL", error="book unavailable/stale at arrival")
            return OrderResult(coid, self.mode, "NO_FILL", error="book unavailable at arrival")
        if not intent.market.is_open_now(now):
            update_order(self.db, coid, status="REJECTED", error="market closed at arrival")
            return OrderResult(coid, self.mode, "REJECTED", error="market closed at arrival")
        walk = book.walk_buy(intent.outcome, intent.count, intent.limit_price, self.depth_haircut)
        if walk.count <= ZERO:
            update_order(
                self.db, coid, status="NO_FILL", filled_count="0", error="no liquidity at/better than limit on arrival"
            )
            return OrderResult(coid, self.mode, "NO_FILL", error="no liquidity at limit on arrival")
        fills = []
        total_fee = ZERO
        for i, (price, qty) in enumerate(walk.levels):
            fee = taker_fee(qty, price, intent.market.fee_multiplier, intent.market.fee_type)
            total_fee += fee
            fill = {
                "fill_id": f"{coid}-f{i}",
                "client_order_id": coid,
                "mode": self.mode,
                "ticker": intent.ticker,
                "outcome": intent.outcome,
                "count": str(qty),
                "price": str(price),
                "fee": str(fee),
                "is_taker": 1,
                "ts": now,
                "raw": json.dumps({"simulated": True, "latency_s": self.latency_s, "haircut": str(self.depth_haircut)}),
            }
            fills.append(fill)
            if self.db is not None:
                self.db.insert("fills", fill)
            self.ledger.apply_fill(
                ticker=intent.ticker,
                outcome=intent.outcome,
                asset=intent.market.asset,
                direction=intent.direction,
                count=qty,
                price=price,
                fee=fee,
                ts=now,
            )
        avg = walk.avg_price.quantize(Decimal("0.000001")) if walk.avg_price is not None else None
        status = "FILLED" if walk.count == intent.count else "PARTIALLY_FILLED"
        update_order(
            self.db,
            coid,
            status=status,
            filled_count=str(walk.count),
            avg_fill_price=str(avg) if avg is not None else None,
            fees=str(total_fee),
        )
        return OrderResult(coid, self.mode, status, walk.count, avg, total_fee, fills=fills)

    async def cancel_bot_resting(self) -> int:
        return 0  # paper orders are IOC: nothing ever rests


class ShadowExecutor:
    """SHADOW: record what WOULD be submitted plus a hypothetical fill estimate.

    No simulated capital is moved and nothing is sent to the exchange.
    """

    mode = "SHADOW"

    def __init__(self, books: BookLookup, db: Database | None, depth_haircut: Decimal = Decimal("0.5")) -> None:
        self.books = books
        self.db = db
        self.depth_haircut = depth_haircut

    def unresolved_count(self) -> int:
        return 0

    async def submit(self, intent: OrderIntent) -> OrderResult:
        coid = new_client_order_id(self.mode)
        book = self.books(intent.ticker)
        est = None
        if book is not None and book.state == BookState.VALID:
            walk = book.walk_buy(intent.outcome, intent.count, intent.limit_price, self.depth_haircut)
            est = {"would_fill": str(walk.count), "avg_price": str(walk.avg_price) if walk.avg_price else None}
        persist_new_order(
            self.db,
            client_order_id=coid,
            mode=self.mode,
            intent=intent,
            book_side="bid" if intent.outcome == "yes" else "ask",
            status="SHADOW",
            request={"hypothetical_fill": est},
        )
        return OrderResult(coid, self.mode, "SHADOW")

    async def cancel_bot_resting(self) -> int:
        return 0
