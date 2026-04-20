"""FastAPI entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import init_db
from app.routers import admin, auth, health, history, research
from app.services.error_copy import message_for

_APP_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _APP_DIR / "templates"
_STATIC_DIR = _APP_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Method", version="0.0.1", lifespan=lifespan)
app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(health.router)
# history.router must come before auth.router so design §8 router-order note
# holds; the two no longer collide (auth.root was removed) but explicit order
# prevents future regressions.
app.include_router(history.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(research.router)
auth.install_exception_handlers(app)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Wrap ``HTTPException(detail=<str>)`` into ``{error, message}`` shape.

    Design §7 note 1: every `HTTPException(404, detail="not_found")` raised
    by routers (research.py / history.py / comment endpoints) — plus
    FastAPI's own default-404 for unmatched routes — surfaces as
    ``{"error": "<code>", "message": <中文>}`` instead of the default
    ``{"detail": "<code>"}`` / ``{"detail": "Not Found"}``.

    Dict-valued ``detail`` (e.g. ``LimitExceededError`` after Issue #5's
    migration already emits ``{"error", "message"}``) is returned verbatim
    so the shape propagates without double-wrapping.
    """
    detail = exc.detail
    headers = getattr(exc, "headers", None)
    if isinstance(detail, dict):
        return JSONResponse(
            status_code=exc.status_code, content=detail, headers=headers
        )
    if isinstance(detail, str):
        # FastAPI's built-in 404 uses detail="Not Found" — normalise to the
        # canonical "not_found" machine code so the Chinese copy matches.
        code = detail
        if exc.status_code == 404 and code in ("Not Found", "not_found"):
            code = "not_found"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": message_for(code)},
            headers=headers,
        )
    # Fallback — detail is some other type. Coerce to string.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(detail), "message": message_for(str(detail))},
        headers=headers,
    )


def run() -> None:
    """Console-script entry point (`method` = `app.main:run`)."""
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)
