"""Local endpoint firewall — defense in depth for account security.

Every REST request and every WebSocket command passes through `check_rest` /
`check_ws_command` BEFORE any bytes leave the process. The policy is
deny-by-default: only the read endpoints needed for market data / portfolio
reconciliation and the V2 order create / cancel / amend / decrease endpoints
are allowed. Funding, transfers, subaccount management, API-key management,
RFQ/communications, FCM and anything unknown is rejected.

This module intentionally has no code path that could perform a deposit,
withdrawal, transfer or key operation; the denylist exists so that a mistake
elsewhere produces a loud, specific error rather than a silent request.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

ID = r"[A-Za-z0-9._:-]{1,128}"
TICKER = r"[A-Z0-9][A-Z0-9._-]{0,127}"

_ALLOW: tuple[tuple[str, str], ...] = (
    # --- public / market data (GET) ---
    ("GET", r"/exchange/status"),
    ("GET", r"/exchange/schedule"),
    ("GET", r"/series"),
    ("GET", rf"/series/{TICKER}"),
    ("GET", r"/series/fee_changes"),
    ("GET", r"/events"),
    ("GET", rf"/events/{TICKER}"),
    ("GET", r"/markets"),
    ("GET", rf"/markets/{TICKER}"),
    ("GET", rf"/markets/{TICKER}/orderbook"),
    ("GET", r"/markets/orderbooks"),
    ("GET", r"/markets/trades"),
    ("GET", r"/markets/candlesticks"),
    ("GET", rf"/series/{TICKER}/markets/{TICKER}/candlesticks"),
    ("GET", r"/historical/cutoff"),
    ("GET", r"/historical/markets"),
    ("GET", rf"/historical/markets/{TICKER}"),
    ("GET", rf"/historical/markets/{TICKER}/candlesticks"),
    ("GET", r"/historical/trades"),
    # --- portfolio reads needed for reconciliation ---
    ("GET", r"/portfolio/balance"),
    ("GET", r"/portfolio/positions"),
    ("GET", r"/portfolio/orders"),
    ("GET", rf"/portfolio/orders/{ID}"),
    ("GET", r"/portfolio/orders/queue_positions"),
    ("GET", r"/portfolio/fills"),
    ("GET", r"/portfolio/settlements"),
    ("GET", r"/portfolio/summary/total_resting_order_value"),
    ("GET", r"/historical/fills"),
    ("GET", r"/historical/orders"),
    ("GET", r"/historical/positions"),
    ("GET", r"/account/limits"),
    # --- trading (V2 event-market orders), single-order only ---
    ("POST", r"/portfolio/events/orders"),
    ("DELETE", rf"/portfolio/events/orders/{ID}"),
    ("POST", rf"/portfolio/events/orders/{ID}/amend"),
    ("POST", rf"/portfolio/events/orders/{ID}/decrease"),
)
_ALLOW_RE = tuple((m, re.compile(p + r"\Z")) for m, p in _ALLOW)

# Checked first so violations get a specific, auditable reason.
_DENY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("deposit", "FUNDING_DEPOSIT"),
    ("withdraw", "FUNDING_WITHDRAWAL"),
    ("transfer", "FUNDS_TRANSFER"),
    ("subaccount", "SUBACCOUNT_MANAGEMENT"),
    ("api_key", "API_KEY_MANAGEMENT"),
    ("apikey", "API_KEY_MANAGEMENT"),
    ("bank", "PAYMENT_METHOD"),
    ("payment", "PAYMENT_METHOD"),
    ("card", "PAYMENT_METHOD"),
    ("paypal", "PAYMENT_METHOD"),
    ("ach", "PAYMENT_METHOD"),
    ("wire", "PAYMENT_METHOD"),
    ("funding", "FUNDING"),
    ("target_balance_allocation", "FUNDS_ALLOCATION"),
    ("intra_exchange", "FUNDS_TRANSFER"),
    ("fcm", "FCM_ADMIN"),
    ("api_usage_level", "ACCOUNT_ADMIN"),
    ("communications", "RFQ_DISABLED"),
    ("order_groups", "ORDER_GROUPS_DISABLED"),
    ("batched", "BATCH_DISABLED"),
)

ALLOWED_WS_CHANNELS = frozenset(
    {
        "orderbook_delta",
        "ticker",
        "trade",
        "fill",
        "user_orders",
        "market_positions",
        "market_lifecycle_v2",
        "cfbenchmarks_value",
        "cfbenchmarks_value_5hz",
    }
)
ALLOWED_WS_COMMANDS = frozenset({"subscribe", "unsubscribe", "update_subscription", "list_subscriptions"})
ALLOWED_WS_ACTIONS = frozenset(
    {"add_markets", "delete_markets", "get_snapshot", "subscribe_indices", "unsubscribe_indices", "indexlist"}
)


class FirewallViolation(PermissionError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"BLOCKED_BY_LOCAL_FIREWALL[{reason}]: {detail}")
        self.reason = reason
        self.detail = detail


def _normalize(path: str) -> str:
    if "?" in path or "#" in path:
        raise FirewallViolation("MALFORMED_PATH", "query/fragment must be passed as params")
    decoded = unquote(path)
    if decoded != path or "%" in path:
        raise FirewallViolation("MALFORMED_PATH", "percent-encoded paths are not allowed")
    if not path.startswith("/") or "//" in path or "/./" in path or "/../" in path or path.endswith(("/..", "/.")):
        raise FirewallViolation("MALFORMED_PATH", f"suspicious path {path!r}")
    if "\\" in path or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in path):
        raise FirewallViolation("MALFORMED_PATH", "non-printable or backslash in path")
    return path.rstrip("/") or "/"


def check_rest(method: str, path: str) -> None:
    """Raise FirewallViolation unless (method, path) is explicitly allowed.

    `path` is relative to /trade-api/v2 (e.g. "/portfolio/balance").
    """
    m = method.upper()
    p = _normalize(path)
    low = p.lower()
    for kw, reason in _DENY_KEYWORDS:
        if kw in low:
            raise FirewallViolation(reason, f"{m} {p}")
    for allowed_method, rx in _ALLOW_RE:
        if allowed_method == m and rx.match(p):
            return
    raise FirewallViolation("NOT_ALLOWLISTED", f"{m} {p}")


def check_ws_command(cmd: str, params: dict[str, object]) -> None:
    if cmd not in ALLOWED_WS_COMMANDS:
        raise FirewallViolation("WS_COMMAND", cmd)
    channels = params.get("channels")
    if channels is not None:
        if not isinstance(channels, list) or not channels:
            raise FirewallViolation("WS_CHANNEL", repr(channels))
        for ch in channels:
            if ch not in ALLOWED_WS_CHANNELS:
                raise FirewallViolation("WS_CHANNEL", str(ch))
    action = params.get("action")
    if action is not None and action not in ALLOWED_WS_ACTIONS:
        raise FirewallViolation("WS_ACTION", str(action))


def allowed_endpoints() -> list[str]:
    return [f"{m} {p}" for m, p in _ALLOW]
