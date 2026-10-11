"""Automated verification of the PACKAGED executable against the local mock exchange.

    python scripts/verify_packaged.py --exe dist/PredictionCryptoBot.exe --launcher wine --report docs/x.md
    python scripts/verify_packaged.py --exe dist/linux/PredictionCryptoBot

The executable is launched exactly as a user would launch it (mock environment
via env vars, isolated data dir). Every check records evidence. Nothing here
can reach the real exchange: the mock environment is localhost-only and LIVE is
disabled in it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class Verifier:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results: list[dict[str, Any]] = []
        self.port = args.port
        self.mock_port = args.mock_port
        self.data = Path(args.data_dir or tempfile.mkdtemp(prefix="pcb-verify-"))
        self.data.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen[bytes] | None = None
        self.mock: subprocess.Popen[bytes] | None = None
        self.log_lines: list[str] = []

    # ------------------------------------------------------------- helpers
    def check(self, name: str, ok: bool, evidence: Any) -> bool:
        self.results.append({"check": name, "result": "PASS" if ok else "FAIL", "evidence": evidence})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(evidence)[:300]}", flush=True)
        return ok

    def get(self, path: str, base: str | None = None) -> Any:
        url = (base or f"http://127.0.0.1:{self.port}") + path
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - localhost only
            return json.loads(r.read())

    def post(self, path: str, body: dict[str, Any] | None = None, base: str | None = None, ui: bool = True) -> Any:
        url = (base or f"http://127.0.0.1:{self.port}") + path
        req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST")  # noqa: S310
        req.add_header("Content-Type", "application/json")
        if ui:
            req.add_header("X-PCB-Client", "dashboard")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"http_status": e.code, **json.loads(e.read() or b"{}")}

    def wait(self, fn: Any, timeout: float, step: float = 1.0) -> Any:
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                last = fn()
                if last:
                    return last
            except Exception as exc:
                last = exc
            time.sleep(step)
        return None

    def comp(self, name: str) -> str:
        return str(self.get("/api/health")["components"][name]["status"])

    def mock_base(self) -> str:
        return f"http://127.0.0.1:{self.mock_port}"

    # ------------------------------------------------------------ processes
    def start_mock(self) -> None:
        try:
            self.get("/mock/state", self.mock_base())
            return
        except Exception:  # noqa: S110
            pass
        self.mock = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "predictionbot.sim.mock_exchange", "--port", str(self.mock_port)],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.wait(lambda: self.get("/mock/state", self.mock_base()), 20, 0.5)

    def _to_native(self, p: Path) -> str:
        return f"Z:{p}" if self.args.launcher and "wine" in self.args.launcher else str(p)

    def launch(self) -> None:
        env = {
            **os.environ,
            "PCB_ENV": "mock",
            "PCB_MOCK_REST": f"http://127.0.0.1:{self.mock_port}/trade-api/v2",
            "PCB_MOCK_WS": f"ws://127.0.0.1:{self.mock_port}/trade-api/ws/v2",
            "KALSHI_API_KEY_ID": "mock-key-0001",
            "KALSHI_PRIVATE_KEY_PATH": self._to_native(Path(self.args.key)),
            "PCB_DATA_DIR": self._to_native(self.data),
            "PCB_NO_BROWSER": "1",
            "PCB_HTTP_PORT": str(self.port),
            "WINEDEBUG": "-all",
        }
        cmd = [*shlex.split(self.args.launcher or ""), str(Path(self.args.exe).resolve())]
        self.proc = subprocess.Popen(  # noqa: S603
            cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.data
        )
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log_lines.append(line.decode("utf-8", "replace").rstrip())

    def kill(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self.args.launcher and "wine" in self.args.launcher:
            subprocess.call(["wineserver", "-k"], env={**os.environ})  # noqa: S607
        time.sleep(2)

    # ----------------------------------------------------------------- flow
    def run(self) -> int:
        a = self.args
        # 0. offline self-test inside the shipped binary (order path + firewall, no network)
        st = subprocess.run(  # noqa: S603
            [*shlex.split(a.launcher or ""), str(Path(a.exe).resolve()), "--self-test"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            env={**os.environ, "WINEDEBUG": "-all"},
        )
        out = st.stdout.decode("utf-8", "replace")
        try:
            rep = json.loads(out[out.index("{") :])
        except ValueError:
            rep = {"passed": False, "raw": out[-500:]}
        self.check(
            "production V2 order request constructed (no order sent)",
            bool(rep.get("passed")),
            rep.get("checks", {}).get("order_body_buy_yes"),
        )
        fw = rep.get("checks", {}).get("firewall", {})
        self.check(
            "funding/deposit/withdraw/transfer/API-key blocked before any HTTP request",
            bool(rep.get("passed")) and fw.get("http_requests_made") == 0,
            fw,
        )

        self.start_mock()
        self.launch()
        up = self.wait(lambda: self.get("/api/build"), 120, 1)
        self.check("executable launches and API responds", bool(up), up)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: S607
        self.check(
            "build matches source (git commit)",
            bool(up) and up.get("git_commit") == head and up.get("source") == "packaged",
            {"exe": up and up.get("git_commit"), "head": head, "dirty": up and up.get("git_dirty")},
        )
        h = self.get("/api/health")
        self.check("starts STOPPED", h["bot_state"] == "STOPPED", h["bot_state"])
        for comp, t in (
            ("CREDENTIALS", 30),
            ("AUTHENTICATION", 60),
            ("RECONCILIATION", 60),
            ("REST", 60),
            ("WEBSOCKET", 60),
            ("MARKET_DISCOVERY", 60),
            ("ORDER_BOOKS", 60),
            ("CF_BENCHMARKS", 60),
        ):
            ok = self.wait(lambda c=comp: self.comp(c) == "OK", t)
            self.check(f"{comp} healthy", bool(ok), self.get("/api/health")["components"][comp])
        s = self.get("/api/state")
        self.check("open 15-minute crypto markets discovered", len(s["markets"]["active"]) >= 1, s["markets"]["active"])
        time.sleep(5)
        ages = self.get("/api/state")["ages"]
        self.check(
            "books + CF remain fresh", (ages["orderbook_max_s"] or 99) < 30 and (ages["cf_max_s"] or 99) < 5, ages
        )
        # reconnect/resubscribe
        before = self.get("/api/state")["connection"]["ws"]["reconnects"]
        self.post("/mock/fault", {"drop_ws": True}, self.mock_base(), ui=False)
        ok = self.wait(
            lambda: (
                self.get("/api/state")["connection"]["ws"]["reconnects"] > before and self.comp("ORDER_BOOKS") == "OK"
            ),
            60,
        )
        evs = [e["code"] for e in self.get("/api/state")["connection"]["ws_events"]]
        self.check("WebSocket reconnect + resubscribe", bool(ok) and "WS_RESUBSCRIBED" in evs, evs[-6:])
        # CF outage + recovery
        self.post("/mock/fault", {"cf_pause_s": 10}, self.mock_base(), ui=False)
        stale = self.wait(lambda: self.comp("CF_BENCHMARKS") == "FAIL", 20)
        reasons = [w["reason"] for w in self.get("/api/state")["watch"]]
        rec = self.wait(lambda: self.comp("CF_BENCHMARKS") == "OK", 30)
        self.check("CF outage → no new risk → recovery", bool(stale) and bool(rec), reasons)
        # model warm-up
        ready = self.wait(lambda: self.comp("MODEL") == "OK", a.warmup_timeout, 5)
        w = self.get("/api/state")["watch"]
        self.check(
            "model warms up; probability available",
            bool(ready) and all(x["raw_prob_yes"] is not None for x in w),
            [(x["ticker"], x["model_state"], x["raw_prob_yes"]) for x in w],
        )
        self.check(
            "strategy computes adjusted EV",
            any((x["evaluation"].get("yes") or {}).get("adjusted_ev") is not None for x in w),
            [(x["ticker"], x["reason"]) for x in w],
        )
        # PAPER
        r = self.post("/api/bot/start")
        self.check("PAPER starts", r.get("ok") is True, r)
        time.sleep(3)
        self.check(
            "PAPER loop keeps running",
            self.get("/api/state")["bot"]["state"] == "RUNNING",
            self.get("/api/health")["readiness"]["phase"],
        )
        target = self.wait(
            lambda: next(
                (
                    x
                    for x in self.get("/api/state")["watch"]
                    if x["model_state"] == "READY" and x["seconds_to_close"] > 120
                ),
                None,
            ),
            900,
            5,
        )
        if target:
            shift = -0.25 if (target["raw_prob_yes"] or 0) >= 0.5 else 0.25
            self.post("/mock/scenario", {"asset": target["asset"], "shift": shift}, self.mock_base(), ui=False)
        filled = self.wait(lambda: self.get("/api/state")["counters"]["session"].get("fills", 0) > 0, 90)
        st = self.get("/api/state")
        self.check(
            "deterministic qualifying opportunity reaches PAPER fill",
            bool(filled),
            {"orders": st["orders"][:2], "session": st["counters"]["session"]},
        )
        self.check(
            "fills + positions persisted",
            st["counters"]["totals"]["fills"] > 0 and st["counters"]["totals"]["open_positions"] > 0,
            st["counters"]["totals"],
        )
        for asset in ("BTC", "ETH", "SOL"):
            self.post("/mock/scenario", {"asset": asset, "shift": 0}, self.mock_base(), ui=False)
        if a.wait_settlement:
            settled = self.wait(lambda: self.get("/api/state")["counters"]["session"].get("settled", 0) > 0, 1200, 10)
            st = self.get("/api/state")
            self.check(
                "market rollover + settlement processed",
                bool(settled) and st["markets"]["rollovers"] >= 1,
                {
                    "session": st["counters"]["session"],
                    "rollovers": st["markets"]["rollovers"],
                    "events": [e["detail"] for e in st["events"] if e["code"] == "MARKET_SETTLED"][:3],
                },
            )
        r = self.post("/api/bot/stop")
        n_orders = self.get("/api/state")["counters"]["totals"]["orders"]
        time.sleep(4)
        st = self.get("/api/state")
        self.check(
            "STOP works (no new orders, positions kept)",
            r.get("ok") and st["bot"]["state"] == "STOPPED" and st["counters"]["totals"]["orders"] == n_orders,
            {"stop": r, "bot": st["bot"]["state"]},
        )
        guard = self.post("/api/bot/start", ui=False)
        self.check("control API refuses requests without dashboard header", guard.get("http_status") == 403, guard)
        live = self.post("/api/bot/mode", {"mode": "LIVE"})
        self.post("/api/bot/confirm_live", {"text": "LIVE"})
        lr = self.post("/api/bot/start")
        self.check("LIVE blocked in mock env / without validation", lr.get("http_status") == 409, lr.get("error"))
        self.post("/api/bot/mode", {"mode": "PAPER"})
        _ = live
        # restart
        self.kill()
        self.launch()
        up2 = self.wait(lambda: self.get("/api/health"), 120, 1)
        self.check(
            "restart returns STOPPED",
            bool(up2) and up2["bot_state"] == "STOPPED" and up2["mode"] == "PAPER",
            up2 and {"bot": up2["bot_state"], "mode": up2["mode"]},
        )
        st = self.get("/api/state")
        self.check("history survives restart", st["counters"]["totals"]["fills"] > 0, st["counters"]["totals"])
        self.kill()
        if self.mock is not None:
            self.mock.terminate()
        return self.report()

    def report(self) -> int:
        passed = sum(r["result"] == "PASS" for r in self.results)
        lines = [
            f"# Packaged executable verification — {self.args.label}",
            "",
            f"- Executable: `{self.args.exe}` (launcher: `{self.args.launcher or 'native'}`)",
            f"- Date (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
            "- Environment: **mock** exchange simulator on localhost "
            "(real Kalshi hosts are not reachable from the build machine)",
            f"- Result: **{passed}/{len(self.results)} checks passed**",
            "",
            "| # | Check | Result | Evidence |",
            "|---|---|---|---|",
        ]
        for i, r in enumerate(self.results, 1):
            ev = json.dumps(r["evidence"], default=str)
            ev = (ev[:400] + "…") if len(ev) > 400 else ev
            lines.append(f"| {i} | {r['check']} | {r['result']} | `{ev.replace('|', '/')}` |")
        if self.args.report:
            Path(self.args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines[:6]))
        return 0 if passed == len(self.results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--launcher", default="")
    ap.add_argument("--key", required=True, help="throwaway PEM key for the mock handshake")
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--mock-port", type=int, default=8799)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--warmup-timeout", type=float, default=420)
    ap.add_argument("--wait-settlement", action="store_true")
    ap.add_argument("--report", default=None)
    ap.add_argument("--label", default="")
    return Verifier(ap.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
