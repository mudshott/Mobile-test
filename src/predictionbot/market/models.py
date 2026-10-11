"""Normalized market metadata derived from the Kalshi `Market` object."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from predictionbot.fixedpoint import ONE, ZERO, D, D_or_none, ceil_to, floor_to

DEFAULT_TICK = Decimal("0.01")


@dataclass(frozen=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal


def parse_ts(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


@dataclass
class MarketInfo:
    ticker: str
    event_ticker: str
    series_ticker: str
    asset: str
    cf_index_id: str
    status: str
    open_ts: float
    close_ts: float
    strike_type: str | None
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    price_ranges: tuple[PriceRange, ...]
    market_type: str = "binary"
    rules_primary: str = ""
    rules_secondary: str = ""
    result: str = ""
    expiration_value: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_type: str = "quadratic"
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(
        cls,
        m: dict[str, Any],
        *,
        series_ticker: str,
        asset: str,
        cf_index_id: str,
        fee_multiplier: Decimal = Decimal("1"),
        fee_type: str = "quadratic",
    ) -> MarketInfo:
        ranges = []
        for r in m.get("price_ranges") or []:
            try:
                ranges.append(PriceRange(D(r["start"]), D(r["end"]), D(r["step"])))
            except (KeyError, ValueError):
                continue
        open_ts = parse_ts(m.get("open_time"))
        close_ts = parse_ts(m.get("close_time"))
        if open_ts is None or close_ts is None:
            raise ValueError(f"market {m.get('ticker')} missing open/close time")
        return cls(
            ticker=str(m["ticker"]),
            event_ticker=str(m.get("event_ticker", "")),
            series_ticker=series_ticker,
            asset=asset,
            cf_index_id=cf_index_id,
            status=str(m.get("status", "")),
            open_ts=open_ts,
            close_ts=close_ts,
            strike_type=m.get("strike_type"),
            floor_strike=D_or_none(m.get("floor_strike")),
            cap_strike=D_or_none(m.get("cap_strike")),
            price_ranges=tuple(ranges),
            market_type=str(m.get("market_type", "binary")),
            rules_primary=str(m.get("rules_primary", "")),
            rules_secondary=str(m.get("rules_secondary", "")),
            result=str(m.get("result", "") or ""),
            expiration_value=D_or_none(m.get("expiration_value")),
            fee_multiplier=fee_multiplier,
            fee_type=fee_type,
            raw=m,
        )

    # ------------------------------------------------------------- timing
    def seconds_to_close(self, now: float) -> float:
        return self.close_ts - now

    @property
    def duration_s(self) -> float:
        return self.close_ts - self.open_ts

    def is_open_now(self, now: float) -> bool:
        return self.status in ("active", "open") and self.open_ts <= now < self.close_ts

    # -------------------------------------------------------------- ticks
    def _range_for(self, p: Decimal) -> PriceRange | None:
        for r in self.price_ranges:
            if r.start <= p <= r.end:
                return r
        return None

    def tick_at(self, p: Decimal) -> Decimal:
        r = self._range_for(p)
        return r.step if r else DEFAULT_TICK

    def is_valid_price(self, p: Decimal) -> bool:
        if p <= ZERO or p >= ONE:
            return False
        if not self.price_ranges:
            return (p / DEFAULT_TICK) == (p / DEFAULT_TICK).to_integral_value()
        r = self._range_for(p)
        if r is None:
            return False
        steps = (p - r.start) / r.step
        return steps == steps.to_integral_value()

    def round_price(self, p: Decimal, *, up: bool) -> Decimal | None:
        """Round to a valid tick (up = more expensive for a buyer)."""
        tick = self.tick_at(p)
        r = self._range_for(p)
        base = r.start if r else ZERO
        q = base + (ceil_to(p - base, tick) if up else floor_to(p - base, tick))
        if self.is_valid_price(q):
            return q
        return None

    # --------------------------------------------------------- settlement
    def yes_wins(self, settlement_value: Decimal) -> bool | None:
        """Apply the market's strike definition to a settlement value."""
        st = self.strike_type
        lo, hi = self.floor_strike, self.cap_strike
        if st == "greater" and lo is not None:
            return settlement_value > lo
        if st == "greater_or_equal" and lo is not None:
            return settlement_value >= lo
        if st == "less" and hi is not None:
            return settlement_value < hi
        if st == "less_or_equal" and hi is not None:
            return settlement_value <= hi
        if st == "between" and lo is not None and hi is not None:
            return lo <= settlement_value <= hi
        return None

    def strike_supported(self) -> bool:
        st = self.strike_type
        if st in ("greater", "greater_or_equal"):
            return self.floor_strike is not None
        if st in ("less", "less_or_equal"):
            return self.cap_strike is not None
        if st == "between":
            return self.floor_strike is not None and self.cap_strike is not None
        return False

    def public(self, now: float) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "series": self.series_ticker,
            "asset": self.asset,
            "cf_index": self.cf_index_id,
            "status": self.status,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "seconds_to_close": round(self.seconds_to_close(now), 1),
            "strike_type": self.strike_type,
            "floor_strike": str(self.floor_strike) if self.floor_strike is not None else None,
            "cap_strike": str(self.cap_strike) if self.cap_strike is not None else None,
            "result": self.result,
        }
