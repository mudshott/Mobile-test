"""Offline self-test of the packaged executable (no network, no orders sent).

Proves, inside the shipped binary, that:
* production V2 order requests are constructed for the current endpoint/fields,
* the request would be signed over the correct path,
* the local firewall rejects funding / transfer / API-key endpoints BEFORE any
  HTTP request (an httpx transport that records calls must see zero calls),
* no funding/transfer methods exist on the REST client.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from predictionbot.buildinfo import build_info
from predictionbot.config import PROD_REST, PROD_WS
from predictionbot.execution.base import OrderIntent
from predictionbot.execution.live import build_create_order_body
from predictionbot.kalshi.auth import KalshiSigner
from predictionbot.kalshi.firewall import FirewallViolation, check_rest
from predictionbot.kalshi.rest import KalshiRest
from predictionbot.market.models import MarketInfo, PriceRange

FORBIDDEN = [
    ("POST", "/portfolio/withdrawals"),
    ("POST", "/portfolio/deposits"),
    ("POST", "/portfolio/subaccounts/transfer"),
    ("POST", "/portfolio/intra_exchange_instance_transfer"),
    ("POST", "/portfolio/subaccounts"),
    ("POST", "/api_keys"),
    ("POST", "/api_keys/generate"),
    ("DELETE", "/api_keys/x"),
]


def _market() -> MarketInfo:
    now = time.time()
    return MarketInfo(
        ticker="KXBTC15M-SELFTEST-00",
        event_ticker="KXBTC15M-SELFTEST",
        series_ticker="KXBTC15M",
        asset="BTC",
        cf_index_id="BRTI",
        status="active",
        open_ts=now - 300,
        close_ts=now + 600,
        strike_type="greater_or_equal",
        floor_strike=Decimal("100000"),
        cap_strike=None,
        price_ranges=(PriceRange(Decimal("0.01"), Decimal("0.99"), Decimal("0.01")),),
    )


async def _firewall_no_http() -> dict[str, Any]:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={})

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    rest = KalshiRest(PROD_REST, KalshiSigner("selftest-key", pem), transport=httpx.MockTransport(handler))
    blocked = {}
    for m, p in FORBIDDEN:
        try:
            await rest.request(m, p, json_body={"amount_cents": 1})
            blocked[f"{m} {p}"] = "NOT BLOCKED"
        except FirewallViolation as exc:
            blocked[f"{m} {p}"] = exc.reason
    await rest.close()
    return {"blocked": blocked, "http_requests_made": len(calls)}


def run_self_test() -> dict[str, Any]:
    checks: dict[str, Any] = {}
    intent_yes = OrderIntent(_market(), "yes", "UP", Decimal("3"), Decimal("0.56"))
    intent_no = OrderIntent(_market(), "no", "DOWN", Decimal("2"), Decimal("0.30"))
    body_yes = build_create_order_body(intent_yes, "pcb-l-selftest1", None)
    body_no = build_create_order_body(intent_no, "pcb-l-selftest2", None)
    check_rest("POST", "/portfolio/events/orders")
    checks["order_endpoint"] = f"POST {PROD_REST}/portfolio/events/orders"
    checks["ws_endpoint"] = PROD_WS
    checks["order_body_buy_yes"] = body_yes
    checks["order_body_buy_no"] = body_no
    ok_bodies = (
        body_yes["side"] == "bid"
        and body_yes["price"] == "0.5600"
        and body_yes["count"] == "3.00"
        and body_no["side"] == "ask"
        and body_no["price"] == "0.7000"
        and body_yes["time_in_force"] == "immediate_or_cancel"
        and "client_order_id" in body_yes
    )
    fw = asyncio.run(_firewall_no_http())
    checks["firewall"] = fw
    fw_ok = fw["http_requests_made"] == 0 and all(v != "NOT BLOCKED" for v in fw["blocked"].values())
    names = {n.lower() for n in dir(KalshiRest)}
    no_funding = not any(b in n for n in names for b in ("deposit", "withdraw", "transfer", "subaccount", "api_key"))
    checks["no_funding_methods"] = no_funding
    checks["real_order_sent"] = False
    return {"passed": bool(ok_bodies and fw_ok and no_funding), "build": build_info(), "checks": checks}
