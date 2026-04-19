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
# FK enforcement is now enabled globally at the engine level (see app/db.py
# _install_fk_pragma_listener). Tests no longer need to issue the PRAGMA.
# ---------------------------------------------------------------------------


async def test_cascade_or_no_cascade(db_session):
    from app.models import LoginCode, User

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
    # children exist must raise an IntegrityError — proving RESTRICT semantics
    # AND that the engine-level FK PRAGMA is active.
    await db_session.delete(user)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_fk_enforcement_enabled(db_session):
    """Inserting a LoginCode with a non-existent user_id must raise IntegrityError.

    Proves FK enforcement is ON at the engine level without any test-level
    PRAGMA — if the engine listener regresses, this test fails immediately.
    """
    from app.models import LoginCode

    now = datetime(2026, 1, 2, 3, 4, 5)
    db_session.add(
        LoginCode(
            user_id=999999,  # no such user
            code_hash="d" * 64,
            salt="y" * 16,
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
    )
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


# ---------------------------------------------------------------------------
# research_requests + uploaded_files (Task 3.1) — new tables introduced in
# this milestone. Tests #17–#19 from design issue-2-task-3.1-file-processor §10.
# ---------------------------------------------------------------------------


async def test_research_requests_crud(db_session):
    """Insert a ResearchRequest, read it back, and verify every column
    round-trips (ULID id, FK to users, status, plan_path/error_message nulls,
    model, created_at, completed_at).
    """
    from app.models import ResearchRequest, User

    now = datetime(2026, 4, 19, 12, 0, 0)
    user = User(email="rr@example.com", status="active", created_at=now, approved_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    req = ResearchRequest(
        id="01HXZK8D7Q3V0S9B4W2N6M5C7R",
        user_id=user.id,
        question="What is the future of AI?",
        status="pending",
        plan_path=None,
        error_message=None,
        model="claude-opus-4-7",
        created_at=now,
        completed_at=None,
    )
    db_session.add(req)
    await db_session.commit()

    result = await db_session.execute(
        select(ResearchRequest).where(ResearchRequest.id == "01HXZK8D7Q3V0S9B4W2N6M5C7R")
    )
    fetched = result.scalar_one()
    assert fetched.id == "01HXZK8D7Q3V0S9B4W2N6M5C7R"
    assert fetched.user_id == user.id
    assert fetched.question == "What is the future of AI?"
    assert fetched.status == "pending"
    assert fetched.plan_path is None
    assert fetched.error_message is None
    assert fetched.model == "claude-opus-4-7"
    assert fetched.created_at == now
    assert fetched.completed_at is None


async def test_uploaded_files_fk_enforces_request_id(db_session):
    """Inserting an uploaded_files row pointing at a non-existent research_request
    must raise IntegrityError. Proves the FK is declared AND that the engine-level
    foreign_keys PRAGMA is active (design §6.2).
    """
    from app.models import UploadedFile

    now = datetime(2026, 4, 19, 12, 0, 0)
    db_session.add(
        UploadedFile(
            request_id="01HXZK8D7Q3V0S9B4W2N6M5C7R",  # no such research_request
            original_name="orphan.pdf",
            stored_path="/abs/path/to/orphan.pdf",
            extracted_path=None,
            size_bytes=100,
            mime_type="application/pdf",
            created_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_research_requests_status_check_constraint(db_session):
    """status must be one of pending/running/done/failed (design §6.1).
    Any other value must raise IntegrityError via the CHECK constraint.
    """
    from app.models import ResearchRequest, User

    now = datetime(2026, 4, 19, 12, 0, 0)
    user = User(email="bogus@example.com", status="active", created_at=now, approved_at=now)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(
        ResearchRequest(
            id="01HXZK8D7Q3V0S9B4W2N6M5C7S",
            user_id=user.id,
            question="Q",
            status="bogus",  # not in the CHECK whitelist
            model="claude-opus-4-7",
            created_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
