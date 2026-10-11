# PyInstaller spec — builds dist/PredictionCryptoBot(.exe), a single-file executable.
# Runtime data (database, config.toml, logs, backups) lives in %LOCALAPPDATA%\PredictionCryptoBot,
# never inside dist/, so rebuilding can never destroy history or secrets.
# ruff: noqa
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
PKG = SRC / "predictionbot"

datas = [
    (str(PKG / "ui" / "static"), "predictionbot/ui/static"),
    (str(PKG / "persistence" / "migrations"), "predictionbot/persistence/migrations"),
    (str(PKG / "research" / "knowledge.json"), "predictionbot/research"),
    (str(PKG / "_build_info.json"), "predictionbot"),
]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "websockets", "websockets.asyncio.client", "websockets.legacy",
    "predictionbot.sim.mock_exchange",
]

a = Analysis(
    [str(PKG / "main.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PredictionCryptoBot",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    runtime_tmpdir=None,
)
