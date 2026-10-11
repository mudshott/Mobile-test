"""CF Benchmarks feed parsing + settlement-aware probability model."""

from __future__ import annotations

import json
import time

from predictionbot.model.probability import SUM_MIN_FULL_WINDOW, SettlementModel, norm_cdf
from predictionbot.reference.cf import CFReferenceFeed
from tests.conftest import feed_with_history, make_market

OFFICIAL_EXAMPLE = {
    "type": "cfbenchmarks_value",
    "sending_ts_ms": 1669149841234,
    "sid": 1,
    "seq": 42,
    "msg": {
        "index_id": "BRTI",
        "received_at": 1710000000123,
        "data": '{"type":"value","id":"BRTI","time":1710000000123,"value":"68000.12"}',
        "avg_60s_data": {
            "value": "68000.12000000",
            "window_size": 3,
            "window_start_ts_ms": 1709999940123,
            "window_end_ts_exclusive": 1710000000123,
        },
        "last_60s_windowed_average_15min": {
            "value": "68000.23000000",
            "window_size": 14,
            "window_start_ts_ms": 1709999980000,
            "window_end_ts_exclusive": 1710000000123,
        },
    },
}


def test_parse_official_example() -> None:
    f = CFReferenceFeed()
    assert f.on_message(OFFICIAL_EXAMPLE, recv_ts=time.time()) == "BRTI"
    assert f.latest("BRTI") == (1710000000123, 68000.12)
    close_ms = 1710000900000  # next quarter-hour boundary after the tick
    fw = f.final_window("BRTI", close_ms)
    assert fw is not None and fw.window_size == 14 and abs(fw.average - 68000.23) < 1e-9


def test_cf_stale_and_recovery() -> None:
    f = CFReferenceFeed(stale_after_s=5)
    f.on_message(OFFICIAL_EXAMPLE, recv_ts=time.time() - 30)
    assert not f.is_fresh("BRTI")
    later = json.loads(json.dumps(OFFICIAL_EXAMPLE))
    later["msg"]["data"] = later["msg"]["data"].replace("1710000000123", "1710000001123")
    f.on_message(later, recv_ts=time.time())
    assert f.is_fresh("BRTI")


def test_cf_rejects_malformed_and_out_of_order() -> None:
    f = CFReferenceFeed()
    bad = json.loads(json.dumps(OFFICIAL_EXAMPLE))
    bad["msg"]["data"] = "{not json"
    f.on_message(bad)
    assert f.indices["BRTI"].errors == 1 and f.latest("BRTI") is None
    neg = json.loads(json.dumps(OFFICIAL_EXAMPLE))
    neg["msg"]["data"] = '{"time":1,"value":"-5"}'
    f.on_message(neg)
    assert f.latest("BRTI") is None  # never fabricate / accept invalid values
    f.on_message(OFFICIAL_EXAMPLE)
    f.on_message(OFFICIAL_EXAMPLE)
    assert f.indices["BRTI"].out_of_order == 1 and f.indices["BRTI"].count == 1


def test_warming_up_reports_missing() -> None:
    now = time.time()
    feed = feed_with_history(seconds=50, now=now)
    p = SettlementModel(feed).predict(make_market(now=now), now)
    assert p.state == "WARMING_UP"
    assert any(m.startswith("INSUFFICIENT_HISTORY:49/300") for m in p.missing)
    assert p.raw_prob_yes is None  # never substitutes zero or a market price


def test_no_cf_data() -> None:
    p = SettlementModel(CFReferenceFeed()).predict(make_market())
    assert p.state == "WARMING_UP" and p.missing and p.missing[0].startswith("CF_NO_DATA")


def test_probability_geometry() -> None:
    now = time.time()
    m = SettlementModel(feed_with_history(spot=100000, now=now))
    at = m.predict(make_market(now=now, strike=100000, seconds_to_close=600), now)
    assert at.state == "READY" and abs(at.raw_prob_yes - 0.5) < 0.03
    above = m.predict(make_market(now=now, strike=99000, seconds_to_close=600), now)
    below = m.predict(make_market(now=now, strike=101000, seconds_to_close=600), now)
    assert above.raw_prob_yes > 0.95 and below.raw_prob_yes < 0.05
    # less time → more certain for the same distance
    near = m.predict(make_market(now=now, strike=99900, seconds_to_close=900), now)
    late = m.predict(make_market(now=now, strike=99900, seconds_to_close=90), now)
    assert late.raw_prob_yes > near.raw_prob_yes
    # conservative probabilities never exceed raw-side probability
    for p in (at, above, below, near, late):
        assert p.cons_prob_yes <= p.raw_prob_yes + 1e-12
        assert p.cons_prob_no <= 1 - p.raw_prob_yes + 1e-12
        assert p.cons_prob_yes <= 0.97 and p.cons_prob_no <= 0.97
        assert 0 <= p.lower_prob_yes <= p.raw_prob_yes <= p.upper_prob_yes <= 1


def test_less_strike_type_inverts() -> None:
    now = time.time()
    m = SettlementModel(feed_with_history(spot=100000, now=now))
    g = m.predict(make_market(now=now, strike=99500, strike_type="greater"), now)
    mk = make_market(now=now, strike=99500, strike_type="less")
    mk.cap_strike, mk.floor_strike = mk.floor_strike, None
    lt = m.predict(mk, now)
    assert abs(g.raw_prob_yes + lt.raw_prob_yes - 1) < 1e-6


def test_variance_formula_full_window() -> None:
    # ΣΣ min(i,j) for i,j=1..60 equals n(n+1)(2n+1)/6
    assert SUM_MIN_FULL_WINDOW == sum(min(i, j) for i in range(1, 61) for j in range(1, 61))


def test_final_window_uses_observed_average() -> None:
    now = time.time()
    feed = feed_with_history(spot=100000, now=now)
    mk = make_market(now=now, strike=100000, seconds_to_close=10)
    close_ms = round(mk.close_ts * 1000)
    # 50 of 60 window ticks already observed well above the strike → YES nearly certain.
    from predictionbot.reference.cf import FinalWindow

    feed.indices["BRTI"].final_windows[close_ms] = FinalWindow(close_ms, 100400.0, 50, now)
    p = SettlementModel(feed).predict(mk, now)
    assert p.state == "READY" and p.window_observed == 50 and p.raw_prob_yes > 0.99
    # missing final-window data inside the last minute → not READY
    feed2 = feed_with_history(spot=100000, now=now)
    feed2.indices["BRTI"].ticks.clear()
    for i in range(400):
        feed2.indices["BRTI"].ticks.append((close_ms - 400_000 - 70_000 + i * 1000, 100000.0 + i))
    p2 = SettlementModel(feed2).predict(mk, now)
    assert p2.state != "READY"


def test_cf_stale_degrades_model() -> None:
    now = time.time()
    feed = feed_with_history(now=now - 30)  # last tick 30 s ago
    p = SettlementModel(feed).predict(make_market(now=now), now)
    assert p.state == "DEGRADED" and any("CF_STALE" in x for x in p.missing)


def test_norm_cdf() -> None:
    assert abs(norm_cdf(0) - 0.5) < 1e-12 and abs(norm_cdf(1.959964) - 0.975) < 1e-6
