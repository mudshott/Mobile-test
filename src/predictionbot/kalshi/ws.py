"""Resilient Kalshi WebSocket client.

* Authenticated handshake (signed GET /trade-api/ws/v2).
* Kalshi sends Ping frames every 10 s; the `websockets` library answers them
  automatically. We also send our own pings (ping_interval) so a dead TCP
  connection is detected even if the server goes silent.
* A reader task only parses frames and enqueues them; a separate dispatcher
  task runs handlers, so a slow handler never blocks the socket reader.
* Subscriptions are *desired state*: after any reconnect every desired
  subscription is re-sent automatically (WS_RESUBSCRIBED).
* Per-sid sequence tracking: duplicates are dropped, gaps are flagged
  (WS_SEQUENCE_GAP) and passed to the handler as ``msg["_gap"] = True``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from predictionbot.kalshi.auth import KalshiSigner
from predictionbot.kalshi.firewall import check_ws_command

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def ws_body(msg: dict[str, Any]) -> dict[str, Any]:
    """The `msg` payload of a WebSocket frame, or {} if absent/malformed."""
    body = msg.get("msg")
    return body if isinstance(body, dict) else {}


ConnectFn = Callable[[str, dict[str, str]], Awaitable[Any]]


@dataclass
class Subscription:
    key: str
    channel: str
    params: dict[str, Any]
    sid: int | None = None
    pending_id: int | None = None
    last_seq: int | None = None
    subscribed_at: float | None = None


@dataclass
class WsStats:
    state: str = "WS_DISCONNECTED"
    connects: int = 0
    reconnects: int = 0
    messages: int = 0
    duplicates: int = 0
    gaps: int = 0
    errors: int = 0
    last_message_ts: float | None = None
    connected_since: float | None = None
    queue_depth: int = 0
    events: deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=200))


async def _default_connect(url: str, headers: dict[str, str]) -> Any:
    import websockets

    return await websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=15,
        ping_interval=10,
        ping_timeout=20,
        close_timeout=5,
        max_size=2**22,
    )


class KalshiWS:
    def __init__(
        self,
        url: str,
        signer: KalshiSigner | None,
        handler: Handler,
        *,
        ws_path: str = "/trade-api/ws/v2",
        connect_fn: ConnectFn | None = None,
        max_backoff_s: float = 30.0,
        queue_size: int = 20000,
    ) -> None:
        self.url = url
        self.signer = signer
        self.handler = handler
        self.ws_path = ws_path
        self._connect_fn = connect_fn or _default_connect
        self.max_backoff_s = max_backoff_s
        self.stats = WsStats()
        self.subs: dict[str, Subscription] = {}
        self._by_sid: dict[int, Subscription] = {}
        self._by_pending: dict[int, Subscription] = {}
        self._ws: Any = None
        self._next_id = 1
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.listeners: list[Callable[[str, str], None]] = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run(), name="ws-run"),
            asyncio.create_task(self._dispatch_loop(), name="ws-dispatch"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        self._set_state("WS_DISCONNECTED", "stopped")

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _event(self, code: str, detail: str = "") -> None:
        self.stats.events.append((time.time(), code, detail))
        log.info("%s %s", code, detail)
        for cb in self.listeners:
            with contextlib.suppress(Exception):
                cb(code, detail)

    def _set_state(self, state: str, detail: str = "") -> None:
        self.stats.state = state
        self._event(state, detail)

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if self.signer is None:
                self._set_state("WS_DISCONNECTED", "no credentials: Kalshi WS requires an authenticated connection")
                await self._sleep_or_stop(10)
                continue
            try:
                headers = self.signer.headers("GET", self.ws_path)
                self._set_state("WS_RECONNECTING" if self.stats.connects else "WS_CONNECTING", self.url)
                ws = await self._connect_fn(self.url, headers)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.errors += 1
                self._event("WS_ERROR", f"connect failed: {exc.__class__.__name__}: {str(exc)[:200]}")
                attempt += 1
                await self._sleep_or_stop(self._backoff(attempt))
                continue
            self._ws = ws
            first = self.stats.connects == 0
            self.stats.connects += 1
            if not first:
                self.stats.reconnects += 1
            self.stats.connected_since = time.time()
            self._connected.set()
            self._set_state("WS_CONNECTED" if first else "WS_RECONNECTED", self.url)
            try:
                await self._resubscribe_all(initial=first)
                attempt = 0
                await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.errors += 1
                self._event("WS_ERROR", f"{exc.__class__.__name__}: {str(exc)[:200]}")
            finally:
                self._connected.clear()
                self._ws = None
                for sub in self.subs.values():
                    sub.sid = None
                    sub.pending_id = None
                    sub.last_seq = None
                self._by_sid.clear()
                self._by_pending.clear()
                with contextlib.suppress(Exception):
                    await ws.close()
            if self._stop.is_set():
                break
            self._set_state("WS_DISCONNECTED", "connection lost")
            attempt += 1
            await self._sleep_or_stop(self._backoff(attempt))

    def _backoff(self, attempt: int) -> float:
        base = min(self.max_backoff_s, 0.5 * 2 ** min(attempt, 8))
        return base * (0.7 + 0.6 * random.random())

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                msg = json.loads(raw)
            except ValueError:
                self.stats.errors += 1
                self._event("WS_ERROR", "malformed frame (not JSON) dropped")
                continue
            if not isinstance(msg, dict):
                self._event("WS_ERROR", "malformed frame (not an object) dropped")
                continue
            msg["_recv_ts"] = time.time()
            self.stats.messages += 1
            self.stats.last_message_ts = msg["_recv_ts"]
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Never silently drop market data: force a full resync instead.
                self._event("WS_ERROR", "dispatch queue overflow; forcing reconnect + resync")
                await ws.close()
                return
            self.stats.queue_depth = self._queue.qsize()

    async def _dispatch_loop(self) -> None:
        while True:
            msg = await self._queue.get()
            self.stats.queue_depth = self._queue.qsize()
            try:
                if self._track(msg):
                    await self.handler(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats.errors += 1
                log.exception("ws handler failed for %s", msg.get("type"))

    def _track(self, msg: dict[str, Any]) -> bool:
        """Control-message bookkeeping + sequence integrity. Returns False to drop."""
        mtype = msg.get("type")
        body = ws_body(msg)
        if mtype == "subscribed":
            sid = body.get("sid")
            sub = self._by_pending.pop(int(msg.get("id", -1)), None)
            if sub is not None and isinstance(sid, int):
                sub.sid = sid
                sub.pending_id = None
                sub.last_seq = None
                sub.subscribed_at = time.time()
                self._by_sid[sid] = sub
                self._event("WS_SUBSCRIBED", f"{sub.key} sid={sid}")
                msg["_sub_key"] = sub.key
            return True
        if mtype == "error":
            self.stats.errors += 1
            sub = self._by_pending.pop(int(msg.get("id", -1)), None)
            key = sub.key if sub else "?"
            self._event("WS_ERROR", f"code={body.get('code')} {body.get('msg')} sub={key}")
            if sub is not None:
                msg["_sub_key"] = sub.key
            if body.get("code") in (10, 25) and isinstance(msg.get("sid"), int):
                # Terminal channel errors: the subscription must be re-created.
                dead = self._by_sid.pop(int(msg["sid"]), None)
                if dead is not None:
                    dead.sid = None
                    asyncio.get_running_loop().create_task(self._send_subscribe(dead))
            return True
        sid = msg.get("sid")
        seq = msg.get("seq")
        sub = self._by_sid.get(sid) if isinstance(sid, int) else None
        if sub is not None:
            msg["_sub_key"] = sub.key
            if isinstance(seq, int):
                if sub.last_seq is not None:
                    if seq <= sub.last_seq:
                        self.stats.duplicates += 1
                        return False
                    if seq != sub.last_seq + 1:
                        self.stats.gaps += 1
                        msg["_gap"] = True
                        self._event("WS_SEQUENCE_GAP", f"{sub.key} expected {sub.last_seq + 1} got {seq}")
                sub.last_seq = seq
        return True

    # -------------------------------------------------------- subscriptions
    async def _send(self, cmd: str, params: dict[str, Any]) -> int:
        check_ws_command(cmd, params)
        async with self._send_lock:
            if self._ws is None:
                raise ConnectionError("websocket not connected")
            msg_id = self._next_id
            self._next_id += 1
            await self._ws.send(json.dumps({"id": msg_id, "cmd": cmd, "params": params}))
            return msg_id

    async def _send_subscribe(self, sub: Subscription) -> None:
        if self._ws is None:
            return
        try:
            msg_id = await self._send("subscribe", {"channels": [sub.channel], **sub.params})
        except Exception as exc:
            self._event("WS_ERROR", f"subscribe {sub.key} failed: {exc}")
            return
        sub.pending_id = msg_id
        self._by_pending[msg_id] = sub

    async def _resubscribe_all(self, *, initial: bool) -> None:
        for sub in list(self.subs.values()):
            await self._send_subscribe(sub)
        if not initial and self.subs:
            self._event("WS_RESUBSCRIBED", f"{len(self.subs)} subscriptions")

    async def ensure(self, key: str, channel: str, params: dict[str, Any]) -> None:
        existing = self.subs.get(key)
        if existing is not None and existing.channel == channel and existing.params == params:
            return
        if existing is not None:
            await self.remove(key)
        sub = Subscription(key, channel, dict(params))
        self.subs[key] = sub
        if self.connected:
            await self._send_subscribe(sub)

    async def remove(self, key: str) -> None:
        sub = self.subs.pop(key, None)
        if sub is None:
            return
        if sub.sid is not None:
            self._by_sid.pop(sub.sid, None)
            if self.connected:
                with contextlib.suppress(Exception):
                    await self._send("unsubscribe", {"sids": [sub.sid]})
        if sub.pending_id is not None:
            self._by_pending.pop(sub.pending_id, None)

    async def request_snapshot(self, key: str, market_tickers: list[str]) -> bool:
        sub = self.subs.get(key)
        if sub is None or sub.sid is None or not self.connected:
            return False
        try:
            await self._send(
                "update_subscription", {"sid": sub.sid, "action": "get_snapshot", "market_tickers": market_tickers}
            )
        except Exception as exc:
            self._event("WS_ERROR", f"get_snapshot {key} failed: {exc}")
            return False
        return True

    async def resubscribe(self, key: str) -> None:
        """Hard reset of one subscription (fresh snapshot + fresh sequence)."""
        sub = self.subs.get(key)
        if sub is None:
            return
        await self.remove(key)
        await self.ensure(key, sub.channel, sub.params)

    def sub_status(self) -> list[dict[str, Any]]:
        return [
            {
                "key": s.key,
                "channel": s.channel,
                "sid": s.sid,
                "last_seq": s.last_seq,
                "pending": s.pending_id is not None,
            }
            for s in self.subs.values()
        ]

    def message_age(self) -> float | None:
        if self.stats.last_message_ts is None:
            return None
        return time.time() - self.stats.last_message_ts
