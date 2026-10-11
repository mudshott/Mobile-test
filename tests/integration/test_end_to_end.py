"""End-to-end: real Engine + API against the in-process MOCK exchange (localhost).

Covers: startup STOPPED → READY, WS + books + CF, PAPER start, deterministic
qualifying opportunity → paper fill → persistence, faults (WS drop, sequence
gap, CF outage) with recovery, STOP, restart returns STOPPED, API guards.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from predictionbot.api.server import create_app
from predictionbot.app.engine import ControlError, Engine
from predictionbot.config import load_settings
from predictionbot.sim.mock_exchange import MockExchange, build_app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _until(cond, timeout: float = 30.0, step: float = 0.2) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        await asyncio.sleep(step)
    raise AssertionError("timed out waiting for condition")


@pytest.fixture
async def mock_exchange():
    port = _free_port()
    ex = MockExchange(seed=7)
    server = uvicorn.Server(uvicorn.Config(build_app(ex), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    await _until(lambda: server.started, 10)
    yield ex, port
    server.should_exit = True
    await task


def _settings(tmp_path: Path, port: int, key_file: Path):
    env = {
        "PCB_ENV": "mock",
        "PCB_MOCK_REST": f"http://127.0.0.1:{port}/trade-api/v2",
        "PCB_MOCK_WS": f"ws://127.0.0.1:{port}/trade-api/ws/v2",
        "KALSHI_API_KEY_ID": "mock-key-0001",
        "KALSHI_PRIVATE_KEY_PATH": str(key_file),
        "PCB_NO_BROWSER": "1",
    }
    s = load_settings(tmp_path, env)
    s.portfolio_reconcile_s = 2.0
    s.discovery_interval_s = 2.0
    return s


def _seed_cf_history(engine: Engine, ex: MockExchange) -> None:
    """Test wiring: give the model the simulator's own recent index path (mock data, mock env).

    Live ticks have already arrived, so the history is merged into the volatility
    buffer directly (the feed itself correctly rejects out-of-order ticks).
    """
    for sim in ex.indices.values():
        st = engine.cf.indices[sim.index_id]
        merged = sorted(set(st.ticks) | set(sim.history[-900:]))
        st.ticks.clear()
        st.ticks.extend(merged)


async def test_full_paper_lifecycle(mock_exchange, tmp_path: Path, key_file: Path) -> None:
    ex, port = mock_exchange
    settings = _settings(tmp_path, port, key_file)
    engine = Engine(settings)
    await engine.start()
    try:
        assert engine.bot_state == "STOPPED"  # opening never starts trading
        await _until(lambda: engine.health()["components"]["WEBSOCKET"]["status"] == "OK", 20)
        await _until(lambda: len(engine.registry.active_verified()) == 3, 20)
        await _until(lambda: engine.health()["components"]["ORDER_BOOKS"]["status"] == "OK", 20)
        await _until(lambda: engine.health()["components"]["CF_BENCHMARKS"]["status"] == "OK", 20)
        await _until(lambda: engine.health()["components"]["RECONCILIATION"]["status"] == "OK", 20)
        _seed_cf_history(engine, ex)
        await _until(lambda: engine.health()["components"]["MODEL"]["status"] == "OK", 20)
        h = engine.health()
        assert h["readiness"]["paper_ready"], h["readiness"]
        assert not h["readiness"]["live_ready"]  # mock env + no validation → LIVE blocked
        with pytest.raises(ControlError):
            engine.set_mode("LIVE")
            engine.confirm_live("LIVE")
            await engine.start_bot()
        engine.set_mode("PAPER")
        # Evaluations persist as observations, never as trades.
        await _until(lambda: engine.counters()["totals"]["observations"] > 0, 10)
        assert engine.counters()["totals"]["orders"] == 0

        await engine.start_bot()
        assert engine.bot_state == "RUNNING"
        # Deterministic qualifying opportunity: make SOL YES / BTC NO cheap relative to the model.
        mkt = {t.info.asset: t.info for t in engine.registry.active()}
        pred_sol = engine.model.predict(mkt["SOL"])
        ex.shift["SOL"] = -0.25 if (pred_sol.raw_prob_yes or 0) >= 0.5 else 0.25
        await _until(lambda: engine.counters()["session"].get("orders", 0) >= 1, 30)
        await _until(lambda: engine.counters()["session"].get("fills", 0) >= 1, 10)
        orders = engine.db.query("SELECT * FROM orders WHERE mode='PAPER'")
        assert orders and orders[0]["status"] in ("FILLED", "PARTIALLY_FILLED")
        assert engine.db.query("SELECT * FROM risk_decisions WHERE verdict IN ('APPROVED','REDUCED')")
        assert engine.paper_ledger.open_positions()
        ex.shift.clear()

        # ---- faults while running
        for c in list(ex.clients):
            await c.ws.close(code=1011)
        await _until(lambda: engine.ws.stats.reconnects >= 1 and engine.ws.connected, 20)
        await _until(lambda: engine.health()["components"]["ORDER_BOOKS"]["status"] == "OK", 20)
        ex.faults["seq_gap"] = True
        await _until(lambda: engine.ws.stats.gaps >= 1, 10)
        await _until(lambda: engine.health()["components"]["ORDER_BOOKS"]["status"] == "OK", 20)
        ex.faults["cf_pause_until"] = time.time() + 9
        await _until(lambda: engine.health()["components"]["CF_BENCHMARKS"]["status"] == "FAIL", 15)
        assert all(
            w["reason"].startswith(("CF_STALE", "TOO_", "INVALID")) or w["decision"] == "NO_TRADE"
            for w in engine.watch.values()
        )
        await _until(lambda: engine.health()["components"]["CF_BENCHMARKS"]["status"] == "OK", 20)

        # ---- STOP: no new orders, positions kept
        n_orders = engine.counters()["session"]["orders"]
        await engine.stop_bot()
        assert engine.bot_state == "STOPPED"
        ex.shift["SOL"] = -0.3
        await asyncio.sleep(3)
        assert engine.db.one("SELECT COUNT(*) n FROM orders WHERE mode='PAPER'")["n"] == n_orders
        assert engine.paper_ledger.open_positions()
        state = engine.state()
        assert state["counters"]["session"]["win_rate"] == "N/A"  # nothing settled yet
        assert state["build"]["model_version"] and state["build"]["schema_version"] == 1
    finally:
        await engine.shutdown()

    # ---- restart: always STOPPED, open position restored, cash restored from fills
    engine2 = Engine(settings)
    await engine2.start()
    try:
        assert engine2.bot_state == "STOPPED" and engine2.mode == "PAPER"
        assert engine2.paper_ledger.open_positions()
        assert engine2.paper_ledger.cash < 1000
    finally:
        await engine2.shutdown()


async def test_api_controls_and_guards(mock_exchange, tmp_path: Path, key_file: Path) -> None:
    _ex, port = mock_exchange
    engine = Engine(_settings(tmp_path, port, key_file))
    app = create_app(engine)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await c.get("/api/state")
            assert r.status_code == 200 and r.json()["bot"]["state"] == "STOPPED"
            assert (await c.get("/")).status_code == 200
            assert (await c.get("/static/app.js")).status_code == 200
            b = (await c.get("/api/build")).json()
            assert {
                "app_version",
                "git_commit",
                "build_timestamp",
                "model_version",
                "strategy_version",
                "schema_version",
            } <= set(b)
            assert (await c.post("/api/bot/start")).status_code == 403  # no header
            hdr = {"X-PCB-Client": "dashboard"}
            assert (
                await c.post("/api/bot/start", headers={**hdr, "Origin": "https://evil.example"})
            ).status_code == 403
            r = await c.post("/api/bot/mode", json={"mode": "LIVE"}, headers=hdr)
            assert r.json()["mode"] == "LIVE"
            r = await c.post("/api/bot/confirm_live", json={"text": "live"}, headers=hdr)
            assert r.status_code == 409  # must type LIVE exactly
            r = await c.post("/api/bot/start", headers=hdr)
            assert r.status_code == 409 and "LIVE" in r.json()["error"]
            assert (await c.post("/api/bot/mode", json={"mode": "BOGUS"}, headers=hdr)).status_code == 409
            r = await c.post("/api/bot/profile", json={"profile": "SAFE_RETURNS"}, headers=hdr)
            assert r.json()["profile"] == "SAFE_RETURNS"
            await c.post("/api/bot/mode", json={"mode": "PAPER"}, headers=hdr)
            r = await c.post("/api/bot/emergency_stop", headers=hdr)
            assert r.json()["ok"]
            r = await c.post("/api/bot/start", headers=hdr)
            assert r.status_code == 409 and "kill switch" in r.json()["error"]
            assert (await c.post("/api/bot/reset_kill_switch", headers=hdr)).json()["ok"]
            v = (await c.post("/api/validation/run", headers=hdr)).json()
            assert v["overall"] in ("INSUFFICIENT_DATA", "NOT_RUN") and v["live_capital_justified"] is False
            s = (await c.get("/api/state")).json()
            assert "Deposits" in s["security"]["note"]
            assert not any(
                "withdraw" in e or "deposit" in e or "transfer" in e for e in s["security"]["allowed_endpoints"]
            )
