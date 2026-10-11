"""Runtime configuration.

Sources (later wins): built-in defaults → `<data_dir>/config.toml` → environment.
Secrets: only the API key *ID* and the *path* to the private key are held here;
the PEM itself is read once by the signer and never logged or serialized.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Environment = Literal["production", "demo", "mock"]

PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
REST_PATH_PREFIX = "/trade-api/v2"
WS_PATH = "/trade-api/ws/v2"


@dataclass(frozen=True)
class SeriesSpec:
    """Supported-series allowlist entry: Kalshi series → asset → settlement index."""

    series_ticker: str
    asset: str
    cf_index_id: str
    # Words that must appear in the market rules/settlement sources to confirm mapping.
    rule_keywords: tuple[str, ...] = ("CF Benchmarks",)


DEFAULT_SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("KXBTC15M", "BTC", "BRTI"),
    SeriesSpec("KXETH15M", "ETH", "ETHUSD_RTI"),
    SeriesSpec("KXSOL15M", "SOL", "SOLUSD_RTI"),
    SeriesSpec("KXXRP15M", "XRP", "XRPUSD_RTI"),
    SeriesSpec("KXDOGE15M", "DOGE", "DOGEUSD_RTI"),
)


def default_data_dir() -> Path:
    override = os.environ.get("PCB_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PredictionCryptoBot"
    return Path.home() / ".predictioncryptobot"


@dataclass
class Settings:
    environment: Environment = "production"
    rest_base: str = PROD_REST
    ws_url: str = PROD_WS
    api_key_id: str | None = None
    private_key_path: Path | None = None
    subaccount: int | None = None
    data_dir: Path = field(default_factory=default_data_dir)
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    open_browser: bool = True
    series: tuple[SeriesSpec, ...] = DEFAULT_SERIES
    paper_starting_capital: str = "1000.00"
    # Operational timing
    portfolio_reconcile_s: float = 25.0
    discovery_interval_s: float = 20.0
    eval_interval_s: float = 1.0
    book_max_quiet_s: float = 20.0
    book_stale_s: float = 45.0
    cf_stale_s: float = 5.0
    ws_stale_s: float = 15.0
    # LIVE gating: LIVE stays blocked until strategy validation PASSES unless the
    # operator deliberately sets this in config.toml.
    allow_live_without_validation: bool = False
    # Rate limits (token buckets, conservative relative to Basic tier).
    read_rate_per_s: float = 10.0
    write_rate_per_s: float = 5.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "predictionbot.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path and self.private_key_path.exists())

    def public_dict(self) -> dict[str, Any]:
        """Serializable view with secrets redacted."""
        return {
            "environment": self.environment,
            "rest_base": self.rest_base,
            "ws_url": self.ws_url,
            "api_key_id": _redact(self.api_key_id),
            "private_key_path_set": self.private_key_path is not None,
            "private_key_file_exists": bool(self.private_key_path and self.private_key_path.exists()),
            "subaccount": self.subaccount,
            "data_dir": str(self.data_dir),
            "series": [s.series_ticker for s in self.series],
            "paper_starting_capital": self.paper_starting_capital,
            "allow_live_without_validation": self.allow_live_without_validation,
        }


def _redact(value: str | None) -> str | None:
    if not value:
        return None
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "…"


class ConfigError(ValueError):
    pass


def _validate_endpoints(s: Settings) -> None:
    if s.environment == "production":
        if s.rest_base != PROD_REST or s.ws_url != PROD_WS:
            raise ConfigError("production environment must use the official production hosts")
    elif s.environment == "demo":
        if s.rest_base != DEMO_REST or s.ws_url != DEMO_WS:
            raise ConfigError("demo environment must use the official demo hosts")
    elif s.environment == "mock":
        for url in (s.rest_base, s.ws_url):
            host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                raise ConfigError("mock environment may only target a localhost simulator")
    else:
        raise ConfigError(f"unknown environment {s.environment!r}")


def load_settings(data_dir: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    s = Settings()
    if data_dir is not None:
        s.data_dir = data_dir
    elif env.get("PCB_DATA_DIR"):
        s.data_dir = Path(env["PCB_DATA_DIR"])
    cfg_file = s.data_dir / "config.toml"
    raw: dict[str, Any] = {}
    if cfg_file.exists():
        raw = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    kalshi = raw.get("kalshi", {})
    app = raw.get("app", {})

    environment = env.get("PCB_ENV") or kalshi.get("environment") or "production"
    if environment not in ("production", "demo", "mock"):
        raise ConfigError(f"unknown environment {environment!r}")
    s.environment = environment  # type: ignore[assignment]
    if environment == "production":
        s.rest_base, s.ws_url = PROD_REST, PROD_WS
    elif environment == "demo":
        s.rest_base, s.ws_url = DEMO_REST, DEMO_WS
    else:
        s.rest_base = env.get("PCB_MOCK_REST") or kalshi.get("mock_rest_base", "http://127.0.0.1:8799/trade-api/v2")
        s.ws_url = env.get("PCB_MOCK_WS") or kalshi.get("mock_ws_url", "ws://127.0.0.1:8799/trade-api/ws/v2")

    s.api_key_id = env.get("KALSHI_API_KEY_ID") or kalshi.get("api_key_id") or None
    key_path = env.get("KALSHI_PRIVATE_KEY_PATH") or kalshi.get("private_key_path")
    s.private_key_path = Path(key_path).expanduser() if key_path else None
    sub = env.get("KALSHI_SUBACCOUNT") or kalshi.get("subaccount")
    s.subaccount = int(sub) if sub not in (None, "") else None

    s.http_host = app.get("host", s.http_host)
    if s.http_host not in ("127.0.0.1", "localhost"):
        raise ConfigError("the control UI only binds to localhost")
    s.http_port = int(env.get("PCB_HTTP_PORT") or app.get("port", s.http_port))
    s.open_browser = (env.get("PCB_NO_BROWSER") is None) and bool(app.get("open_browser", True))
    s.paper_starting_capital = str(app.get("paper_starting_capital", s.paper_starting_capital))
    s.allow_live_without_validation = bool(app.get("allow_live_without_validation", False))

    series_cfg = raw.get("series")
    if series_cfg:
        s.series = tuple(
            SeriesSpec(
                e["series_ticker"], e["asset"], e["cf_index_id"], tuple(e.get("rule_keywords", ["CF Benchmarks"]))
            )
            for e in series_cfg
        )
    _validate_endpoints(s)
    return s


CONFIG_TEMPLATE = """# PredictionCryptoBot configuration. This file lives OUTSIDE the program folder
# and is never overwritten by rebuilds. Do NOT paste your private key here —
# reference the .pem file by path.

[kalshi]
environment = "production"          # production | demo
api_key_id = ""                     # Kalshi API key ID (scope: read + write::trade ONLY)
private_key_path = ""               # e.g. C:/Users/you/kalshi/trading-key.pem
# subaccount = 1                    # optional: subaccount the key is restricted to

[app]
port = 8765
open_browser = true
paper_starting_capital = "1000.00"
allow_live_without_validation = false
"""


def ensure_data_dir(s: Settings) -> None:
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / "backups").mkdir(exist_ok=True)
    cfg = s.data_dir / "config.toml"
    if not cfg.exists():
        cfg.write_text(CONFIG_TEMPLATE, encoding="utf-8")
