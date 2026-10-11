"""WebSocket client: subscriptions, sequence integrity, reconnect + resubscribe."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from predictionbot.kalshi.auth import KalshiSigner
from predictionbot.kalshi.ws import KalshiWS


class FakeConn:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def push(self, obj: Any) -> None:
        self.inbox.put_nowait(obj if isinstance(obj, str) else json.dumps(obj))

    def drop(self) -> None:
        self.inbox.put_nowait(None)

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)

    def __aiter__(self) -> FakeConn:
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def _wait(cond: Any, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_subscribe_gap_duplicate_reconnect(rsa_pem: bytes) -> None:
    conns: list[FakeConn] = []
    headers_seen: list[dict[str, str]] = []

    async def connect(url: str, headers: dict[str, str]) -> FakeConn:
        headers_seen.append(headers)
        c = FakeConn()
        conns.append(c)
        return c

    got: list[dict[str, Any]] = []

    async def handler(msg: dict[str, Any]) -> None:
        got.append(msg)

    ws = KalshiWS("wss://x/trade-api/ws/v2", KalshiSigner("kid", rsa_pem), handler, connect_fn=connect)
    ws.max_backoff_s = 0.05
    events: list[str] = []
    ws.listeners.append(lambda c, d: events.append(c))
    await ws.ensure("ob:T", "orderbook_delta", {"market_tickers": ["T"]})
    ws.start()
    await _wait(lambda: conns and conns[0].sent)
    assert "KALSHI-ACCESS-SIGNATURE" in headers_seen[0]
    sub_cmd = conns[0].sent[0]
    assert sub_cmd["cmd"] == "subscribe" and sub_cmd["params"]["channels"] == ["orderbook_delta"]
    c = conns[0]
    c.push({"id": sub_cmd["id"], "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 7}})
    c.push({"type": "orderbook_snapshot", "sid": 7, "seq": 1, "msg": {"market_ticker": "T"}})
    c.push({"type": "orderbook_delta", "sid": 7, "seq": 2, "msg": {"market_ticker": "T"}})
    c.push({"type": "orderbook_delta", "sid": 7, "seq": 2, "msg": {"market_ticker": "T"}})  # duplicate
    c.push("{malformed")
    c.push({"type": "orderbook_delta", "sid": 7, "seq": 5, "msg": {"market_ticker": "T"}})  # gap
    await _wait(lambda: len(got) >= 4)
    assert ws.stats.duplicates == 1
    assert ws.stats.gaps == 1
    assert got[-1].get("_gap") is True
    assert "WS_SEQUENCE_GAP" in events and "WS_SUBSCRIBED" in events
    # disconnect → reconnect → automatic resubscribe
    c.drop()
    await _wait(lambda: len(conns) >= 2 and conns[1].sent)
    assert conns[1].sent[0]["cmd"] == "subscribe"
    conns[1].push({"id": conns[1].sent[0]["id"], "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 9}})
    await _wait(lambda: "WS_RESUBSCRIBED" in events)
    assert "WS_RECONNECTED" in events
    await _wait(lambda: ws.subs["ob:T"].sid == 9)
    # new connection: sequence restarts without a false gap
    conns[1].push({"type": "orderbook_snapshot", "sid": 9, "seq": 1, "msg": {"market_ticker": "T"}})
    await _wait(lambda: got[-1].get("sid") == 9)
    assert ws.stats.gaps == 1
    await ws.stop()


async def test_slow_handler_does_not_block_reader(rsa_pem: bytes) -> None:
    conn = FakeConn()

    async def connect(url: str, headers: dict[str, str]) -> FakeConn:
        return conn

    release = asyncio.Event()

    async def slow(msg: dict[str, Any]) -> None:
        await release.wait()

    ws = KalshiWS("wss://x", KalshiSigner("kid", rsa_pem), slow, connect_fn=connect)
    ws.start()
    await asyncio.sleep(0.05)
    for i in range(50):
        conn.push({"type": "ticker", "sid": 1, "msg": {"i": i}})
    await _wait(lambda: ws.stats.messages == 50)  # reader consumed all while handler blocked
    release.set()
    await ws.stop()


async def test_no_credentials_never_connects() -> None:
    called = False

    async def connect(url: str, headers: dict[str, str]) -> FakeConn:
        nonlocal called
        called = True
        return FakeConn()

    async def h(m: dict[str, Any]) -> None:
        return None

    ws = KalshiWS("wss://x", None, h, connect_fn=connect)
    ws.start()
    await asyncio.sleep(0.05)
    assert not called and ws.stats.state == "WS_DISCONNECTED"
    await ws.stop()


async def test_connect_failure_backs_off_and_retries(rsa_pem: bytes) -> None:
    attempts = 0

    async def connect(url: str, headers: dict[str, str]) -> FakeConn:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("refused")
        return FakeConn()

    async def h(m: dict[str, Any]) -> None:
        return None

    ws = KalshiWS("wss://x", KalshiSigner("kid", rsa_pem), h, connect_fn=connect)
    ws._backoff = lambda attempt: 0.01  # type: ignore[method-assign]
    ws.start()
    await _wait(lambda: ws.connected)
    assert attempts == 3
    await ws.stop()


async def test_null_id_ack_does_not_crash(rsa_pem: bytes) -> None:
    conn = FakeConn()

    async def connect(url: str, headers: dict[str, str]) -> FakeConn:
        return conn

    got: list[dict[str, Any]] = []

    async def h(m: dict[str, Any]) -> None:
        got.append(m)

    ws = KalshiWS("wss://x", KalshiSigner("kid", rsa_pem), h, connect_fn=connect)
    ws.start()
    await _wait(lambda: ws.connected)
    conn.push({"id": None, "type": "subscribed", "msg": {"channel": "x", "sid": 3}})
    conn.push({"id": None, "type": "error", "msg": {"code": 8, "msg": "Unknown channel name"}})
    conn.push({"type": "ticker", "sid": 99, "msg": {}})
    await _wait(lambda: len(got) == 3)
    await ws.stop()
