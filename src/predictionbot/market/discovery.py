"""Dynamic discovery + rotation of CURRENT 15-minute crypto markets.

No contract tickers are hard-coded: only a supported-series allowlist
(series → asset → CF Benchmarks index). Every market is verified before it
can be evaluated:

* series exists and lists CF Benchmarks as a settlement source (or the market
  rules name it),
* market is binary, currently open (open_time ≤ now < close_time, status active),
* duration is 15 minutes (±1 min),
* strike definition is supported and present.

Lifecycle: ACTIVE (evaluate/enter) → CLOSED (no new entries; await result)
→ SETTLED (result recorded) → retired from memory.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from predictionbot.config import SeriesSpec
from predictionbot.fixedpoint import D_or_none
from predictionbot.kalshi.rest import KalshiError, KalshiHTTPError, KalshiRest
from predictionbot.market.models import MarketInfo

log = logging.getLogger(__name__)

FIFTEEN_MIN = 900.0
DURATION_TOLERANCE = 60.0
SERIES_REFRESH_S = 3600.0
SERIES_RETRY_UNAVAILABLE_S = 600.0
FINAL_STATUSES = ("settled", "finalized")


@dataclass
class SeriesState:
    spec: SeriesSpec
    available: bool = False
    verified: bool = False
    notes: list[str] = field(default_factory=list)
    fee_type: str = "quadratic"
    fee_multiplier: Decimal = Decimal("1")
    checked_ts: float = 0.0
    settlement_sources: list[str] = field(default_factory=list)


@dataclass
class TrackedMarket:
    info: MarketInfo
    phase: str  # ACTIVE | CLOSED | SETTLED
    verified: bool
    verify_notes: list[str]
    first_seen_ts: float
    last_refresh_ts: float


def verify_market(m: MarketInfo, series: SeriesState, now: float) -> list[str]:
    problems: list[str] = []
    if m.market_type != "binary":
        problems.append(f"market_type={m.market_type}")
    if abs(m.duration_s - FIFTEEN_MIN) > DURATION_TOLERANCE:
        problems.append(f"duration={m.duration_s:.0f}s (expected 900s)")
    if not (m.open_ts <= now < m.close_ts):
        problems.append("not currently open by time")
    if m.status not in ("active", "open"):
        problems.append(f"status={m.status}")
    if not m.strike_supported():
        problems.append(f"strike unsupported/missing ({m.strike_type}, floor={m.floor_strike})")
    rules = (m.rules_primary + " " + m.rules_secondary).lower()
    keywords = [k.lower() for k in series.spec.rule_keywords]
    in_rules = any(k in rules for k in keywords)
    in_sources = any(any(k in s.lower() for k in keywords) for s in series.settlement_sources)
    if not (in_rules or in_sources):
        problems.append("settlement source does not reference CF Benchmarks")
    if not series.verified:
        problems.append("series not verified")
    return problems


class MarketRegistry:
    def __init__(self, specs: tuple[SeriesSpec, ...]) -> None:
        self.series: dict[str, SeriesState] = {s.series_ticker: SeriesState(s) for s in specs}
        self.markets: dict[str, TrackedMarket] = {}
        self.last_discovery_ts: float | None = None
        self.last_discovery_ok: bool = False
        self.last_error: str | None = None
        self.rollovers = 0
        self.last_close_ts: float | None = None

    # -------------------------------------------------------------- views
    def active(self) -> list[TrackedMarket]:
        return [t for t in self.markets.values() if t.phase == "ACTIVE"]

    def active_verified(self) -> list[TrackedMarket]:
        return [t for t in self.active() if t.verified]

    def closed(self) -> list[TrackedMarket]:
        return [t for t in self.markets.values() if t.phase == "CLOSED"]

    def get(self, ticker: str) -> MarketInfo | None:
        t = self.markets.get(ticker)
        return t.info if t else None

    def expected_indices(self) -> set[str]:
        return {s.spec.cf_index_id for s in self.series.values() if s.available}

    # ------------------------------------------------------------- refresh
    async def _refresh_series(self, rest: KalshiRest, st: SeriesState, now: float) -> None:
        try:
            data = await rest.get_series(st.spec.series_ticker)
        except KalshiHTTPError as exc:
            st.available = False
            st.verified = False
            st.notes = [f"series lookup failed: HTTP {exc.status}"]
            st.checked_ts = now
            return
        st.checked_ts = now
        if not data:
            st.available, st.verified, st.notes = False, False, ["series not found"]
            return
        st.available = True
        st.settlement_sources = [
            str(s.get("name", "")) + " " + str(s.get("url", "")) for s in data.get("settlement_sources") or []
        ]
        st.fee_type = str(data.get("fee_type") or "quadratic")
        mult = D_or_none(data.get("fee_multiplier"))
        st.fee_multiplier = mult if mult is not None else Decimal("1")
        notes = []
        cats = " ".join([str(data.get("category", ""))] + [str(c) for c in data.get("categories") or []]).lower()
        if "crypto" not in cats:
            notes.append(f"category not crypto: {cats!r}")
        if not st.fee_type.startswith("quadratic"):
            notes.append(f"unsupported fee_type {st.fee_type}")
        st.notes = notes
        st.verified = not notes

    async def refresh(self, rest: KalshiRest, now: float | None = None) -> tuple[list[str], list[str]]:
        """Discover open markets. Returns (newly_active_tickers, newly_closed_tickers)."""
        now = time.time() if now is None else now
        new: list[str] = []
        closed: list[str] = []
        errors: list[str] = []
        for st in self.series.values():
            retry = SERIES_REFRESH_S if st.available else SERIES_RETRY_UNAVAILABLE_S
            if now - st.checked_ts > retry:
                try:
                    await self._refresh_series(rest, st, now)
                except KalshiError as exc:
                    errors.append(f"{st.spec.series_ticker}: {exc}")
                    continue
            if not st.available:
                continue
            try:
                raw_markets = await rest.get_markets(series_ticker=st.spec.series_ticker, status="open")
            except KalshiError as exc:
                errors.append(f"{st.spec.series_ticker} markets: {exc}")
                continue
            for raw in raw_markets:
                ticker = raw.get("ticker")
                if not ticker:
                    continue
                try:
                    info = MarketInfo.from_api(
                        raw,
                        series_ticker=st.spec.series_ticker,
                        asset=st.spec.asset,
                        cf_index_id=st.spec.cf_index_id,
                        fee_multiplier=st.fee_multiplier,
                        fee_type=st.fee_type,
                    )
                except (KeyError, ValueError) as exc:
                    errors.append(f"{ticker}: malformed market ({exc})")
                    continue
                if not (info.open_ts <= now < info.close_ts):
                    continue  # never treat unopened / expired markets as active
                tm = self.markets.get(ticker)
                problems = verify_market(info, st, now)
                if tm is None:
                    self.markets[ticker] = TrackedMarket(info, "ACTIVE", not problems, problems, now, now)
                    new.append(ticker)
                elif tm.phase == "ACTIVE":
                    tm.info = info
                    tm.verified = not problems
                    tm.verify_notes = problems
                    tm.last_refresh_ts = now
        closed.extend(self.close_expired(now))
        if new and self.last_close_ts is not None and now - self.last_close_ts < 300:
            self.rollovers += 1
            self.last_close_ts = None
        self.last_discovery_ts = now
        self.last_discovery_ok = not errors
        self.last_error = "; ".join(errors)[:500] if errors else None
        return new, closed

    def close_expired(self, now: float) -> list[str]:
        out = []
        for ticker, tm in self.markets.items():
            if tm.phase == "ACTIVE" and now >= tm.info.close_ts:
                tm.phase = "CLOSED"
                out.append(ticker)
        if out:
            self.last_close_ts = now
        return out

    async def refresh_closed(self, rest: KalshiRest) -> list[MarketInfo]:
        """Poll closed markets for their official result. Returns newly settled markets."""
        settled: list[MarketInfo] = []
        for tm in self.closed():
            try:
                raw = await rest.get_market(tm.info.ticker)
            except KalshiError as exc:
                log.debug("closed market refresh %s failed: %s", tm.info.ticker, exc)
                continue
            if not raw:
                continue
            tm.info.status = str(raw.get("status", tm.info.status))
            tm.info.result = str(raw.get("result", "") or "")
            ev = D_or_none(raw.get("expiration_value"))
            if ev is not None:
                tm.info.expiration_value = ev
            tm.info.raw = raw
            if tm.info.status in FINAL_STATUSES and tm.info.result in ("yes", "no"):
                tm.phase = "SETTLED"
                settled.append(tm.info)
        return settled

    def apply_lifecycle(self, msg: dict[str, Any]) -> str | None:
        """market_lifecycle_v2: update tracked markets; returns ticker if relevant."""
        ticker = msg.get("market_ticker")
        tm = self.markets.get(str(ticker)) if ticker else None
        if tm is None:
            return None
        et = msg.get("event_type")
        if et == "determined" and msg.get("result") in ("yes", "no"):
            tm.info.result = str(msg["result"])
        if et in ("deactivated",):
            tm.info.status = "inactive"
        if et == "activated":
            tm.info.status = "active"
        if et == "close_date_updated" and isinstance(msg.get("close_ts"), (int, float)):
            tm.info.close_ts = float(msg["close_ts"])
        if et == "settled":
            tm.info.status = "settled"
        return tm.info.ticker

    def retire(self, now: float, keep_s: float = 3600.0) -> None:
        for ticker in [
            t for t, tm in self.markets.items() if tm.phase == "SETTLED" and now - tm.info.close_ts > keep_s
        ]:
            del self.markets[ticker]

    def public(self, now: float) -> dict[str, Any]:
        return {
            "series": {
                k: {
                    "asset": s.spec.asset,
                    "cf_index": s.spec.cf_index_id,
                    "available": s.available,
                    "verified": s.verified,
                    "notes": s.notes,
                    "fee_type": s.fee_type,
                    "fee_multiplier": str(s.fee_multiplier),
                }
                for k, s in self.series.items()
            },
            "active": [t.info.ticker for t in self.active()],
            "closed_awaiting_result": [t.info.ticker for t in self.closed()],
            "last_discovery_age_s": round(now - self.last_discovery_ts, 1) if self.last_discovery_ts else None,
            "last_discovery_ok": self.last_discovery_ok,
            "last_error": self.last_error,
            "rollovers": self.rollovers,
        }
