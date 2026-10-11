"""Strategy validation — statistical evidence, separate from operational health.

PARTITIONS (assigned once per market at first sight, never moved):
  * The unit of independence is the 15-minute WINDOW (close time), not the
    market: BTC/ETH/SOL/XRP/DOGE windows closing together are correlated, so
    every market in a window gets the same partition.
  * Markets first seen while a PAPER/LIVE session is running → LIVE_FORWARD.
  * Otherwise deterministic hash of the window → TRAIN 50% / VALIDATION 20% /
    OOS_TEST 30%. TRAIN/VALIDATION may be used for research and threshold
    tuning; OOS_TEST and LIVE_FORWARD are never used for tuning.
  * Repeated observations inside one contract are NOT independent: studies
    take one prediction per market and cluster by window.

POLICY (pre-registered; see docs/FINAL_DEVELOPMENT_REPORT.md for the math):
  * P&L: n_required = ⌈((z₀.₉₅ + z₀.₈₀)·σ / MDE)²⌉ independent traded windows,
    MDE = $0.03/contract, σ = max(observed SD, 0.30). With σ = 0.48 this is
    1,583 windows; PASS also requires the 95% cluster-bootstrap lower bound of
    mean P&L/contract > 0.
  * Calibration: ≥ 400 independent windows (≥ ~4 days), ECE ≤ 0.03 and no
    populated bucket (n ≥ 30) whose Wilson 95% CI misses the mean prediction
    by more than 0.02.
  * High-confidence (≥0.90) region: n ≥ ⌈1.96²·0.9·0.1/0.03²⌉ = 385 and
    Wilson lower bound ≥ mean prediction − 0.03, else the >30%-style
    high-confidence sizing override stays DISABLED.
  * Paper execution: ≥ 97 fills (±0.10 fill-rate precision at 95%).
  * Execution stress: mean P&L/contract must stay > 0 with prices 1¢ worse.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from predictionbot.buildinfo import MODEL_VERSION, STRATEGY_VERSION
from predictionbot.fixedpoint import D
from predictionbot.persistence.db import Database
from predictionbot.research import stats
from predictionbot.strategy.fees import taker_fee

PARTITIONS = ("TRAIN", "VALIDATION", "OOS_TEST", "LIVE_FORWARD")


def window_id(close_ts: float) -> int:
    return round(close_ts / 900.0)


def hash_partition(close_ts: float) -> str:
    h = int(hashlib.sha256(f"pcb-window-{window_id(close_ts)}".encode()).hexdigest()[:8], 16) % 10
    if h < 5:
        return "TRAIN"
    if h < 7:
        return "VALIDATION"
    return "OOS_TEST"


def assign_partition(db: Database, ticker: str, close_ts: float, *, live_session_running: bool) -> str:
    row = db.one("SELECT partition FROM validation_partitions WHERE ticker=?", (ticker,))
    if row:
        return str(row["partition"])
    # Keep a whole window in one partition: reuse the partition of any market in the same window.
    wid = window_id(close_ts)
    sibling = db.one(
        "SELECT vp.partition FROM validation_partitions vp JOIN markets m ON m.ticker = vp.ticker "
        "WHERE CAST(ROUND(m.close_ts / 900.0) AS INTEGER) = ? LIMIT 1",
        (wid,),
    )
    if sibling:
        part, rule = str(sibling["partition"]), "window-sibling"
    elif live_session_running:
        part, rule = "LIVE_FORWARD", "first-seen-during-running-session"
    else:
        part, rule = hash_partition(close_ts), "sha256(window) 50/20/30"
    db.insert("validation_partitions", {"ticker": ticker, "partition": part, "assigned_ts": time.time(), "rule": rule})
    return part


@dataclass(frozen=True)
class ValidationPolicy:
    mde_per_contract: float = 0.03
    sd_floor: float = 0.30
    sd_prior: float = 0.48
    min_calibration_windows: int = 400
    max_ece: float = 0.03
    bucket_tolerance: float = 0.02
    bucket_min_n: int = 30
    high_conf_threshold: float = 0.90
    high_conf_margin: float = 0.03
    min_paper_fills: int = 97
    stress_price_shift: float = 0.01

    @property
    def high_conf_min_n(self) -> int:
        return stats.required_n_for_proportion(self.high_conf_threshold, self.high_conf_margin)

    def pnl_required_windows(self, observed_sd: float | None) -> int:
        sd = max(self.sd_floor, observed_sd if observed_sd is not None else self.sd_prior)
        return stats.required_n_for_mean(sd, self.mde_per_contract)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["high_conf_min_n"] = self.high_conf_min_n
        d["pnl_required_windows_at_prior_sd"] = self.pnl_required_windows(None)
        return d


@dataclass
class StudyResult:
    study_id: str
    study_type: str
    verdict: str  # PASS | FAIL | INSUFFICIENT_DATA
    reason: str
    metrics: dict[str, Any]
    unique_markets: int
    observations: int
    fills: int
    dataset_hash: str
    partition: str
    time_start: float | None
    time_end: float | None
    assumptions: dict[str, Any]


def _hash_rows(rows: list[tuple[Any, ...]]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: json.dumps(x, default=str)):
        h.update(json.dumps(r, default=str).encode())
    return h.hexdigest()[:16]


def _persist(db: Database, r: StudyResult, *, fee_model: str, slippage_model: str, latency_model: str) -> None:
    db.insert(
        "validation_studies",
        {
            "study_id": r.study_id,
            "study_type": r.study_type,
            "created_ts": time.time(),
            "model_version": MODEL_VERSION,
            "strategy_version": STRATEGY_VERSION,
            "dataset_hash": r.dataset_hash,
            "time_start": r.time_start,
            "time_end": r.time_end,
            "partition": r.partition,
            "unique_markets": r.unique_markets,
            "observations": r.observations,
            "fills": r.fills,
            "assumptions": r.assumptions,
            "fee_model": fee_model,
            "slippage_model": slippage_model,
            "latency_model": latency_model,
            "metrics": r.metrics,
            "verdict": r.verdict,
            "reason": r.reason,
        },
    )


FEE_MODEL = "Kalshi quadratic taker 0.07*C*P*(1-P)*mult, rounded up to $0.01 per order/level"
SLIPPAGE_MODEL = "IOC limit 1 tick above best ask; only 50% of displayed depth assumed reachable"
LATENCY_MODEL = "order matched against the book 250 ms after the decision"


class Validator:
    def __init__(self, db: Database, policy: ValidationPolicy | None = None) -> None:
        self.db = db
        self.policy = policy or ValidationPolicy()

    # --------------------------------------------------------- calibration
    def calibration_study(
        self, partitions: tuple[str, ...] = ("OOS_TEST", "LIVE_FORWARD"), target_s: float = 180.0
    ) -> StudyResult:
        p = self.policy
        ph = ",".join("?" * len(partitions))
        rows = self.db.query(
            f"""SELECT pr.id, pr.ticker, pr.ts, pr.raw_prob_yes, pr.seconds_to_close, pr.yes_bid, pr.yes_ask,
                       m.result, m.close_ts, vp.partition
                FROM predictions pr
                JOIN markets m ON m.ticker = pr.ticker
                JOIN validation_partitions vp ON vp.ticker = pr.ticker
                WHERE pr.state='READY' AND pr.raw_prob_yes IS NOT NULL AND m.result IN ('yes','no')
                  AND pr.model_version = ? AND vp.partition IN ({ph})
                  AND pr.seconds_to_close BETWEEN 60 AND 600""",  # noqa: S608
            (MODEL_VERSION, *partitions),
        )
        best: dict[str, dict[str, Any]] = {}
        for r in rows:
            cur = best.get(r["ticker"])
            if cur is None or abs(r["seconds_to_close"] - target_s) < abs(cur["seconds_to_close"] - target_s):
                best[r["ticker"]] = r
        sel = list(best.values())
        probs = [float(r["raw_prob_yes"]) for r in sel]
        outs = [1 if r["result"] == "yes" else 0 for r in sel]
        windows = {window_id(float(r["close_ts"])) for r in sel}
        buckets = stats.calibration_buckets(probs, outs)
        e = stats.ece(buckets, len(probs))
        # Market-implied baseline at the same moment (mid of the Kalshi quote), for comparison only.
        mk_probs, mk_outs = [], []
        for r in sel:
            try:
                mid = (float(r["yes_bid"]) + float(r["yes_ask"])) / 2
            except (TypeError, ValueError):
                continue
            mk_probs.append(mid)
            mk_outs.append(1 if r["result"] == "yes" else 0)
        hc = [
            (pp, o)
            for pp, o in zip(probs, outs, strict=True)
            if pp >= p.high_conf_threshold or pp <= 1 - p.high_conf_threshold
        ]
        # Fold the low tail into the high tail (P(NO) ≥ 0.9 is a ≥0.9 prediction too).
        hc_folded = [(pp, o) if pp >= 0.5 else (1 - pp, 1 - o) for pp, o in hc]
        hc_n = len(hc_folded)
        hc_wins = sum(o for _, o in hc_folded)
        hc_mean = sum(pp for pp, _ in hc_folded) / hc_n if hc_n else None
        hc_lo, hc_hi = stats.wilson_interval(hc_wins, hc_n)
        hc_ok = hc_n >= p.high_conf_min_n and hc_mean is not None and hc_lo >= hc_mean - p.high_conf_margin
        # Bucket test with a Bonferroni-adjusted Wilson interval: family-wise α = 0.05 across
        # the populated buckets, so a calibrated model is not failed by multiple comparisons.
        populated = [b for b in buckets if (b["n"] or 0) >= p.bucket_min_n and b["mean_pred"] is not None]
        z_adj = stats.z_two_sided(0.05 / max(1, len(populated)))
        bad_buckets = []
        for b in populated:
            n_b = int(b["n"] or 0)
            lo_b, hi_b = stats.wilson_interval(round(float(b["empirical"] or 0) * n_b), n_b, z_adj)
            if not (lo_b - p.bucket_tolerance <= float(b["mean_pred"] or 0) <= hi_b + p.bucket_tolerance):
                bad_buckets.append(b)
        metrics = {
            "n_markets": len(sel),
            "n_windows": len(windows),
            "brier": stats.brier(probs, outs),
            "log_loss": stats.log_loss(probs, outs),
            "ece": e,
            "buckets": buckets,
            "market_baseline_brier": stats.brier(mk_probs, mk_outs),
            "market_baseline_n": len(mk_probs),
            "high_confidence": {
                "n": hc_n,
                "required_n": p.high_conf_min_n,
                "mean_pred": hc_mean,
                "empirical": (hc_wins / hc_n) if hc_n else None,
                "wilson_lo": hc_lo,
                "wilson_hi": hc_hi,
                "override_allowed": hc_ok,
            },
            "base_rate_yes": (sum(outs) / len(outs)) if outs else None,
        }
        if len(windows) < p.min_calibration_windows:
            verdict, reason = (
                "INSUFFICIENT_DATA",
                f"{len(windows)}/{p.min_calibration_windows} independent settled windows",
            )
        elif e is not None and e > p.max_ece:
            verdict, reason = "FAIL", f"ECE {e:.4f} > {p.max_ece}"
        elif bad_buckets:
            verdict, reason = "FAIL", f"{len(bad_buckets)} bucket(s) mis-calibrated beyond tolerance"
        else:
            verdict, reason = "PASS", f"ECE {e:.4f} over {len(windows)} windows"
        res = StudyResult(
            str(uuid.uuid4()),
            "CALIBRATION",
            verdict,
            reason,
            metrics,
            len(sel),
            len(rows),
            0,
            _hash_rows([(r["ticker"], r["id"], r["result"]) for r in sel]),
            "+".join(partitions),
            min((float(r["ts"]) for r in sel), default=None),
            max((float(r["ts"]) for r in sel), default=None),
            {
                "selection": f"one READY prediction per market closest to {target_s:.0f}s before close (60–600s)",
                "independence_unit": "15-minute window",
                "model_version": MODEL_VERSION,
            },
        )
        _persist(self.db, res, fee_model="n/a", slippage_model="n/a", latency_model="n/a")
        return res

    # ------------------------------------------------------- paper P&L
    def _settled_positions(self, mode: str, partitions: tuple[str, ...]) -> list[dict[str, Any]]:
        ph = ",".join("?" * len(partitions))
        return self.db.query(
            f"""SELECT s.*, m.close_ts, m.asset, vp.partition FROM settlements s
                JOIN markets m ON m.ticker = s.ticker
                JOIN validation_partitions vp ON vp.ticker = s.ticker
                WHERE s.mode = ? AND vp.partition IN ({ph})""",  # noqa: S608
            (mode, *partitions),
        )

    def pnl_study(self, mode: str = "PAPER", partitions: tuple[str, ...] = ("OOS_TEST", "LIVE_FORWARD")) -> StudyResult:
        p = self.policy
        rows = self._settled_positions(mode, partitions)
        clusters: dict[str, list[float]] = {}
        per_contract: list[float] = []
        total_pnl = Decimal(0)
        for r in rows:
            cnt = D(r["count"])
            if cnt <= 0:
                continue
            pc = float(D(r["pnl"]) / cnt)
            per_contract.append(pc)
            clusters.setdefault(str(window_id(float(r["close_ts"]))), []).append(pc)
            total_pnl += D(r["pnl"])
        point, lo, hi = stats.cluster_bootstrap_mean_ci(clusters)
        sd = None
        if len(per_contract) >= 2:
            m = sum(per_contract) / len(per_contract)
            sd = math.sqrt(sum((x - m) ** 2 for x in per_contract) / (len(per_contract) - 1))
        need = p.pnl_required_windows(sd)
        fills = self.db.one("SELECT COUNT(*) AS n FROM fills WHERE mode=?", (mode,))
        nfills = int(fills["n"]) if fills else 0
        wins = sum(1 for x in per_contract if x > 0)
        metrics = {
            "positions": len(per_contract),
            "windows": len(clusters),
            "required_windows": need,
            "mean_pnl_per_contract": point,
            "ci95_lo": lo,
            "ci95_hi": hi,
            "sd_per_contract": sd,
            "total_pnl": str(total_pnl),
            "wins": wins,
            "losses": len(per_contract) - wins,
            "win_rate": (wins / len(per_contract)) if per_contract else None,
            "fills": nfills,
            "required_fills": p.min_paper_fills,
        }
        if len(clusters) < need or nfills < p.min_paper_fills:
            verdict = "INSUFFICIENT_DATA"
            reason = f"{len(clusters)}/{need} traded windows, {nfills}/{p.min_paper_fills} fills"
        elif lo is not None and lo > 0:
            verdict, reason = "PASS", f"95% CI lower bound {lo:.4f} > 0 over {len(clusters)} windows"
        else:
            verdict, reason = "FAIL", f"95% CI lower bound {lo} ≤ 0"
        res = StudyResult(
            str(uuid.uuid4()),
            f"{mode}_PNL",
            verdict,
            reason,
            metrics,
            len(rows),
            len(rows),
            nfills,
            _hash_rows([(r["ticker"], r["outcome"], r["pnl"]) for r in rows]),
            "+".join(partitions),
            None,
            None,
            {"independence_unit": "15-minute window", "ci": "cluster bootstrap 2000x"},
        )
        _persist(self.db, res, fee_model=FEE_MODEL, slippage_model=SLIPPAGE_MODEL, latency_model=LATENCY_MODEL)
        return res

    # --------------------------------------------- execution sensitivity
    def execution_sensitivity_study(
        self, mode: str = "PAPER", partitions: tuple[str, ...] = ("OOS_TEST", "LIVE_FORWARD")
    ) -> StudyResult:
        rows = self._settled_positions(mode, partitions)
        rng = random.Random(7)
        scenarios: dict[str, dict[str, Any]] = {}
        for shift in (Decimal("0"), Decimal("0.01"), Decimal("0.02"), Decimal("0.03")):
            vals = []
            for r in rows:
                cnt = D(r["count"])
                if cnt <= 0:
                    continue
                avg = D(r["cost"]) / cnt + shift
                if avg >= 1:
                    vals.append(-1.0)
                    continue
                fee = taker_fee(cnt, avg)
                pnl = D(r["payout"]) - cnt * avg - fee
                vals.append(float(pnl / cnt))
            scenarios[f"price_worse_{shift}"] = {
                "n": len(vals),
                "mean_pnl_per_contract": (sum(vals) / len(vals)) if vals else None,
            }
        kept = [r for r in rows if rng.random() >= 0.25]
        vals = [float(D(r["pnl"]) / D(r["count"])) for r in kept if D(r["count"]) > 0]
        scenarios["missed_fills_25pct_random"] = {
            "n": len(vals),
            "mean_pnl_per_contract": (sum(vals) / len(vals)) if vals else None,
        }
        shifted = scenarios[f"price_worse_{Decimal(str(self.policy.stress_price_shift))}"]
        if not rows:
            verdict, reason = "INSUFFICIENT_DATA", "no settled positions"
        elif shifted["mean_pnl_per_contract"] is not None and shifted["mean_pnl_per_contract"] > 0:
            verdict, reason = "PASS", "edge survives 1¢ worse execution"
        else:
            verdict, reason = "FAIL", "edge does not survive 1¢ worse execution"
        res = StudyResult(
            str(uuid.uuid4()),
            "EXECUTION_SENSITIVITY",
            verdict,
            reason,
            {"scenarios": scenarios},
            len(rows),
            len(rows),
            0,
            _hash_rows([(r["ticker"], r["outcome"]) for r in rows]),
            "+".join(partitions),
            None,
            None,
            {"note": "per-contract P&L recomputed with fees at shifted price"},
        )
        _persist(self.db, res, fee_model=FEE_MODEL, slippage_model="price shifts of 0–3¢/contract", latency_model="n/a")
        return res

    # ------------------------------------------------ settlement audit
    def settlement_rule_audit(self, markets: list[dict[str, Any]]) -> StudyResult:
        """Historical check that our strike logic reproduces Kalshi's official results."""
        from predictionbot.market.models import MarketInfo

        agree = disagree = skipped = 0
        examples: list[dict[str, Any]] = []
        for raw in markets:
            try:
                info = MarketInfo.from_api(raw, series_ticker="", asset="", cf_index_id="")
            except (KeyError, ValueError):
                skipped += 1
                continue
            if info.expiration_value is None or info.result not in ("yes", "no"):
                skipped += 1
                continue
            ours = info.yes_wins(info.expiration_value)
            if ours is None:
                skipped += 1
                continue
            if ours == (info.result == "yes"):
                agree += 1
            else:
                disagree += 1
                if len(examples) < 10:
                    examples.append(
                        {
                            "ticker": info.ticker,
                            "expiration_value": str(info.expiration_value),
                            "floor": str(info.floor_strike),
                            "strike_type": info.strike_type,
                            "result": info.result,
                        }
                    )
        n = agree + disagree
        verdict = "INSUFFICIENT_DATA" if n < 50 else ("PASS" if disagree == 0 else "FAIL")
        res = StudyResult(
            str(uuid.uuid4()),
            "SETTLEMENT_RULE_AUDIT",
            verdict,
            f"{agree}/{n} agree, {skipped} skipped",
            {"agree": agree, "disagree": disagree, "skipped": skipped, "examples": examples},
            n,
            n,
            0,
            _hash_rows([(m.get("ticker"), m.get("result")) for m in markets]),
            "HISTORICAL",
            None,
            None,
            {"source": "Kalshi settled markets (expiration_value, strike, result)"},
        )
        _persist(self.db, res, fee_model="n/a", slippage_model="n/a", latency_model="n/a")
        return res

    # ------------------------------------------------------- overall
    def latest(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.db.query("SELECT * FROM validation_studies ORDER BY created_ts DESC LIMIT 200"):
            if r["study_type"] not in out:
                r["metrics"] = json.loads(r["metrics"]) if r["metrics"] else {}
                r["assumptions"] = json.loads(r["assumptions"]) if r["assumptions"] else {}
                out[r["study_type"]] = r
        return out

    def run_all(self) -> dict[str, Any]:
        cal = self.calibration_study()
        pnl = self.pnl_study("PAPER")
        stress = self.execution_sensitivity_study("PAPER")
        return self.summarize({"CALIBRATION": cal, "PAPER_PNL": pnl, "EXECUTION_SENSITIVITY": stress})

    def summarize(self, results: dict[str, StudyResult] | None = None) -> dict[str, Any]:
        latest = self.latest()
        verdicts = {
            k: latest.get(k, {}).get("verdict", "NOT_RUN")
            for k in ("CALIBRATION", "PAPER_PNL", "EXECUTION_SENSITIVITY")
        }
        if all(v == "PASS" for v in verdicts.values()):
            overall = "PASS"
        elif any(v == "FAIL" for v in verdicts.values()):
            overall = "FAIL"
        else:
            overall = "INSUFFICIENT_DATA"
        hc = latest.get("CALIBRATION", {}).get("metrics", {}).get("high_confidence", {})
        return {
            "overall": overall,
            "live_capital_justified": overall == "PASS",
            "verdicts": verdicts,
            "high_confidence_override_allowed": bool(hc.get("override_allowed")),
            "policy": self.policy.public(),
            "studies": {
                k: {
                    kk: v.get(kk)
                    for kk in (
                        "study_id",
                        "created_ts",
                        "verdict",
                        "reason",
                        "metrics",
                        "unique_markets",
                        "observations",
                        "fills",
                        "dataset_hash",
                        "partition",
                        "model_version",
                        "strategy_version",
                    )
                }
                for k, v in latest.items()
            },
        }
