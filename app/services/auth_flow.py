"""Pure auth-logic service for Method (Task 2.3).

Implements the spec §4 state machine as coroutines over an ``AsyncSession``.
The module owns all secret-touching logic (login-code, approval-token, and
session-token generation + hashing) and exposes a small typed API that the
FastAPI routers in Task 2.4 consume.

Contract summary (see ``docs/design/issue-1-task-2.3-auth-flow.md``):

- No ``session.commit()`` — caller owns the transaction boundary. Each
  function calls ``session.flush()`` so rows get IDs assigned for downstream
  work inside the same transaction.
- All timestamps are naive UTC (``datetime.now(UTC).replace(tzinfo=None)``)
  via ``_utcnow()``.
- Email is normalised (``.strip().lower()``) at the public entry points.
- Raw tokens / codes are never logged. Log records include at most an
  8-char hash prefix for traceability.
- Mailer call-seams (``send_login_code``, ``send_approval_request``,
  ``send_activation_notice``) are imported at module level so tests can
  monkeypatch them at ``app.services.auth_flow.<name>``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config
from app.models import ApprovalToken, LoginCode, Session, User
from app.services.mailer import (
    MailerError,  # noqa: F401  (re-exported for router-side isinstance checks)
    send_activation_notice,
    send_approval_request,
    send_login_code,
)

__all__ = [
    "ApprovalTokenError",
    "AuthError",
    "COOKIE_FLAGS",
    "COOKIE_NAME",
    "InvalidCodeError",
    "MailerError",
    "RateLimitError",
    "approve_user",
    "invalidate_session_cookie",
    "request_login_code",
    "send_activation_notice",
    "send_approval_request",
    "send_login_code",
    "validate_session_cookie",
    "verify_login_code",
]

logger = logging.getLogger("method.auth_flow")


# ---------------------------------------------------------------------------
# Exceptions (design §2)
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class for auth_flow errors."""


class RateLimitError(AuthError):
    """Raised when request_login_code is called again within the 60s window."""


class InvalidCodeError(AuthError):
    """Raised when verify_login_code cannot match / user locked out / expired."""


class ApprovalTokenError(AuthError):
    """Raised when approve_user is called with a bad / expired / used token."""


# ---------------------------------------------------------------------------
# Policy constants (HARNESS §4)
# ---------------------------------------------------------------------------


COOKIE_FLAGS: dict = {"httponly": True, "samesite": "lax", "secure": False}
COOKIE_NAME: str = "method_session"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_RATE_LIMIT_SECONDS = 60
_LOCKOUT_WINDOW_MIN = 15
_LOCKOUT_THRESHOLD = 5


def _utcnow() -> datetime:
    """Return a naive-UTC datetime (design §7)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(text: str) -> str:
    """Return lowercase 64-char hex sha256 digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_prefix(h: str) -> str:
    """First 8 hex chars of a sha256 hash — safe to log."""
    return h[:8]


def _normalize_email(email: str) -> str:
    """Strip whitespace and lowercase — applied at every public entry."""
    return email.strip().lower()


def _gen_login_code() -> str:
    """Return a 6-digit zero-padded numeric code (20 bits entropy)."""
    return f"{secrets.randbelow(10**6):06d}"


def _gen_salt() -> str:
    """Return a 32-char hex salt for per-row login-code salting."""
    return secrets.token_hex(16)


