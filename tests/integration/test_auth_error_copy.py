"""Integration tests for Issue #5: auth routes must return ``{error, message}``.

Contract: ``docs/design/issue-5-error-copy.md`` §3.2, §4.1, §5.

All 4xx/5xx JSON error bodies from ``app/routers/auth.py`` must contain BOTH:

  - ``error`` — the machine code (backwards-compatible with existing clients)
  - ``message`` — the Chinese end-user copy from design §4.1 (verbatim)

These tests are RED until the auth routes are migrated. The design table is
the single source of truth; strings below MUST match §4.1 character for
character.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Expected Chinese copy — transcribed verbatim from design §4.1.
# ---------------------------------------------------------------------------

MSG_RATE_LIMIT = "请求过于频繁，请稍后再试"
MSG_MAIL_SEND_FAILED = "验证码邮件发送失败，请稍后重试"
MSG_INVALID_OR_EXPIRED = "验证码无效或已过期，请重新获取"
MSG_UNAUTHENTICATED = "登录已过期，请刷新页面重新登录"
MSG_BAD_ORIGIN = "请求来源校验失败，请刷新页面重试"


# ===========================================================================
# 1. rate_limit (auth.py:164) — 429
# ===========================================================================


async def test_request_code_rate_limit_returns_error_and_message(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    """Design §4.1: rate_limit / 429 / "请求过于频繁，请稍后再试"."""
    user = await seeded_user("rl@example.com", status="active")
    # Pre-seed a fresh code so the 60s window fires on this request.
    await seed_login_code(user.id, plaintext="000000")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "rl@example.com"}
    )
    assert resp.status_code == 429
    body = resp.json()
    assert body.get("error") == "rate_limit"
    assert body.get("message") == MSG_RATE_LIMIT, (
        f"expected message={MSG_RATE_LIMIT!r}, got {body!r}"
    )


# ===========================================================================
# 2. mail_send_failed (auth.py:170) — 503
# ===========================================================================


async def test_request_code_mail_send_failed_returns_error_and_message(
    app_client, failing_login_mailer, pinned_admin_email, seeded_user
):
    """Design §4.1: mail_send_failed / 503 / "验证码邮件发送失败，请稍后重试"."""
    await seeded_user("smtpfail@example.com", status="active")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "smtpfail@example.com"}
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body.get("error") == "mail_send_failed"
    assert body.get("message") == MSG_MAIL_SEND_FAILED, (
        f"expected message={MSG_MAIL_SEND_FAILED!r}, got {body!r}"
    )


# ===========================================================================
# 3. invalid_or_expired (auth.py:197) — 400
# ===========================================================================


async def test_verify_code_invalid_returns_error_and_message(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    """Design §4.1: invalid_or_expired / 400 / "验证码无效或已过期，请重新获取"."""
    user = await seeded_user("bad@example.com", status="active")
    await seed_login_code(user.id, plaintext="111111")

    resp = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "bad@example.com", "code": "999999"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "invalid_or_expired"
    assert body.get("message") == MSG_INVALID_OR_EXPIRED, (
        f"expected message={MSG_INVALID_OR_EXPIRED!r}, got {body!r}"
    )


async def test_verify_code_expired_returns_error_and_message(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    """Expired code hits the same error code/message."""
    user = await seeded_user("exp@example.com", status="active")
    past = _utcnow_naive() - timedelta(minutes=5)
    await seed_login_code(user.id, plaintext="222222", expires_at=past)

    resp = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "exp@example.com", "code": "222222"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "invalid_or_expired"
    assert body.get("message") == MSG_INVALID_OR_EXPIRED


# ===========================================================================
# 4. unauthenticated (auth.py:276) — 401
#    Triggered by hitting an auth-gated endpoint without a session cookie.
# ===========================================================================


async def test_unauthenticated_returns_error_and_message(app_client):
    """Design §4.1: unauthenticated / 401 / "登录已过期，请刷新页面重新登录".

    No session cookie → middleware/dependency raises ``_Unauthenticated``
    → exception handler returns 401 with the canonical body.
    """
    # /api/research requires auth. A POST without a cookie trips the handler.
    app_client.cookies.clear()
    resp = await app_client.post("/api/research", data={"question": "Q?"})
    assert resp.status_code == 401
    body = resp.json()
    assert body.get("error") == "unauthenticated"
    assert body.get("message") == MSG_UNAUTHENTICATED, (
        f"expected message={MSG_UNAUTHENTICATED!r}, got {body!r}"
    )


# ===========================================================================
# 5. bad_origin (auth.py:289) — 403
# ===========================================================================


async def test_bad_origin_returns_error_and_message(
    app_client, mailer_mocks, pinned_admin_email
):
    """Design §4.1: bad_origin / 403 / "请求来源校验失败，请刷新页面重试"."""
    resp = await app_client.post(
        "/api/auth/request_code",
        json={"email": "xorigin@example.com"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body.get("error") == "bad_origin"
    assert body.get("message") == MSG_BAD_ORIGIN, (
        f"expected message={MSG_BAD_ORIGIN!r}, got {body!r}"
    )


# ===========================================================================
# 6. BC contract: `error` field is never dropped or renamed (design §3.2).
# ===========================================================================


async def test_error_field_still_present_after_message_addition(
    app_client, mailer_mocks, pinned_admin_email
):
    """The ``error`` machine code MUST remain in the response body even after
    ``message`` is added. Design §3.2 BC contract.
    """
    resp = await app_client.post(
        "/api/auth/request_code",
        json={"email": "bc@example.com"},
        headers={"Origin": "http://evil.example.com"},
    )
    body = resp.json()
    # Must contain BOTH keys — not replaced.
    assert "error" in body, f"BC broken: 'error' missing from {body!r}"
    assert "message" in body, f"message missing from {body!r}"
