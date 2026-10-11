"""Local Kalshi binary order book.

Kalshi books carry BIDS ONLY for each outcome: ``yes_dollars_fp`` are YES bids
and ``no_dollars_fp`` are NO bids, each as ``[price_dollars, count_fp]``. In a
binary market a NO bid at price q is a YES ask at 1-q, so:

    best YES ask = 1 - best NO bid      (cost to BUY YES immediately)
    best NO  ask = 1 - best YES bid     (cost to BUY NO immediately)

Buying YES as a taker consumes NO bids from the highest NO bid downwards.

Integrity: snapshot first, then strictly sequential deltas on the
subscription. A gap, negative level or unexpected market marks the book
INVALID and asks for a resync; it is never traded while not VALID.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from predictionbot.fixedpoint import ONE, ZERO, D, FixedPointError


class BookState(StrEnum):
    AWAITING_SNAPSHOT = "AWAITING_SNAPSHOT"
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass
class ApplyResult:
    ok: bool
    need_resync: bool = False
    reason: str | None = None


@dataclass
class Fillable:
    """Result of walking the book for a taker buy."""

    count: Decimal
    cost: Decimal  # dollars, excluding fees
    worst_price: Decimal | None
    levels: list[tuple[Decimal, Decimal]] = field(default_factory=list)

    @property
    def avg_price(self) -> Decimal | None:
        return (self.cost / self.count) if self.count > 0 else None


class OrderBook:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.yes: dict[Decimal, Decimal] = {}
        self.no: dict[Decimal, Decimal] = {}
        self.state = BookState.AWAITING_SNAPSHOT
        self.invalid_reason: str | None = "NO_SNAPSHOT_YET"
        self.source: str = "none"
        self.last_seq: int | None = None
        self.last_source_ts_ms: int | None = None
        self.last_recv_ts: float | None = None
        self.last_update_ts: float | None = None
        self.last_confirmed_ts: float | None = None  # snapshot or delta seen
        self.snapshots = 0
        self.deltas = 0
        self.resyncs = 0

    # ------------------------------------------------------------ mutation
    def invalidate(self, reason: str) -> None:
        if self.state != BookState.INVALID:
            self.resyncs += 1
        self.state = BookState.INVALID
        self.invalid_reason = reason

    @staticmethod
    def _levels(raw: Any) -> dict[Decimal, Decimal]:
        out: dict[Decimal, Decimal] = {}
        for lvl in raw or []:
            if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
                raise FixedPointError(f"bad level {lvl!r}")
            p, q = D(lvl[0]), D(lvl[1])
            if p <= ZERO or p >= ONE or q < ZERO:
                raise FixedPointError(f"level out of range {lvl!r}")
            if q > ZERO:
                out[p] = q
        return out

    def apply_snapshot(self, msg: dict[str, Any], *, seq: int | None, source: str = "ws") -> ApplyResult:
        try:
            yes = self._levels(msg.get("yes_dollars_fp", msg.get("yes_dollars")))
            no = self._levels(msg.get("no_dollars_fp", msg.get("no_dollars")))
        except FixedPointError as exc:
            self.invalidate("MALFORMED_SNAPSHOT")
            return ApplyResult(False, True, f"malformed snapshot: {exc}")
        self.yes, self.no = yes, no
        self.last_seq = seq
        self.source = source
        now = time.time()
        self.last_recv_ts = now
        self.last_update_ts = now
        self.last_confirmed_ts = now
        self.snapshots += 1
        if self._crossed():
            self.invalidate("CROSSED_BOOK")
            return ApplyResult(False, True, "crossed book in snapshot")
        self.state = BookState.VALID
        self.invalid_reason = None
        return ApplyResult(True)

    def apply_delta(self, msg: dict[str, Any], *, seq: int | None, gap: bool = False) -> ApplyResult:
        if self.state != BookState.VALID:
            # Ignore deltas until a fresh snapshot arrives.
            return ApplyResult(False, False, f"book not valid ({self.invalid_reason})")
        if gap:
            self.invalidate("SEQUENCE_GAP")
            return ApplyResult(False, True, "sequence gap")
        try:
            price = D(msg.get("price_dollars", msg.get("price")))
            delta = D(msg.get("delta_fp", msg.get("delta")))
        except FixedPointError as exc:
            self.invalidate("MALFORMED_DELTA")
            return ApplyResult(False, True, f"malformed delta: {exc}")
        side = msg.get("side")
        if side not in ("yes", "no") or price <= ZERO or price >= ONE:
            self.invalidate("MALFORMED_DELTA")
            return ApplyResult(False, True, "malformed delta side/price")
        book = self.yes if side == "yes" else self.no
        new_q = book.get(price, ZERO) + delta
        if new_q < ZERO:
            self.invalidate("NEGATIVE_LEVEL")
            return ApplyResult(False, True, "delta would make level negative")
        if new_q == ZERO:
            book.pop(price, None)
        else:
            book[price] = new_q
        if seq is not None:
            self.last_seq = seq
        ts_ms = msg.get("ts_ms")
        if isinstance(ts_ms, int):
            self.last_source_ts_ms = ts_ms
        now = time.time()
        self.last_recv_ts = now
        self.last_update_ts = now
        self.last_confirmed_ts = now
        self.deltas += 1
        if self._crossed():
            self.invalidate("CROSSED_BOOK")
            return ApplyResult(False, True, "crossed book after delta")
        return ApplyResult(True)

    def _crossed(self) -> bool:
        by, bn = self.best_yes_bid(), self.best_no_bid()
        return by is not None and bn is not None and by + bn > ONE

    # ------------------------------------------------------------- queries
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes) if self.yes else None

    def best_no_bid(self) -> Decimal | None:
        return max(self.no) if self.no else None

    def yes_ask(self) -> Decimal | None:
        bn = self.best_no_bid()
        return ONE - bn if bn is not None else None

    def no_ask(self) -> Decimal | None:
        by = self.best_yes_bid()
        return ONE - by if by is not None else None

    def spread(self) -> Decimal | None:
        ask, bid = self.yes_ask(), self.best_yes_bid()
        if ask is None or bid is None:
            return None
        return ask - bid

    def ask_levels(self, outcome: str) -> list[tuple[Decimal, Decimal]]:
        """Executable ask ladder for buying `outcome` (best first): (price, size)."""
        opposite = self.no if outcome == "yes" else self.yes
        return sorted(((ONE - p, q) for p, q in opposite.items()), key=lambda x: x[0])

    def depth_at_or_better(self, outcome: str, limit_price: Decimal) -> Decimal:
        return sum((q for p, q in self.ask_levels(outcome) if p <= limit_price), ZERO)

    def walk_buy(
        self, outcome: str, max_count: Decimal, limit_price: Decimal, depth_haircut: Decimal = ONE
    ) -> Fillable:
        """Simulate a taker buy of up to `max_count` at or better than `limit_price`."""
        remaining = max_count
        cost = ZERO
        filled = ZERO
        worst: Decimal | None = None
        used: list[tuple[Decimal, Decimal]] = []
        for price, size in self.ask_levels(outcome):
            if price > limit_price or remaining <= ZERO:
                break
            avail = (size * depth_haircut).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            take = min(avail, remaining)
            if take <= ZERO:
                continue
            filled += take
            cost += take * price
            remaining -= take
            worst = price
            used.append((price, take))
        return Fillable(filled, cost, worst, used)

    def age(self, now: float | None = None) -> float | None:
        if self.last_confirmed_ts is None:
            return None
        return (time.time() if now is None else now) - self.last_confirmed_ts

    def public(self, now: float | None = None) -> dict[str, Any]:
        def f(x: Decimal | None) -> str | None:
            return str(x) if x is not None else None

        ya, na = self.ask_levels("yes")[:5], self.ask_levels("no")[:5]
        age = self.age(now)
        return {
            "state": self.state.value,
            "invalid_reason": self.invalid_reason,
            "source": self.source,
            "best_yes_bid": f(self.best_yes_bid()),
            "best_no_bid": f(self.best_no_bid()),
            "yes_ask": f(self.yes_ask()),
            "no_ask": f(self.no_ask()),
            "spread": f(self.spread()),
            "yes_ask_depth": [[str(p), str(q)] for p, q in ya],
            "no_ask_depth": [[str(p), str(q)] for p, q in na],
            "last_seq": self.last_seq,
            "age_s": round(age, 2) if age is not None else None,
            "snapshots": self.snapshots,
            "deltas": self.deltas,
            "resyncs": self.resyncs,
        }
