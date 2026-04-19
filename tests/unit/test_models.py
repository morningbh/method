"""Unit tests for ORM models (Task 2.1).

Covers CRUD + constraints for User, LoginCode, Session, ApprovalToken,
and verifies indexes from spec §2.1 exist after init_db().
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


async def test_user_created_with_status_pending(db_session):
    from app.models import User

    now = datetime(2026, 1, 2, 3, 4, 5)
    user = User(email="alice@example.com", status="pending", created_at=now)
    db_session.add(user)
    await db_session.commit()

    result = await db_session.execute(select(User).where(User.email == "alice@example.com"))
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.email == "alice@example.com"
    assert fetched.status == "pending"
    assert fetched.created_at == now
    assert fetched.approved_at is None


async def test_user_email_unique(db_session):
    from app.models import User

    now = datetime(2026, 1, 2, 3, 4, 5)
    db_session.add(User(email="dup@example.com", status="pending", created_at=now))
    await db_session.commit()

    db_session.add(User(email="dup@example.com", status="active", created_at=now))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_user_status_check_constraint(db_session):
    from app.models import User

    now = datetime(2026, 1, 2, 3, 4, 5)
    db_session.add(User(email="bad@example.com", status="bogus", created_at=now))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# LoginCode
# ---------------------------------------------------------------------------


async def test_login_code_crud(db_session):
    from app.models import LoginCode, User

    now = datetime(2026, 1, 2, 3, 4, 5)
    user = User(email="lc@example.com", status="active", created_at=now, approved_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    code = LoginCode(
        user_id=user.id,
        code_hash="a" * 64,
        salt="s" * 16,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db_session.add(code)
    await db_session.commit()

    result = await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.code_hash == "a" * 64
    assert fetched.salt == "s" * 16
    assert fetched.expires_at == now + timedelta(minutes=10)
    assert fetched.used_at is None
    assert fetched.created_at == now


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


async def test_session_crud(db_session):
    from app.models import Session, User

    now = datetime(2026, 1, 2, 3, 4, 5)
    user = User(email="sess@example.com", status="active", created_at=now, approved_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    sess = Session(
        user_id=user.id,
        token_hash="t" * 64,
        expires_at=now + timedelta(days=30),
        created_at=now,
    )
    db_session.add(sess)
    await db_session.commit()

    result = await db_session.execute(
        select(Session).where(Session.token_hash == "t" * 64)
    )
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.user_id == user.id
    assert fetched.expires_at == now + timedelta(days=30)
    assert fetched.created_at == now


async def test_session_token_hash_unique(db_session):
    from app.models import Session, User

    now = datetime(2026, 1, 2, 3, 4, 5)
    user = User(email="sess2@example.com", status="active", created_at=now, approved_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(
        Session(
            user_id=user.id,
            token_hash="shared" * 12,
            expires_at=now + timedelta(days=30),
            created_at=now,
        )
    )
    await db_session.commit()

    db_session.add(
        Session(
            user_id=user.id,
            token_hash="shared" * 12,
            expires_at=now + timedelta(days=30),
            created_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# ApprovalToken
# ---------------------------------------------------------------------------


async def test_approval_token_crud(db_session):
    from app.models import ApprovalToken, User

    now = datetime(2026, 1, 2, 3, 4, 5)
    user = User(email="approve@example.com", status="pending", created_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    tok = ApprovalToken(
        user_id=user.id,
        token_hash="b" * 64,
        expires_at=now + timedelta(days=7),
    )
    db_session.add(tok)
    await db_session.commit()

    result = await db_session.execute(
        select(ApprovalToken).where(ApprovalToken.user_id == user.id)
    )
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.token_hash == "b" * 64
    assert fetched.expires_at == now + timedelta(days=7)
    assert fetched.used_at is None


# ---------------------------------------------------------------------------
# FK behavior — spec DDL uses REFERENCES with no ON DELETE clause (default
# RESTRICT). MVP doesn't delete users, so we only need to document the
# behavior: deleting a referenced user while child rows exist must raise.
#
# NOTE: SQLite does NOT enforce foreign keys by default. Production code
# will turn them on via PRAGMA; here we just validate the semantic chosen
# by the schema (no CASCADE declared).
# ---------------------------------------------------------------------------


async def test_cascade_or_no_cascade(db_session):
    from app.models import LoginCode, User

    # Turn on FK enforcement for this connection (SQLite default is off).
    await db_session.execute(text("PRAGMA foreign_keys = ON"))

    now = datetime(2026, 1, 2, 3, 4, 5)
    user = User(email="restrict@example.com", status="active", created_at=now, approved_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(
        LoginCode(
            user_id=user.id,
            code_hash="c" * 64,
            salt="x" * 16,
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
    )
    await db_session.commit()

    # With no ON DELETE CASCADE in the schema, deleting a parent while
    # children exist must raise an IntegrityError.
    await db_session.delete(user)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Indexes — spec §2.1 requires idx_sessions_token and idx_login_codes_user.
# ---------------------------------------------------------------------------


async def test_indexes_exist(db_session):
    result = await db_session.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'index'")
    )
    names = {row[0] for row in result.all()}
    assert "idx_sessions_token" in names
    assert "idx_login_codes_user" in names
