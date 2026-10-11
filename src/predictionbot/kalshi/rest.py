"""Async Kalshi REST client (Trade API v2).

Guarantees:
* Every request passes the local firewall before any network I/O.
* Requests are signed per the official scheme over the *full* path
  (``/trade-api/v2/...``) without the query string.
* Reads are rate-limited and retried on transient failure; writes are NEVER
  retried automatically — an ambiguous write (timeout / connection drop after
  send) raises :class:`AmbiguousWriteError` so the executor reconciles first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from predictionbot.kalshi.auth import KalshiSigner
from predictionbot.kalshi.firewall import check_rest

log = logging.getLogger(__name__)


class KalshiError(RuntimeError):
    pass


class KalshiHTTPError(KalshiError):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"HTTP {status} {method} {path}: {body[:300]}")
        self.status = status
        self.body = body


class RateLimitedError(KalshiHTTPError):
    pass


class AuthRequiredError(KalshiError):
    pass


class TransientNetworkError(KalshiError):
    pass


class AmbiguousWriteError(KalshiError):
    """A write was (possibly) sent but the outcome is unknown. Reconcile before acting."""


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float | None = None) -> None:
        self.rate = rate_per_s
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_s)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                await asyncio.sleep((cost - self.tokens) / self.rate)


@dataclass
class RestStats:
    requests: int = 0
    errors: int = 0
    rate_limited: int = 0
    last_ok_ts: float | None = None
    last_error_ts: float | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    server_clock_skew_s: float | None = None


class KalshiRest:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None,
        *,
        read_rate_per_s: float = 10.0,
        write_rate_per_s: float = 5.0,
        timeout_s: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlparse(self.base_url).path  # "/trade-api/v2"
        self.signer = signer
        self._read_bucket = TokenBucket(read_rate_per_s)
        self._write_bucket = TokenBucket(write_rate_per_s)
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._sleep = sleep
        self.stats = RestStats()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def authenticated(self) -> bool:
        return self.signer is not None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        retries: int = 2,
    ) -> dict[str, Any]:
        method = method.upper()
        check_rest(method, path)  # raises BEFORE any network activity
        is_write = method != "GET"
        if auth and self.signer is None:
            raise AuthRequiredError(f"{method} {path} requires API credentials")
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = 1 if is_write else retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await (self._write_bucket if is_write else self._read_bucket).acquire()
            full_path = self.path_prefix + path
            headers = {"Accept": "application/json"}
            if auth and self.signer is not None:
                headers.update(self.signer.headers(method, full_path))
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            t0 = time.monotonic()
            self.stats.requests += 1
            try:
                resp = await self._client.request(
                    method, self.base_url + path, params=clean_params, json=json_body, headers=headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.UnsupportedProtocol) as exc:
                # Failed before the request could reach the exchange: never ambiguous.
                self._record_error(f"{exc.__class__.__name__} on {method} {path}: {str(exc)[:120]}")
                last_exc = TransientNetworkError(f"{exc.__class__.__name__}: {str(exc)[:200]}")
                if is_write:
                    raise last_exc from exc
                await self._sleep(min(4.0, 0.5 * 2**attempt))
                continue
            except httpx.TransportError as exc:
                self._record_error(f"{exc.__class__.__name__} on {method} {path}")
                if is_write:
                    # The request may have reached the exchange. Do not retry blindly.
                    raise AmbiguousWriteError(f"{method} {path}: {exc.__class__.__name__}") from exc
                last_exc = TransientNetworkError(str(exc) or exc.__class__.__name__)
                await self._sleep(min(4.0, 0.5 * 2**attempt))
                continue
            self.stats.last_latency_ms = (time.monotonic() - t0) * 1000
            self._note_server_date(resp)
            if resp.status_code == 429:
                self.stats.rate_limited += 1
                self._record_error(f"429 on {method} {path}")
                err: Exception = RateLimitedError(429, method, path, resp.text)
                if is_write:
                    raise err  # 429 means the exchange rejected it: safe, not ambiguous
                last_exc = err
                await self._sleep(min(8.0, 1.0 * 2**attempt))
                continue
            if resp.status_code >= 500:
                self._record_error(f"{resp.status_code} on {method} {path}")
                if is_write:
                    # 5xx after a write is ambiguous: the exchange may have processed it.
                    raise AmbiguousWriteError(f"{method} {path}: HTTP {resp.status_code}")
                last_exc = KalshiHTTPError(resp.status_code, method, path, resp.text)
                await self._sleep(min(4.0, 0.5 * 2**attempt))
                continue
            if resp.status_code >= 400:
                self._record_error(f"{resp.status_code} on {method} {path}")
                raise KalshiHTTPError(resp.status_code, method, path, resp.text)
            self.stats.last_ok_ts = time.time()
            if not resp.content:
                return {}
            try:
                data = resp.json()
            except ValueError as exc:
                self._record_error(f"malformed JSON on {method} {path}")
                if is_write:
                    raise AmbiguousWriteError(f"{method} {path}: malformed response") from exc
                last_exc = KalshiError(f"malformed JSON from {path}")
                continue
            if not isinstance(data, dict):
                raise KalshiError(f"unexpected payload type from {path}")
            return data
        assert last_exc is not None
        raise last_exc

    def _record_error(self, msg: str) -> None:
        self.stats.errors += 1
        self.stats.last_error = msg
        self.stats.last_error_ts = time.time()
        log.warning("REST %s", msg)

    def _note_server_date(self, resp: httpx.Response) -> None:
        date = resp.headers.get("date")
        if not date:
            return
        from email.utils import parsedate_to_datetime

        try:
            server = parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            return
        self.stats.server_clock_skew_s = time.time() - server

    # ------------------------------------------------------------------ reads
    async def exchange_status(self) -> dict[str, Any]:
        return await self.request("GET", "/exchange/status", auth=False)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        data = await self.request("GET", f"/series/{series_ticker}", auth=False)
        return dict(data.get("series") or {})

    async def get_markets(self, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            data = await self.request("GET", "/markets", params={**params, "cursor": cursor, "limit": 200}, auth=False)
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return out

    async def get_market(self, ticker: str) -> dict[str, Any]:
        data = await self.request("GET", f"/markets/{ticker}", auth=False)
        return dict(data.get("market") or {})

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        data = await self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, auth=False)
        return dict(data.get("orderbook_fp") or data.get("orderbook") or {})

    async def get_balance(self, subaccount: int | None = None) -> dict[str, Any]:
        return await self.request("GET", "/portfolio/balance", params={"subaccount": subaccount})

    async def _paged(self, path: str, key: str, params: dict[str, Any], max_pages: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self.request("GET", path, params={**params, "cursor": cursor, "limit": 200})
            out.extend(data.get(key) or [])
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return out

    async def get_positions(self, subaccount: int | None = None, **params: Any) -> list[dict[str, Any]]:
        return await self._paged(
            "/portfolio/positions", "market_positions", {"subaccount": subaccount, "count_filter": "position", **params}
        )

    async def get_orders(self, subaccount: int | None = None, **params: Any) -> list[dict[str, Any]]:
        return await self._paged("/portfolio/orders", "orders", {"subaccount": subaccount, **params})

    async def get_order(self, order_id: str) -> dict[str, Any]:
        data = await self.request("GET", f"/portfolio/orders/{order_id}")
        return dict(data.get("order") or {})

    async def get_fills(self, subaccount: int | None = None, **params: Any) -> list[dict[str, Any]]:
        return await self._paged("/portfolio/fills", "fills", {"subaccount": subaccount, **params})

    async def get_settlements(self, subaccount: int | None = None, **params: Any) -> list[dict[str, Any]]:
        return await self._paged("/portfolio/settlements", "settlements", {"subaccount": subaccount, **params})

    async def get_historical_markets(self, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            data = await self.request(
                "GET", "/historical/markets", params={**params, "cursor": cursor, "limit": 200}, auth=False
            )
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return out

    # ----------------------------------------------------------------- writes
    async def create_order_v2(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/portfolio/events/orders", json_body=body)

    async def cancel_order_v2(
        self, order_id: str, *, market_ticker: str | None = None, subaccount: int | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": market_ticker, "subaccount": subaccount},
        )

    async def decrease_order_v2(self, order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/portfolio/events/orders/{order_id}/decrease", json_body=body)

    async def amend_order_v2(self, order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/portfolio/events/orders/{order_id}/amend", json_body=body)
