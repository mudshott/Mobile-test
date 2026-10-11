"""Settlement-aware probability model.

Question answered: "Given what is observable now, what is the defensible
probability that THIS Kalshi contract settles YES under its own rules?"

Settlement: A = mean of the 60 one-second CF RTI ticks in (close-60s, close].
YES iff A satisfies the market's strike rule (greater / greater_or_equal /
less / less_or_equal / between, against floor/cap strike).

Model (horizon ≤ 15 min, zero drift): price follows an arithmetic random walk
with per-second absolute volatility σ·S, σ estimated from realized 1 s CF log
returns. For window ticks at future offsets u_i the average has

    E[A]   = (k·m_obs + n·S) / 60
    Var[A] = σ²S² · Σ_i Σ_j min(u_i, u_j) / 3600

where k ticks are already observed (final minute) with mean m_obs and n = 60-k
are still in the future. Closed forms: if τ ≥ 60 s (a = τ - 60),
ΣΣ = 3600·a + 60·61·121/6; if inside the final minute, ΣΣ = n(n+1)(2n+1)/6.

An ensemble {Gaussian, Student-t(ν=4)} × {σ_short, σ_long} gives the raw
probability (mean) and model disagreement (range). Uncertainty adds the
σ-estimation error band (σ·(1 ± 2/√(2N))). The conservative probability for a
side is the least favourable bound minus a small floor, capped at
`CONSERVATIVE_CAP` until held-out calibration of the high-confidence region
exists.

The model NEVER uses Kalshi prices as an input.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from predictionbot.buildinfo import MODEL_VERSION
from predictionbot.market.models import MarketInfo
from predictionbot.model.features import estimate_vol, momentum
from predictionbot.reference.cf import CFReferenceFeed

MIN_RETURNS = 300  # ≥5 minutes of 1 s returns before READY
MIN_COVERAGE = 0.80
CONSERVATIVE_FLOOR = 0.005
CONSERVATIVE_CAP = 0.97
STUDENT_NU = 4.0
SUM_MIN_FULL_WINDOW = 60 * 61 * 121 / 6  # Σ_i Σ_j min(i, j), i,j = 1..60


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def student_t_cdf(x: float, nu: float) -> float:
    """CDF of Student-t via the regularized incomplete beta function."""
    if x == 0:
        return 0.5
    t2 = x * x
    ib = _reg_inc_beta(nu / 2.0, 0.5, nu / (nu + t2))
    return 1.0 - 0.5 * ib if x > 0 else 0.5 * ib


def _reg_inc_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b + lbeta)
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes betacf)."""
    fpmin, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > fpmin else fpmin)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > fpmin else fpmin)
        c = 1.0 + aa / c
        c = c if abs(c) > fpmin else fpmin
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > fpmin else fpmin)
        c = 1.0 + aa / c
        c = c if abs(c) > fpmin else fpmin
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _cdf(z: float, dist: str) -> float:
    if dist == "gauss":
        return norm_cdf(z)
    # Unit-variance Student-t.
    scale = math.sqrt(STUDENT_NU / (STUDENT_NU - 2))
    return student_t_cdf(z * scale, STUDENT_NU)


@dataclass
class Prediction:
    ticker: str
    ts: float
    state: str  # WARMING_UP | READY | DEGRADED | FAILED
    model_version: str = MODEL_VERSION
    missing: list[str] = field(default_factory=list)
    raw_prob_yes: float | None = None
    lower_prob_yes: float | None = None
    upper_prob_yes: float | None = None
    cons_prob_yes: float | None = None
    cons_prob_no: float | None = None
    uncertainty: float | None = None
    disagreement: float | None = None
    seconds_to_close: float | None = None
    spot: float | None = None
    spot_age_s: float | None = None
    expected_avg: float | None = None
    sd_avg: float | None = None
    sigma_short: float | None = None
    sigma_long: float | None = None
    n_returns: int = 0
    coverage: float | None = None
    window_observed: int = 0
    research: dict[str, float | None] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.state == "READY" and self.cons_prob_yes is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prob_condition(market: MarketInfo, mean: float, sd: float, dist: str) -> float:
    st = market.strike_type
    if sd <= 0:
        sd = 1e-12

    def p_above(k: float) -> float:
        return 1.0 - _cdf((k - mean) / sd, dist)

    if st in ("greater", "greater_or_equal"):
        assert market.floor_strike is not None
        return p_above(float(market.floor_strike))
    if st in ("less", "less_or_equal"):
        assert market.cap_strike is not None
        return 1.0 - p_above(float(market.cap_strike))
    if st == "between":
        assert market.floor_strike is not None and market.cap_strike is not None
        return max(0.0, p_above(float(market.floor_strike)) - p_above(float(market.cap_strike)))
    raise ValueError(f"unsupported strike_type {st}")


