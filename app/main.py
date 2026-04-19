"""FastAPI entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.db import init_db
from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Method", version="0.0.1", lifespan=lifespan)
app.include_router(health.router)


def run() -> None:
    """Console-script entry point (`method` = `app.main:run`)."""
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)
