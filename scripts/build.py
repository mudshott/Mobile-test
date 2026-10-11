"""Build the single-file executable with PyInstaller.

    python scripts/build.py                      # build with the current interpreter
    python scripts/build.py --python "C:/Python312/python.exe"
    python scripts/build.py --python "wine C:/winpy/python.exe"   # cross-build from Linux via Wine

Steps: secret scan → write build provenance (git commit, dirty flag, timestamp,
versions) into src/predictionbot/_build_info.json → PyInstaller with
packaging/PredictionCryptoBot.spec → dist/PredictionCryptoBot[.exe].
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from predictionbot.buildinfo import write_build_info  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="interpreter (may include a launcher, e.g. 'wine X.exe')")
    ap.add_argument("--skip-secret-scan", action="store_true")
    ap.add_argument("--distpath", default=str(ROOT / "dist"))
    args = ap.parse_args()
    if not args.skip_secret_scan:
        rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "secret_scan.py")])  # noqa: S603
        if rc != 0:
            print("refusing to build: secret scan failed", file=sys.stderr)
            return rc
    info = write_build_info(ROOT / "src" / "predictionbot" / "_build_info.json")
    print("build provenance:", json.dumps(info))
    cmd = [
        *shlex.split(args.python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        args.distpath,
        "--workpath",
        str(ROOT / "build"),
        str(ROOT / "packaging" / "PredictionCryptoBot.spec"),
    ]
    print("running:", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)  # noqa: S603


if __name__ == "__main__":
    sys.exit(main())
