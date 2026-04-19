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
from sqlalchemy.orm.session import SessionTransaction as _SessionTransaction

from app import config as _config


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# ---------------------------------------------------------------------------
# Rollback-without-expire patch
# ---------------------------------------------------------------------------
#
# Under async SQLAlchemy (aiosqlite), reading an expired ORM attribute outside
# a greenlet-spawn context raises ``MissingGreenlet`` — the ORM wants to issue
# a lazy-load SQL to repopulate the attribute, but it cannot bridge async→sync
# from plain Python code. The default ``Session.rollback()`` unconditionally
# expires every persistent object (via ``SessionTransaction._restore_snapshot``)
# so that the next access fetches fresh state. For sync code this is fine; for
# async code it means any router (or test) that holds a reference to an ORM
# object across a rollback can no longer even read its PK without triggering
# a greenlet error.
#
# ``expire_on_commit=False`` only suppresses the commit half of the expire
# behaviour; SQLAlchemy 2.x does not expose an equivalent flag for rollback.
# We therefore monkey-patch ``_restore_snapshot`` at import time: pending
# inserts are still expunged, deleted objects reinstated, and identity-key
# switches reverted — only the blanket attribute-expire loop is skipped. The
# patch is process-wide (all Sessions in the app inherit it), which matches
# how SQLAlchemy itself configures cross-session behaviours.
#
# Trade-off: after rollback, loaded attribute values may be stale relative to
# the DB. Callers that care must re-query explicitly (``session.refresh(obj)``
# or a fresh ``select(...)``). In practice Method's code writes through
# typed services that return fresh ORM objects on every call, so staleness
# across rollback boundaries is not a concern.


def _restore_snapshot_no_expire(
    self: _SessionTransaction, dirty_only: bool = False,
) -> None:
    assert self._is_transaction_boundary

    to_expunge = set(self._new).union(self.session._new)
    self.session._expunge_states(to_expunge, to_transient=True)

    for s, (oldkey, _newkey) in self._key_switches.items():
        self.session.identity_map.safe_discard(s)
        s.key = oldkey
        if s not in to_expunge:
            self.session.identity_map.replace(s)

    for s in set(self._deleted).union(self.session._deleted):
        self.session._update_impl(s, revert_deletion=True)

    assert not self.session._deleted
    # Intentionally omit the per-state `_expire` loop that vanilla SQLAlchemy
    # runs here — see the module-level comment above.


_SessionTransaction._restore_snapshot = _restore_snapshot_no_expire


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
        Path(_config.settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            f"sqlite+aiosqlite:///{_config.settings.db_path}",
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
