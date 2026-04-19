"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield an httpx.AsyncClient bound to the FastAPI app with lifespan run."""
    db_path = tmp_path / "test.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))

    # Reset cached settings + engine so the tmp DB is used for this test.
    from app import config as config_mod

    config_mod.settings = config_mod.Settings()

    from app import db as db_mod

    await db_mod.reset_engine_for_tests()

    # Import app AFTER env vars are set so any module-level config reads see them.
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
