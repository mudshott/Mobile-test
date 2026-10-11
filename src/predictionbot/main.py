"""Application entry point (also the PyInstaller entry).

Opening the application NEVER starts trading: the engine starts with the bot
STOPPED in PAPER mode, every time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path

from predictionbot.buildinfo import build_info
from predictionbot.config import ConfigError, Settings, ensure_data_dir, load_settings


class RedactFilter(logging.Filter):
    """Defense in depth: scrub anything that looks like key material from logs."""

    _patterns = (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        re.compile(r"(KALSHI-ACCESS-SIGNATURE['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9+/=]{20,}"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for p in self._patterns:
            msg = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", msg)
        record.msg, record.args = msg, ()
        return True


def setup_logging(s: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        s.log_dir / "predictionbot.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(RedactFilter())
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(RedactFilter())
        root.addHandler(sh)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="PredictionCryptoBot")
    ap.add_argument("--data-dir", type=Path, default=None, help="runtime data/config directory")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--env", choices=["production", "demo", "mock"], default=None)
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="offline checks of order construction + firewall")
    args = ap.parse_args(argv)
    if args.version:
        print(build_info())
        return 0
    if args.self_test:
        import json

        from predictionbot.selftest import run_self_test

        report = run_self_test()
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    if args.env:
        os.environ["PCB_ENV"] = args.env
    if args.port:
        os.environ["PCB_HTTP_PORT"] = str(args.port)
    if args.no_browser:
        os.environ["PCB_NO_BROWSER"] = "1"
    try:
        settings = load_settings(args.data_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    ensure_data_dir(settings)
    setup_logging(settings)
    log = logging.getLogger("predictionbot")
    log.info("starting %s", build_info())

    import uvicorn

    from predictionbot.api.server import create_app
    from predictionbot.app.engine import Engine

    engine = Engine(settings)
    app = create_app(engine)
    config = uvicorn.Config(
        app, host=settings.http_host, port=settings.http_port, log_config=None, access_log=False, lifespan="on"
    )
    server = uvicorn.Server(config)
    url = f"http://{settings.http_host}:{settings.http_port}/"
    if settings.open_browser:

        def _open() -> None:
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.1)

        threading.Thread(target=_open, daemon=True).start()
    print(f"PredictionCryptoBot dashboard: {url}  (bot starts STOPPED)")
    asyncio.run(server.serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
