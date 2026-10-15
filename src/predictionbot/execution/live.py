"""LIVE execution on Kalshi via the V2 event-order API.

Order construction (POST /trade-api/v2/portfolio/events/orders):
    {"ticker", "client_order_id", "side": "bid"|"ask", "count": "N.00",
     "price": "0.XXXX", "time_in_force": "immediate_or_cancel",
     "self_trade_prevention_type": "taker_at_cross", "cancel_order_on_pause": true,
     ["subaccount": n]}
V2 quotes everything on the YES book: BUY YES at p → side "bid", price p;
BUY NO at q → side "ask" (sell YES) at price 1 − q, which opens NO exposure.

Ambiguity rules:
* Every order is persisted as PENDING_SUBMIT *before* the request is sent.
* A timeout / dropped connection / 5xx after send → AMBIGUOUS. The order is
  NEVER resubmitted automatically. `reconcile_ambiguous` searches the
  exchange's orders by ticker+time for our client_order_id; while any order is
  unresolved the risk governor blocks all new risk.
* HTTP 409 (duplicate client_order_id) is also treated as ambiguous and reconciled.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from predictionbot.execution.base import (
    OrderIntent,
    OrderResult,
    new_client_order_id,
    persist_new_order,
    update_order,
)
from predictionbot.fixedpoint import ONE, ZERO, D, D_or_none, fmt_count, fmt_price
from predictionbot.kalshi.firewall import FirewallViolation
from predictionbot.kalshi.rest import (
    AmbiguousWriteError,
    KalshiConfigError,
    KalshiError,
    KalshiHTTPError,
    KalshiRest,
    RateLimitedError,
    RequestBuildError,
    TransientNetworkError,
)
from predictionbot.market.models import parse_ts
from predictionbot.persistence.db import Database
from predictionbot.portfolio.ledger import Ledger, direction_of
from predictionbot.strategy.fees import taker_fee

log = logging.getLogger(__name__)
AMBIGUOUS_GIVE_UP_S = 120.0


class LiveNotArmedError(RuntimeError):
    pass


def build_create_order_body(intent: OrderIntent, client_order_id: str, subaccount: int | None) -> dict[str, Any]:
    if intent.outcome == "yes":
        side, yes_price = "bid", intent.limit_price
    elif intent.outcome == "no":
        side, yes_price = "ask", ONE - intent.limit_price
    else:
        raise ValueError(f"bad outcome {intent.outcome}")
    if not intent.market.is_valid_price(yes_price):
        raise ValueError(f"price {yes_price} is not on the market's tick grid")
    body: dict[str, Any] = {
        "ticker": intent.ticker,
        "client_order_id": client_order_id,
        "side": side,
        "count": fmt_count(intent.count),
        "price": fmt_price(yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }
    if subaccount is not None:
        body["subaccount"] = subaccount
    return body


@dataclass
class NormalizedFill:
    fill_id: str
    order_id: str | None
    client_order_id: str | None
    ticker: str
    outcome: str
    book_side: str
    count: Decimal
    price: Decimal  # price paid per OUTCOME contract
    fee: Decimal
    is_taker: bool
    ts: float

    @property
    def is_opening_buy(self) -> bool:
        return (self.outcome == "yes" and self.book_side == "bid") or (self.outcome == "no" and self.book_side == "ask")


def normalize_fill(f: dict[str, Any]) -> NormalizedFill | None:
    """Accept both REST `Fill` and WS `fill` payloads (fixed-point fields)."""
    try:
        fill_id = str(f.get("fill_id") or f.get("trade_id"))
        ticker = str(f.get("ticker") or f.get("market_ticker"))
        outcome = str(f.get("outcome_side") or f.get("purchased_side") or f.get("side"))
        book_side = str(f.get("book_side") or ("bid" if f.get("action") == "buy" else "ask"))
        count = D(f.get("count_fp", f.get("count")))
        yes_price = D(f.get("yes_price_dollars"))
        no_price = D_or_none(f.get("no_price_dollars"))
        price = yes_price if outcome == "yes" else (no_price if no_price is not None else ONE - yes_price)
        fee = D(f.get("fee_cost", "0"))
    except (ValueError, TypeError):
        return None
    if fill_id in ("None", "") or outcome not in ("yes", "no"):
        return None
    ts_ms = f.get("ts_ms")
    ts = ts_ms / 1000.0 if isinstance(ts_ms, (int, float)) else (parse_ts(f.get("created_time")) or time.time())
    return NormalizedFill(
        fill_id,
        f.get("order_id"),
        f.get("client_order_id"),
        ticker,
        outcome,
        book_side,
        count,
        price,
        fee,
        bool(f.get("is_taker", True)),
        ts,
    )


class LiveExecutor:
    mode = "LIVE"

    def __init__(self, rest: KalshiRest, ledger: Ledger, db: Database | None, *, subaccount: int | None = None) -> None:
        self.rest = rest
        self.ledger = ledger
        self.db = db
        self.subaccount = subaccount
        self.armed = False
        self.ambiguous: dict[str, tuple[str, float, OrderIntent | None]] = {}
        self.known_fill_ids: set[str] = set()
        self.market_meta: dict[str, tuple[str, str | None]] = {}  # ticker -> (asset, strike_type)
        self.submitted = 0
        self.rejected = 0

    def load_known_fills(self) -> None:
        if self.db is None:
            return
        for r in self.db.query("SELECT fill_id FROM fills WHERE mode='LIVE'"):
            self.known_fill_ids.add(r["fill_id"])

    def unresolved_count(self) -> int:
        return len(self.ambiguous)

    @property
    def reject_ratio(self) -> float:
        return self.rejected / self.submitted if self.submitted >= 5 else 0.0

    async def submit(self, intent: OrderIntent) -> OrderResult:
        if not self.armed:
            raise LiveNotArmedError("LIVE execution is not armed (explicit operator confirmation required)")
        coid = new_client_order_id(self.mode)
        try:
            body = build_create_order_body(intent, coid, self.subaccount)
        except ValueError as exc:
            return OrderResult(coid, self.mode, "REJECTED", error=f"construction: {exc}")
        self.market_meta[intent.ticker] = (intent.market.asset, intent.market.strike_type)
        persist_new_order(
            self.db,
            client_order_id=coid,
            mode=self.mode,
            intent=intent,
            book_side=body["side"],
            status="PENDING_SUBMIT",
            request=body,
        )
        reserve = intent.count * intent.limit_price + taker_fee(
            intent.count, intent.limit_price, intent.market.fee_multiplier, intent.market.fee_type
        )
        self.ledger.reserve(coid, reserve)
        try:
            resp = await self.rest.create_order_v2(body)
        except (TransientNetworkError, KalshiConfigError, RequestBuildError, FirewallViolation) as exc:
            # Never reached the exchange: a local failure, not an exchange rejection.
            return self._mark_not_sent(coid, exc)
        except AmbiguousWriteError as exc:
            self.submitted += 1
            return self._mark_ambiguous(coid, intent, str(exc))
        except KalshiHTTPError as exc:
            self.submitted += 1
            if exc.status == 409:
                return self._mark_ambiguous(coid, intent, f"409 duplicate client_order_id: {exc.body[:200]}")
            return self._mark_rejected(coid, f"rate limited: {exc}" if isinstance(exc, RateLimitedError) else str(exc))
        except KalshiError as exc:
            self.submitted += 1
            return self._mark_rejected(coid, str(exc))
        except Exception as exc:
            # Unknown failure while sending: the order may exist. Fail safe → reconcile, never resubmit.
            self.submitted += 1
            log.exception("order %s: unexpected error while sending; treated as AMBIGUOUS", coid)
            return self._mark_ambiguous(coid, intent, f"unexpected {exc.__class__.__name__}: {exc}")
        self.submitted += 1
        try:
            return await self._finalize(coid, intent, resp)
        except Exception as exc:
            # The exchange accepted the order; if recording it fails, reconcile rather than lose track of it.
            log.exception("order %s accepted but finalize failed; treated as AMBIGUOUS", coid)
            return self._mark_ambiguous(coid, intent, f"accepted; finalize failed: {exc.__class__.__name__}: {exc}")

    def _mark_ambiguous(self, coid: str, intent: OrderIntent, error: str) -> OrderResult:
        """Outcome unknown: keep capital reserved, block new risk, reconcile by client_order_id."""
        self.ambiguous[coid] = (intent.ticker, time.time(), intent)
        try:
            update_order(self.db, coid, status="AMBIGUOUS", error=error[:500])
        except Exception:
            log.exception("could not persist AMBIGUOUS state for %s", coid)
        log.error("order %s outcome ambiguous: %s — reconciling, NOT resubmitting", coid, error)
        return OrderResult(coid, self.mode, "AMBIGUOUS", error=error[:200])

    def _mark_rejected(self, coid: str, error: str) -> OrderResult:
        self.ledger.release(coid)
        self.rejected += 1
        update_order(self.db, coid, status="REJECTED", error=error[:500])
        return OrderResult(coid, self.mode, "REJECTED", error=error[:200])

    def _mark_not_sent(self, coid: str, exc: Exception) -> OrderResult:
        self.ledger.release(coid)
        update_order(self.db, coid, status="REJECTED", error=f"not sent: {str(exc)[:480]}")
        return OrderResult(coid, self.mode, "REJECTED", error=f"not sent: {str(exc)[:180]}")

    async def _finalize(self, coid: str, intent: OrderIntent, resp: dict[str, Any]) -> OrderResult:
        order_id = resp.get("order_id")
        filled = D_or_none(resp.get("fill_count", resp.get("fill_count_fp"))) or ZERO
        avg_yes = D_or_none(resp.get("average_fill_price"))
        avg = None
        if avg_yes is not None:
            avg = avg_yes if intent.outcome == "yes" else ONE - avg_yes
        status = "FILLED" if filled >= intent.count else ("PARTIALLY_FILLED" if filled > 0 else "NO_FILL")
        update_order(
            self.db,
            coid,
            status=status,
            exchange_order_id=order_id,
            filled_count=str(filled),
            avg_fill_price=str(avg) if avg is not None else None,
            response=resp,
        )
        self.ledger.release(coid)
        fills: list[dict[str, Any]] = []
        if filled > 0 and order_id:
            try:
                for raw in await self.rest.get_fills(self.subaccount, order_id=order_id):
                    nf = normalize_fill(raw)
                    if nf is not None:
                        self.apply_fill(nf, client_order_id=coid)
                        fills.append(raw)
            except KalshiError as exc:
                log.warning("fill fetch for %s failed (%s); WS/reconciliation will backfill", order_id, exc)
        return OrderResult(coid, self.mode, status, filled, avg, ZERO, order_id, fills=fills)

    def apply_fill(self, nf: NormalizedFill, client_order_id: str | None = None) -> bool:
        """Idempotently record an exchange fill. Returns True if new."""
        if nf.fill_id in self.known_fill_ids:
            return False
        if self.db is not None:
            try:
                self.db.insert(
                    "fills",
                    {
                        "fill_id": nf.fill_id,
                        "client_order_id": client_order_id or nf.client_order_id,
                        "exchange_order_id": nf.order_id,
                        "mode": self.mode,
                        "ticker": nf.ticker,
                        "outcome": nf.outcome,
                        "count": str(nf.count),
                        "price": str(nf.price),
                        "fee": str(nf.fee),
                        "is_taker": int(nf.is_taker),
                        "ts": nf.ts,
                    },
                )
            except Exception as exc:  # duplicate after restart etc.
                log.debug("fill insert skipped: %s", exc)
        if nf.is_opening_buy:
            asset, strike_type = self.market_meta.get(nf.ticker, ("OTHER", None))
            self.ledger.apply_fill(
                ticker=nf.ticker,
                outcome=nf.outcome,
                asset=asset,
                direction=direction_of(nf.outcome, strike_type),
                count=nf.count,
                price=nf.price,
                fee=nf.fee,
                ts=nf.ts,
            )
        # Only now is the fill fully recorded; if anything above raised, a retry will re-apply it.
        self.known_fill_ids.add(nf.fill_id)
        return True

    def recover_unresolved_from_db(self) -> int:
        """After a restart, any LIVE order left PENDING_SUBMIT/AMBIGUOUS must be reconciled."""
        if self.db is None:
            return 0
        rows = self.db.query(
            "SELECT client_order_id, ticker, created_ts FROM orders "
            "WHERE mode='LIVE' AND status IN ('PENDING_SUBMIT','AMBIGUOUS')"
        )
        for r in rows:
            self.ambiguous.setdefault(r["client_order_id"], (r["ticker"], float(r["created_ts"]), None))
            update_order(self.db, r["client_order_id"], status="AMBIGUOUS", error="recovered after restart")
        return len(rows)

    async def reconcile_ambiguous(self) -> None:
        for coid, (ticker, since, _intent) in list(self.ambiguous.items()):
            try:
                orders = await self.rest.get_orders(self.subaccount, ticker=ticker, min_ts=int(since) - 120)
            except KalshiError as exc:
                log.warning("ambiguous reconcile for %s failed: %s", coid, exc)
                continue
            match = next((o for o in orders if o.get("client_order_id") == coid), None)
            if match is not None:
                del self.ambiguous[coid]
                self.ledger.release(coid)
                filled = D_or_none(match.get("fill_count_fp")) or ZERO
                initial = D_or_none(match.get("initial_count_fp")) or filled
                ex_status = str(match.get("status", ""))
                if ex_status == "resting":
                    status = "RESTING"
                elif filled > 0:
                    status = "FILLED" if filled >= initial else "PARTIALLY_FILLED"
                else:
                    status = "NO_FILL"
                update_order(
                    self.db,
                    coid,
                    status=status,
                    exchange_order_id=match.get("order_id"),
                    filled_count=str(filled),
                    error="resolved by reconciliation",
                    response=match,
                )
                if filled > 0 and match.get("order_id"):
                    try:
                        for raw in await self.rest.get_fills(self.subaccount, order_id=match["order_id"]):
                            nf = normalize_fill(raw)
                            if nf is not None:
                                self.apply_fill(nf, client_order_id=coid)
                    except KalshiError as exc:
                        log.warning("fill fetch after reconcile failed: %s", exc)
                continue
            if time.time() - since > AMBIGUOUS_GIVE_UP_S:
                del self.ambiguous[coid]
                self.ledger.release(coid)
                update_order(
                    self.db,
                    coid,
                    status="NOT_FOUND",
                    error=f"not on exchange after {AMBIGUOUS_GIVE_UP_S:.0f}s of reconciliation",
                )

    async def cancel_bot_resting(self) -> int:
        """Cancel bot-owned resting orders (STOP policy). Positions are NOT liquidated."""
        if self.db is None:
            return 0
        n = 0
        for row in self.db.query(
            "SELECT client_order_id, exchange_order_id, ticker FROM orders "
            "WHERE mode='LIVE' AND bot_owned=1 AND status='RESTING' AND exchange_order_id IS NOT NULL"
        ):
            try:
                await self.rest.cancel_order_v2(
                    row["exchange_order_id"], market_ticker=row["ticker"], subaccount=self.subaccount
                )
                update_order(self.db, row["client_order_id"], status="CANCELED")
                n += 1
            except KalshiError as exc:
                log.error("cancel %s failed: %s", row["client_order_id"], exc)
        return n
