"""Build provenance: lets the operator tell a fresh EXE from a stale one.

`_build_info.json` is written by `scripts/build.py` immediately before packaging.
When running from source the values are computed live from git.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from predictionbot import __version__

MODEL_VERSION = "settle-gauss-avg-1.0"
STRATEGY_VERSION = "ev-taker-ioc-1.0"
SCHEMA_VERSION = 1  # bump together with persistence/migrations

_HERE = Path(__file__).resolve().parent


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],  # noqa: S607
            cwd=_HERE,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


@lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "app_version": __version__,
        "model_version": MODEL_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    baked = _HERE / "_build_info.json"
    if baked.exists():
        try:
            data = json.loads(baked.read_text(encoding="utf-8"))
            info.update({k: data[k] for k in ("git_commit", "git_dirty", "build_timestamp") if k in data})
            info["source"] = "packaged"
            return info
        except (OSError, ValueError):
            pass
    commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    info["git_commit"] = commit or "unknown"
    info["git_dirty"] = dirty
    info["build_timestamp"] = datetime.now(UTC).isoformat(timespec="seconds")
    info["source"] = "source-tree"
    return info


def write_build_info(path: Path) -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD") or "unknown"
    data = {
        "git_commit": commit,
        "git_dirty": bool(_git("status", "--porcelain")),
        "build_timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "app_version": __version__,
        "model_version": MODEL_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
