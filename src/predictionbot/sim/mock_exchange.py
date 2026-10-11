"""Local MOCK Kalshi exchange — a protocol-faithful SIMULATOR for testing.

It is NOT market data. It exists so the full pipeline (auth handshake, REST,
WebSocket subscriptions, sequence numbers, snapshot/delta, get_snapshot,
CF Benchmarks ticks with final-minute averages, 15-minute market rotation,
settlement, IOC order matching) can be exercised end-to-end — including the
packaged executable — without touching the real exchange.

The application only talks to it in the explicit `mock` environment, which is
restricted to localhost and has LIVE trading disabled.

Control endpoints (test harness):
  POST /mock/scenario {"asset": "BTC", "shift": 0.15}   shift YES quotes by +/-$ (creates mispricing)
  POST /mock/fault {"drop_ws": true} | {"seq_gap": true} | {"cf_pause_s": 12} | {"rest_500": 3}
       | {"duplicate_delta": true} | {"malformed": true}
  GET  /mock/state
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

ASSETS = {
    "BTC": ("KXBTC15M", "BRTI", 65000.0, 0.00010),
    "ETH": ("KXETH15M", "ETHUSD_RTI", 3200.0, 0.00013),
    "SOL": ("KXSOL15M", "SOLUSD_RTI", 150.0, 0.00018),
}
QUARTER = 900


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def quarter_start(ts: float) -> int:
    return int(ts // QUARTER) * QUARTER


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class IndexSim:
    index_id: str
    price: float
    sigma: float
    history: list[tuple[int, float]] = field(default_factory=list)

    def step(self, ts_ms: int, rng: random.Random) -> float:
        self.price *= math.exp(self.sigma * rng.gauss(0, 1))
        self.history.append((ts_ms, self.price))
        if len(self.history) > 7200:
            self.history = self.history[-7200:]
        return self.price

    def avg_window(self, end_ms: int, seconds: int = 60) -> tuple[float, int]:
        vals = [v for t, v in self.history if end_ms - seconds * 1000 < t <= end_ms]
        return (sum(vals) / len(vals), len(vals)) if vals else (self.price, 0)


@dataclass
class MockMarket:
    ticker: str
    series: str
    asset: str
    open_ts: int
    close_ts: int
    floor_strike: float
    status: str = "active"
    result: str = ""
    expiration_value: str = ""
    yes: dict[str, float] = field(default_factory=dict)  # price str -> size (YES bids)
    no: dict[str, float] = field(default_factory=dict)  # price str -> size (NO bids)

    def api(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "event_ticker": self.ticker.rsplit("-", 1)[0],
            "market_type": "binary",
            "title": f"{self.asset} price up in next 15 mins?",
            "yes_sub_title": f"Target Price: {self.floor_strike:,.2f}",
            "no_sub_title": "",
            "created_time": iso(self.open_ts - 60),
            "updated_time": iso(time.time()),
            "open_time": iso(self.open_ts),
            "close_time": iso(self.close_ts),
            "expected_expiration_time": iso(self.close_ts),
            "latest_expiration_time": iso(self.close_ts + 3600),
            "settlement_timer_seconds": 5,
            "status": self.status,
            "result": self.result,
            "can_close_early": False,
            "strike_type": "greater_or_equal",
            "floor_strike": self.floor_strike,
            "expiration_value": self.expiration_value,
            "rules_primary": (
                f"If the simple average of the sixty seconds of CF Benchmarks' {ASSETS[self.asset][1]} "
                f"before {iso(self.close_ts)} is at least {self.floor_strike:.2f}, then the market resolves to Yes."
            ),
            "rules_secondary": "MOCK SIMULATOR MARKET — not real.",
            "price_level_structure": "linear_cent",
            "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
            "yes_bid_dollars": "0",
            "yes_ask_dollars": "0",
            "no_bid_dollars": "0",
            "no_ask_dollars": "0",
            "last_price_dollars": "0",
            "volume_fp": "0.00",
            "volume_24h_fp": "0.00",
            "open_interest_fp": "0.00",
            "notional_value_dollars": "1.0000",
            "settlement_bounds_type": "default",
        }


class Sub:
    def __init__(self, sid: int, channel: str, params: dict[str, Any]) -> None:
        self.sid = sid
        self.channel = channel
        self.tickers: set[str] = set(
            params.get("market_tickers") or ([params["market_ticker"]] if params.get("market_ticker") else [])
        )
        self.indices: set[str] = set(params.get("index_ids") or [])
        self.seq = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.subs: dict[int, Sub] = {}
        self.lock = asyncio.Lock()

    async def send(self, obj: dict[str, Any]) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(obj))


class MockExchange:
    def __init__(self, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.indices = {a: IndexSim(cf, p, s) for a, (_, cf, p, s) in ASSETS.items()}
        self.markets: dict[str, MockMarket] = {}
        self.clients: set[Client] = set()
        self.next_sid = 1
        self.shift: dict[str, float] = {}
        self.faults: dict[str, Any] = {}
        self.balance_cents = 50000
        self.orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.started = time.time()
        self._warm()

    def _warm(self) -> None:
        now_ms = int(time.time() * 1000)
        for sim in self.indices.values():
            for i in range(900, 0, -1):
                sim.step(now_ms - i * 1000, self.rng)
        self._roll(time.time())

    # ------------------------------------------------------------- markets
    def _ticker(self, series: str, close_ts: int) -> str:
        return f"{series}-{time.strftime('%y%b%d%H%M', time.gmtime(close_ts)).upper()}-{close_ts % 100:02d}"

    def _roll(self, now: float) -> None:
        qs = quarter_start(now)
        for asset, (series, _, _, _) in ASSETS.items():
            close = qs + QUARTER
            t = self._ticker(series, close)
            if t not in self.markets:
                sim = self.indices[asset]
                strike, _ = sim.avg_window(qs * 1000)
                m = MockMarket(t, series, asset, qs, close, round(strike, 2))
                self._quote(m, now)
                self.markets[t] = m

    def fair_yes(self, m: MockMarket, now: float) -> float:
        sim = self.indices[m.asset]
        tau = max(1.0, m.close_ts - now)
        sd = sim.sigma * sim.price * math.sqrt(max(1.0, tau - 40))
        return min(0.98, max(0.02, 1 - _norm_cdf((m.floor_strike - sim.price) / sd)))

    def _quote(self, m: MockMarket, now: float) -> None:
        fair = self.fair_yes(m, now) + self.shift.get(m.asset, 0.0)
        fair = min(0.97, max(0.03, fair))
        bid = math.floor((fair - 0.01) * 100) / 100
        ask = math.ceil((fair + 0.01) * 100) / 100
        bid = max(0.01, min(bid, 0.97))
        ask = min(0.99, max(ask, bid + 0.01))
        m.yes = {f"{bid - i * 0.01:.2f}": float(self.rng.randint(20, 200)) for i in range(4) if bid - i * 0.01 >= 0.01}
        no_bid = 1 - ask
        m.no = {
            f"{no_bid - i * 0.01:.2f}": float(self.rng.randint(20, 200)) for i in range(4) if no_bid - i * 0.01 >= 0.01
        }

    def book_snapshot(self, m: MockMarket) -> dict[str, Any]:
        return {
            "market_ticker": m.ticker,
            "market_id": str(uuid.uuid5(uuid.NAMESPACE_URL, m.ticker)),
            "yes_dollars_fp": [[f"{float(p):.4f}", f"{q:.2f}"] for p, q in sorted(m.yes.items())],
            "no_dollars_fp": [[f"{float(p):.4f}", f"{q:.2f}"] for p, q in sorted(m.no.items())],
        }

    # ------------------------------------------------------------- ticking
    async def run(self) -> None:
        last_quote = 0.0
        while True:
            now = time.time()
            now_ms = int(now * 1000)
            cf_paused = self.faults.get("cf_pause_until", 0) > now
            for sim in self.indices.values():
                sim.step(now_ms, self.rng)
            self._roll(now)
            for m in list(self.markets.values()):
                if m.status == "active" and now >= m.close_ts:
                    avg, _n = self.indices[m.asset].avg_window(m.close_ts * 1000)
                    m.status = "determined"
                    m.expiration_value = f"{avg:.2f}"
                    m.result = "yes" if avg >= m.floor_strike else "no"
                elif m.status == "determined" and now >= m.close_ts + 5:
                    m.status = "settled"
                if m.status == "settled" and now > m.close_ts + 4 * 3600:
                    del self.markets[m.ticker]
            if not cf_paused:
                await self._broadcast_cf(now_ms)
            if now - last_quote >= 0.5:
                last_quote = now
                await self._requote(now)
            await asyncio.sleep(max(0.01, 1.0 - (time.time() - now)))

    async def _broadcast_cf(self, now_ms: int) -> None:
        for c in list(self.clients):
            for sub in c.subs.values():
                if sub.channel != "cfbenchmarks_value":
                    continue
                for _asset, sim in self.indices.items():
                    if sim.index_id not in sub.indices and "all" not in sub.indices:
                        continue
                    avg60, n60 = sim.avg_window(now_ms - 1)
                    msg: dict[str, Any] = {
                        "index_id": sim.index_id,
                        "received_at": now_ms,
                        "data": json.dumps(
                            {"type": "value", "id": sim.index_id, "time": now_ms, "value": f"{sim.price:.8f}"}
                        ),
                        "avg_60s_data": {
                            "value": f"{avg60:.8f}",
                            "window_size": n60,
                            "window_start_ts_ms": now_ms - 60000,
                            "window_end_ts_exclusive": now_ms,
                        },
                    }
                    close_ms = ((now_ms - 1) // (QUARTER * 1000) + 1) * QUARTER * 1000
                    if close_ms - now_ms < 60000:
                        _avg, _n = sim.avg_window(now_ms)
                        vals = [v for t, v in sim.history if close_ms - 60000 < t <= now_ms]
                        if vals:
                            msg["last_60s_windowed_average_15min"] = {
                                "value": f"{sum(vals) / len(vals):.8f}",
                                "window_size": len(vals),
                                "window_start_ts_ms": close_ms - 60000,
                                "window_end_ts_exclusive": now_ms + 1,
                            }
                    with contextlib.suppress(Exception):
                        await c.send(
                            {
                                "type": "cfbenchmarks_value",
                                "sid": sub.sid,
                                "seq": sub.next_seq(),
                                "msg": msg,
                                "sending_ts_ms": now_ms,
                            }
                        )

    async def _requote(self, now: float) -> None:
        for m in self.markets.values():
            if m.status != "active":
                continue
            old_yes, old_no = dict(m.yes), dict(m.no)
            self._quote(m, now)
            deltas = []
            for side, old, new in (("yes", old_yes, m.yes), ("no", old_no, m.no)):
                for p in set(old) | set(new):
                    d = new.get(p, 0.0) - old.get(p, 0.0)
                    if abs(d) > 1e-9:
                        deltas.append((side, p, d))
            for c in list(self.clients):
                for sub in list(c.subs.values()):
                    if sub.channel != "orderbook_delta" or m.ticker not in sub.tickers:
                        continue
                    for side, p, d in deltas:
                        seq = sub.next_seq()
                        if self.faults.pop("seq_gap", False):
                            seq = sub.next_seq() + 1
                            sub.seq = seq
                        body = {
                            "market_ticker": m.ticker,
                            "market_id": str(uuid.uuid5(uuid.NAMESPACE_URL, m.ticker)),
                            "price_dollars": f"{float(p):.4f}",
                            "delta_fp": f"{d:.2f}",
                            "side": side,
                            "ts_ms": int(now * 1000),
                        }
                        frame = {"type": "orderbook_delta", "sid": sub.sid, "seq": seq, "msg": body}
                        with contextlib.suppress(Exception):
                            await c.send(frame)
                            if self.faults.pop("duplicate_delta", False):
                                await c.send(frame)
                            if self.faults.pop("malformed", False):
                                await c.ws.send_text("{not json")

    # ---------------------------------------------------------------- REST
    def rest_fault(self) -> JSONResponse | None:
        n = int(self.faults.get("rest_500", 0))
        if n > 0:
            self.faults["rest_500"] = n - 1
            return JSONResponse({"error": "mock injected 500"}, status_code=500)
        return None

    def market_list(self, req: Request) -> list[dict[str, Any]]:
        series = req.query_params.get("series_ticker")
        status = req.query_params.get("status")
        out = []
        for m in self.markets.values():
            if series and m.series != series:
                continue
            if status == "open" and m.status != "active":
                continue
            if status == "settled" and m.status != "settled":
                continue
            out.append(m.api())
        return out

    def match_ioc(self, body: dict[str, Any]) -> dict[str, Any]:
        m = self.markets.get(body["ticker"])
        coid = body.get("client_order_id") or str(uuid.uuid4())
        if any(o["client_order_id"] == coid for o in self.orders):
            raise ValueError("duplicate")
        order_id = str(uuid.uuid4())
        count = float(body["count"])
        price = float(body["price"])
        filled = 0.0
        cost = 0.0
        if m is not None and m.status == "active":
            if body["side"] == "bid":  # buy YES: consume NO bids where 1-no <= price
                levels = sorted(((1 - float(p), q, p) for p, q in m.no.items()), key=lambda x: x[0])
                book = m.no
            else:  # sell YES / buy NO at yes price: consume YES bids >= price
                levels = sorted(((float(p), q, p) for p, q in m.yes.items()), key=lambda x: -x[0])
                book = m.yes
            for px, q, key in levels:
                if (body["side"] == "bid" and px > price + 1e-9) or (body["side"] == "ask" and px < price - 1e-9):
                    break
                take = min(q, count - filled)
                if take <= 0:
                    break
                filled += take
                cost += take * px
                book[key] = q - take
                fill_yes_price = px
                self.fills.append(
                    {
                        "fill_id": str(uuid.uuid4()),
                        "trade_id": str(uuid.uuid4()),
                        "order_id": order_id,
                        "client_order_id": coid,
                        "ticker": m.ticker,
                        "market_ticker": m.ticker,
                        "outcome_side": "yes" if body["side"] == "bid" else "no",
                        "book_side": body["side"],
                        "count_fp": f"{take:.2f}",
                        "yes_price_dollars": f"{fill_yes_price:.4f}",
                        "no_price_dollars": f"{1 - fill_yes_price:.4f}",
                        "is_taker": True,
                        "fee_cost": f"{math.ceil(0.07 * take * px * (1 - px) * 100) / 100:.4f}",
                        "created_time": iso(time.time()),
                        "exchange_index": 0,
                    }
                )
        order = {
            "order_id": order_id,
            "client_order_id": coid,
            "ticker": body["ticker"],
            "status": "executed" if filled else "canceled",
            "fill_count_fp": f"{filled:.2f}",
            "initial_count_fp": f"{count:.2f}",
            "remaining_count_fp": "0.00",
            "book_side": body["side"],
            "outcome_side": "yes" if body["side"] == "bid" else "no",
            "created_time": iso(time.time()),
        }
        self.orders.append(order)
        return {
            "order_id": order_id,
            "client_order_id": coid,
            "fill_count": f"{filled:.2f}",
            "remaining_count": "0.00",
            "average_fill_price": f"{cost / filled:.4f}" if filled else None,
            "ts_ms": int(time.time() * 1000),
        }


def build_app(ex: MockExchange) -> Starlette:
    def need_auth(req: Request) -> JSONResponse | None:
        if not all(
            req.headers.get(h) for h in ("kalshi-access-key", "kalshi-access-timestamp", "kalshi-access-signature")
        ):
            return JSONResponse({"error": {"code": "authentication_error"}}, status_code=401)
        return None

    async def exchange_status(req: Request) -> JSONResponse:
        return ex.rest_fault() or JSONResponse({"exchange_active": True, "trading_active": True, "mock": True})

    async def series(req: Request) -> JSONResponse:
        t = req.path_params["t"]
        for _, (s, _cf, _, _) in ASSETS.items():
            if s == t:
                return JSONResponse(
                    {
                        "series": {
                            "ticker": s,
                            "frequency": "fifteen_min",
                            "title": f"MOCK {s}",
                            "category": "Crypto",
                            "categories": ["Crypto"],
                            "tags": [],
                            "settlement_sources": [{"name": "CF Benchmarks", "url": "https://www.cfbenchmarks.com"}],
                            "contract_url": "",
                            "contract_terms_url": "",
                            "fee_type": "quadratic",
                            "fee_multiplier": 1,
                        }
                    }
                )
        return JSONResponse({"error": "not found"}, status_code=404)

    async def markets(req: Request) -> JSONResponse:
        return ex.rest_fault() or JSONResponse({"markets": ex.market_list(req), "cursor": ""})

    async def market(req: Request) -> JSONResponse:
        m = ex.markets.get(req.path_params["t"])
        return JSONResponse({"market": m.api()}) if m else JSONResponse({"error": "not found"}, status_code=404)

    async def orderbook(req: Request) -> JSONResponse:
        m = ex.markets.get(req.path_params["t"])
        if not m:
            return JSONResponse({"error": "not found"}, status_code=404)
        snap = ex.book_snapshot(m)
        return JSONResponse(
            {"orderbook_fp": {"yes_dollars": snap["yes_dollars_fp"], "no_dollars": snap["no_dollars_fp"]}}
        )

    async def balance(req: Request) -> JSONResponse:
        return need_auth(req) or JSONResponse(
            {
                "balance": ex.balance_cents,
                "balance_dollars": f"{ex.balance_cents / 100:.2f}",
                "portfolio_value": ex.balance_cents,
                "updated_ts": int(time.time()),
            }
        )

    async def positions(req: Request) -> JSONResponse:
        return need_auth(req) or JSONResponse({"market_positions": [], "event_positions": [], "cursor": ""})

    async def orders(req: Request) -> JSONResponse:
        if (r := need_auth(req)) is not None:
            return r
        status = req.query_params.get("status")
        ticker = req.query_params.get("ticker")
        out = [o for o in ex.orders if (not status or o["status"] == status) and (not ticker or o["ticker"] == ticker)]
        return JSONResponse({"orders": out, "cursor": ""})

    async def fills(req: Request) -> JSONResponse:
        if (r := need_auth(req)) is not None:
            return r
        oid = req.query_params.get("order_id")
        return JSONResponse({"fills": [f for f in ex.fills if not oid or f["order_id"] == oid], "cursor": ""})

    async def settlements(req: Request) -> JSONResponse:
        return need_auth(req) or JSONResponse({"settlements": [], "cursor": ""})

    async def create_order(req: Request) -> JSONResponse:
        if (r := need_auth(req)) is not None:
            return r
        body = await req.json()
        try:
            return JSONResponse(ex.match_ioc(body), status_code=201)
        except ValueError:
            return JSONResponse({"error": {"code": "duplicate_client_order_id"}}, status_code=409)

    async def scenario(req: Request) -> JSONResponse:
        body = await req.json()
        ex.shift[str(body.get("asset", "BTC"))] = float(body.get("shift", 0.0))
        return JSONResponse({"ok": True, "shift": ex.shift})

    async def fault(req: Request) -> JSONResponse:
        body = await req.json()
        if body.get("drop_ws"):
            for c in list(ex.clients):
                with contextlib.suppress(Exception):
                    await c.ws.close(code=1011)
        if body.get("cf_pause_s"):
            ex.faults["cf_pause_until"] = time.time() + float(body["cf_pause_s"])
        for k in ("seq_gap", "duplicate_delta", "malformed"):
            if body.get(k):
                ex.faults[k] = True
        if body.get("rest_500"):
            ex.faults["rest_500"] = int(body["rest_500"])
        return JSONResponse({"ok": True, "faults": {k: v for k, v in ex.faults.items()}})

    async def state(req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "markets": [m.api() for m in ex.markets.values()],
                "clients": len(ex.clients),
                "indices": {k: s.price for k, s in ex.indices.items()},
                "orders": len(ex.orders),
            }
        )

    async def ws_endpoint(ws: WebSocket) -> None:
        if not all(
            ws.headers.get(h) for h in ("kalshi-access-key", "kalshi-access-timestamp", "kalshi-access-signature")
        ):
            await ws.close(code=4001)
            return
        await ws.accept()
        client = Client(ws)
        ex.clients.add(client)
        try:
            while True:
                raw = await ws.receive_text()
                cmd = json.loads(raw)
                mid, name, params = cmd.get("id"), cmd.get("cmd"), cmd.get("params") or {}
                if name == "subscribe":
                    for ch in params.get("channels", []):
                        sid = ex.next_sid
                        ex.next_sid += 1
                        sub = Sub(sid, ch, params)
                        client.subs[sid] = sub
                        await client.send({"id": mid, "type": "subscribed", "msg": {"channel": ch, "sid": sid}})
                        if ch == "orderbook_delta":
                            for t in sub.tickers:
                                m = ex.markets.get(t)
                                if m:
                                    await client.send(
                                        {
                                            "type": "orderbook_snapshot",
                                            "sid": sid,
                                            "seq": sub.next_seq(),
                                            "msg": ex.book_snapshot(m),
                                        }
                                    )
                elif name == "unsubscribe":
                    for sid in params.get("sids", []):
                        client.subs.pop(sid, None)
                    await client.send({"id": mid, "type": "unsubscribed", "msg": {}})
                elif name == "update_subscription":
                    upd = client.subs.get(int(params.get("sid") or 0))
                    if upd is None:
                        await client.send(
                            {"id": mid, "type": "error", "msg": {"code": 7, "msg": "Unknown subscription ID"}}
                        )
                        continue
                    if params.get("action") == "get_snapshot":
                        for t in params.get("market_tickers", []):
                            m = ex.markets.get(t)
                            if m:
                                await client.send(
                                    {
                                        "type": "orderbook_snapshot",
                                        "sid": upd.sid,
                                        "seq": upd.next_seq(),
                                        "msg": ex.book_snapshot(m),
                                    }
                                )
                    await client.send({"id": mid, "type": "ok", "msg": {}})
                else:
                    await client.send({"id": mid, "type": "error", "msg": {"code": 5, "msg": "Unknown command"}})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            ex.clients.discard(client)

    p = "/trade-api/v2"
    routes = [
        Route(f"{p}/exchange/status", exchange_status),
        Route(f"{p}/series/{{t}}", series),
        Route(f"{p}/markets", markets),
        Route(f"{p}/markets/{{t}}", market),
        Route(f"{p}/markets/{{t}}/orderbook", orderbook),
        Route(f"{p}/portfolio/balance", balance),
        Route(f"{p}/portfolio/positions", positions),
        Route(f"{p}/portfolio/orders", orders),
        Route(f"{p}/portfolio/fills", fills),
        Route(f"{p}/portfolio/settlements", settlements),
        Route(f"{p}/portfolio/events/orders", create_order, methods=["POST"]),
        Route("/mock/scenario", scenario, methods=["POST"]),
        Route("/mock/fault", fault, methods=["POST"]),
        Route("/mock/state", state),
        WebSocketRoute("/trade-api/ws/v2", ws_endpoint),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        task = asyncio.create_task(ex.run())
        yield
        task.cancel()

    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    uvicorn.run(build_app(MockExchange(a.seed)), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
