from __future__ import annotations

import random
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from predictionbot.buildinfo import MODEL_VERSION, SCHEMA_VERSION
from predictionbot.config import SeriesSpec
from predictionbot.kalshi.auth import KalshiSigner
from predictionbot.kalshi.rest import AmbiguousWriteError, KalshiHTTPError, KalshiRest, RateLimitedError
from predictionbot.market.discovery import MarketRegistry, SeriesState, verify_market
from predictionbot.persistence.db import Database, DatabaseError
from predictionbot.portfolio.ledger import Ledger
from predictionbot.research import stats
from predictionbot.research.knowledge import list_hypotheses, record_result, seed_knowledge
from predictionbot.research.validation import ValidationPolicy, Validator, assign_partition, hash_partition, window_id
from tests.conftest import make_market

D = Decimal


# ---------------------------------------------------------------- persistence
def test_migration_and_version_guard(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    db.open()
    assert db.schema_version() == SCHEMA_VERSION
    db.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    db.close()
    db2 = Database(tmp_path / "x.db")
    with pytest.raises(DatabaseError):
        db2.open()


def test_backup_before_migration(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "x.db")
    db.open()
    db.insert("health_events", {"ts": 1.0, "component": "T", "code": "C", "detail": ""})
    db.execute("UPDATE meta SET value='0' WHERE key='schema_version'")  # pretend an old schema
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','0')")
    db.close()
    # schema 0 → treated as fresh; force a "v1 → v2" style situation by faking current=1 and a pending v2 file
    import predictionbot.persistence.db as dbmod

    v2 = tmp_path / "002_test.sql"
    v2.write_text("CREATE TABLE IF NOT EXISTS extra_test (x INTEGER);", encoding="utf-8")
    real = dbmod._migrations
    monkeypatch.setattr(dbmod, "_migrations", lambda: [*real(), (2, v2)])
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", 2)
    db3 = Database(tmp_path / "x.db", tmp_path / "bk")
    db3.conn if False else None
    db3.path.parent.mkdir(exist_ok=True)
    import sqlite3

    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.commit()
    c.close()
    db3.open()
    assert db3.schema_version() == 2
    backups = list((tmp_path / "bk").glob("*.db"))
    assert backups, "a backup must be written before migrating an existing database"
    assert db3.one("SELECT COUNT(*) n FROM health_events")["n"] == 1  # history preserved
    db3.close()


def test_restart_with_open_paper_position(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    db.open()
    led = Ledger("PAPER", db, D("1000"))
    led.apply_fill(
        ticker="KXBTC15M-A", outcome="yes", asset="BTC", direction="UP", count=D("10"), price=D("0.40"), fee=D("0.17")
    )
    db.close()
    db2 = Database(tmp_path / "x.db")
    db2.open()
    led2 = Ledger("PAPER", db2, D("0"))
    led2.load_open_positions({})
    pos = led2.open_positions()
    assert len(pos) == 1 and pos[0].count == D("10") and pos[0].cost == D("4.00") and pos[0].fees == D("0.17")
    recs = led2.settle_market("KXBTC15M-A", "yes", source="test")
    assert recs[0].pnl == D("10") - D("4.00") - D("0.17")
    assert db2.one("SELECT * FROM settlements")["pnl"] == str(recs[0].pnl)
    db2.close()


def test_ledger_settlement_math_and_counters() -> None:
    led = Ledger("PAPER", None, D("100"))
    led.start_session()
    led.apply_fill(
        ticker="M", outcome="no", asset="BTC", direction="DOWN", count=D("5"), price=D("0.30"), fee=D("0.08")
    )
    assert led.cash == D("100") - D("1.50") - D("0.08")
    assert led.equity == D("100") - D("0.08")
    [r] = led.settle_market("M", "yes", source="t")
    assert r.payout == 0 and r.pnl == D("-1.58") and led.consecutive_losses == 1
    assert led.settle_market("M", "yes", source="t") == []  # idempotent


def test_yes_wins_rules() -> None:
    m = make_market(strike=100, strike_type="greater_or_equal")
    assert m.yes_wins(D("100")) is True and m.yes_wins(D("99.99")) is False
    m.strike_type = "greater"
    assert m.yes_wins(D("100")) is False
    m.strike_type, m.cap_strike = "between", D("110")
    assert m.yes_wins(D("105")) is True and m.yes_wins(D("111")) is False


# ---------------------------------------------------------------- validation
def test_policy_numbers_are_derived() -> None:
    p = ValidationPolicy()
    assert p.high_conf_min_n == 385
    assert p.pnl_required_windows(None) == 1583
    assert p.pnl_required_windows(0.1) == stats.required_n_for_mean(0.30, 0.03)  # SD floor applies
    assert stats.required_n_for_proportion(0.5, 0.1) == 97 == p.min_paper_fills


def test_partitions_are_immutable_and_window_grouped(db: Database) -> None:
    close = 1_800_000_000.0
    for t in ("KXBTC15M-A", "KXETH15M-A"):
        db.insert(
            "markets",
            {
                "ticker": t,
                "series": "S",
                "asset": "X",
                "cf_index": "I",
                "close_ts": close,
                "first_seen_ts": 0,
                "updated_ts": 0,
            },
        )
    p1 = assign_partition(db, "KXBTC15M-A", close, live_session_running=False)
    p2 = assign_partition(db, "KXETH15M-A", close, live_session_running=True)
    assert p1 == p2 == hash_partition(close)  # same window → same partition, regardless of session
    assert assign_partition(db, "KXBTC15M-A", close, live_session_running=True) == p1  # never moves


def test_hash_partition_proportions() -> None:
    counts: dict[str, int] = {}
    for i in range(5000):
        k = hash_partition(1_800_000_000 + i * 900)
        counts[k] = counts.get(k, 0) + 1
    assert 0.45 < counts["TRAIN"] / 5000 < 0.55 and 0.25 < counts["OOS_TEST"] / 5000 < 0.35


def _seed_settled(db: Database, n: int, calibrated: bool) -> None:
    rng = random.Random(1)
    base = 1_800_000_000.0
    for i in range(n):
        close = base + i * 900
        t = f"KXBTC15M-W{i}"
        p = rng.uniform(0.05, 0.95)
        truth = p if calibrated else min(0.99, p + 0.25)
        res = "yes" if rng.random() < truth else "no"
        db.insert(
            "markets",
            {
                "ticker": t,
                "series": "KXBTC15M",
                "asset": "BTC",
                "cf_index": "BRTI",
                "close_ts": close,
                "result": res,
                "first_seen_ts": 0,
                "updated_ts": 0,
            },
        )
        db.insert("validation_partitions", {"ticker": t, "partition": "OOS_TEST", "assigned_ts": 0, "rule": "test"})
        db.insert(
            "predictions",
            {
                "ts": close - 180,
                "ticker": t,
                "model_version": MODEL_VERSION,
                "state": "READY",
                "raw_prob_yes": p,
                "seconds_to_close": 180.0,
                "yes_bid": "0.40",
                "yes_ask": "0.45",
            },
        )


def test_calibration_insufficient_then_pass_then_fail(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    db.open()
    _seed_settled(db, 50, True)
    assert Validator(db).calibration_study().verdict == "INSUFFICIENT_DATA"
    db.close()
    db = Database(tmp_path / "b.db")
    db.open()
    _seed_settled(db, 3000, True)
    r = Validator(db).calibration_study()
    assert r.verdict == "PASS", r.reason
    assert r.metrics["n_windows"] == 3000 and r.metrics["ece"] < 0.03
    stored = db.one("SELECT * FROM validation_studies WHERE study_id=?", (r.study_id,))
    assert stored["dataset_hash"] and stored["model_version"] == MODEL_VERSION
    db.close()
    db = Database(tmp_path / "c.db")
    db.open()
    _seed_settled(db, 3000, False)
    assert Validator(db).calibration_study().verdict == "FAIL"
    db.close()


def test_pnl_study_requires_independent_windows(db: Database) -> None:
    v = Validator(db)
    assert v.pnl_study().verdict == "INSUFFICIENT_DATA"
    summary = v.summarize()
    assert summary["overall"] in ("INSUFFICIENT_DATA", "NOT_RUN") and summary["live_capital_justified"] is False


def test_execution_sensitivity_study(db: Database) -> None:
    close = 1_800_000_000.0
    for i in range(10):
        t = f"KXBTC15M-S{i}"
        db.insert(
            "markets",
            {
                "ticker": t,
                "series": "S",
                "asset": "BTC",
                "cf_index": "BRTI",
                "close_ts": close + i * 900,
                "result": "yes",
                "first_seen_ts": 0,
                "updated_ts": 0,
            },
        )
        db.insert("validation_partitions", {"ticker": t, "partition": "LIVE_FORWARD", "assigned_ts": 0, "rule": "t"})
        db.insert(
            "settlements",
            {
                "mode": "PAPER",
                "ticker": t,
                "outcome": "yes",
                "result": "yes",
                "settled_ts": 0,
                "count": "10",
                "cost": "9.70",
                "fees": "0.03",
                "payout": "10",
                "pnl": "0.27",
                "source": "t",
            },
        )
    r = Validator(db).execution_sensitivity_study()
    sc = r.metrics["scenarios"]
    assert sc["price_worse_0"]["mean_pnl_per_contract"] > sc["price_worse_0.03"]["mean_pnl_per_contract"]
    assert r.verdict == "PASS"  # 2.7¢ edge survives 1¢ worse price: 10 - 9.80 - 0.02 fee = +1.8¢
    db.execute("UPDATE settlements SET cost='9.95', pnl='0.02'")
    assert Validator(db).execution_sensitivity_study().verdict == "FAIL"  # 0.5¢ edge does not survive


def test_wilson_and_bootstrap() -> None:
    lo, hi = stats.wilson_interval(90, 100)
    assert 0.82 < lo < 0.85 and 0.94 < hi < 0.96
    pt, lo, hi = stats.cluster_bootstrap_mean_ci({"a": [1.0], "b": [-1.0], "c": [1.0], "d": [1.0]})
    assert pt == 0.5 and lo is not None and lo <= pt <= hi


def test_knowledge_base(db: Database) -> None:
    assert seed_knowledge(db) >= 5
    assert seed_knowledge(db) == 0  # never re-seeded/overwritten
    record_result(db, "H-MOM-001", "REJECTED", "no OOS improvement")
    h = {x["id"]: x for x in list_hypotheses(db)}
    assert h["H-MOM-001"]["status"] == "REJECTED"
    with pytest.raises(ValueError):
        record_result(db, "H-MOM-001", "PROMOTED_TO_RULE", "")


# ---------------------------------------------------------------------- REST
async def test_rest_retries_reads_not_writes(rsa_pem: bytes) -> None:
    calls = {"GET": 0, "POST": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls[req.method] += 1
        return httpx.Response(500, text="boom")

    async def nosleep(_: float) -> None:
        return None

    rest = KalshiRest(
        "https://x.invalid/trade-api/v2", KalshiSigner("k", rsa_pem), transport=httpx.MockTransport(h), sleep=nosleep
    )
    with pytest.raises(KalshiHTTPError):
        await rest.get_balance()
    assert calls["GET"] == 3
    with pytest.raises(AmbiguousWriteError):
        await rest.create_order_v2({"ticker": "X"})
    assert calls["POST"] == 1


async def test_rest_429_and_signing_path(rsa_pem: bytes) -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(429, text="slow down")

    async def nosleep(_: float) -> None:
        return None

    rest = KalshiRest(
        "https://x.invalid/trade-api/v2", KalshiSigner("k", rsa_pem), transport=httpx.MockTransport(h), sleep=nosleep
    )
    with pytest.raises(RateLimitedError):
        await rest.create_order_v2({"ticker": "X"})
    assert len(seen) == 1 and seen[0].url.path == "/trade-api/v2/portfolio/events/orders"
    assert rest.stats.rate_limited == 1


async def test_rest_clock_skew_tracked(rsa_pem: bytes) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"exchange_active": True}, headers={"date": "Mon, 01 Jan 2024 00:00:00 GMT"})

    rest = KalshiRest("https://x.invalid/trade-api/v2", None, transport=httpx.MockTransport(h))
    await rest.exchange_status()
    assert rest.stats.server_clock_skew_s is not None and rest.stats.server_clock_skew_s > 1e6


# ----------------------------------------------------------------- discovery
def _api_market(ticker: str, open_ts: float, close_ts: float, status: str = "active") -> dict:
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    return {
        "ticker": ticker,
        "event_ticker": ticker[:-3],
        "market_type": "binary",
        "status": status,
        "open_time": iso(open_ts),
        "close_time": iso(close_ts),
        "strike_type": "greater_or_equal",
        "floor_strike": 100000,
        "rules_primary": "average of CF Benchmarks BRTI",
        "rules_secondary": "",
        "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
    }


async def test_discovery_rotation_and_exclusions() -> None:
    now = time.time()
    qs = int(now // 900) * 900
    state = {
        "markets": [
            _api_market("KXBTC15M-CUR-00", qs, qs + 900),
            _api_market("KXBTC15M-FUT-00", qs + 900, qs + 1800),  # far-future/unopened: must not be active
            _api_market("KXBTC15M-HOUR-00", qs, qs + 3600),  # wrong duration: tracked but unverified
        ]
    }

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/series/KXBTC15M"):
            return httpx.Response(
                200,
                json={
                    "series": {
                        "ticker": "KXBTC15M",
                        "category": "Crypto",
                        "settlement_sources": [{"name": "CF Benchmarks"}],
                        "fee_type": "quadratic",
                        "fee_multiplier": 1,
                    }
                },
            )
        if req.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": state["markets"], "cursor": ""})
        return httpx.Response(404, json={})

    rest = KalshiRest("https://x.invalid/trade-api/v2", None, transport=httpx.MockTransport(h))
    reg = MarketRegistry((SeriesSpec("KXBTC15M", "BTC", "BRTI"),))
    new, closed = await reg.refresh(rest, now)
    assert set(new) == {"KXBTC15M-CUR-00", "KXBTC15M-HOUR-00"}
    assert [t.info.ticker for t in reg.active_verified()] == ["KXBTC15M-CUR-00"]
    assert "duration" in " ".join(reg.markets["KXBTC15M-HOUR-00"].verify_notes)
    # rollover: the current market expires, the next one opens
    later = qs + 900 + 5
    state["markets"] = [_api_market("KXBTC15M-FUT-00", qs + 900, qs + 1800)]
    new, closed = await reg.refresh(rest, later)
    assert new == ["KXBTC15M-FUT-00"] and "KXBTC15M-CUR-00" in closed
    assert reg.markets["KXBTC15M-CUR-00"].phase == "CLOSED"
    assert reg.rollovers == 1


def test_verify_market_requires_cf_source() -> None:
    now = time.time()
    m = make_market(now=now)
    m.rules_primary = "average of some other index"
    st = SeriesState(
        SeriesSpec("KXBTC15M", "BTC", "BRTI"), available=True, verified=True, settlement_sources=["Coinbase"]
    )
    assert any("CF Benchmarks" in p for p in verify_market(m, st, now))
    st.settlement_sources = ["CF Benchmarks https://cfbenchmarks.com"]
    assert verify_market(m, st, now) == []
    assert window_id(m.close_ts) == round(m.close_ts / 900)
