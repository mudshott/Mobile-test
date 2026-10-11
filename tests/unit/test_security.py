"""Account-security tests: the local firewall must reject funding/transfer/API-key
endpoints BEFORE any HTTP request is made, and secrets must never leak."""

from __future__ import annotations

import base64
import logging

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from predictionbot.kalshi import firewall
from predictionbot.kalshi.auth import KalshiSigner, SignerError
from predictionbot.kalshi.firewall import FirewallViolation, check_rest, check_ws_command
from predictionbot.kalshi.rest import KalshiRest
from predictionbot.main import RedactFilter

FORBIDDEN = [
    ("POST", "/portfolio/deposits"),
    ("GET", "/portfolio/deposits"),
    ("POST", "/portfolio/withdrawals"),
    ("GET", "/portfolio/withdrawals"),
    ("POST", "/portfolio/subaccounts"),
    ("POST", "/portfolio/subaccounts/transfer"),
    ("GET", "/portfolio/subaccounts/transfers"),
    ("PUT", "/portfolio/subaccounts/netting"),
    ("POST", "/portfolio/intra_exchange_instance_transfer"),
    ("POST", "/portfolio/target_balance_allocation"),
    ("POST", "/api_keys"),
    ("POST", "/api_keys/generate"),
    ("DELETE", "/api_keys/abc123"),
    ("GET", "/api_keys"),
    ("POST", "/account/api_usage_level/upgrade"),
    ("POST", "/fcm/subtraders"),
    ("POST", "/communications/rfqs"),
    ("POST", "/portfolio/order_groups/create"),
    ("DELETE", "/portfolio/events/orders"),  # cancel-ALL across subaccounts is not bot-owned-only
    ("POST", "/portfolio/events/orders/batched"),
    ("POST", "/portfolio/bank_accounts"),
    ("POST", "/portfolio/payment_methods"),
    ("POST", "/portfolio/cards"),
    ("POST", "/paypal/link"),
    ("POST", "/funding/ach"),
    ("POST", "/portfolio/orders"),  # legacy order endpoint not used
    ("GET", "/portfolio/../api_keys"),
    ("GET", "/portfolio/%2e%2e/api_keys"),
    ("GET", "/portfolio/balance?x=1"),
    ("GET", "//portfolio/balance"),
]

ALLOWED = [
    ("GET", "/exchange/status"),
    ("GET", "/series/KXBTC15M"),
    ("GET", "/markets"),
    ("GET", "/markets/KXBTC15M-26OCT111515-00"),
    ("GET", "/markets/KXBTC15M-26OCT111515-00/orderbook"),
    ("GET", "/portfolio/balance"),
    ("GET", "/portfolio/positions"),
    ("GET", "/portfolio/orders"),
    ("GET", "/portfolio/fills"),
    ("GET", "/portfolio/settlements"),
    ("POST", "/portfolio/events/orders"),
    ("DELETE", "/portfolio/events/orders/0f3c9a1e-1111-2222-3333-444455556666"),
    ("POST", "/portfolio/events/orders/0f3c9a1e-1111-2222-3333-444455556666/amend"),
    ("POST", "/portfolio/events/orders/0f3c9a1e-1111-2222-3333-444455556666/decrease"),
]


@pytest.mark.parametrize(("method", "path"), FORBIDDEN)
def test_forbidden_endpoints_rejected(method: str, path: str) -> None:
    with pytest.raises(FirewallViolation):
        check_rest(method, path)


@pytest.mark.parametrize(("method", "path"), ALLOWED)
def test_required_endpoints_allowed(method: str, path: str) -> None:
    check_rest(method, path)


def test_specific_reasons() -> None:
    for path, reason in [
        ("/portfolio/withdrawals", "FUNDING_WITHDRAWAL"),
        ("/portfolio/deposits", "FUNDING_DEPOSIT"),
        ("/portfolio/subaccounts/transfer", "FUNDS_TRANSFER"),
        ("/api_keys/generate", "API_KEY_MANAGEMENT"),
    ]:
        with pytest.raises(FirewallViolation) as ei:
            check_rest("POST", path)
        assert ei.value.reason == reason


@pytest.mark.parametrize(("method", "path"), FORBIDDEN[:20])
async def test_firewall_blocks_before_any_http_request(method: str, path: str, rsa_pem: bytes) -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={})

    rest = KalshiRest(
        "https://example.invalid/trade-api/v2", KalshiSigner("k" * 10, rsa_pem), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(FirewallViolation):
        await rest.request(method, path, json_body={"amount": 100} if method != "GET" else None)
    assert calls == []
    await rest.close()


def test_rest_client_has_no_funding_methods() -> None:
    names = {n.lower() for n in dir(KalshiRest)}
    for bad in ("deposit", "withdraw", "transfer", "subaccount", "api_key", "apikey", "bank", "payment"):
        assert not any(bad in n for n in names), bad


def test_ws_firewall() -> None:
    check_ws_command("subscribe", {"channels": ["orderbook_delta"], "market_tickers": ["X"]})
    check_ws_command("update_subscription", {"sid": 1, "action": "get_snapshot"})
    with pytest.raises(FirewallViolation):
        check_ws_command("subscribe", {"channels": ["communications"]})
    with pytest.raises(FirewallViolation):
        check_ws_command("transfer", {})
    with pytest.raises(FirewallViolation):
        check_ws_command("update_subscription", {"sid": 1, "action": "accept_quote"})


def test_allowlist_contains_no_write_except_orders() -> None:
    for m, p in firewall._ALLOW:
        if m != "GET":
            assert p.startswith("/portfolio/events/orders"), (m, p)


def test_rsa_signature_verifies(rsa_pem: bytes) -> None:
    s = KalshiSigner("my-key-id", rsa_pem)
    h = s.headers("get", "/trade-api/v2/portfolio/balance?limit=5", now_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "my-key-id"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    pub = serialization.load_pem_private_key(rsa_pem, None).public_key()
    msg = b"1700000000000GET/trade-api/v2/portfolio/balance"  # query string excluded
    pub.verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        msg,  # type: ignore[call-arg]
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies(ed_pem: bytes) -> None:
    s = KalshiSigner("id", ed_pem)
    h = s.headers("POST", "/trade-api/v2/portfolio/events/orders", now_ms=1)
    pub = serialization.load_pem_private_key(ed_pem, None).public_key()
    pub.verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1POST/trade-api/v2/portfolio/events/orders")  # type: ignore[call-arg]


def test_signer_never_leaks(rsa_pem: bytes) -> None:
    s = KalshiSigner("abcdefghijkl", rsa_pem)
    assert "BEGIN" not in repr(s) and "abcdefghijkl" not in repr(s)
    with pytest.raises(SignerError) as ei:
        fake = b"-----BEGIN " + b"PRIVATE KEY-----\nnotakey\n-----END " + b"PRIVATE KEY-----"
        KalshiSigner("id", fake)
    assert "notakey" not in str(ei.value)


def test_log_redaction(rsa_pem: bytes) -> None:
    rec = logging.LogRecord("x", logging.INFO, "", 0, "leak %s", (rsa_pem.decode(),), None)
    RedactFilter().filter(rec)
    assert "PRIVATE KEY" not in rec.getMessage()
    rec2 = logging.LogRecord("x", logging.INFO, "", 0, "KALSHI-ACCESS-SIGNATURE: " + "A" * 40, (), None)
    RedactFilter().filter(rec2)
    assert "A" * 40 not in rec2.getMessage()
