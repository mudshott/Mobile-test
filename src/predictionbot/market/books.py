"""Order-book manager: one `orderbook_delta` subscription PER MARKET.

Per-market subscriptions give per-market sequence numbers, so a gap in one
market never invalidates the others. Recovery ladder for a bad book:
  1. `update_subscription get_snapshot` (cheap, same sid, keeps sequence),
  2. if no snapshot arrives within `resync_timeout_s`: unsubscribe + resubscribe,
  3. never permanently disabled — every maintenance pass retries.
A quiet but valid book is re-confirmed with get_snapshot every `max_quiet_s`.
REST snapshots are a display/research backstop only while the WebSocket is
down; books sourced from REST are never tradeable.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from predictionbot.kalshi.rest import KalshiError, KalshiRest
from predictionbot.kalshi.ws import KalshiWS, ws_body
from predictionbot.market.orderbook import BookState, OrderBook

log = logging.getLogger(__name__)


def _key(ticker: str) -> str:
    return f"ob:{ticker}"


class BookManager:
    def __init__(
        self,
        ws: KalshiWS,
        rest: KalshiRest | None,
        *,
        max_quiet_s: float = 20.0,
        resync_timeout_s: float = 6.0,
        rest_backstop_interval_s: float = 10.0,
    ) -> None:
        self.ws = ws
        self.rest = rest
        self.books: dict[str, OrderBook] = {}
        self.max_quiet_s = max_quiet_s
        self.resync_timeout_s = resync_timeout_s
        self.rest_backstop_interval_s = rest_backstop_interval_s
        self._resync_requested: dict[str, float] = {}
        self._confirm_requested: dict[str, float] = {}
        self._last_rest: dict[str, float] = {}
        self.gaps = 0
        self.resubscribes = 0
        self.rest_snapshots = 0

    def get(self, ticker: str) -> OrderBook | None:
        return self.books.get(ticker)

    async def track(self, ticker: str) -> None:
        if ticker in self.books:
            return
        self.books[ticker] = OrderBook(ticker)
        await self.ws.ensure(_key(ticker), "orderbook_delta", {"market_tickers": [ticker]})

    async def untrack(self, ticker: str) -> None:
        self.books.pop(ticker, None)
        self._resync_requested.pop(ticker, None)
        self._confirm_requested.pop(ticker, None)
        await self.ws.remove(_key(ticker))

    def on_disconnect(self) -> None:
        for book in self.books.values():
            if book.state == BookState.VALID:
                book.invalidate("WS_DISCONNECTED")

    async def on_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        body = ws_body(msg)
        ticker = body.get("market_ticker")
        if mtype == "subscribed":
            return
        if not isinstance(ticker, str) or ticker not in self.books:
            return
        book = self.books[ticker]
        seq = msg.get("seq") if isinstance(msg.get("seq"), int) else None
        if mtype == "orderbook_snapshot":
            res = book.apply_snapshot(body, seq=seq, source="ws")
            self._resync_requested.pop(ticker, None)
            self._confirm_requested.pop(ticker, None)
            if not res.ok:
                await self._request_resync(ticker, res.reason or "bad snapshot")
        elif mtype == "orderbook_delta":
            if msg.get("_gap"):
                self.gaps += 1
            res = book.apply_delta(body, seq=seq, gap=bool(msg.get("_gap")))
            if res.need_resync:
                await self._request_resync(ticker, res.reason or "delta failure")

    async def _request_resync(self, ticker: str, reason: str) -> None:
        if ticker in self._resync_requested:
            return
        self._resync_requested[ticker] = time.time()
        log.info("resync %s: %s", ticker, reason)
        if not await self.ws.request_snapshot(_key(ticker), [ticker]):
            await self.ws.resubscribe(_key(ticker))
            self.resubscribes += 1

    async def maintain(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for ticker, book in list(self.books.items()):
            if not self.ws.connected:
                await self._rest_backstop(ticker, book, now)
                continue
            if book.state != BookState.VALID:
                started = self._resync_requested.get(ticker)
                if started is None:
                    await self._request_resync(ticker, book.invalid_reason or "not valid")
                elif now - started > self.resync_timeout_s:
                    # Escalate: fresh subscription → fresh snapshot + fresh sequence.
                    self._resync_requested[ticker] = now
                    await self.ws.resubscribe(_key(ticker))
                    self.resubscribes += 1
                continue
            age = book.age(now)
            if age is not None and age > self.max_quiet_s:
                last = self._confirm_requested.get(ticker, 0.0)
                if now - last > self.max_quiet_s / 2:
                    self._confirm_requested[ticker] = now
                    await self.ws.request_snapshot(_key(ticker), [ticker])

    async def _rest_backstop(self, ticker: str, book: OrderBook, now: float) -> None:
        if self.rest is None or now - self._last_rest.get(ticker, 0.0) < self.rest_backstop_interval_s:
            return
        self._last_rest[ticker] = now
        try:
            ob = await self.rest.get_orderbook(ticker)
        except KalshiError as exc:
            log.debug("REST orderbook backstop %s failed: %s", ticker, exc)
            return
        book.apply_snapshot(ob, seq=None, source="rest")
        self.rest_snapshots += 1

    def public(self, now: float | None = None) -> dict[str, Any]:
        return {
            "books": {t: b.public(now) for t, b in self.books.items()},
            "gaps": self.gaps,
            "resubscribes": self.resubscribes,
            "rest_snapshots": self.rest_snapshots,
        }
