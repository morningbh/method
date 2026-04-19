"""Unit tests for the auth flow service (Task 2.3).

These tests are written from the design document
(``docs/design/issue-1-task-2.3-auth-flow.md``) and the ORM contract in
``app/models.py`` — no peeking at the implementation (which does not yet
exist). They exercise ``app.services.auth_flow`` end-to-end against a real
(per-test SQLite) DB via the shared ``db_session`` fixture. Mailer calls are
monkeypatched at the ``app.services.auth_flow.send_*`` boundary so we can
assert on call arguments without hitting SMTP.

Covers the 20 cases enumerated in design §11 plus 3 additional cases
implied by the design (email-lowercasing, admin self-bootstrap `users.status`,
and the no-commit/caller-owns-transaction invariant).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    """Mirror the module-internal naive-UTC policy (design §7)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Recorder:
    """Async callable that records positional/keyword args, ignoring the
    real implementation. Installed via monkeypatch at module level."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mailer_mocks(monkeypatch):
    """Monkeypatch send_login_code / send_approval_request / send_activation_notice
    at the ``app.services.auth_flow`` module boundary (NOT at
    ``app.services.mailer`` — that's the wrong seam per the /tester prompt).

    Yields a dict with the three recorders.
    """
    from app.services import auth_flow as af  # type: ignore[import-not-found]

    login = _Recorder()
    approval = _Recorder()
    activation = _Recorder()
    monkeypatch.setattr(af, "send_login_code", login)
    monkeypatch.setattr(af, "send_approval_request", approval)
    monkeypatch.setattr(af, "send_activation_notice", activation)
    yield {
        "send_login_code": login,
        "send_approval_request": approval,
        "send_activation_notice": activation,
    }


@pytest_asyncio.fixture
async def admin_email(monkeypatch):
    """Pin settings.admin_email to a known value for the test."""
    from app import config as config_mod

    monkeypatch.setattr(config_mod.settings, "admin_email", "admin@example.com")
    return "admin@example.com"


# ---------------------------------------------------------------------------
# helpers for crafting DB rows directly (so tests don't rely on auth_flow
# internals for setup)
# ---------------------------------------------------------------------------


async def _insert_user(
    db_session, email: str, status: str, created_at: datetime | None = None
):
    from app.models import User

    now = created_at or _utcnow_naive()
    approved = now if status == "active" else None
    user = User(email=email, status=status, created_at=now, approved_at=approved)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _insert_login_code(
    db_session,
    user_id: int,
    plaintext: str,
    *,
    salt: str = "s" * 32,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
    created_at: datetime | None = None,
):
    from app.models import LoginCode

    now = _utcnow_naive()
    row = LoginCode(
        user_id=user_id,
        code_hash=_sha256(plaintext + salt),
        salt=salt,
        expires_at=expires_at or (now + timedelta(minutes=10)),
        used_at=used_at,
        created_at=created_at or now,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _insert_session(
    db_session,
    user_id: int,
    raw_token: str,
    *,
    expires_at: datetime | None = None,
):
    from app.models import Session

    now = _utcnow_naive()
    row = Session(
        user_id=user_id,
        token_hash=_sha256(raw_token),
        expires_at=expires_at or (now + timedelta(days=30)),
        created_at=now,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _insert_approval_token(
    db_session,
    user_id: int,
    raw_token: str,
    *,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
):
    from app.models import ApprovalToken

    now = _utcnow_naive()
    row = ApprovalToken(
        user_id=user_id,
        token_hash=_sha256(raw_token),
        expires_at=expires_at or (now + timedelta(days=7)),
        used_at=used_at,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


# ===========================================================================
# 1. request_login_code — new user → pending
# ===========================================================================


async def test_request_login_code_new_user_creates_pending(
    db_session, mailer_mocks, admin_email
):
    from app.models import ApprovalToken, LoginCode, User
    from app.services.auth_flow import request_login_code

    result = await request_login_code(db_session, "newcomer@example.com")
    await db_session.commit()  # caller owns the transaction per design §12

    assert result == "pending"

    # users row exists with status=pending
    user = (
        await db_session.execute(select(User).where(User.email == "newcomer@example.com"))
    ).scalar_one()
    assert user.status == "pending"
    assert user.approved_at is None

    # approval_tokens row exists for the user
    tokens = (
        await db_session.execute(select(ApprovalToken).where(ApprovalToken.user_id == user.id))
    ).scalars().all()
    assert len(tokens) == 1
    assert tokens[0].used_at is None

    # admin got an approval email
    assert len(mailer_mocks["send_approval_request"].calls) == 1
    args, kwargs = mailer_mocks["send_approval_request"].calls[0]
    combined = list(args) + list(kwargs.values())
    assert admin_email in combined
    assert "newcomer@example.com" in combined

    # no login code issued for a pending user
    codes = (
        await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    ).scalars().all()
    assert codes == []
    assert mailer_mocks["send_login_code"].calls == []


# ===========================================================================
# 2. request_login_code — admin self-bootstrap → active + code sent
# ===========================================================================


async def test_request_login_code_admin_short_circuit_activates_directly(
    db_session, mailer_mocks, admin_email
):
    from app.models import LoginCode, User
    from app.services.auth_flow import request_login_code

    result = await request_login_code(db_session, admin_email)
    await db_session.commit()

    assert result == "sent"

    user = (
        await db_session.execute(select(User).where(User.email == admin_email))
    ).scalar_one()
    assert user.status == "active"
    assert user.approved_at is not None

    # A login_code row was issued.
    codes = (
        await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    ).scalars().all()
    assert len(codes) == 1

    # send_login_code was called with the admin email and a 6-digit code.
    assert len(mailer_mocks["send_login_code"].calls) == 1
    args, kwargs = mailer_mocks["send_login_code"].calls[0]
    combined = list(args) + list(kwargs.values())
    assert admin_email in combined
    # Find the code argument (the one that is not the email).
    code = next(v for v in combined if v != admin_email)
    assert isinstance(code, str)
    assert len(code) == 6 and code.isdigit()

    # Admin self-bootstrap must NOT also trigger the approval-request mail.
    assert mailer_mocks["send_approval_request"].calls == []


# ===========================================================================
# 3. request_login_code — pending user resubmits → "pending", no email, no new row
# ===========================================================================


async def test_request_login_code_pending_returns_pending_no_email(
    db_session, mailer_mocks, admin_email
):
    from app.models import ApprovalToken, LoginCode
    from app.services.auth_flow import request_login_code

    user = await _insert_user(db_session, "pending@example.com", "pending")
    # Seed one approval_token to verify no second one is created.
    await _insert_approval_token(db_session, user.id, raw_token="seed-token-1")

    result = await request_login_code(db_session, "pending@example.com")
    await db_session.commit()

    assert result == "pending"

    # Unchanged number of approval_tokens (still 1) — design §5 row-2: unchanged.
    tokens = (
        await db_session.execute(select(ApprovalToken).where(ApprovalToken.user_id == user.id))
    ).scalars().all()
    assert len(tokens) == 1

    # No login_codes row created.
    codes = (
        await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    ).scalars().all()
    assert codes == []

    # No mailer calls whatsoever.
    assert mailer_mocks["send_login_code"].calls == []
    assert mailer_mocks["send_approval_request"].calls == []
    assert mailer_mocks["send_activation_notice"].calls == []


# ===========================================================================
# 4. request_login_code — rejected user → "rejected", no email
# ===========================================================================


async def test_request_login_code_rejected_returns_rejected_no_email(
    db_session, mailer_mocks, admin_email
):
    from app.models import LoginCode
    from app.services.auth_flow import request_login_code

    user = await _insert_user(db_session, "denied@example.com", "rejected")

    result = await request_login_code(db_session, "denied@example.com")
    await db_session.commit()

    assert result == "rejected"

    codes = (
        await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    ).scalars().all()
    assert codes == []
    assert mailer_mocks["send_login_code"].calls == []
    assert mailer_mocks["send_approval_request"].calls == []


# ===========================================================================
# 5. request_login_code — active user → login_code issued, email sent
# ===========================================================================


async def test_request_login_code_active_sends_code(
    db_session, mailer_mocks, admin_email
):
    from app.models import LoginCode
    from app.services.auth_flow import request_login_code

    user = await _insert_user(db_session, "active@example.com", "active")

    result = await request_login_code(db_session, "active@example.com")
    await db_session.commit()

    assert result == "sent"

    codes = (
        await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    ).scalars().all()
    assert len(codes) == 1

    # send_login_code called with user email and a 6-digit plaintext code.
    assert len(mailer_mocks["send_login_code"].calls) == 1
    args, kwargs = mailer_mocks["send_login_code"].calls[0]
    combined = list(args) + list(kwargs.values())
    assert "active@example.com" in combined
    code_values = [v for v in combined if v != "active@example.com"]
    assert len(code_values) == 1
    code = code_values[0]
    assert isinstance(code, str) and len(code) == 6 and code.isdigit()

    # The stored code_hash must match sha256(code + salt) for the row.
    assert codes[0].code_hash == _sha256(code + codes[0].salt)


# ===========================================================================
# 6. request_login_code — rate limit within 60s → RateLimitError
# ===========================================================================


async def test_request_login_code_rate_limit_within_60s(
    db_session, mailer_mocks, admin_email
):
    from app.models import LoginCode
    from app.services.auth_flow import RateLimitError, request_login_code

    user = await _insert_user(db_session, "rl@example.com", "active")
    # Capture PK before any rollback — SQLAlchemy expires ORM attributes on
    # rollback by default, and reading an expired attribute on aiosqlite
    # triggers ``MissingGreenlet`` (cannot emit a lazy-load SELECT from plain
    # async code). Using a plain int sidesteps the issue entirely.
    user_id = user.id

    # First call issues a code.
    first = await request_login_code(db_session, "rl@example.com")
    await db_session.commit()
    assert first == "sent"

    count_after_first = len(
        (
            await db_session.execute(
                select(LoginCode).where(LoginCode.user_id == user_id)
            )
        ).scalars().all()
    )
    assert count_after_first == 1

    # Second call within 60s raises RateLimitError.
    mailer_mocks["send_login_code"].calls.clear()
    with pytest.raises(RateLimitError):
        await request_login_code(db_session, "rl@example.com")

    # No new login_code row inserted (still 1) — the failed call MUST NOT
    # create a row. Rollback any pending state first.
    await db_session.rollback()
    count_after_second = len(
        (
            await db_session.execute(
                select(LoginCode).where(LoginCode.user_id == user_id)
            )
        ).scalars().all()
    )
    assert count_after_second == 1
    assert mailer_mocks["send_login_code"].calls == []


# ===========================================================================
# 7. verify_login_code — success: returns raw 43-char token, marks code used,
#                       inserts sessions row
# ===========================================================================


async def test_verify_login_code_success_returns_token_and_marks_used(
    db_session, mailer_mocks, admin_email
):
    from app.models import LoginCode, Session
    from app.services.auth_flow import verify_login_code

    user = await _insert_user(db_session, "verify@example.com", "active")
    code_row = await _insert_login_code(
        db_session, user.id, plaintext="123456", salt="a" * 32
    )

    raw_token = await verify_login_code(db_session, "verify@example.com", "123456")
    await db_session.commit()

    # Returned token: 43 chars, urlsafe b64 (no padding).
    assert isinstance(raw_token, str)
    assert len(raw_token) == 43
    assert "=" not in raw_token
    # urlsafe alphabet only
    import string

    allowed = set(string.ascii_letters + string.digits + "-_")
    assert set(raw_token) <= allowed

    # sessions row exists with token_hash = sha256(raw).
    sess = (
        await db_session.execute(
            select(Session).where(Session.token_hash == _sha256(raw_token))
        )
    ).scalar_one()
    assert sess.user_id == user.id

    # login_code used_at got stamped.
    refreshed = (
        await db_session.execute(select(LoginCode).where(LoginCode.id == code_row.id))
    ).scalar_one()
    assert refreshed.used_at is not None


# ===========================================================================
# 8. verify_login_code — wrong code → InvalidCodeError
# ===========================================================================


async def test_verify_login_code_wrong_code_raises_invalid(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import InvalidCodeError, verify_login_code

    user = await _insert_user(db_session, "wrong@example.com", "active")
    await _insert_login_code(db_session, user.id, plaintext="111111", salt="a" * 32)

    with pytest.raises(InvalidCodeError):
        await verify_login_code(db_session, "wrong@example.com", "999999")


# ===========================================================================
# 9. verify_login_code — expired code → InvalidCodeError
# ===========================================================================


async def test_verify_login_code_expired_raises_invalid(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import InvalidCodeError, verify_login_code

    user = await _insert_user(db_session, "expired@example.com", "active")
    past = _utcnow_naive() - timedelta(minutes=1)
    await _insert_login_code(
        db_session, user.id, plaintext="222222", salt="b" * 32, expires_at=past
    )

    with pytest.raises(InvalidCodeError):
        await verify_login_code(db_session, "expired@example.com", "222222")


# ===========================================================================
# 10. verify_login_code — code already used (used_at set) → InvalidCodeError
# ===========================================================================


async def test_verify_login_code_reused_raises_invalid(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import InvalidCodeError, verify_login_code

    user = await _insert_user(db_session, "reuse@example.com", "active")
    now = _utcnow_naive()
    await _insert_login_code(
        db_session,
        user.id,
        plaintext="333333",
        salt="c" * 32,
        used_at=now - timedelta(minutes=1),
    )

    with pytest.raises(InvalidCodeError):
        await verify_login_code(db_session, "reuse@example.com", "333333")


# ===========================================================================
# 11. verify_login_code — lockout after ≥5 expired-unused codes in 15 min window
# ===========================================================================


async def test_verify_login_code_lockout_after_5_wrong(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import InvalidCodeError, verify_login_code

    user = await _insert_user(db_session, "locked@example.com", "active")
    now = _utcnow_naive()

    # 5 login_codes rows, each created in the last 15 min and already expired,
    # used_at IS NULL — matches §4 Option B count.
    for i in range(5):
        await _insert_login_code(
            db_session,
            user.id,
            plaintext=f"44444{i}",
            salt=f"{i}" * 32,
            expires_at=now - timedelta(minutes=1),  # expired
            used_at=None,
            created_at=now - timedelta(minutes=5),  # within 15 min window
        )

    # And a 6th, still-fresh code with a known plaintext — even with the
    # correct code we MUST be locked out.
    await _insert_login_code(
        db_session,
        user.id,
        plaintext="999999",
        salt="z" * 32,
        expires_at=now + timedelta(minutes=10),
        used_at=None,
        created_at=now,
    )

    with pytest.raises(InvalidCodeError):
        await verify_login_code(db_session, "locked@example.com", "999999")


# ===========================================================================
# 12. approve_user — success: activates user + sends activation notice
# ===========================================================================


async def test_approve_user_success_activates_and_sends_notice(
    db_session, mailer_mocks, admin_email
):
    from app.models import ApprovalToken, User
    from app.services.auth_flow import approve_user

    user = await _insert_user(db_session, "approve@example.com", "pending")
    raw = "raw-approval-token-xyz"
    tok_row = await _insert_approval_token(db_session, user.id, raw_token=raw)

    returned = await approve_user(db_session, raw)
    await db_session.commit()

    # Returned user is active, approved_at set.
    assert returned.email == "approve@example.com"
    assert returned.status == "active"
    assert returned.approved_at is not None

    # Re-fetch and confirm persisted state.
    refreshed_user = (
        await db_session.execute(select(User).where(User.id == user.id))
    ).scalar_one()
    assert refreshed_user.status == "active"
    assert refreshed_user.approved_at is not None

    refreshed_tok = (
        await db_session.execute(
            select(ApprovalToken).where(ApprovalToken.id == tok_row.id)
        )
    ).scalar_one()
    assert refreshed_tok.used_at is not None

    # Activation notice went out.
    assert len(mailer_mocks["send_activation_notice"].calls) == 1
    args, kwargs = mailer_mocks["send_activation_notice"].calls[0]
    combined = list(args) + list(kwargs.values())
    assert "approve@example.com" in combined


# ===========================================================================
# 13. approve_user — expired token → ApprovalTokenError
# ===========================================================================


async def test_approve_user_expired_token_raises(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import ApprovalTokenError, approve_user

    user = await _insert_user(db_session, "exp@example.com", "pending")
    raw = "expired-approval-token"
    await _insert_approval_token(
        db_session,
        user.id,
        raw_token=raw,
        expires_at=_utcnow_naive() - timedelta(days=1),
    )

    with pytest.raises(ApprovalTokenError):
        await approve_user(db_session, raw)


# ===========================================================================
# 14. approve_user — token already used → ApprovalTokenError
# ===========================================================================


async def test_approve_user_reused_token_raises(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import ApprovalTokenError, approve_user

    user = await _insert_user(db_session, "used@example.com", "pending")
    raw = "already-used-approval-token"
    await _insert_approval_token(
        db_session,
        user.id,
        raw_token=raw,
        used_at=_utcnow_naive() - timedelta(minutes=1),
    )

    with pytest.raises(ApprovalTokenError):
        await approve_user(db_session, raw)


# ===========================================================================
# 15. approve_user — unknown token → ApprovalTokenError
# ===========================================================================


async def test_approve_user_unknown_token_raises(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import ApprovalTokenError, approve_user

    # No approval_tokens rows at all — any raw token must be rejected.
    with pytest.raises(ApprovalTokenError):
        await approve_user(db_session, "a-token-that-doesnt-exist-anywhere")


# ===========================================================================
# 16. validate_session_cookie — valid cookie → returns User
# ===========================================================================


async def test_validate_session_cookie_valid_returns_user(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import validate_session_cookie

    user = await _insert_user(db_session, "sess-valid@example.com", "active")
    raw = "raw-cookie-token-alpha"
    await _insert_session(db_session, user.id, raw_token=raw)

    result = await validate_session_cookie(db_session, raw)

    assert result is not None
    assert result.id == user.id
    assert result.email == "sess-valid@example.com"


# ===========================================================================
# 17. validate_session_cookie — missing/malformed/unknown → None, no raise
# ===========================================================================


async def test_validate_session_cookie_invalid_returns_none(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import validate_session_cookie

    # empty string
    assert await validate_session_cookie(db_session, "") is None
    # unknown token (no session row at all)
    assert await validate_session_cookie(db_session, "no-such-token") is None
    # obviously malformed garbage
    assert await validate_session_cookie(db_session, "!!!not-base64!!!") is None


# ===========================================================================
# 18. validate_session_cookie — expired session → None
# ===========================================================================


async def test_validate_session_cookie_expired_returns_none(
    db_session, mailer_mocks, admin_email
):
    from app.services.auth_flow import validate_session_cookie

    user = await _insert_user(db_session, "sess-exp@example.com", "active")
    raw = "raw-cookie-token-expired"
    await _insert_session(
        db_session,
        user.id,
        raw_token=raw,
        expires_at=_utcnow_naive() - timedelta(seconds=1),
    )

    assert await validate_session_cookie(db_session, raw) is None


# ===========================================================================
# 19. invalidate_session_cookie — removes matching row; second call is no-op
# ===========================================================================


async def test_invalidate_session_cookie_removes_row(
    db_session, mailer_mocks, admin_email
):
    from app.models import Session
    from app.services.auth_flow import invalidate_session_cookie

    user = await _insert_user(db_session, "logout@example.com", "active")
    raw = "raw-cookie-token-logout"
    await _insert_session(db_session, user.id, raw_token=raw)

    # First call removes it.
    result1 = await invalidate_session_cookie(db_session, raw)
    await db_session.commit()
    assert result1 is None

    remaining = (
        await db_session.execute(
            select(Session).where(Session.token_hash == _sha256(raw))
        )
    ).scalars().all()
    assert remaining == []

    # Second call is a silent no-op (no raise, still returns None).
    result2 = await invalidate_session_cookie(db_session, raw)
    await db_session.commit()
    assert result2 is None


# ===========================================================================
# 20. COOKIE_FLAGS / COOKIE_NAME constants pin HARNESS §4 policy
# ===========================================================================


async def test_cookie_flags_policy_documented():
    """The module must expose COOKIE_FLAGS and COOKIE_NAME with the exact
    values from design §2 / HARNESS §4. `secure=False` now; M5 flips it to
    True once the deployment is behind HTTPS."""
    from app.services import auth_flow

    assert auth_flow.COOKIE_FLAGS == {
        "httponly": True,
        "samesite": "lax",
        "secure": False,
    }
    assert auth_flow.COOKIE_NAME == "method_session"


# ===========================================================================
# 21. Additional — email normalised to lowercase on write (§8 field mapping)
# ===========================================================================


async def test_request_login_code_email_normalized_to_lowercase(
    db_session, mailer_mocks, admin_email
):
    from app.models import User
    from app.services.auth_flow import request_login_code

    # First submission with mixed case.
    result1 = await request_login_code(db_session, "Alice@Example.COM")
    await db_session.commit()
    assert result1 == "pending"

    users = (await db_session.execute(select(User))).scalars().all()
    # exactly one user row and its email is stored lowercase
    assert len(users) == 1
    assert users[0].email == "alice@example.com"

    # Second submission with different casing — must NOT create a duplicate.
    result2 = await request_login_code(db_session, "ALICE@example.com")
    await db_session.commit()
    # Still one user row (same user, still pending per design §5 row-2).
    users2 = (await db_session.execute(select(User))).scalars().all()
    assert len(users2) == 1
    assert users2[0].id == users[0].id
    assert result2 == "pending"


# ===========================================================================
# 22. Additional — admin self-bootstrap: users.status == "active" at creation,
#     approved_at set; no separate approve_user call needed.
# ===========================================================================


async def test_approve_user_on_admin_self_bootstrap_creates_active_user(
    db_session, mailer_mocks, admin_email
):
    """Design §5 row-4 + §2 docstring: admin email submits on a cold DB →
    `users.status='active'` and `approved_at` set in one step."""
    from app.models import User
    from app.services.auth_flow import request_login_code

    result = await request_login_code(db_session, admin_email)
    await db_session.commit()

    assert result == "sent"

    user = (
        await db_session.execute(select(User).where(User.email == admin_email))
    ).scalar_one()
    assert user.status == "active"
    assert user.approved_at is not None

    # No approval_request email for the admin — they're bootstrapping themselves.
    assert mailer_mocks["send_approval_request"].calls == []


# ===========================================================================
# 23. Additional — module does NOT commit; caller owns the transaction.
# ===========================================================================


async def test_module_does_not_commit_caller_owns_transaction(
    db_session, mailer_mocks, admin_email
):
    """Per design §12: this module only `flush`es, it does NOT `commit`. After
    a call returns, the session must still be in an uncommitted state so the
    router (caller) can roll back on downstream failure."""
    from app.services.auth_flow import request_login_code

    await request_login_code(db_session, "not-committed@example.com")

    # The session must either still be in an active transaction OR have
    # pending identity-map changes — either of which proves `commit()` was NOT
    # called inside the service. A clean, committed session would show
    # `in_transaction() is False` AND no `dirty`/`new`/`deleted`.
    dirty_or_new = (
        bool(db_session.new) or bool(db_session.dirty) or bool(db_session.deleted)
    )
    assert db_session.in_transaction() or dirty_or_new, (
        "auth_flow must not commit — caller owns the transaction boundary"
    )


# ===========================================================================
# 24. request_login_code — email domain in AUTO_APPROVED_DOMAINS auto-activates
# ===========================================================================


@pytest_asyncio.fixture
async def auto_approved_xvc(monkeypatch):
    """Pin settings.auto_approved_domains to 'xvc.com,projectstar.ai' for the test."""
    from app import config as config_mod

    monkeypatch.setattr(
        config_mod.settings, "auto_approved_domains", "xvc.com,projectstar.ai"
    )


async def test_request_login_code_xvc_domain_auto_activates(
    db_session, mailer_mocks, admin_email, auto_approved_xvc
):
    from app.models import LoginCode, User
    from app.services.auth_flow import request_login_code

    result = await request_login_code(db_session, "alice@xvc.com")
    await db_session.commit()

    assert result == "sent"

    user = (
        await db_session.execute(select(User).where(User.email == "alice@xvc.com"))
    ).scalar_one()
    assert user.status == "active"
    assert user.approved_at is not None

    codes = (
        await db_session.execute(select(LoginCode).where(LoginCode.user_id == user.id))
    ).scalars().all()
    assert len(codes) == 1

    # Must NOT trigger the admin-approval mail.
    assert mailer_mocks["send_approval_request"].calls == []
    # Must send the user a login code.
    assert len(mailer_mocks["send_login_code"].calls) == 1


async def test_request_login_code_projectstar_domain_auto_activates(
    db_session, mailer_mocks, admin_email, auto_approved_xvc
):
    from app.models import User
    from app.services.auth_flow import request_login_code

    # Case-insensitivity: domain written in mixed case.
    result = await request_login_code(db_session, "bob@ProjectStar.AI")
    await db_session.commit()

    assert result == "sent"
    user = (
        await db_session.execute(
            select(User).where(User.email == "bob@projectstar.ai")
        )
    ).scalar_one()
    assert user.status == "active"
    assert mailer_mocks["send_approval_request"].calls == []


async def test_request_login_code_non_approved_domain_still_pending(
    db_session, mailer_mocks, admin_email, auto_approved_xvc
):
    """Regression: only whitelisted domains auto-approve; everyone else stays pending."""
    from app.models import User
    from app.services.auth_flow import request_login_code

    result = await request_login_code(db_session, "random@gmail.com")
    await db_session.commit()

    assert result == "pending"
    user = (
        await db_session.execute(
            select(User).where(User.email == "random@gmail.com")
        )
    ).scalar_one()
    assert user.status == "pending"
    # Admin gets the approval request; user does NOT get a login code.
    assert len(mailer_mocks["send_approval_request"].calls) == 1
    assert mailer_mocks["send_login_code"].calls == []