def _gen_raw_token() -> str:
    """Return a 43-char urlsafe-b64 token (32 bytes of entropy, no padding)."""
    return secrets.token_urlsafe(32).rstrip("=")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def request_login_code(
    session: AsyncSession, email: str,
) -> Literal["sent", "pending", "rejected"]:
    """Drive the spec §4.1 request path.

    Branches (see design §5):

    - admin bootstrap: no user AND email == admin_email → create active user
      AND issue login code → ``"sent"``.
    - new user: no user AND not admin → create pending user + approval_token,
      email admin → ``"pending"``.
    - existing pending/rejected: return current status, no side effects.
    - existing active: rate-limit check, then issue login code → ``"sent"``.

    Raises:
        RateLimitError: active user asked for another code within 60s.
    """
    addr = _normalize_email(email)
    admin = _config.settings.admin_email.strip().lower()

    # Look up existing user by normalised email.
    existing = (
        await session.execute(select(User).where(User.email == addr))
    ).scalar_one_or_none()

    if existing is None:
        if addr == admin:
            # Admin self-bootstrap: create active user + issue login code.
            now = _utcnow()
            user = User(
                email=addr,
                status="active",
                created_at=now,
                approved_at=now,
            )
            session.add(user)
            await session.flush()
            await _issue_login_code(session, user)
            return "sent"

        # New (non-admin) user → pending + approval token + admin email.
        now = _utcnow()
        user = User(
            email=addr,
            status="pending",
            created_at=now,
            approved_at=None,
        )
        session.add(user)
        await session.flush()

        raw_token = _gen_raw_token()
        token_hash = _sha256(raw_token)
        approval = ApprovalToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=now + timedelta(days=_config.settings.approval_token_ttl_days),
            used_at=None,
        )
        session.add(approval)
        await session.flush()

        approve_url = (
            f"{_config.settings.base_url.rstrip('/')}/admin/approve?token={raw_token}"
        )
        logger.info(
            "auth_flow.request_login_code new_user email=%s approval_hash_prefix=%s",
            addr,
            _hash_prefix(token_hash),
        )
        await send_approval_request(admin, addr, approve_url)
        return "pending"

    # Existing user branches.
    if existing.status == "pending":
        logger.info(
            "auth_flow.request_login_code pending_resubmit email=%s", addr
        )
        return "pending"

    if existing.status == "rejected":
        logger.info(
            "auth_flow.request_login_code rejected email=%s", addr
        )
        return "rejected"

    # Active user — apply 60s rate limit before issuing a new code.
    cutoff = _utcnow() - timedelta(seconds=_RATE_LIMIT_SECONDS)
    recent = (
        await session.execute(
            select(LoginCode.id)
            .where(LoginCode.user_id == existing.id)
            .where(LoginCode.created_at > cutoff)
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent is not None:
        logger.warning(
            "auth_flow.request_login_code rate_limited email=%s", addr
        )
        raise RateLimitError("login code requested too recently")

    await _issue_login_code(session, existing)
    return "sent"


async def _issue_login_code(session: AsyncSession, user: User) -> None:
    """Create a login_codes row for ``user`` and dispatch the mail."""
    now = _utcnow()
    code = _gen_login_code()
    salt = _gen_salt()
    code_hash = _sha256(code + salt)
    row = LoginCode(
        user_id=user.id,
        code_hash=code_hash,
        salt=salt,
        expires_at=now + timedelta(minutes=_config.settings.login_code_ttl_min),
        used_at=None,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "auth_flow.issue_login_code email=%s code_hash_prefix=%s",
        user.email,
        _hash_prefix(code_hash),
    )
    # Mailer is the last step — if it raises, the caller rolls back the txn
    # and the login_codes row never becomes visible.
    await send_login_code(user.email, code)


async def verify_login_code(
    session: AsyncSession, email: str, code: str,
) -> str:
    """Verify a submitted login code and mint a session.

    Returns the raw (unhashed) session cookie token — 43 urlsafe chars.
    Raises ``InvalidCodeError`` for any non-success path (wrong code, expired,
    already-used, user not active, user locked out). The error message is
    deliberately non-specific so the router cannot leak enumeration signals.
    """
    addr = _normalize_email(email)

    user = (
        await session.execute(select(User).where(User.email == addr))
    ).scalar_one_or_none()
    if user is None or user.status != "active":
        raise InvalidCodeError("invalid_or_expired")

    now = _utcnow()

    # Lockout check — design §4 Option B.
    lockout_cutoff = now - timedelta(minutes=_LOCKOUT_WINDOW_MIN)
    failed_rows = (
        await session.execute(
            select(LoginCode.id)
            .where(LoginCode.user_id == user.id)
            .where(LoginCode.used_at.is_(None))
            .where(LoginCode.expires_at < now)
            .where(LoginCode.created_at > lockout_cutoff)
        )
    ).scalars().all()
    if len(failed_rows) >= _LOCKOUT_THRESHOLD:
        logger.warning(
            "auth_flow.verify_login_code lockout email=%s count=%d",
            addr,
            len(failed_rows),
        )
        raise InvalidCodeError("invalid_or_expired")

    # Fetch candidate rows: unused and unexpired.
    candidates = (
        await session.execute(
            select(LoginCode)
            .where(LoginCode.user_id == user.id)
            .where(LoginCode.used_at.is_(None))
            .where(LoginCode.expires_at > now)
        )
    ).scalars().all()

    matched: LoginCode | None = None
    for row in candidates:
        expected = _sha256(code + row.salt)
        if hmac.compare_digest(row.code_hash, expected):
            matched = row
            break

    if matched is None:
        raise InvalidCodeError("invalid_or_expired")

    # Mark the code used and mint a session token.
    matched.used_at = now
    raw_token = _gen_raw_token()
    token_hash = _sha256(raw_token)
    sess = Session(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=now + timedelta(days=_config.settings.session_ttl_days),
        created_at=now,
    )
    session.add(sess)
    await session.flush()

    logger.info(
        "auth_flow.verify_login_code ok email=%s session_hash_prefix=%s",
        addr,
        _hash_prefix(token_hash),
    )
    return raw_token


async def approve_user(session: AsyncSession, raw_token: str) -> User:
    """Mark a user active using the admin-link approval token.

    Raises ``ApprovalTokenError`` if the token is unknown, expired, or
    already used. On success, returns the updated ``User``.
    """
    if not raw_token:
        raise ApprovalTokenError("invalid_or_expired")

    token_hash = _sha256(raw_token)
    now = _utcnow()

    tok = (
        await session.execute(
            select(ApprovalToken).where(ApprovalToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if tok is None:
        raise ApprovalTokenError("invalid_or_expired")
    if tok.used_at is not None:
        raise ApprovalTokenError("invalid_or_expired")
    if tok.expires_at <= now:
        raise ApprovalTokenError("invalid_or_expired")

    user = (
        await session.execute(select(User).where(User.id == tok.user_id))
    ).scalar_one_or_none()
    if user is None:
        raise ApprovalTokenError("invalid_or_expired")

    user.status = "active"
    user.approved_at = now
    tok.used_at = now
    await session.flush()

    logger.info(
        "auth_flow.approve_user email=%s token_hash_prefix=%s",
        user.email,
        _hash_prefix(token_hash),
    )
    await send_activation_notice(user.email)
    return user


async def validate_session_cookie(
    session: AsyncSession, raw_token: str,
) -> User | None:
    """Return the owning ``User`` if the cookie is valid, else ``None``.

    Never raises: the router treats ``None`` as "redirect to /login", so
    empty / malformed / unknown / expired tokens all collapse to ``None``.
    """
    if not raw_token:
        return None

    token_hash = _sha256(raw_token)
    now = _utcnow()

    sess = (
        await session.execute(
            select(Session).where(Session.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if sess is None:
        return None
    if sess.expires_at <= now:
        return None

    user = (
        await session.execute(select(User).where(User.id == sess.user_id))
    ).scalar_one_or_none()
    return user


async def invalidate_session_cookie(
    session: AsyncSession, raw_token: str,
) -> None:
    """Delete the matching sessions row. No-op if missing. Never raises."""
    if not raw_token:
        return None

    token_hash = _sha256(raw_token)
    await session.execute(
        delete(Session).where(Session.token_hash == token_hash)
    )
    await session.flush()
    return None
