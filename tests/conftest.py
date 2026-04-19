"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _fresh_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give each test a unique SQLite file and reset any cached engine/settings.

    We set DB_PATH in the environment *before* app modules are imported, and
    we also clear any cached `app.config` / `app.db` / `app.main` modules so
    the new DB_PATH takes effect.
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    # Drop any cached app modules so the next import re-reads the env.
    import sys

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)

    return db_file


@pytest_asyncio.fixture
async def app_client(_fresh_db_path: Path):
    """Yield an httpx.AsyncClient bound to the FastAPI app via ASGITransport."""
    import httpx

    from app.main import app

    # Trigger lifespan (init_db) via LifespanManager-less manual call is
    # unnecessary — httpx ASGITransport doesn't run lifespan by default, so we
    # call init_db() directly to create tables.
    from app.db import init_db

    await init_db()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
