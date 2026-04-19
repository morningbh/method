"""FastAPI entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import init_db
from app.routers import admin, auth, health, research

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
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(research.router)
auth.install_exception_handlers(app)


def run() -> None:
    """Console-script entry point (`method` = `app.main:run`)."""
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)
