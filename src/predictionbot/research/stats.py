"""Small, dependency-free statistics helpers used by validation studies."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Sequence

Z_95_ONE_SIDED = 1.6449
Z_95_TWO_SIDED = 1.9600
Z_80_POWER = 0.8416


def z_two_sided(alpha: float) -> float:
    """Two-sided normal quantile z such that P(|Z| > z) = alpha (bisection on erfc)."""
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if math.erfc(mid / math.sqrt(2)) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def wilson_interval(successes: int, n: int, z: float = Z_95_TWO_SIDED) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float | None:
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes, strict=True)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int], eps: float = 1e-6) -> float | None:
    if not probs:
        return None
    total = 0.0
    for p, o in zip(probs, outcomes, strict=True):
        q = min(1 - eps, max(eps, p))
        total += -(o * math.log(q) + (1 - o) * math.log(1 - q))
    return total / len(probs)


def calibration_buckets(
    probs: Sequence[float],
    outcomes: Sequence[int],
    edges: Sequence[float] = (0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001),
) -> list[dict[str, float | int | None]]:
    out: list[dict[str, float | int | None]] = []
    for lo, hi in itertools.pairwise(edges):
        idx = [i for i, p in enumerate(probs) if lo <= p < hi]
        n = len(idx)
        if n == 0:
            out.append(
                {
                    "lo": lo,
                    "hi": min(hi, 1.0),
                    "n": 0,
                    "mean_pred": None,
                    "empirical": None,
                    "ci_lo": None,
                    "ci_hi": None,
                }
            )
            continue
        wins = sum(outcomes[i] for i in idx)
        lo_ci, hi_ci = wilson_interval(wins, n)
        out.append(
            {
                "lo": lo,
                "hi": min(hi, 1.0),
                "n": n,
                "mean_pred": sum(probs[i] for i in idx) / n,
                "empirical": wins / n,
                "ci_lo": lo_ci,
                "ci_hi": hi_ci,
            }
        )
    return out


def ece(buckets: list[dict[str, float | int | None]], total: int) -> float | None:
    if total == 0:
        return None
    s = 0.0
    for b in buckets:
        n = int(b["n"] or 0)
        if n:
            s += n / total * abs(float(b["mean_pred"] or 0) - float(b["empirical"] or 0))
    return s


def cluster_bootstrap_mean_ci(
    clusters: dict[str, list[float]], iterations: int = 2000, seed: int = 12345, alpha: float = 0.05
) -> tuple[float | None, float | None, float | None]:
    """Mean of per-cluster means with a cluster bootstrap CI (clusters = independent windows)."""
    means = [sum(v) / len(v) for v in clusters.values() if v]
    if not means:
        return (None, None, None)
    point = sum(means) / len(means)
    if len(means) < 2:
        return (point, None, None)
    rng = random.Random(seed)
    k = len(means)
    boots = []
    for _ in range(iterations):
        sample = [means[rng.randrange(k)] for _ in range(k)]
        boots.append(sum(sample) / k)
    boots.sort()
    lo = boots[int(alpha / 2 * iterations)]
    hi = boots[int((1 - alpha / 2) * iterations) - 1]
    return (point, lo, hi)


def required_n_for_mean(sd: float, mde: float, z_alpha: float = Z_95_ONE_SIDED, z_beta: float = Z_80_POWER) -> int:
    """Independent units needed to detect a mean of `mde` (one-sided α, power 1-β)."""
    if mde <= 0:
        raise ValueError("mde must be positive")
    return math.ceil(((z_alpha + z_beta) * sd / mde) ** 2)


def required_n_for_proportion(p: float, margin: float, z: float = Z_95_TWO_SIDED) -> int:
    return math.ceil(z * z * p * (1 - p) / (margin * margin))
