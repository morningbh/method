"""Fixtures for the integration test suite (Task 2.4).

These build on top of the shared ``app_client`` / ``db_session`` fixtures in
``tests/conftest.py``. Because ``app_client`` and ``db_session`` each call
``_reset_app_state_for_tmp_db`` (which disposes the async engine), using them
together in the same test would invalidate the engine that ``app_client`` is
holding. We therefore avoid ``db_session`` in integration tests and provide
two integration-local helpers instead:

- ``integration_db`` — yields an ``AsyncSession`` bound to the same cached
  engine/sessionmaker that ``app_client`` is driving. Order-sensitive: it
  depends on ``app_client`` being resolved first so the app lifespan has
  already run ``init_db()``.
- ``seeded_user`` — a factory that inserts a user in a given status (and
  optionally seeds login codes / approval tokens) and returns the ORM row.
- ``mailer_mocks`` — monkeypatches the mailer call-seams on
  ``app.services.auth_flow`` (the same seam the 2.3 unit tests use — module
  level, not ``app.services.mailer``).
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Recorder:
    """Async callable that records (args, kwargs) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class _FailingMailer:
    """Async callable that always raises ``MailerError``."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs) -> None:
        from app.services.mailer import MailerError

        self.calls.append((args, kwargs))
        raise MailerError("simulated mailer failure for test")


@pytest_asyncio.fixture
async def mailer_mocks(monkeypatch, app_client):
    """Patch send_* on app.services.auth_flow (the routers' consumption seam).

    Depends on ``app_client`` so the app has already been imported (and the
    ``auth_flow`` module loaded) before we patch its attributes.
    """
    from app.services import auth_flow as af

    login = _Recorder()
    approval = _Recorder()
    activation = _Recorder()
    monkeypatch.setattr(af, "send_login_code", login)
    monkeypatch.setattr(af, "send_approval_request", approval)
    monkeypatch.setattr(af, "send_activation_notice", activation)
    return {
        "send_login_code": login,
        "send_approval_request": approval,
        "send_activation_notice": activation,
    }


@pytest_asyncio.fixture
async def failing_login_mailer(monkeypatch, app_client):
    """Patch ``send_login_code`` to raise ``MailerError``. For 503 test."""
    from app.services import auth_flow as af

    failing = _FailingMailer()
    # Keep the others as successful recorders so mixed calls don't fail.
    approval = _Recorder()
    activation = _Recorder()
    monkeypatch.setattr(af, "send_login_code", failing)
    monkeypatch.setattr(af, "send_approval_request", approval)
    monkeypatch.setattr(af, "send_activation_notice", activation)
    return {
        "send_login_code": failing,
        "send_approval_request": approval,
        "send_activation_notice": activation,
    }


@pytest_asyncio.fixture
async def integration_db(app_client) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession bound to the same engine as ``app_client``.

    Must not reset the engine — ``app_client`` already set up DB_PATH and
    ran ``init_db`` via the lifespan. We just open a session against the
    cached sessionmaker.
    """
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        yield session


@pytest_asyncio.fixture
async def pinned_admin_email(monkeypatch, app_client):
    """Pin settings.admin_email to a known value. Depends on app_client so the
    settings object has already been instantiated for the tmp DB.
    """
    from app import config as config_mod

    monkeypatch.setattr(config_mod.settings, "admin_email", "admin@example.com")
    return "admin@example.com"


@pytest_asyncio.fixture
async def seeded_user(integration_db):
    """Factory: insert a User row with the requested status. Returns the row."""
    from app.models import User

    created: list[User] = []

    async def _factory(
        email: str,
        status: str = "active",
        *,
        created_at: datetime | None = None,
        approved_at: datetime | None = None,
    ) -> User:
        now = created_at or _utcnow_naive()
        approved = approved_at if approved_at is not None else (
            now if status == "active" else None
        )
        user = User(
            email=email.strip().lower(),
            status=status,
            created_at=now,
            approved_at=approved,
        )
        integration_db.add(user)
        await integration_db.commit()
        await integration_db.refresh(user)
        created.append(user)
        return user

    return _factory


@pytest_asyncio.fixture
async def seed_login_code(integration_db):
    """Factory: insert a login_codes row directly for ``user``.

    Returns the row. Used for rate-limit / expired / reused tests that must
    seed state independently of ``request_login_code``.
    """
    from app.models import LoginCode

    async def _factory(
        user_id: int,
        plaintext: str = "123456",
        *,
        salt: str = "s" * 32,
        expires_at: datetime | None = None,
        used_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> LoginCode:
        now = _utcnow_naive()
        row = LoginCode(
            user_id=user_id,
            code_hash=_sha256(plaintext + salt),
            salt=salt,
            expires_at=expires_at if expires_at is not None else (now + timedelta(minutes=10)),
            used_at=used_at,
            created_at=created_at if created_at is not None else now,
        )
        integration_db.add(row)
        await integration_db.commit()
        await integration_db.refresh(row)
        return row

    return _factory


@pytest_asyncio.fixture
async def seed_approval_token(integration_db):
    """Factory: insert an approval_tokens row and return (row, raw_token)."""
    from app.models import ApprovalToken

    async def _factory(
        user_id: int,
        raw_token: str = "approve-raw-token-fixture",
        *,
        expires_at: datetime | None = None,
        used_at: datetime | None = None,
    ) -> tuple[ApprovalToken, str]:
        now = _utcnow_naive()
        row = ApprovalToken(
            user_id=user_id,
            token_hash=_sha256(raw_token),
            expires_at=expires_at if expires_at is not None else (now + timedelta(days=7)),
            used_at=used_at,
        )
        integration_db.add(row)
        await integration_db.commit()
        await integration_db.refresh(row)
        return row, raw_token

    return _factory


@pytest_asyncio.fixture
async def seed_session(integration_db):
    """Factory: insert a sessions row for ``user_id`` and return (row, raw_token)."""
    from app.models import Session as SessionRow

    async def _factory(
        user_id: int,
        raw_token: str = "session-raw-token-fixture",
        *,
        expires_at: datetime | None = None,
    ) -> tuple[SessionRow, str]:
        now = _utcnow_naive()
        row = SessionRow(
            user_id=user_id,
            token_hash=_sha256(raw_token),
            expires_at=expires_at if expires_at is not None else (now + timedelta(days=30)),
            created_at=now,
        )
        integration_db.add(row)
        await integration_db.commit()
        await integration_db.refresh(row)
        return row, raw_token

    return _factory
