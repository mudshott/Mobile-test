from __future__ import annotations

import math
import random
import time
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from predictionbot.market.models import MarketInfo, PriceRange
from predictionbot.market.orderbook import OrderBook
from predictionbot.persistence.db import Database
from predictionbot.reference.cf import CFReferenceFeed


@pytest.fixture(scope="session")
def rsa_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture(scope="session")
def ed_pem() -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def key_file(tmp_path: Path, rsa_pem: bytes) -> Path:
    p = tmp_path / "k.pem"
    p.write_bytes(rsa_pem)
    return p


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "t.db")
    d.open()
    yield d
    d.close()


def make_market(
    ticker: str = "KXBTC15M-TEST-00",
    *,
    now: float | None = None,
    seconds_to_close: float = 400.0,
    strike: float = 100000.0,
    strike_type: str = "greater_or_equal",
    status: str = "active",
    asset: str = "BTC",
    index: str = "BRTI",
) -> MarketInfo:
    now = time.time() if now is None else now
    close = now + seconds_to_close
    return MarketInfo(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        series_ticker="KXBTC15M",
        asset=asset,
        cf_index_id=index,
        status=status,
        open_ts=close - 900,
        close_ts=close,
        strike_type=strike_type,
        floor_strike=Decimal(str(strike)),
        cap_strike=None,
        price_ranges=(PriceRange(Decimal("0.01"), Decimal("0.99"), Decimal("0.01")),),
        rules_primary="CF Benchmarks BRTI average",
    )


def feed_with_history(
    index: str = "BRTI",
    *,
    spot: float = 100000.0,
    sigma: float = 0.0001,
    seconds: int = 900,
    now: float | None = None,
    seed: int = 3,
    end_at_spot: bool = True,
) -> CFReferenceFeed:
    """Synthetic 1 s tick history for unit tests (test fixture, not market data)."""
    now = time.time() if now is None else now
    rng = random.Random(seed)
    path = [1.0]
    for _ in range(seconds - 1):
        path.append(path[-1] * math.exp(sigma * rng.gauss(0, 1)))
    scale = spot / path[-1] if end_at_spot else spot
    feed = CFReferenceFeed(stale_after_s=5.0)
    feed.expect({index})
    t0 = int(now * 1000) - (seconds - 1) * 1000
    for i, v in enumerate(path):
        ts = t0 + i * 1000
        feed.on_message(
            {
                "type": "cfbenchmarks_value",
                "msg": {
                    "index_id": index,
                    "received_at": ts,
                    "data": f'{{"type":"value","id":"{index}","time":{ts},"value":"{v * scale}"}}',
                },
            },
            recv_ts=now - (seconds - 1 - i),
        )
    return feed


def make_book(ticker: str, yes_bids: list[tuple[str, str]], no_bids: list[tuple[str, str]]) -> OrderBook:
    b = OrderBook(ticker)
    res = b.apply_snapshot(
        {"yes_dollars_fp": [list(x) for x in yes_bids], "no_dollars_fp": [list(x) for x in no_bids]}, seq=1, source="ws"
    )
    assert res.ok
    return b