class SettlementModel:
    def __init__(self, feed: CFReferenceFeed) -> None:
        self.feed = feed

    def predict(self, market: MarketInfo, now: float | None = None) -> Prediction:
        now = time.time() if now is None else now
        now_ms = int(now * 1000)
        pred = Prediction(ticker=market.ticker, ts=now, state="WARMING_UP")
        tau = market.close_ts - now
        pred.seconds_to_close = tau
        try:
            return self._predict(market, now, now_ms, tau, pred)
        except Exception as exc:  # model must fail closed, never crash the loop
            pred.state = "FAILED"
            pred.missing.append(f"MODEL_ERROR:{exc.__class__.__name__}")
            return pred

    def _predict(self, market: MarketInfo, now: float, now_ms: int, tau: float, pred: Prediction) -> Prediction:
        if not market.strike_supported():
            pred.state = "FAILED"
            pred.missing.append(f"STRIKE_UNSUPPORTED_OR_MISSING:{market.strike_type}")
            return pred
        if tau <= 0:
            pred.state = "FAILED"
            pred.missing.append("MARKET_CLOSED")
            return pred
        idx = market.cf_index_id
        latest = self.feed.latest(idx)
        if latest is None:
            pred.missing.append(f"CF_NO_DATA:{idx}")
            return pred
        _src_ms, spot = latest
        pred.spot = spot
        st = self.feed.indices[idx]
        pred.spot_age_s = st.age(now)
        ticks = self.feed.history(idx, now_ms - 1800 * 1000)
        vol = estimate_vol(ticks, now_ms)
        pred.n_returns = vol.n_returns
        pred.coverage = round(vol.coverage, 3)
        pred.sigma_short, pred.sigma_long = vol.sigma_short, vol.sigma_long
        pred.research = {
            "momentum_60s": momentum(ticks, now_ms, 60),
            "momentum_300s": momentum(ticks, now_ms, 300),
            "vol_ratio_short_long": (vol.sigma_short / vol.sigma_long) if vol.sigma_short and vol.sigma_long else None,
        }
        if vol.n_returns < MIN_RETURNS or vol.sigma_short is None or vol.sigma_long is None:
            pred.missing.append(f"INSUFFICIENT_HISTORY:{vol.n_returns}/{MIN_RETURNS} one-second returns")
            return pred
        degraded: list[str] = []
        if not self.feed.is_fresh(idx, now):
            degraded.append(f"CF_STALE:{idx} age={pred.spot_age_s}")
        if vol.coverage < MIN_COVERAGE:
            degraded.append(f"CF_HISTORY_GAPS:coverage={vol.coverage:.2f}")

        # ---- settlement-window geometry
        close_ms = round(market.close_ts * 1000)
        if tau >= 60:
            k, m_obs = 0, 0.0
            a = tau - 60.0
            sum_min = 3600.0 * a + SUM_MIN_FULL_WINDOW
            n_future = 60
        else:
            fw = self.feed.final_window(idx, close_ms)
            elapsed = 60 - math.ceil(tau)
            if fw is None or fw.window_size < max(0, elapsed - 2):
                pred.missing.append(f"FINAL_WINDOW_INCOMPLETE:have={fw.window_size if fw else 0} expected≈{elapsed}")
                pred.state = "WARMING_UP"
                return pred
            k = min(60, fw.window_size)
            m_obs = fw.average
            n_future = 60 - k
            sum_min = n_future * (n_future + 1) * (2 * n_future + 1) / 6.0
        pred.window_observed = k
        mean = (k * m_obs + n_future * spot) / 60.0
        pred.expected_avg = mean

        probs: list[float] = []
        bounds: list[float] = []
        n_eff = max(30, vol.n_returns)
        rel_err = 2.0 / math.sqrt(2.0 * n_eff)
        for sigma in (vol.sigma_short, vol.sigma_long):
            for dist in ("gauss", "student"):
                sd = sigma * spot * math.sqrt(sum_min) / 60.0
                p = _prob_condition(market, mean, sd, dist)
                probs.append(p)
                for f in (1.0 - rel_err, 1.0 + rel_err):
                    bounds.append(_prob_condition(market, mean, sd * f, dist))
        sd_main = max(vol.sigma_short, vol.sigma_long) * spot * math.sqrt(sum_min) / 60.0
        pred.sd_avg = sd_main
        raw = sum(probs) / len(probs)
        lo = min(probs + bounds)
        hi = max(probs + bounds)
        raw = min(max(raw, 0.0005), 0.9995)
        pred.raw_prob_yes = raw
        pred.lower_prob_yes = lo
        pred.upper_prob_yes = hi
        pred.disagreement = max(probs) - min(probs)
        pred.uncertainty = hi - lo
        pred.cons_prob_yes = min(CONSERVATIVE_CAP, max(0.0, lo - CONSERVATIVE_FLOOR))
        pred.cons_prob_no = min(CONSERVATIVE_CAP, max(0.0, (1.0 - hi) - CONSERVATIVE_FLOOR))
        if degraded:
            pred.state = "DEGRADED"
            pred.missing.extend(degraded)
        else:
            pred.state = "READY"
        return pred


def decimal_prob(p: float) -> Decimal:
    return Decimal(repr(round(p, 6)))
