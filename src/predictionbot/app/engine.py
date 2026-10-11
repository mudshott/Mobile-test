"""Application engine: the single authoritative owner of runtime state.

Normal flow: open → initialize → connect → reconcile → discover → sync books
→ receive CF data → warm model → READY. The bot itself always starts STOPPED;
only an explicit START (and for LIVE an explicit typed confirmation) enables
order submission. Evaluation runs continuously while stopped so the operator
can see probabilities / EV / reasons, but nothing is submitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from decimal import Decimal
from typing import Any

from predictionbot.buildinfo import MODEL_VERSION, STRATEGY_VERSION, build_info
from predictionbot.config import Settings
from predictionbot.execution.base import OrderIntent
from predictionbot.execution.live import LiveExecutor, normalize_fill
from predictionbot.execution.paper import PaperExecutor, ShadowExecutor
from predictionbot.fixedpoint import ZERO, D, D_or_none
from predictionbot.kalshi.auth import KalshiSigner, SignerError
from predictionbot.kalshi.firewall import allowed_endpoints
from predictionbot.kalshi.rest import KalshiError, KalshiRest
from predictionbot.kalshi.ws import ConnectFn, KalshiWS, ws_body
from predictionbot.market.books import BookManager
from predictionbot.market.discovery import MarketRegistry, TrackedMarket
from predictionbot.market.models import MarketInfo
from predictionbot.market.orderbook import BookState
from predictionbot.model.probability import Prediction, SettlementModel
from predictionbot.persistence.db import Database, DatabaseError
from predictionbot.portfolio.ledger import Ledger, direction_of
from predictionbot.reference.cf import CFReferenceFeed, IndexState
from predictionbot.research.knowledge import list_hypotheses, seed_knowledge
from predictionbot.research.validation import Validator, assign_partition
from predictionbot.risk.governor import OpsStatus, Proposal, RiskGovernor, effective_profile
from predictionbot.risk.profiles import PROFILES, ProfileName, RiskProfile
from predictionbot.risk.sizing import size_order
from predictionbot.strategy.evaluator import Evaluation, ExecutionAssumptions, evaluate
from predictionbot.strategy.reasons import Reason

log = logging.getLogger(__name__)

MODES = ("PAPER", "SHADOW", "LIVE")
PERSIST_EVERY_S = 5.0
ORDER_COOLDOWN_S = 30.0


class ControlError(RuntimeError):
    pass


def _round1(x: float | None) -> float | None:
    return round(x, 1) if x is not None else None


def _f(x: Any) -> str | None:
    return str(x) if x is not None else None


class Engine:
    def __init__(
        self,
        settings: Settings,
        *,
        ws_connect: ConnectFn | None = None,
        http_transport: Any = None,
    ) -> None:
        self.settings = settings
        self.started_ts = time.time()
        self.db = Database(settings.db_path, settings.data_dir / "backups")
        self.signer: KalshiSigner | None = None
        self.signer_error: str | None = None
        if settings.api_key_id and settings.private_key_path:
            try:
                self.signer = KalshiSigner.from_file(settings.api_key_id, settings.private_key_path)
            except SignerError as exc:
                self.signer_error = str(exc)
        else:
            self.signer_error = "API key id / private key path not configured"
        self.rest = KalshiRest(
            settings.rest_base,
            self.signer,
            read_rate_per_s=settings.read_rate_per_s,
            write_rate_per_s=settings.write_rate_per_s,
            transport=http_transport,
        )
        self.ws = KalshiWS(settings.ws_url, self.signer, self._on_ws, connect_fn=ws_connect)
        self.ws.listeners.append(self._on_ws_event)
        self.cf = CFReferenceFeed(settings.cf_stale_s)
        self.registry = MarketRegistry(settings.series)
        self.books = BookManager(self.ws, self.rest, max_quiet_s=settings.book_max_quiet_s)
        self.model = SettlementModel(self.cf)
        self.governor = RiskGovernor()
        self.assumptions = ExecutionAssumptions(book_stale_s=settings.book_stale_s)
        self.paper_ledger = Ledger("PAPER", self.db, D(settings.paper_starting_capital))
        self.live_ledger = Ledger("LIVE", self.db, ZERO)
        self.live_ledger.cash_source = "exchange"
        self.paper_exec = PaperExecutor(self.paper_ledger, self.books.get, self.db, book_stale_s=settings.book_stale_s)
        self.shadow_exec = ShadowExecutor(self.books.get, self.db)
        self.live_exec = LiveExecutor(self.rest, self.live_ledger, self.db, subaccount=settings.subaccount)
        self.validator = Validator(self.db)
        # ---- control state (always STOPPED at process start)
        self.bot_state = "STOPPED"
        self.mode = "PAPER"
        self.profile = ProfileName.BALANCED
        self.live_confirmed = False
        self.kill_switch = False
        self.session_id: int | None = None
        self.session_started_ts: float | None = None
        self.stop_reason: str | None = None
        # ---- runtime observations
        self.watch: dict[str, dict[str, Any]] = {}
        self._last_persist: dict[str, float] = {}
        self._last_decision: dict[str, str] = {}
        self._cooldown: dict[str, float] = {}
        self._acting: set[str] = set()
        self._cf_buffer: list[tuple[str, int, float, float]] = []
        self.auth: dict[str, Any] = {"ok": False, "detail": "not checked", "checked_ts": None}
        self.rest_public_ok_ts: float | None = None
        self.exchange_status: dict[str, Any] = {}
        self.recon: dict[str, Any] = {
            "ok": False,
            "last_ok_ts": None,
            "last_attempt_ts": None,
            "error": None,
            "resting_orders": 0,
            "positions": 0,
            "balance": None,
            "portfolio_value": None,
        }
        self.order_update_ts: float | None = None
        self.validation_summary: dict[str, Any] = {"overall": "NOT_RUN"}
        self.hc_override_allowed = False
        self.events: deque[dict[str, Any]] = deque(maxlen=300)
        self._tasks: list[asyncio.Task[None]] = []
        self._fill_watermark_ts: float = time.time() - 3600
        self._health_last: dict[str, str] = {}
        self._started = False
        self.loop_errors = 0

    # ================================================================ lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.db.open()
        seed_knowledge(self.db)
        self.live_exec.load_known_fills()
        recovered = self.live_exec.recover_unresolved_from_db()
        if recovered:
            self.event("EXECUTION", "RECOVERED_UNRESOLVED_ORDERS", f"{recovered} LIVE order(s) need reconciliation")
        self._mark_abandoned_sessions()
        self.paper_ledger.load_open_positions({})
        self._restore_paper_cash()
        self._reload_cf_history()
        if self.signer_error:
            self.event("AUTH", "CREDENTIALS_MISSING", self.signer_error)
        await self._setup_private_subscriptions()
        self.ws.start()
        self._tasks = [
            asyncio.create_task(self._guard(self._initial_and_discovery_loop), name="discovery"),
            asyncio.create_task(self._guard(self._eval_loop), name="eval"),
            asyncio.create_task(self._guard(self._books_loop), name="books"),
            asyncio.create_task(self._guard(self._reconcile_loop), name="reconcile"),
            asyncio.create_task(self._guard(self._settlement_loop), name="settlement"),
            asyncio.create_task(self._guard(self._persist_loop), name="persist"),
            asyncio.create_task(self._guard(self._validation_loop), name="validation"),
        ]
        self.event("APP", "STARTED", f"environment={self.settings.environment} bot=STOPPED")

    async def shutdown(self) -> None:
        if self.bot_state == "RUNNING":
            await self.stop_bot("application shutdown")
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        await self.ws.stop()
        await self.rest.close()
        self._flush_cf()
        self.db.close()

    async def _guard(self, fn: Any) -> None:
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.loop_errors += 1
                log.exception("loop %s crashed; restarting", fn.__name__)
                self.event("APP", "LOOP_ERROR", f"{fn.__name__}: {exc.__class__.__name__}: {exc}")
                await asyncio.sleep(2)

    def event(self, component: str, code: str, detail: str = "") -> None:
        rec = {"ts": time.time(), "component": component, "code": code, "detail": detail[:500]}
        self.events.appendleft(rec)
        log.info("[%s] %s %s", component, code, detail)
        try:
            if self.db._conn is not None:
                self.db.insert("health_events", rec)
        except DatabaseError:
            pass

    def _mark_abandoned_sessions(self) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_ts=?, stop_reason='process exited while running' WHERE ended_ts IS NULL",
            (time.time(),),
        )

    def _reload_cf_history(self, max_age_s: float = 1800.0) -> None:
        """Re-seed the volatility buffer from persisted REAL CF ticks (never fabricated).

        Freshness is NOT restored: an index only becomes fresh when a new live tick arrives.
        """
        since = int((time.time() - max_age_s) * 1000)
        rows = self.db.query(
            "SELECT index_id, source_ts_ms, value FROM cf_ticks WHERE source_ts_ms >= ? ORDER BY source_ts_ms", (since,)
        )
        for r in rows:
            st = self.cf.indices.setdefault(r["index_id"], IndexState(r["index_id"]))
            st.ticks.append((int(r["source_ts_ms"]), float(r["value"])))
            st.last_source_ts_ms = int(r["source_ts_ms"])
            st.last_value = float(r["value"])
        if rows:
            self.event("CF", "CF_HISTORY_RELOADED", f"{len(rows)} persisted ticks (last {max_age_s / 60:.0f} min)")

    def _restore_paper_cash(self) -> None:
        """Paper cash = starting capital − all paper fill costs − fees + settlement payouts."""
        start = D(self.settings.paper_starting_capital)
        spent = sum(
            (
                D(r["count"]) * D(r["price"]) + D(r["fee"])
                for r in self.db.query("SELECT count, price, fee FROM fills WHERE mode='PAPER'")
            ),
            ZERO,
        )
        payouts = sum(
            (D(r["payout"]) for r in self.db.query("SELECT payout FROM settlements WHERE mode='PAPER'")), ZERO
        )
        self.paper_ledger.cash = start - spent + payouts
        self.paper_ledger.start_session()
        self.paper_ledger.day_start_equity = self.paper_ledger.equity

    # ============================================================ WS handling
    async def _setup_private_subscriptions(self) -> None:
        await self.ws.ensure("lifecycle", "market_lifecycle_v2", {})
        await self.ws.ensure("fill", "fill", {})
        await self.ws.ensure("user_orders", "user_orders", {})
        await self.ws.ensure("positions", "market_positions", {})

    async def _sync_cf_subscription(self) -> None:
        idx = sorted(self.registry.expected_indices())
        if not idx:
            return
        self.cf.expect(set(idx))
        await self.ws.ensure("cf", "cfbenchmarks_value", {"index_ids": idx})

    def _on_ws_event(self, code: str, detail: str) -> None:
        if code in ("WS_DISCONNECTED", "WS_RECONNECTING"):
            self.books.on_disconnect()
        if code in (
            "WS_CONNECTED",
            "WS_RECONNECTED",
            "WS_RESUBSCRIBED",
            "WS_SEQUENCE_GAP",
            "WS_ERROR",
            "WS_DISCONNECTED",
        ):
            try:
                if self.db._conn is not None:
                    self.db.insert(
                        "health_events",
                        {"ts": time.time(), "component": "WEBSOCKET", "code": code, "detail": detail[:500]},
                    )
            except DatabaseError:
                pass

    async def _on_ws(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        body = ws_body(msg)
        if mtype in ("orderbook_snapshot", "orderbook_delta"):
            await self.books.on_message(msg)
        elif mtype == "cfbenchmarks_value":
            idx = self.cf.on_message(msg, msg.get("_recv_ts"))
            if idx:
                latest = self.cf.latest(idx)
                if latest:
                    self._cf_buffer.append((idx, latest[0], latest[1], time.time()))
        elif mtype == "fill":
            nf = normalize_fill(body)
            if nf is not None:
                self.order_update_ts = time.time()
                if self.live_exec.apply_fill(nf):
                    self.event("EXECUTION", "LIVE_FILL", f"{nf.ticker} {nf.outcome} {nf.count}@{nf.price}")
        elif mtype == "user_order":
            self.order_update_ts = time.time()
            coid = body.get("client_order_id")
            if coid and str(coid).startswith("pcb-l-"):
                status = str(body.get("status", ""))
                mapped = {"resting": "RESTING", "canceled": "CANCELED", "executed": "FILLED"}.get(status)
                if mapped:
                    self.db.execute(
                        "UPDATE orders SET status=?, exchange_order_id=COALESCE(exchange_order_id, ?), "
                        "filled_count=?, updated_ts=? WHERE client_order_id=? AND status NOT IN "
                        "('PENDING_SUBMIT','AMBIGUOUS')",
                        (mapped, body.get("order_id"), str(body.get("fill_count_fp", "0")), time.time(), coid),
                    )
        elif mtype == "market_position":
            self.order_update_ts = time.time()
        elif mtype == "market_lifecycle_v2":
            self.registry.apply_lifecycle(body)

    # ============================================================== discovery
    async def _initial_and_discovery_loop(self) -> None:
        while True:
            await self._discovery_tick()
            now = time.time()
            next_boundary = (int(now) // 900 + 1) * 900 + 2.0
            delay = min(self.settings.discovery_interval_s, max(1.0, next_boundary - now))
            await asyncio.sleep(delay)

    async def _discovery_tick(self) -> None:
        try:
            self.exchange_status = await self.rest.exchange_status()
            self.rest_public_ok_ts = time.time()
        except KalshiError as exc:
            self.event("REST", "EXCHANGE_STATUS_FAILED", str(exc)[:200])
        if self.signer is not None and (not self.auth["ok"] or time.time() - (self.auth["checked_ts"] or 0) > 300):
            await self._check_auth()
        try:
            new, closed = await self.registry.refresh(self.rest)
        except KalshiError as exc:
            self.event("DISCOVERY", "DISCOVERY_FAILED", str(exc)[:200])
            return
        for ticker in new:
            tm = self.registry.markets[ticker]
            self._persist_market(tm)
            await self.books.track(ticker)
            self.event(
                "DISCOVERY",
                "MARKET_ADDED",
                f"{ticker} closes {time.strftime('%H:%M:%S', time.gmtime(tm.info.close_ts))}Z "
                f"verified={tm.verified} {'; '.join(tm.verify_notes)}",
            )
        for ticker in closed:
            await self._on_market_closed(ticker)
        for tm in self.registry.active():
            self._persist_market(tm)
        await self._sync_cf_subscription()
        await self._restore_position_markets()

    async def _restore_position_markets(self) -> None:
        tickers = {p.ticker for p in self.paper_ledger.open_positions()} | {
            p.ticker for p in self.live_ledger.open_positions()
        }
        for t in tickers:
            if t in self.registry.markets:
                continue
            series = next((s for s in self.registry.series.values() if t.startswith(s.spec.series_ticker + "-")), None)
            if series is None:
                continue
            try:
                raw = await self.rest.get_market(t)
                info = MarketInfo.from_api(
                    raw,
                    series_ticker=series.spec.series_ticker,
                    asset=series.spec.asset,
                    cf_index_id=series.spec.cf_index_id,
                )
            except (KalshiError, KeyError, ValueError) as exc:
                log.debug("restore market %s failed: %s", t, exc)
                continue
            phase = "ACTIVE" if info.is_open_now(time.time()) else "CLOSED"
            self.registry.markets[t] = TrackedMarket(
                info, phase, False, ["restored after restart"], time.time(), time.time()
            )
            self._persist_market(self.registry.markets[t])
            if phase == "ACTIVE":
                await self.books.track(t)

    async def _check_auth(self) -> None:
        try:
            bal = await self.rest.get_balance(self.settings.subaccount)
            self.auth = {"ok": True, "detail": "authenticated (GET /portfolio/balance)", "checked_ts": time.time()}
            self._apply_balance(bal)
        except KalshiError as exc:
            self.auth = {"ok": False, "detail": f"auth check failed: {str(exc)[:200]}", "checked_ts": time.time()}
            self.event("AUTH", "AUTH_FAILED", str(exc)[:200])

    def _persist_market(self, tm: TrackedMarket) -> None:
        i = tm.info
        self.db.upsert(
            "markets",
            {
                "ticker": i.ticker,
                "series": i.series_ticker,
                "asset": i.asset,
                "cf_index": i.cf_index_id,
                "event_ticker": i.event_ticker,
                "open_ts": i.open_ts,
                "close_ts": i.close_ts,
                "strike_type": i.strike_type,
                "floor_strike": _f(i.floor_strike),
                "cap_strike": _f(i.cap_strike),
                "status": i.status,
                "result": i.result or None,
                "expiration_value": _f(i.expiration_value),
                "verified": int(tm.verified),
                "verify_notes": "; ".join(tm.verify_notes),
                "first_seen_ts": tm.first_seen_ts,
                "updated_ts": time.time(),
            },
            keys=("ticker",),
        )
        assign_partition(
            self.db,
            i.ticker,
            i.close_ts,
            live_session_running=self.bot_state == "RUNNING" and self.mode in ("PAPER", "LIVE"),
        )

    async def _on_market_closed(self, ticker: str) -> None:
        await self.books.untrack(ticker)
        w = self.watch.get(ticker)
        if w is not None:
            w["phase"] = "CLOSED"
        self.event("ROTATION", "MARKET_CLOSED", f"{ticker}: entries stopped, awaiting official result")

    # ============================================================ evaluation
    async def _eval_loop(self) -> None:
        while True:
            t0 = time.time()
            await self._eval_tick(t0)
            await asyncio.sleep(max(0.05, self.settings.eval_interval_s - (time.time() - t0)))

    def selected_profile(self) -> RiskProfile:
        return PROFILES[self.profile]

    def _ledger(self) -> Ledger:
        return self.live_ledger if self.mode == "LIVE" else self.paper_ledger

    async def _eval_tick(self, now: float) -> None:
        for ticker in self.registry.close_expired(now):
            await self._on_market_closed(ticker)
        ops = self._ops_status("")
        eff, _notes = effective_profile(self.profile, self._ledger(), ops)
        for tm in self.registry.active():
            info = tm.info
            pred = self.model.predict(info, now)
            book = self.books.get(info.ticker)
            ev = evaluate(
                info,
                book,
                pred,
                eff,
                verified=tm.verified,
                cf_fresh=self.cf.is_fresh(info.cf_index_id, now),
                assumptions=self.assumptions,
                now=now,
            )
            if self.bot_state != "RUNNING" and ev.decision != "NO_TRADE":
                action = f"{ev.decision} (bot stopped: not submitted)"
            else:
                action = ev.decision
            self._update_watch(tm, pred, ev, action, now)
            self._maybe_persist(info, pred, ev, book, now)
            if (
                self.bot_state == "RUNNING"
                and ev.decision != "NO_TRADE"
                and now - self._cooldown.get(info.ticker, 0.0) >= ORDER_COOLDOWN_S
                and info.ticker not in self._acting
            ):
                self._acting.add(info.ticker)
                try:
                    await self._act(tm, pred, ev, eff)
                finally:
                    self._acting.discard(info.ticker)

    def _update_watch(self, tm: TrackedMarket, pred: Prediction, ev: Evaluation, action: str, now: float) -> None:
        info = tm.info
        book = self.books.get(info.ticker)
        self.watch[info.ticker] = {
            "ticker": info.ticker,
            "asset": info.asset,
            "phase": tm.phase,
            "verified": tm.verified,
            "verify_notes": tm.verify_notes,
            "seconds_to_close": round(info.close_ts - now, 1),
            "strike_type": info.strike_type,
            "floor_strike": _f(info.floor_strike),
            "cap_strike": _f(info.cap_strike),
            "cf_index": info.cf_index_id,
            "spot": pred.spot,
            "model_state": pred.state,
            "model_missing": pred.missing,
            "raw_prob_yes": pred.raw_prob_yes,
            "cons_prob_yes": pred.cons_prob_yes,
            "cons_prob_no": pred.cons_prob_no,
            "uncertainty": pred.uncertainty,
            "disagreement": pred.disagreement,
            "expected_avg": pred.expected_avg,
            "window_observed": pred.window_observed,
            "n_returns": pred.n_returns,
            "yes_ask": _f(book.yes_ask()) if book else None,
            "no_ask": _f(book.no_ask()) if book else None,
            "yes_bid": _f(book.best_yes_bid()) if book else None,
            "spread": _f(ev.spread),
            "book_state": book.state.value if book else "MISSING",
            "book_age_s": _round1(book.age(now)) if book else None,
            "evaluation": ev.public(),
            "decision": ev.decision,
            "action": action,
            "reason": ev.primary_reason,
            "updated_ts": now,
        }

    def _maybe_persist(self, info: MarketInfo, pred: Prediction, ev: Evaluation, book: Any, now: float) -> None:
        changed = self._last_decision.get(info.ticker) != ev.primary_reason.split(":")[0]
        if (
            not changed
            and now - self._last_persist.get(info.ticker, 0.0) < PERSIST_EVERY_S
            and ev.decision == "NO_TRADE"
        ):
            return
        self._last_persist[info.ticker] = now
        self._last_decision[info.ticker] = ev.primary_reason.split(":")[0]
        try:
            pid = self.db.insert(
                "predictions",
                {
                    "ts": now,
                    "ticker": info.ticker,
                    "session_id": self.session_id,
                    "model_version": MODEL_VERSION,
                    "state": pred.state,
                    "raw_prob_yes": pred.raw_prob_yes,
                    "cons_prob_yes": pred.cons_prob_yes,
                    "cons_prob_no": pred.cons_prob_no,
                    "lower_prob_yes": pred.lower_prob_yes,
                    "upper_prob_yes": pred.upper_prob_yes,
                    "uncertainty": pred.uncertainty,
                    "disagreement": pred.disagreement,
                    "seconds_to_close": pred.seconds_to_close,
                    "spot": pred.spot,
                    "expected_avg": pred.expected_avg,
                    "sd_avg": pred.sd_avg,
                    "sigma_short": pred.sigma_short,
                    "sigma_long": pred.sigma_long,
                    "n_returns": pred.n_returns,
                    "coverage": pred.coverage,
                    "window_observed": pred.window_observed,
                    "yes_bid": _f(book.best_yes_bid()) if book else None,
                    "yes_ask": _f(book.yes_ask()) if book else None,
                    "no_bid": _f(book.best_no_bid()) if book else None,
                    "no_ask": _f(book.no_ask()) if book else None,
                    "spread": _f(ev.spread),
                    "missing": pred.missing,
                    "research": pred.research,
                },
            )
            chosen = ev.chosen
            best = chosen or max(
                (s for s in (ev.yes, ev.no) if s is not None and s.adjusted_ev is not None),
                key=lambda s: s.adjusted_ev or ZERO,
                default=None,
            )
            ev_id = self.db.insert(
                "evaluations",
                {
                    "ts": now,
                    "ticker": info.ticker,
                    "session_id": self.session_id,
                    "prediction_id": pid,
                    "mode": self.mode,
                    "profile": ev.profile,
                    "decision": ev.decision,
                    "primary_reason": ev.primary_reason,
                    "reasons": ev.reasons,
                    "side": best.outcome if best else None,
                    "best_ask": _f(best.best_ask) if best else None,
                    "limit_price": _f(best.limit_price) if best else None,
                    "raw_edge": best.raw_edge if best else None,
                    "adjusted_ev": _f(best.adjusted_ev) if best else None,
                    "p_cons": best.p_cons if best else None,
                    "depth": _f(best.depth_at_limit) if best else None,
                    "seconds_to_close": ev.seconds_to_close,
                },
            )
            self.watch.get(info.ticker, {})["evaluation_id"] = ev_id
        except DatabaseError as exc:
            self.event("DATABASE", "WRITE_FAILED", str(exc))

    def _ops_status(self, ticker: str) -> OpsStatus:
        now = time.time()
        ws_age = self.ws.message_age()
        ok_db, _ = self.db.healthy() if self.db._conn is not None else (False, "closed")
        api_ok = self.rest.stats.last_ok_ts is not None and now - self.rest.stats.last_ok_ts < 90
        recon_ok = True
        if self.mode == "LIVE":
            last = self.recon.get("last_ok_ts")
            recon_ok = (
                bool(self.recon.get("ok"))
                and last is not None
                and now - last < 2.5 * self.settings.portfolio_reconcile_s
            )
        orders_in_market = 0
        if ticker and self.session_id is not None:
            row = self.db.one(
                "SELECT COUNT(*) AS n FROM orders WHERE session_id=? AND ticker=? AND mode=? AND "
                "status NOT IN ('REJECTED','NOT_FOUND')",
                (self.session_id, ticker, self.mode),
            )
            orders_in_market = int(row["n"]) if row else 0
        return OpsStatus(
            kill_switch=self.kill_switch,
            ws_healthy=self.ws.connected and ws_age is not None and ws_age < self.settings.ws_stale_s,
            portfolio_reconciled=recon_ok,
            unresolved_orders=self.live_exec.unresolved_count() if self.mode == "LIVE" else 0,
            db_healthy=ok_db,
            api_healthy=api_ok,
            orders_in_market=orders_in_market,
            execution_reject_ratio=self.live_exec.reject_ratio if self.mode == "LIVE" else 0.0,
        )

    async def _act(self, tm: TrackedMarket, pred: Prediction, ev: Evaluation, eff: RiskProfile) -> None:
        side = ev.chosen
        if side is None or side.limit_price is None or side.fee_per_contract is None or side.p_cons is None:
            return
        info = tm.info
        ledger = self._ledger()
        all_in = side.limit_price + side.fee_per_contract
        sz = size_order(
            p_cons=D(repr(round(side.p_cons, 6))),
            all_in_cost=all_in,
            bankroll=ledger.equity,
            profile=eff,
            depth=side.depth_at_limit,
            high_confidence_validated=self.hc_override_allowed,
        )
        direction = direction_of(side.outcome, info.strike_type)
        proposal = Proposal(
            info.ticker,
            info.asset,
            side.outcome,
            direction,
            sz.count,
            side.limit_price,
            info.fee_multiplier,
            info.fee_type,
        )
        # Governor runs immediately before submission so limit changes between signal and submit are honoured.
        decision = self.governor.evaluate(proposal, ledger, self.profile, self._ops_status(info.ticker))
        if sz.count <= 0:
            decision.verdict = "REJECTED"
            decision.reasons.append(f"{Reason.SIZE_ZERO.value}:{sz.binding}")
        rd_id = self.db.insert(
            "risk_decisions",
            {
                "ts": time.time(),
                "evaluation_id": self.watch.get(info.ticker, {}).get("evaluation_id"),
                "ticker": info.ticker,
                "session_id": self.session_id,
                "mode": self.mode,
                "verdict": decision.verdict,
                "requested_count": str(sz.count),
                "approved_count": str(decision.approved_count),
                "selected_profile": decision.selected_profile,
                "effective_profile": decision.effective_profile,
                "bankroll": str(decision.bankroll),
                "reasons": decision.reasons,
                "snapshot": {
                    **decision.snapshot,
                    "sizing": {
                        "binding": sz.binding,
                        "kelly_full": str(sz.kelly_fraction_full),
                        "stake": str(sz.stake),
                    },
                },
            },
        )
        w = self.watch.get(info.ticker)
        if not decision.approved:
            if w is not None:
                w["action"] = f"NO TRADE — {Reason.RISK_REJECTED.value}:{','.join(decision.reasons)}"
                w["reason"] = f"{Reason.RISK_REJECTED.value}:{decision.reasons[-1] if decision.reasons else ''}"
            self._cooldown[info.ticker] = time.time() - ORDER_COOLDOWN_S + 5  # re-check in 5 s
            return
        if self.bot_state != "RUNNING" or not info.is_open_now(time.time()):
            return  # STOP pressed or market closed while deciding
        intent = OrderIntent(
            info,
            side.outcome,
            direction,
            decision.approved_count,
            side.limit_price,
            self.session_id,
            w.get("evaluation_id") if w else None,
            rd_id,
        )
        executor = {"PAPER": self.paper_exec, "SHADOW": self.shadow_exec, "LIVE": self.live_exec}[self.mode]
        self._cooldown[info.ticker] = time.time()
        result = await executor.submit(intent)  # type: ignore[attr-defined]
        self.order_update_ts = time.time()
        self.event(
            "EXECUTION",
            f"{self.mode}_ORDER_{result.status}",
            f"{info.ticker} {ev.decision} {side.outcome} x{decision.approved_count} @≤{side.limit_price} "
            f"filled={result.filled_count} avg={result.avg_fill_price} {result.error or ''}",
        )
        if w is not None:
            w["action"] = f"{ev.decision} → {self.mode} {result.status} {result.filled_count}/{decision.approved_count}"

    # ================================================================ books
    async def _books_loop(self) -> None:
        last_cf_resub = 0.0
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            await self.books.maintain(now)
            # CF watchdog: stale expected index while WS is up → resubscribe (rate-limited).
            if self.ws.connected and self.cf.expected and now - last_cf_resub > 30:
                stale = [i for i in self.cf.expected if not self.cf.is_fresh(i, now)]
                sub = self.ws.subs.get("cf")
                if stale and sub is not None and sub.subscribed_at and now - sub.subscribed_at > 10:
                    last_cf_resub = now
                    self.event("CF", "CF_STALE_RESUBSCRIBE", ",".join(stale))
                    await self.ws.resubscribe("cf")

    # ========================================================= reconciliation
    async def _reconcile_loop(self) -> None:
        await asyncio.sleep(1.0)
        while True:
            if self.signer is not None:
                await self.reconcile_portfolio()
                with contextlib.suppress(KalshiError):
                    await self.live_exec.reconcile_ambiguous()
            await asyncio.sleep(self.settings.portfolio_reconcile_s if self.live_exec.unresolved_count() == 0 else 3.0)

    def _apply_balance(self, bal: dict[str, Any]) -> Decimal | None:
        cash = D_or_none(bal.get("balance_dollars"))
        if cash is None and isinstance(bal.get("balance"), int):
            cash = Decimal(bal["balance"]) / 100
        if cash is not None:
            self.recon["balance"] = str(cash)
        pv = bal.get("portfolio_value")
        self.recon["portfolio_value"] = str(Decimal(pv) / 100) if isinstance(pv, int) else None
        return cash

    async def reconcile_portfolio(self) -> bool:
        self.recon["last_attempt_ts"] = time.time()
        try:
            bal = await self.rest.get_balance(self.settings.subaccount)
            cash = self._apply_balance(bal)
            positions = await self.rest.get_positions(self.settings.subaccount)
            resting = await self.rest.get_orders(self.settings.subaccount, status="resting")
            fills = await self.rest.get_fills(self.settings.subaccount, min_ts=int(self._fill_watermark_ts))
        except KalshiError as exc:
            self.recon.update({"ok": False, "error": str(exc)[:300]})
            self.event("RECONCILE", "RECONCILE_FAILED", str(exc)[:200])
            return False
        meta = {t: (tm.info.asset, tm.info.strike_type) for t, tm in self.registry.markets.items()}
        self.live_exec.market_meta.update(meta)
        new_fills = 0
        for raw in fills:
            nf = normalize_fill(raw)
            if nf is not None and self.live_exec.apply_fill(nf):
                new_fills += 1
        self._fill_watermark_ts = time.time() - 300
        if cash is not None:
            self.live_ledger.replace_from_exchange(cash, positions, meta)
        self.db.insert(
            "balance_snapshots",
            {
                "ts": time.time(),
                "mode": "LIVE",
                "cash": str(cash),
                "portfolio_value": self.recon.get("portfolio_value"),
                "source": "REST",
            },
        )
        bot_resting = [o for o in resting if str(o.get("client_order_id", "")).startswith("pcb-l-")]
        self.recon.update(
            {
                "ok": True,
                "last_ok_ts": time.time(),
                "error": None,
                "resting_orders": len(resting),
                "bot_resting_orders": len(bot_resting),
                "positions": len(positions),
                "new_fills": new_fills,
            }
        )
        self.auth = {"ok": True, "detail": "authenticated (portfolio reconciliation)", "checked_ts": time.time()}
        return True

    # ============================================================ settlement
    async def _settlement_loop(self) -> None:
        while True:
            await asyncio.sleep(10.0)
            settled = await self.registry.refresh_closed(self.rest)
            for info in settled:
                await self._process_settlement(info)
            self.registry.retire(time.time())

    async def _process_settlement(self, info: MarketInfo) -> None:
        self.db.execute(
            "UPDATE markets SET status=?, result=?, expiration_value=?, settled_ts=?, updated_ts=? WHERE ticker=?",
            (info.status, info.result, _f(info.expiration_value), time.time(), time.time(), info.ticker),
        )
        recs = self.paper_ledger.settle_market(info.ticker, info.result, source="KALSHI_MARKET_RESULT")
        recs += self.live_ledger.settle_market(info.ticker, info.result, source="KALSHI_MARKET_RESULT")
        self.watch.pop(info.ticker, None)
        detail = f"{info.ticker} result={info.result} value={info.expiration_value}"
        if recs:
            detail += " | " + "; ".join(f"{r.outcome} x{r.count} pnl={r.pnl}" for r in recs)
        self.event("SETTLEMENT", "MARKET_SETTLED", detail)

    # ============================================================== persist
    async def _persist_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            self._flush_cf()
            self._health_transitions()

    def _flush_cf(self) -> None:
        if not self._cf_buffer or self.db._conn is None:
            return
        buf, self._cf_buffer = self._cf_buffer, []
        try:
            self.db.executemany(
                "INSERT OR IGNORE INTO cf_ticks(index_id, source_ts_ms, value, recv_ts) VALUES (?,?,?,?)", buf
            )
        except DatabaseError as exc:
            self.event("DATABASE", "CF_PERSIST_FAILED", str(exc))

    def _health_transitions(self) -> None:
        for name, comp in self.health()["components"].items():
            status = comp["status"]
            if self._health_last.get(name) != status:
                self._health_last[name] = status
                self.event("HEALTH", f"{name}_{status}", str(comp.get("detail", ""))[:300])

    # ============================================================ validation
    async def _validation_loop(self) -> None:
        await asyncio.sleep(30.0)
        while True:
            await self.run_validation()
            await asyncio.sleep(1800.0)

    async def run_validation(self) -> dict[str, Any]:
        try:
            summary = await asyncio.to_thread(self.validator.run_all)
        except DatabaseError as exc:
            self.event("VALIDATION", "VALIDATION_FAILED", str(exc))
            return self.validation_summary
        self.validation_summary = summary
        self.hc_override_allowed = bool(summary.get("high_confidence_override_allowed"))
        return summary

    # ================================================================ health
    def health(self) -> dict[str, Any]:
        now = time.time()
        comps: dict[str, dict[str, Any]] = {}

        def put(name: str, status: str, detail: str, age: float | None = None) -> None:
            comps[name] = {"status": status, "detail": detail, "age_s": round(age, 1) if age is not None else None}

        put(
            "CREDENTIALS",
            "OK" if self.signer else "FAIL",
            "API key loaded" if self.signer else (self.signer_error or "missing"),
        )
        a_age = now - self.auth["checked_ts"] if self.auth["checked_ts"] else None
        put("AUTHENTICATION", "OK" if self.auth["ok"] else "FAIL", str(self.auth["detail"]), a_age)
        r_age = now - self.rest.stats.last_ok_ts if self.rest.stats.last_ok_ts else None
        put(
            "REST",
            "OK" if r_age is not None and r_age < 90 else "FAIL",
            f"last error: {self.rest.stats.last_error}" if self.rest.stats.last_error else "ok",
            r_age,
        )
        ws_age = self.ws.message_age()
        ws_ok = self.ws.connected and ws_age is not None and ws_age < self.settings.ws_stale_s
        put(
            "WEBSOCKET",
            "OK" if ws_ok else "FAIL",
            f"{self.ws.stats.state}; reconnects={self.ws.stats.reconnects}",
            ws_age,
        )
        skew = self.rest.stats.server_clock_skew_s
        if skew is None:
            put("CLOCK", "WARN", "skew unknown (no exchange Date header yet)")
        else:
            put(
                "CLOCK",
                "OK" if abs(skew) <= 2 else ("WARN" if abs(skew) <= 10 else "FAIL"),
                f"local − exchange = {skew:+.2f}s (request signatures are timestamped)",
            )
        db_ok, db_detail = self.db.healthy() if self.db._conn is not None else (False, "closed")
        put("DATABASE", "OK" if db_ok else "FAIL", db_detail)
        rc_age = now - self.recon["last_ok_ts"] if self.recon.get("last_ok_ts") else None
        rc_ok = bool(self.recon["ok"]) and rc_age is not None and rc_age < 2.5 * self.settings.portfolio_reconcile_s
        put("RECONCILIATION", "OK" if rc_ok else "FAIL", self.recon.get("error") or "ok", rc_age)
        active = self.registry.active_verified()
        disc_age = now - self.registry.last_discovery_ts if self.registry.last_discovery_ts else None
        if active:
            put("MARKET_DISCOVERY", "OK", f"{len(active)} verified open market(s)", disc_age)
        elif self.registry.last_discovery_ts:
            unverified = [f"{t.info.ticker}: {'; '.join(t.verify_notes)}" for t in self.registry.active()]
            notes = "; ".join(unverified) or "no open 15-minute crypto markets right now"
            series_notes = "; ".join(f"{k}: {', '.join(s.notes)}" for k, s in self.registry.series.items() if s.notes)
            put("MARKET_DISCOVERY", "WARN", f"{notes} {series_notes}".strip(), disc_age)
        else:
            put("MARKET_DISCOVERY", "FAIL", self.registry.last_error or "not yet run", disc_age)
        books = [self.books.get(t.info.ticker) for t in active]
        good = [
            b
            for b in books
            if b is not None
            and b.state == BookState.VALID
            and b.source == "ws"
            and (b.age(now) or 1e9) <= self.settings.book_stale_s
        ]
        if not active:
            put("ORDER_BOOKS", "WARN", "no active markets")
        else:
            bad = [f"{b.ticker}:{b.state.value}:{b.invalid_reason}" for b in books if b is not None and b not in good]
            put(
                "ORDER_BOOKS",
                "OK" if len(good) == len(active) else ("WARN" if good else "FAIL"),
                f"{len(good)}/{len(active)} valid & fresh " + "; ".join(bad),
            )
        exp = sorted(self.cf.expected)
        fresh = [i for i in exp if self.cf.is_fresh(i, now)]
        if not exp:
            put("CF_BENCHMARKS", "WARN", "no indices expected yet")
        else:
            ages = {i: (self.cf.indices[i].age(now) if i in self.cf.indices else None) for i in exp}
            put(
                "CF_BENCHMARKS",
                "OK" if len(fresh) == len(exp) else ("WARN" if fresh else "FAIL"),
                ", ".join(f"{i}={'n/a' if a is None else f'{a:.1f}s'}" for i, a in ages.items()),
            )
        states = [w.get("model_state") for w in self.watch.values() if w.get("phase") == "ACTIVE"]
        if states and all(s == "READY" for s in states):
            put("MODEL", "OK", f"{len(states)} market(s) READY ({MODEL_VERSION})")
        elif "READY" in states:
            put(
                "MODEL",
                "WARN",
                "partially ready: "
                + "; ".join(
                    f"{t}:{w['model_state']}:{','.join(w['model_missing'])}"
                    for t, w in self.watch.items()
                    if w.get("model_state") != "READY"
                ),
            )
        elif states:
            put(
                "MODEL",
                "WARN" if "WARMING_UP" in states else "FAIL",
                "; ".join(f"{t}:{w['model_state']}:{','.join(w['model_missing'])}" for t, w in self.watch.items()),
            )
        else:
            put("MODEL", "WARN", "no active markets to model")
        evals = [float(w["updated_ts"]) for w in self.watch.values() if w.get("updated_ts") is not None]
        e_age = now - max(evals) if evals else None
        put(
            "STRATEGY",
            "OK" if e_age is not None and e_age < 5 else "WARN",
            f"{STRATEGY_VERSION}; last evaluation",
            e_age,
        )
        ops = self._ops_status("")
        loss_hit = []
        prof = self.selected_profile()
        led = self._ledger()
        if led.session_loss_pct >= prof.max_session_drawdown_pct:
            loss_hit.append("session loss limit")
        if led.daily_loss_pct >= prof.max_daily_loss_pct:
            loss_hit.append("daily loss limit")
        put("RISK", "FAIL" if loss_hit else "OK", ", ".join(loss_hit) or f"governor active ({self.profile.value})")
        if self.mode == "LIVE":
            ex = "OK" if (self.signer and self.auth["ok"] and ops.unresolved_orders == 0) else "FAIL"
            put("EXECUTION", ex, f"LIVE armed={self.live_exec.armed} unresolved={ops.unresolved_orders}")
        else:
            put("EXECUTION", "OK", f"{self.mode} executor ready")
        put(
            "KILL_SWITCH",
            "FAIL" if self.kill_switch else "OK",
            "ENGAGED — reset required" if self.kill_switch else "clear",
        )
        return {"components": comps, "readiness": self._readiness(comps)}

    def _readiness(self, comps: dict[str, dict[str, Any]]) -> dict[str, Any]:
        core = ["CREDENTIALS", "REST", "WEBSOCKET", "DATABASE", "MARKET_DISCOVERY", "KILL_SWITCH"]
        paper_block = [f"{c}: {comps[c]['detail']}" for c in core if comps[c]["status"] == "FAIL"]
        live_extra = ["AUTHENTICATION", "RECONCILIATION", "EXECUTION", "RISK", "CLOCK"]
        live_block = paper_block + [f"{c}: {comps[c]['detail']}" for c in live_extra if comps[c]["status"] == "FAIL"]
        if self.validation_summary.get("overall") != "PASS" and not self.settings.allow_live_without_validation:
            live_block.append(
                f"STRATEGY_VALIDATION: {self.validation_summary.get('overall')} "
                "(LIVE requires statistical evidence; see Strategy Validation)"
            )
        if self.settings.environment == "mock":
            live_block.append("ENVIRONMENT: mock simulator — LIVE is disabled")
        tradeable = [
            t
            for t, w in self.watch.items()
            if w.get("model_state") == "READY" and w.get("book_state") == "VALID" and w.get("phase") == "ACTIVE"
        ]
        order = [
            ("INITIALIZING", "DATABASE"),
            ("CONNECTING", "REST"),
            ("CONNECTING", "WEBSOCKET"),
            ("AUTHENTICATING", "AUTHENTICATION"),
            ("RECONCILING", "RECONCILIATION"),
            ("DISCOVERING", "MARKET_DISCOVERY"),
            ("SYNCING_BOOKS", "ORDER_BOOKS"),
            ("RECEIVING_SETTLEMENT_REFERENCE", "CF_BENCHMARKS"),
            ("WARMING_UP_MODEL", "MODEL"),
        ]
        phase = "READY"
        for ph, comp in order:
            if comps[comp]["status"] != "OK":
                phase = ph
                break
        return {
            "phase": phase,
            "paper_ready": not paper_block,
            "paper_blockers": paper_block,
            "shadow_ready": not paper_block,
            "live_ready": not live_block,
            "live_blockers": live_block,
            "tradeable_markets": tradeable,
        }

    # ============================================================== controls
    def set_mode(self, mode: str) -> None:
        mode = mode.upper()
        if mode not in MODES:
            raise ControlError(f"mode must be one of {MODES}")
        if self.bot_state != "STOPPED":
            raise ControlError("stop the bot before changing mode")
        self.mode = mode
        self.live_confirmed = False
        self.event("CONTROL", "MODE_SELECTED", mode)

    def set_profile(self, name: str) -> None:
        try:
            p = ProfileName(name)
        except ValueError as exc:
            raise ControlError(f"profile must be one of {[x.value for x in ProfileName]}") from exc
        order = [ProfileName.SAFE, ProfileName.BALANCED, ProfileName.AGGRESSIVE]
        if self.bot_state != "STOPPED" and order.index(p) > order.index(self.profile):
            raise ControlError("risk can only be raised while the bot is stopped")
        self.profile = p
        self.event("CONTROL", "PROFILE_SELECTED", p.value)

    def confirm_live(self, text: str) -> None:
        if self.mode != "LIVE":
            raise ControlError("select LIVE mode first")
        if text != "LIVE":
            raise ControlError("type LIVE exactly to confirm real-money trading")
        self.live_confirmed = True
        self.event("CONTROL", "LIVE_CONFIRMED", "operator typed LIVE")

    async def start_bot(self) -> dict[str, Any]:
        if self.bot_state != "STOPPED":
            raise ControlError("bot is already running")
        if self.kill_switch:
            raise ControlError("kill switch engaged — reset it first")
        rd = self.health()["readiness"]
        if self.mode in ("PAPER", "SHADOW") and not rd["paper_ready"]:
            raise ControlError("operational readiness failed: " + "; ".join(rd["paper_blockers"]))
        if self.mode == "LIVE":
            if not self.live_confirmed:
                raise ControlError("LIVE requires the typed confirmation")
            if not rd["live_ready"]:
                raise ControlError("LIVE blocked: " + "; ".join(rd["live_blockers"]))
            if not await self.reconcile_portfolio():
                raise ControlError("LIVE blocked: fresh portfolio reconciliation failed")
        led = self._ledger()
        led.start_session()
        bi = build_info()
        self.session_id = self.db.insert(
            "sessions",
            {
                "started_ts": time.time(),
                "mode": self.mode,
                "profile": self.profile.value,
                "environment": self.settings.environment,
                "app_version": bi["app_version"],
                "git_commit": bi["git_commit"],
                "model_version": MODEL_VERSION,
                "strategy_version": STRATEGY_VERSION,
                "starting_bankroll": str(led.equity),
            },
        )
        self.session_started_ts = time.time()
        self.live_exec.armed = self.mode == "LIVE"
        self.bot_state = "RUNNING"
        self.stop_reason = None
        self._cooldown.clear()
        self.event("CONTROL", "BOT_STARTED", f"mode={self.mode} profile={self.profile.value} session={self.session_id}")
        return {"session_id": self.session_id}

    async def stop_bot(self, reason: str = "operator STOP") -> dict[str, Any]:
        was = self.bot_state
        # 1) immediately block new strategy orders
        self.bot_state = "STOPPED"
        self.live_exec.armed = False
        self.live_confirmed = False
        self.stop_reason = reason
        cancelled = 0
        # 2) cancel bot-owned resting entry orders (positions are NOT liquidated)
        with contextlib.suppress(KalshiError):
            cancelled = await self.live_exec.cancel_bot_resting()
        if self.session_id is not None:
            self.db.execute(
                "UPDATE sessions SET ended_ts=?, stop_reason=? WHERE id=?", (time.time(), reason, self.session_id)
            )
        self.event("CONTROL", "BOT_STOPPED", f"{reason}; was={was}; cancelled_resting={cancelled}; positions kept")
        return {"cancelled_resting": cancelled}

    async def emergency_stop(self) -> dict[str, Any]:
        self.kill_switch = True
        res = await self.stop_bot("EMERGENCY STOP")
        self.event("CONTROL", "KILL_SWITCH_ENGAGED", "manual reset required before START")
        return res

    def reset_kill_switch(self) -> None:
        if self.bot_state != "STOPPED":
            raise ControlError("bot must be stopped")
        self.kill_switch = False
        self.event("CONTROL", "KILL_SWITCH_RESET", "")

    # ================================================================ views
    def counters(self) -> dict[str, Any]:
        def n(sql: str, params: tuple[Any, ...] = ()) -> int:
            row = self.db.one(sql, params)
            return int(row["n"]) if row and row["n"] is not None else 0

        sess: dict[str, Any] = {"session_id": self.session_id}
        if self.session_id is not None:
            sid = self.session_id
            mode = self.mode
            sess["orders"] = n("SELECT COUNT(*) n FROM orders WHERE session_id=?", (sid,))
            sess["fills"] = n(
                "SELECT COUNT(*) n FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id "
                "WHERE o.session_id=?",
                (sid,),
            )
            tickers = "SELECT DISTINCT ticker FROM orders WHERE session_id=? AND CAST(filled_count AS REAL) > 0"
            sess["open_unsettled"] = n(
                f"SELECT COUNT(*) n FROM positions WHERE mode=? AND status='OPEN' AND ticker IN ({tickers})",  # noqa: S608
                (mode, sid),
            )
            rows = self.db.query(f"SELECT pnl FROM settlements WHERE mode=? AND ticker IN ({tickers})", (mode, sid))  # noqa: S608
            wins = sum(1 for r in rows if D(r["pnl"]) > 0)
            sess["settled"] = len(rows)
            sess["wins"] = wins
            sess["losses"] = len(rows) - wins
            sess["win_rate"] = f"{wins / len(rows):.1%}" if rows else "N/A"
            sess["realized_pnl"] = str(sum((D(r["pnl"]) for r in rows), ZERO))
        totals = {
            "observations": n("SELECT COUNT(*) n FROM predictions"),
            "signals": n("SELECT COUNT(*) n FROM evaluations WHERE decision IN ('TRADE_UP','TRADE_DOWN')"),
            "orders": n("SELECT COUNT(*) n FROM orders WHERE mode IN ('PAPER','LIVE')"),
            "shadow_orders": n("SELECT COUNT(*) n FROM orders WHERE mode='SHADOW'"),
            "fills": n("SELECT COUNT(*) n FROM fills"),
            "open_positions": n("SELECT COUNT(*) n FROM positions WHERE status='OPEN'"),
            "settled_trades": n("SELECT COUNT(*) n FROM settlements"),
            "unique_markets": n("SELECT COUNT(*) n FROM markets"),
            "evaluated_markets": n("SELECT COUNT(DISTINCT ticker) n FROM predictions WHERE state='READY'"),
            "oos_markets": n("SELECT COUNT(*) n FROM validation_partitions WHERE partition='OOS_TEST'"),
            "live_forward_markets": n("SELECT COUNT(*) n FROM validation_partitions WHERE partition='LIVE_FORWARD'"),
            "settled_markets": n("SELECT COUNT(*) n FROM markets WHERE result IN ('yes','no')"),
        }
        return {"session": sess, "totals": totals}

    def orders_view(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT client_order_id, exchange_order_id, mode, ticker, outcome, book_side, limit_price, "
            "count, filled_count, avg_fill_price, fees, status, created_ts, error FROM orders "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def state(self) -> dict[str, Any]:
        now = time.time()
        h = self.health()
        led = self._ledger()
        ws_age = self.ws.message_age()
        book_ages = [a for a in (b.age(now) for b in self.books.books.values()) if a is not None]
        cf_ages = [a for a in (s.age(now) for s in self.cf.indices.values()) if a is not None]
        return {
            "ts": now,
            "build": build_info(),
            "settings": self.settings.public_dict(),
            "exchange_status": self.exchange_status,
            "bot": {
                "state": self.bot_state,
                "mode": self.mode,
                "profile": self.profile.value,
                "effective_profile": effective_profile(self.profile, led, self._ops_status(""))[0].name.value,
                "live_confirmed": self.live_confirmed,
                "live_armed": self.live_exec.armed,
                "kill_switch": self.kill_switch,
                "session_id": self.session_id,
                "session_started_ts": self.session_started_ts,
                "stop_reason": self.stop_reason,
            },
            "health": h,
            "ages": {
                "portfolio_reconciliation_s": (now - self.recon["last_ok_ts"])
                if self.recon.get("last_ok_ts")
                else None,
                "websocket_s": ws_age,
                "orderbook_max_s": max(book_ages) if book_ages else None,
                "cf_max_s": max(cf_ages) if cf_ages else None,
                "order_fill_update_s": (now - self.order_update_ts) if self.order_update_ts else None,
            },
            "connection": {
                "environment": self.settings.environment,
                "rest_base": self.settings.rest_base,
                "ws_url": self.settings.ws_url,
                "auth": self.auth,
                "rest": vars(self.rest.stats),
                "ws": {k: v for k, v in vars(self.ws.stats).items() if k != "events"},
                "ws_events": [{"ts": t, "code": c, "detail": d} for t, c, d in list(self.ws.stats.events)[-40:]],
                "subscriptions": self.ws.sub_status(),
            },
            "portfolio": {
                "paper": self.paper_ledger.public(),
                "live": self.live_ledger.public(),
                "exchange": self.recon,
            },
            "markets": self.registry.public(now),
            "watch": sorted(self.watch.values(), key=lambda w: (w.get("asset", ""), w.get("seconds_to_close") or 0)),
            "books": self.books.public(now),
            "cf": self.cf.public(now),
            "orders": self.orders_view(),
            "counters": self.counters(),
            "validation": self.validation_summary,
            "profiles": {p.value: PROFILES[p].public() for p in ProfileName},
            "security": {
                "allowed_endpoints": allowed_endpoints(),
                "note": "Deposits, withdrawals, transfers, subaccount and API-key management are not "
                "implemented and are blocked by the local firewall before any request.",
            },
            "events": list(self.events)[:100],
            "loop_errors": self.loop_errors,
        }

    def research_view(self) -> dict[str, Any]:
        return {"hypotheses": list_hypotheses(self.db), "validation": self.validation_summary}
