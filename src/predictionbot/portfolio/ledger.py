"""Position / cash ledger per execution mode.

PAPER: this ledger is the book of record (simulated capital).
LIVE: the exchange is authoritative; `replace_from_exchange` overwrites cash
and positions on every reconciliation, while realtime fills keep it current
between reconciliations.

Exposure of a long binary position = its cost (the maximum loss). Correlated
exposure groups positions by DIRECTION across all crypto assets (UP = YES on
an above-strike market, DOWN = NO), because 15-minute crypto moves are highly
correlated across BTC/ETH/SOL/XRP/DOGE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from predictionbot.fixedpoint import ZERO, D
from predictionbot.persistence.db import Database


@dataclass
class Position:
    ticker: str
    outcome: str  # yes | no
    asset: str
    count: Decimal
    cost: Decimal
    fees: Decimal
    opened_ts: float
    status: str = "OPEN"
    direction: str = "UP"

    @property
    def exposure(self) -> Decimal:
        return self.cost + self.fees


@dataclass
class SettlementRecord:
    ticker: str
    outcome: str
    result: str
    count: Decimal
    cost: Decimal
    fees: Decimal
    payout: Decimal
    pnl: Decimal
    settled_ts: float
    source: str


def direction_of(outcome: str, strike_type: str | None) -> str:
    up_on_yes = strike_type not in ("less", "less_or_equal")
    return "UP" if (outcome == "yes") == up_on_yes else "DOWN"


class Ledger:
    def __init__(self, mode: str, db: Database | None, starting_cash: Decimal = ZERO) -> None:
        self.mode = mode
        self.db = db
        self.cash = starting_cash
        self.positions: dict[tuple[str, str], Position] = {}
        self.reserved: dict[str, Decimal] = {}  # client_order_id -> reserved dollars
        self.session_start_equity: Decimal = starting_cash
        self.peak_equity: Decimal = starting_cash
        self.day_start_equity: Decimal = starting_cash
        self.day_key: str = time.strftime("%Y-%m-%d", time.gmtime())
        self.realized_pnl_session: Decimal = ZERO
        self.consecutive_losses = 0
        self.last_update_ts: float | None = None
        self.cash_source = "local"

    # ---------------------------------------------------------------- state
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.count > 0 and p.status == "OPEN"]

    @property
    def deployed(self) -> Decimal:
        return sum((p.exposure for p in self.open_positions()), ZERO)

    @property
    def reserved_total(self) -> Decimal:
        return sum(self.reserved.values(), ZERO)

    @property
    def equity(self) -> Decimal:
        """Cash + open positions at cost (conservative: no unrealized gains)."""
        return self.cash + sum((p.cost for p in self.open_positions()), ZERO)

    @property
    def available(self) -> Decimal:
        return self.cash - self.reserved_total

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return ZERO
        return max(ZERO, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def session_loss_pct(self) -> Decimal:
        if self.session_start_equity <= 0:
            return ZERO
        return max(ZERO, (self.session_start_equity - self.equity) / self.session_start_equity)

    @property
    def daily_loss_pct(self) -> Decimal:
        self._roll_day()
        if self.day_start_equity <= 0:
            return ZERO
        return max(ZERO, (self.day_start_equity - self.equity) / self.day_start_equity)

    def _roll_day(self) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime())
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = self.equity

    def start_session(self) -> None:
        self.session_start_equity = self.equity
        self.peak_equity = self.equity
        self.realized_pnl_session = ZERO
        self.consecutive_losses = 0

    def exposure_by_market(self, ticker: str) -> Decimal:
        return sum((p.exposure for p in self.open_positions() if p.ticker == ticker), ZERO)

    def exposure_by_asset(self, asset: str) -> Decimal:
        return sum((p.exposure for p in self.open_positions() if p.asset == asset), ZERO)

    def exposure_by_direction(self, direction: str) -> Decimal:
        return sum((p.exposure for p in self.open_positions() if p.direction == direction), ZERO)

    # ------------------------------------------------------------- mutation
    def reserve(self, client_order_id: str, amount: Decimal) -> None:
        self.reserved[client_order_id] = amount

    def release(self, client_order_id: str) -> None:
        self.reserved.pop(client_order_id, None)

    def apply_fill(
        self,
        *,
        ticker: str,
        outcome: str,
        asset: str,
        direction: str,
        count: Decimal,
        price: Decimal,
        fee: Decimal,
        ts: float | None = None,
    ) -> Position:
        ts = time.time() if ts is None else ts
        key = (ticker, outcome)
        pos = self.positions.get(key)
        if pos is None or pos.status != "OPEN":
            pos = Position(ticker, outcome, asset, ZERO, ZERO, ZERO, ts, "OPEN", direction)
            self.positions[key] = pos
        pos.count += count
        pos.cost += count * price
        pos.fees += fee
        if self.cash_source == "local":
            self.cash -= count * price + fee
        self.last_update_ts = ts
        self._persist_position(pos)
        return pos

    def settle_market(
        self, ticker: str, result: str, *, source: str, ts: float | None = None
    ) -> list[SettlementRecord]:
        ts = time.time() if ts is None else ts
        out: list[SettlementRecord] = []
        for (t, outcome), pos in list(self.positions.items()):
            if t != ticker or pos.status != "OPEN" or pos.count <= 0:
                continue
            payout = pos.count if outcome == result else ZERO
            pnl = payout - pos.cost - pos.fees
            if self.cash_source == "local":
                self.cash += payout
            pos.status = "SETTLED"
            rec = SettlementRecord(ticker, outcome, result, pos.count, pos.cost, pos.fees, payout, pnl, ts, source)
            out.append(rec)
            self.realized_pnl_session += pnl
            self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
            self._persist_position(pos, result=result, payout=payout, pnl=pnl, settled_ts=ts)
            if self.db is not None:
                self.db.upsert(
                    "settlements",
                    {
                        "mode": self.mode,
                        "ticker": ticker,
                        "outcome": outcome,
                        "result": result,
                        "settled_ts": ts,
                        "count": str(pos.count),
                        "cost": str(pos.cost),
                        "fees": str(pos.fees),
                        "payout": str(payout),
                        "pnl": str(pnl),
                        "source": source,
                    },
                    keys=("mode", "ticker", "outcome"),
                )
        self.mark()
        return out

    def mark(self) -> None:
        eq = self.equity
        if eq > self.peak_equity:
            self.peak_equity = eq
        self.last_update_ts = time.time()

    def replace_from_exchange(
        self, cash: Decimal, positions: list[dict[str, Any]], asset_of: dict[str, tuple[str, str | None]]
    ) -> None:
        """LIVE reconciliation: the exchange is authoritative.

        `position_fp` > 0 means YES contracts, < 0 means NO contracts;
        `market_exposure_dollars` is the cost basis of the open position.
        """
        self.cash_source = "exchange"
        self.cash = cash
        seen: set[tuple[str, str]] = set()
        now = time.time()
        for p in positions:
            try:
                qty = D(p.get("position_fp", "0"))
                exposure = D(p.get("market_exposure_dollars", "0"))
                fees = D(p.get("fees_paid_dollars", "0"))
            except ValueError:
                continue
            if qty == 0:
                continue
            ticker = str(p.get("ticker"))
            outcome = "yes" if qty > 0 else "no"
            asset, strike_type = asset_of.get(ticker, ("OTHER", None))
            key = (ticker, outcome)
            seen.add(key)
            pos = self.positions.get(key)
            if pos is None:
                pos = Position(
                    ticker, outcome, asset, ZERO, ZERO, ZERO, now, "OPEN", direction_of(outcome, strike_type)
                )
                self.positions[key] = pos
            pos.count = abs(qty)
            pos.cost = exposure
            pos.fees = fees
            pos.status = "OPEN"
            self._persist_position(pos)
        for key, pos in self.positions.items():
            if key not in seen and pos.status == "OPEN":
                # Closed/settled on the exchange; settlement processing records the outcome.
                pos.status = "CLOSED_ON_EXCHANGE"
                self._persist_position(pos)
        self.mark()

    def _persist_position(
        self,
        pos: Position,
        *,
        result: str | None = None,
        payout: Decimal | None = None,
        pnl: Decimal | None = None,
        settled_ts: float | None = None,
    ) -> None:
        if self.db is None:
            return
        self.db.upsert(
            "positions",
            {
                "mode": self.mode,
                "ticker": pos.ticker,
                "outcome": pos.outcome,
                "count": str(pos.count),
                "cost": str(pos.cost),
                "fees": str(pos.fees),
                "status": pos.status,
                "opened_ts": pos.opened_ts,
                "updated_ts": time.time(),
                "settled_ts": settled_ts,
                "result": result,
                "payout": str(payout) if payout is not None else None,
                "realized_pnl": str(pnl) if pnl is not None else None,
                "asset": pos.asset,
            },
            keys=("mode", "ticker", "outcome"),
        )

    def load_open_positions(self, direction_lookup: dict[str, str | None]) -> None:
        """Restore open positions after restart (PAPER)."""
        if self.db is None:
            return
        for r in self.db.query("SELECT * FROM positions WHERE mode=? AND status='OPEN'", (self.mode,)):
            pos = Position(
                r["ticker"],
                r["outcome"],
                r["asset"] or "OTHER",
                D(r["count"]),
                D(r["cost"]),
                D(r["fees"]),
                float(r["opened_ts"]),
                "OPEN",
                direction_of(r["outcome"], direction_lookup.get(r["ticker"])),
            )
            self.positions[(pos.ticker, pos.outcome)] = pos

    def public(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cash": str(self.cash),
            "cash_source": self.cash_source,
            "available": str(self.available),
            "reserved": str(self.reserved_total),
            "deployed": str(self.deployed),
            "equity": str(self.equity),
            "session_start_equity": str(self.session_start_equity),
            "session_pnl": str(self.equity - self.session_start_equity),
            "realized_pnl_session": str(self.realized_pnl_session),
            "peak_equity": str(self.peak_equity),
            "drawdown_pct": str(self.drawdown_pct.quantize(D("0.0001"))),
            "daily_loss_pct": str(self.daily_loss_pct.quantize(D("0.0001"))),
            "consecutive_losses": self.consecutive_losses,
            "positions": [
                {
                    "ticker": p.ticker,
                    "outcome": p.outcome,
                    "direction": p.direction,
                    "asset": p.asset,
                    "count": str(p.count),
                    "avg_entry": str((p.cost / p.count).quantize(D("0.0001"))) if p.count > 0 else None,
                    "cost": str(p.cost),
                    "fees": str(p.fees),
                }
                for p in self.open_positions()
            ],
        }
