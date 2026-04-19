"""Shared pytest fixtures."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession


async def _reset_app_state_for_tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the app at a fresh per-test SQLite DB and reset cached engine/settings."""
    db_path = tmp_path / "test.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))

    # Reset cached settings so the tmp DB path is picked up.
    from app import config as config_mod

    config_mod.settings = config_mod.Settings()

    # Dispose any previously cached async engine / sessionmaker.
    from app import db as db_mod

    await db_mod.reset_engine_for_tests()


@pytest_asyncio.fixture
async def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield an httpx.AsyncClient bound to the FastAPI app with lifespan run."""
    await _reset_app_state_for_tmp_db(tmp_path, monkeypatch)

    # Import app AFTER env vars are set so any module-level config reads see them.
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def db_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    """Provide an AsyncSession against a fresh DB for this test."""
    await _reset_app_state_for_tmp_db(tmp_path, monkeypatch)

    from app.db import get_sessionmaker, init_db

    await init_db()

    async with get_sessionmaker()() as session:
        yield session
