"""Shared order types for PAPER / SHADOW / LIVE execution."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from predictionbot.fixedpoint import ZERO
from predictionbot.market.models import MarketInfo
from predictionbot.persistence.db import Database

TERMINAL = {"FILLED", "PARTIALLY_FILLED", "CANCELED", "REJECTED", "NOT_FOUND", "SHADOW", "NO_FILL"}
UNRESOLVED = {"PENDING_SUBMIT", "AMBIGUOUS"}


def new_client_order_id(mode: str) -> str:
    return f"pcb-{mode[0].lower()}-{uuid.uuid4().hex}"


@dataclass
class OrderIntent:
    market: MarketInfo
    outcome: str  # "yes" (UP) | "no" (DOWN)
    direction: str
    count: Decimal
    limit_price: Decimal  # price of the OUTCOME contract
    session_id: int | None = None
    evaluation_id: int | None = None
    risk_decision_id: int | None = None

    @property
    def ticker(self) -> str:
        return self.market.ticker


@dataclass
class OrderResult:
    client_order_id: str
    mode: str
    status: str
    filled_count: Decimal = ZERO
    avg_fill_price: Decimal | None = None
    fees: Decimal = ZERO
    exchange_order_id: str | None = None
    error: str | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)


def persist_new_order(
    db: Database | None,
    *,
    client_order_id: str,
    mode: str,
    intent: OrderIntent,
    book_side: str,
    status: str,
    request: dict[str, Any] | None = None,
) -> None:
    if db is None:
        return
    now = time.time()
    db.insert(
        "orders",
        {
            "client_order_id": client_order_id,
            "mode": mode,
            "session_id": intent.session_id,
            "ticker": intent.ticker,
            "outcome": intent.outcome,
            "book_side": book_side,
            "limit_price": str(intent.limit_price),
            "count": str(intent.count),
            "status": status,
            "time_in_force": "immediate_or_cancel",
            "created_ts": now,
            "updated_ts": now,
            "evaluation_id": intent.evaluation_id,
            "risk_decision_id": intent.risk_decision_id,
            "request": json.dumps(request) if request is not None else None,
        },
    )


def update_order(db: Database | None, client_order_id: str, **fields: Any) -> None:
    if db is None or not fields:
        return
    fields["updated_ts"] = time.time()
    cols = ",".join(f"{k}=?" for k in fields)
    vals = [
        json.dumps(v) if isinstance(v, (dict, list)) else (str(v) if isinstance(v, Decimal) else v)
        for v in fields.values()
    ]
    db.execute(f"UPDATE orders SET {cols} WHERE client_order_id=?", [*vals, client_order_id])  # noqa: S608
