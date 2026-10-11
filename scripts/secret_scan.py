"""Repository secret scan (run before every commit / build).

Fails if tracked or staged files contain private-key material, Kalshi API key
IDs assigned in config-like files, bearer tokens or common cloud keys.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "pem_private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{100,}"),
    "api_key_id_assignment": re.compile(
        r"(?i)(api_key_id|KALSHI_API_KEY_ID)\s*[=:]\s*[\"']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    ),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "bearer": re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}"),
}
FORBIDDEN_FILES = re.compile(r"(\.pem|\.key|config\.toml|\.env|\.db)$")


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / line for line in out.splitlines() if line]


def main() -> int:
    problems = []
    for f in tracked_files():
        rel = f.relative_to(ROOT).as_posix()
        if FORBIDDEN_FILES.search(rel):
            problems.append(f"{rel}: secret-bearing file type must not be committed")
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, rx in PATTERNS.items():
            if rx.search(text):
                problems.append(f"{rel}: {name}")
    for p in problems:
        print("SECRET-SCAN FAIL:", p)
    print(f"secret scan: {len(problems)} problem(s) in {len(tracked_files())} files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
