"""Async SQLAlchemy engine, session factory, and Base class."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _install_fk_pragma_listener(engine: AsyncEngine) -> None:
    """Enable SQLite FK enforcement on every new connection.

    SQLite defaults to foreign_keys=OFF. We enable it globally at the engine
    level so every session gets the pragma automatically — auth code (M2) and
    downstream code relies on FK enforcement (RESTRICT, CASCADE, etc.).
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine() -> AsyncEngine:
    """Return the cached async engine, creating it on first use."""
    global _engine
    if _engine is None:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            f"sqlite+aiosqlite:///{settings.db_path}",
            echo=False,
            future=True,
        )
        _install_fk_pragma_listener(_engine)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the cached async sessionmaker, creating it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def reset_engine_for_tests() -> None:
    """Dispose the engine and clear cached factories. Test-only."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession in a context manager, closing it on exit."""
    async with get_sessionmaker()() as session:
        yield session


async def init_db() -> None:
    """Create all tables defined on Base.metadata."""
    # Importing models ensures they are registered on Base.metadata. models.py
    # is currently empty; later milestones will add tables.
    from app import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
