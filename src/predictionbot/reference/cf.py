"""CF Benchmarks settlement-reference data (Kalshi `cfbenchmarks_value` channel).

Kalshi's crypto contracts settle on the simple average of the 60 one-second
CF Benchmarks Real-Time Index values in the final minute before close. This
feed is the ONLY source used for settlement geometry. Other price sources may
be added as model features but never substitute for it.

Message (per official AsyncAPI spec)::

    {"type": "cfbenchmarks_value", "sid": 1, "seq": 42, "msg": {
        "index_id": "BRTI", "received_at": 1710000000123,
        "data": "{\\"type\\":\\"value\\",\\"id\\":\\"BRTI\\",\\"time\\":1710000000123,\\"value\\":\\"68000.12\\"}",
        "avg_60s_data": {...},
        "last_60s_windowed_average_15min": {"value": "...", "window_size": 14, ...}}}

`last_60s_windowed_average_15min` is only present in the final minute before
a quarter-hour close; its `window_size` counts ticks accumulated so far.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

QUARTER_MS = 15 * 60 * 1000


@dataclass
class FinalWindow:
    quarter_close_ms: int
    average: float
    window_size: int
    updated_ts: float


@dataclass
class IndexState:
    index_id: str
    ticks: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=4 * 3600))
    last_value: float | None = None
    last_source_ts_ms: int | None = None
    last_kalshi_received_ms: int | None = None
    last_local_recv_ts: float | None = None
    count: int = 0
    errors: int = 0
    out_of_order: int = 0
    last_error: str | None = None
    final_windows: dict[int, FinalWindow] = field(default_factory=dict)

    def age(self, now: float | None = None) -> float | None:
        if self.last_local_recv_ts is None:
            return None
        return (time.time() if now is None else now) - self.last_local_recv_ts


class CFReferenceFeed:
    def __init__(self, stale_after_s: float = 5.0) -> None:
        self.stale_after_s = stale_after_s
        self.indices: dict[str, IndexState] = {}
        self.expected: set[str] = set()
        self.subscribed = False

    def expect(self, index_ids: set[str]) -> None:
        self.expected = set(index_ids)
        for i in index_ids:
            self.indices.setdefault(i, IndexState(i))

    def on_message(self, msg: dict[str, Any], recv_ts: float | None = None) -> str | None:
        """Ingest one cfbenchmarks_value message. Returns index_id or None if rejected."""
        body = msg.get("msg")
        if not isinstance(body, dict):
            return None
        index_id = body.get("index_id")
        if not isinstance(index_id, str):
            return None
        st = self.indices.setdefault(index_id, IndexState(index_id))
        recv = time.time() if recv_ts is None else recv_ts
        try:
            raw = body.get("data")
            frame = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(frame, dict):
                raise ValueError("data frame is not an object")
            value = float(frame["value"])
            raw_ts = frame.get("time") or body.get("received_at")
            if raw_ts is None:
                raise ValueError("frame has no timestamp")
            src_ts = int(raw_ts)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid index value {value}")
        except (KeyError, TypeError, ValueError) as exc:
            st.errors += 1
            st.last_error = f"malformed CF frame: {exc}"
            return None
        if st.last_source_ts_ms is not None and src_ts <= st.last_source_ts_ms:
            st.out_of_order += 1
            return index_id
        st.ticks.append((src_ts, value))
        st.last_value = value
        st.last_source_ts_ms = src_ts
        ra = body.get("received_at")
        st.last_kalshi_received_ms = int(ra) if isinstance(ra, (int, float)) else None
        st.last_local_recv_ts = recv
        st.count += 1
        fw = body.get("last_60s_windowed_average_15min")
        if isinstance(fw, dict):
            try:
                close_ms = ((src_ts - 1) // QUARTER_MS + 1) * QUARTER_MS
                st.final_windows[close_ms] = FinalWindow(close_ms, float(fw["value"]), int(fw["window_size"]), recv)
                for k in [k for k in st.final_windows if k < close_ms - 6 * QUARTER_MS]:
                    del st.final_windows[k]
            except (KeyError, TypeError, ValueError):
                st.errors += 1
                st.last_error = "malformed final-window average"
        return index_id

    # --------------------------------------------------------------- queries
    def is_fresh(self, index_id: str, now: float | None = None) -> bool:
        st = self.indices.get(index_id)
        if st is None:
            return False
        age = st.age(now)
        return age is not None and age <= self.stale_after_s

    def latest(self, index_id: str) -> tuple[int, float] | None:
        st = self.indices.get(index_id)
        if st is None or st.last_value is None or st.last_source_ts_ms is None:
            return None
        return st.last_source_ts_ms, st.last_value

    def history(self, index_id: str, since_ms: int) -> list[tuple[int, float]]:
        st = self.indices.get(index_id)
        if st is None:
            return []
        return [t for t in st.ticks if t[0] >= since_ms]

    def final_window(self, index_id: str, close_ms: int) -> FinalWindow | None:
        st = self.indices.get(index_id)
        if st is None:
            return None
        fw = st.final_windows.get(close_ms)
        if fw is not None:
            return fw
        # Fallback: compute from our own 1 s ticks in (close-60s, close].
        ticks = [v for t, v in st.ticks if close_ms - 60_000 < t <= close_ms]
        if not ticks:
            return None
        return FinalWindow(close_ms, sum(ticks) / len(ticks), len(ticks), time.time())

    def public(self, now: float | None = None) -> dict[str, Any]:
        out = {}
        for i, st in self.indices.items():
            age = st.age(now)
            out[i] = {
                "expected": i in self.expected,
                "value": st.last_value,
                "source_ts_ms": st.last_source_ts_ms,
                "kalshi_received_ms": st.last_kalshi_received_ms,
                "age_s": round(age, 2) if age is not None else None,
                "fresh": self.is_fresh(i, now),
                "observations": st.count,
                "buffered": len(st.ticks),
                "errors": st.errors,
                "out_of_order": st.out_of_order,
                "last_error": st.last_error,
            }
        return out
