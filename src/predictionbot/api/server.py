"""Local HTTP API + dashboard. The backend is authoritative; the UI only renders
`/api/state` (polled) and sends explicit control commands.

Mutating endpoints require the `X-PCB-Client: dashboard` header and a
localhost Origin (if any). Browsers cannot attach custom headers cross-site
without a CORS pre-flight, which this server never approves.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from predictionbot.app.engine import ControlError, Engine
from predictionbot.buildinfo import build_info

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"


class _Enc(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, (set, tuple)):
            return list(o)
        return str(o)


def _json(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, cls=_Enc),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


class ModeBody(BaseModel):
    mode: str


class ProfileBody(BaseModel):
    profile: str


class ConfirmBody(BaseModel):
    text: str


def _guard(request: Request) -> None:
    if request.headers.get("x-pcb-client") != "dashboard":
        raise HTTPException(403, "missing X-PCB-Client header")
    origin = request.headers.get("origin")
    if origin:
        host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            raise HTTPException(403, "cross-origin request refused")


def create_app(engine: Engine, *, manage_engine: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if manage_engine:
            await engine.start()
        try:
            yield
        finally:
            if manage_engine:
                await engine.shutdown()

    app = FastAPI(title="PredictionCryptoBot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ControlError)
    async def control_error(_: Request, exc: ControlError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/state")
    async def state() -> Response:
        return _json(engine.state())

    @app.get("/api/health")
    async def health() -> Response:
        h = engine.health()
        return _json({"bot_state": engine.bot_state, "mode": engine.mode, **h})

    @app.get("/api/build")
    async def build() -> Response:
        return _json(build_info())

    @app.get("/api/research")
    async def research() -> Response:
        return _json(engine.research_view())

    @app.post("/api/bot/mode")
    async def mode(body: ModeBody, request: Request) -> Response:
        _guard(request)
        engine.set_mode(body.mode)
        return _json({"ok": True, "mode": engine.mode})

    @app.post("/api/bot/profile")
    async def profile(body: ProfileBody, request: Request) -> Response:
        _guard(request)
        engine.set_profile(body.profile)
        return _json({"ok": True, "profile": engine.profile.value})

    @app.post("/api/bot/confirm_live")
    async def confirm_live(body: ConfirmBody, request: Request) -> Response:
        _guard(request)
        engine.confirm_live(body.text)
        return _json({"ok": True})

    @app.post("/api/bot/start")
    async def start(request: Request) -> Response:
        _guard(request)
        return _json({"ok": True, **(await engine.start_bot())})

    @app.post("/api/bot/stop")
    async def stop(request: Request) -> Response:
        _guard(request)
        return _json({"ok": True, **(await engine.stop_bot())})

    @app.post("/api/bot/emergency_stop")
    async def emergency(request: Request) -> Response:
        _guard(request)
        return _json({"ok": True, **(await engine.emergency_stop())})

    @app.post("/api/bot/reset_kill_switch")
    async def reset_kill(request: Request) -> Response:
        _guard(request)
        engine.reset_kill_switch()
        return _json({"ok": True})

    @app.post("/api/validation/run")
    async def run_validation(request: Request) -> Response:
        _guard(request)
        return _json(await engine.run_validation())

    return app
