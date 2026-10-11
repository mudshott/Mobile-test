"""Feature extraction from settlement-reference ticks.

Only features listed in `TRADING_FEATURES` feed the probability model. The
`RESEARCH_FEATURES` are computed and persisted so they can be tested
out-of-sample; they must earn promotion through the research pipeline
(see research/knowledge.py) before influencing trades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TRADING_FEATURES = ("settlement_geometry", "realized_vol_1s")
RESEARCH_FEATURES = ("momentum_60s", "momentum_300s", "book_imbalance_top", "vol_ratio_short_long")


@dataclass
class VolEstimate:
    sigma_short: float | None  # per-sqrt-second log-return vol, EWMA (half-life 5 min)
    sigma_long: float | None  # plain realized vol over the long window
    n_returns: int
    coverage: float  # fraction of seconds in the window with a fresh 1 s return


def one_second_returns(ticks: list[tuple[int, float]]) -> list[tuple[int, float, float]]:
    """(end_ts_ms, log_return, dt_seconds) between consecutive ticks; dt ≥ 0.5 s."""
    out: list[tuple[int, float, float]] = []
    prev: tuple[int, float] | None = None
    for ts, v in ticks:
        if v <= 0:
            continue
        if prev is not None:
            dt = (ts - prev[0]) / 1000.0
            if dt >= 0.5:
                out.append((ts, math.log(v / prev[1]), dt))
                prev = (ts, v)
            continue
        prev = (ts, v)
    return out


def estimate_vol(
    ticks: list[tuple[int, float]], now_ms: int, long_window_s: int = 1800, half_life_s: float = 300.0
) -> VolEstimate:
    window = [t for t in ticks if t[0] >= now_ms - long_window_s * 1000]
    rets = one_second_returns(window)
    rets = [r for r in rets if r[2] <= 10.0]  # ignore returns spanning outages
    n = len(rets)
    if n == 0:
        return VolEstimate(None, None, 0, 0.0)
    span_s = max(1.0, (window[-1][0] - window[0][0]) / 1000.0)
    coverage = min(1.0, sum(r[2] for r in rets) / span_s)
    # Variance per second, normalising each return by its own dt.
    var_long = sum(r[1] ** 2 / r[2] for r in rets) / n
    lam = math.log(2) / half_life_s
    wsum = 0.0
    vsum = 0.0
    for ts, r, dt in rets:
        w = math.exp(-lam * (now_ms - ts) / 1000.0)
        wsum += w
        vsum += w * r * r / dt
    var_short = vsum / wsum if wsum > 0 else var_long
    return VolEstimate(math.sqrt(var_short), math.sqrt(var_long), n, coverage)


def momentum(ticks: list[tuple[int, float]], now_ms: int, lookback_s: int) -> float | None:
    if not ticks:
        return None
    past = [v for t, v in ticks if t <= now_ms - lookback_s * 1000]
    if not past:
        return None
    return math.log(ticks[-1][1] / past[-1])
