"""Integration tests for the auth HTTP boundary (Task 2.4).

Written from ``docs/design/issue-1-task-2.4-auth-routes.md`` (authoritative
contract) and ``docs/design/issue-1-task-2.3-auth-flow.md`` (the upstream
service contract whose exceptions the routers translate to HTTP). These tests
are RED until ``app/routers/auth.py`` / ``app/routers/admin.py`` /
``app/main.py`` / the four templates exist and are wired together.

Fixtures live in ``tests/integration/conftest.py``. We deliberately do NOT
use the shared ``db_session`` fixture from the top-level conftest because it
would reset the async engine after ``app_client`` already built it.

Coverage binding (design §9 / dispatcher audit):

- ``app/routers/auth.py``    → tests 1-13, 18, 19, 20, 21, 22, 23, 24
- ``app/routers/admin.py``   → tests 14, 15, 16, 17
- ``app/templates/login.html``          → test 18
- ``app/templates/approved.html``       → test 14
- ``app/templates/approval_error.html`` → tests 15, 16, 17
- ``app/templates/landing.html``        → test 20
- ``app/templates/base.html``           → transitively via every HTML test
- ``app/main.py`` wiring → transitively via every routed test
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_set_cookie(raw_header: str) -> dict:
    """Parse a single Set-Cookie header value into a dict.

    Keys are lower-cased; attribute-only tokens like ``HttpOnly`` and
    ``Secure`` map to ``True``. The first segment is kept as ``("_name", value)``.
    """
    parts = [p.strip() for p in raw_header.split(";") if p.strip()]
    assert parts, f"empty Set-Cookie header: {raw_header!r}"
    name, _, value = parts[0].partition("=")
    out: dict = {"_name": name, "_value": value}
    for attr in parts[1:]:
        if "=" in attr:
            k, _, v = attr.partition("=")
            out[k.strip().lower()] = v.strip()
        else:
            out[attr.strip().lower()] = True
    return out


# ===========================================================================
# 1. POST /api/auth/request_code — new user → pending + admin email sent
# ===========================================================================


async def test_post_request_code_new_user_returns_pending_and_sends_admin_email(
    app_client, mailer_mocks, pinned_admin_email, integration_db
):
    from app.models import ApprovalToken, User

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "newcomer@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}

    # admin was emailed exactly once
    assert len(mailer_mocks["send_approval_request"].calls) == 1
    args, _kwargs = mailer_mocks["send_approval_request"].calls[0]
    # expected signature: send_approval_request(admin_email, user_email, approve_url)
    assert args[0] == pinned_admin_email
    assert args[1] == "newcomer@example.com"
    assert "/admin/approve?token=" in args[2]

    # DB rows inserted
    user = (
        await integration_db.execute(
            select(User).where(User.email == "newcomer@example.com")
        )
    ).scalar_one()
    assert user.status == "pending"

    approval = (
        await integration_db.execute(
            select(ApprovalToken).where(ApprovalToken.user_id == user.id)
        )
    ).scalar_one()
    assert approval.used_at is None

    # login-code mailer NOT invoked for pending branch
    assert mailer_mocks["send_login_code"].calls == []


# ===========================================================================
# 2. POST /api/auth/request_code — active user → sent + login_code email
# ===========================================================================


async def test_post_request_code_active_user_returns_sent(
    app_client, mailer_mocks, pinned_admin_email, seeded_user
):
    await seeded_user("active@example.com", status="active")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "active@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "sent"}

    # login_code email sent exactly once, to the user, with a 6-digit code
    assert len(mailer_mocks["send_login_code"].calls) == 1
    args, _kwargs = mailer_mocks["send_login_code"].calls[0]
    assert args[0] == "active@example.com"
    assert isinstance(args[1], str)
    assert len(args[1]) == 6 and args[1].isdigit()

    # admin email NOT invoked for active-user branch
    assert mailer_mocks["send_approval_request"].calls == []


# ===========================================================================
# 3. POST /api/auth/request_code — admin self-registration short-circuits to sent
# ===========================================================================


async def test_post_request_code_admin_self_registration_returns_sent(
    app_client, mailer_mocks, pinned_admin_email, integration_db
):
    from app.models import User

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": pinned_admin_email}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "sent"}

    # Admin was created active, not pending
    user = (
        await integration_db.execute(
            select(User).where(User.email == pinned_admin_email)
        )
    ).scalar_one()
    assert user.status == "active"
    assert user.approved_at is not None

    # login_code mailer called (not approval_request)
    assert len(mailer_mocks["send_login_code"].calls) == 1
    assert mailer_mocks["send_approval_request"].calls == []


# ===========================================================================
# 4. POST /api/auth/request_code — pending user → pending, no email
# ===========================================================================


async def test_post_request_code_pending_user_returns_pending(
    app_client, mailer_mocks, pinned_admin_email, seeded_user
):
    await seeded_user("pending@example.com", status="pending")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "pending@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}

    # No email dispatched (design §5 row-2: existing pending → no side effects)
    assert mailer_mocks["send_login_code"].calls == []
    assert mailer_mocks["send_approval_request"].calls == []
    assert mailer_mocks["send_activation_notice"].calls == []


# ===========================================================================
# 5. POST /api/auth/request_code — rejected user → rejected, no email
# ===========================================================================


async def test_post_request_code_rejected_user_returns_rejected(
    app_client, mailer_mocks, pinned_admin_email, seeded_user
):
    await seeded_user("rejected@example.com", status="rejected")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "rejected@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "rejected"}

    assert mailer_mocks["send_login_code"].calls == []
    assert mailer_mocks["send_approval_request"].calls == []


# ===========================================================================
# 6. POST /api/auth/request_code — 60s rate-limit → 429
# ===========================================================================


async def test_post_request_code_rate_limit_returns_429(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    user = await seeded_user("busy@example.com", status="active")
    # Pre-seed a fresh login_code (created_at = now) to trigger the 60s window.
    await seed_login_code(user.id, plaintext="000000")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "busy@example.com"}
    )
    assert resp.status_code == 429
    # Issue #5: body now contains both `error` (BC machine code) + `message`.
    assert resp.json().get("error") == "rate_limit"

    # No new login-code email triggered by this blocked request
    assert mailer_mocks["send_login_code"].calls == []


# ===========================================================================
# 7. POST /api/auth/request_code — mailer failure → 503 + rollback
# ===========================================================================


async def test_post_request_code_mailer_failure_returns_503_and_rolls_back(
    app_client, failing_login_mailer, pinned_admin_email, seeded_user, integration_db
):
    from app.models import LoginCode

    user = await seeded_user("mailfail@example.com", status="active")

    resp = await app_client.post(
        "/api/auth/request_code", json={"email": "mailfail@example.com"}
    )
    assert resp.status_code == 503
    assert resp.json().get("error") == "mail_send_failed"

    # Transaction must have been rolled back → no login_codes row persisted.
    rows = (
        await integration_db.execute(
            select(LoginCode).where(LoginCode.user_id == user.id)
        )
    ).scalars().all()
    assert rows == []


# ===========================================================================
# 8. POST /api/auth/verify_code — correct code → cookie + {"ok": true}
# ===========================================================================


async def test_post_verify_code_correct_sets_cookie_returns_ok(
    app_client, mailer_mocks, pinned_admin_email, seeded_user
):
    await seeded_user("verifyme@example.com", status="active")

    # First request_code to provision a real (unknown-plaintext) code, then
    # reach into the recorded mailer call to get the 6-digit plaintext.
    r1 = await app_client.post(
        "/api/auth/request_code", json={"email": "verifyme@example.com"}
    )
    assert r1.status_code == 200, r1.text
    assert len(mailer_mocks["send_login_code"].calls) == 1
    (_to, code), _kwargs = mailer_mocks["send_login_code"].calls[0]

    r2 = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "verifyme@example.com", "code": code},
    )
    assert r2.status_code == 200
    assert r2.json() == {"ok": True}

    # Set-Cookie header present, name = method_session
    raw_cookie = r2.headers.get("set-cookie")
    assert raw_cookie is not None, "verify_code should issue a Set-Cookie header"
    parsed = _parse_set_cookie(raw_cookie)
    assert parsed["_name"] == "method_session"
    assert parsed["_value"]  # non-empty


# ===========================================================================
# 9. POST /api/auth/verify_code — wrong code → 400
# ===========================================================================


async def test_post_verify_code_wrong_returns_400(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    user = await seeded_user("wrongcode@example.com", status="active")
    # Seed a real, unexpired code. We'll submit a DIFFERENT value.
    await seed_login_code(user.id, plaintext="111111")

    resp = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "wrongcode@example.com", "code": "222222"},
    )
    assert resp.status_code == 400
    assert resp.json().get("error") == "invalid_or_expired"


# ===========================================================================
# 10. POST /api/auth/verify_code — expired code → 400
# ===========================================================================


async def test_post_verify_code_expired_returns_400(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    user = await seeded_user("expired@example.com", status="active")
    # Seed code that expired 1 minute ago.
    past = _utcnow_naive() - timedelta(minutes=1)
    await seed_login_code(user.id, plaintext="333333", expires_at=past)

    resp = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "expired@example.com", "code": "333333"},
    )
    assert resp.status_code == 400
    assert resp.json().get("error") == "invalid_or_expired"


# ===========================================================================
# 11. POST /api/auth/verify_code — already-used code → 400
# ===========================================================================


async def test_post_verify_code_reused_returns_400(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_login_code
):
    user = await seeded_user("reused@example.com", status="active")
    await seed_login_code(
        user.id, plaintext="444444", used_at=_utcnow_naive()
    )

    resp = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "reused@example.com", "code": "444444"},
    )
    assert resp.status_code == 400
    assert resp.json().get("error") == "invalid_or_expired"


# ===========================================================================
# 12. POST /api/auth/logout — clears cookie + deletes sessions row
# ===========================================================================


async def test_post_logout_clears_cookie_and_deletes_session(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_session, integration_db
):
    from app.models import Session as SessionRow

    user = await seeded_user("logout@example.com", status="active")
    _row, raw = await seed_session(user.id, raw_token="logout-raw-token")

    # Attach the cookie and call logout.
    app_client.cookies.set("method_session", raw)
    resp = await app_client.post("/api/auth/logout")
    # Reset cookie jar so subsequent tests aren't polluted.
    app_client.cookies.clear()

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # The Set-Cookie header should clear the cookie.
    raw_header = resp.headers.get("set-cookie")
    assert raw_header is not None
    parsed = _parse_set_cookie(raw_header)
    assert parsed["_name"] == "method_session"
    assert parsed["_value"] == ""
    # Max-Age=0 marks the cookie for immediate deletion.
    assert parsed.get("max-age") == "0"

    # Sessions row deleted.
    remaining = (
        await integration_db.execute(
            select(SessionRow).where(SessionRow.token_hash == _sha256(raw))
        )
    ).scalar_one_or_none()
    assert remaining is None


# ===========================================================================
# 13. POST /api/auth/logout — no session → 401
# ===========================================================================


async def test_post_logout_without_session_returns_401(app_client):
    # Make sure no cookie leaks from a prior test.
    app_client.cookies.clear()
    resp = await app_client.post("/api/auth/logout")
    assert resp.status_code == 401
    assert resp.json().get("error") == "unauthenticated"


# ===========================================================================
# 14. GET /admin/approve — valid token → activates user + renders approved.html
# ===========================================================================


async def test_get_admin_approve_valid_token_activates_user_and_renders_approved(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_approval_token, integration_db
):
    from app.models import ApprovalToken, User

    user = await seeded_user("tobeapproved@example.com", status="pending")
    _tok, raw = await seed_approval_token(user.id, raw_token="valid-raw-token")
    user_id = user.id
    tok_id = _tok.id

    resp = await app_client.get("/admin/approve", params={"token": raw})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # Template context: email rendered into page body.
    assert "tobeapproved@example.com" in resp.text

    # DB reflects activation.
    integration_db.expire_all()
    refreshed_user = (
        await integration_db.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert refreshed_user.status == "active"
    assert refreshed_user.approved_at is not None

    refreshed_tok = (
        await integration_db.execute(
            select(ApprovalToken).where(ApprovalToken.id == tok_id)
        )
    ).scalar_one()
    assert refreshed_tok.used_at is not None

    # Activation notice sent.
    assert len(mailer_mocks["send_activation_notice"].calls) == 1


# ===========================================================================
# 15. GET /admin/approve — expired token → approval_error.html
# ===========================================================================


async def test_get_admin_approve_expired_token_renders_error(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_approval_token, integration_db
):
    from app.models import User

    user = await seeded_user("expired-tok@example.com", status="pending")
    past = _utcnow_naive() - timedelta(days=1)
    _tok, raw = await seed_approval_token(
        user.id, raw_token="expired-raw-token", expires_at=past
    )
    user_id = user.id

    resp = await app_client.get("/admin/approve", params={"token": raw})
    # Per design §2.4: HTML success-code even on error — it's a human-facing page.
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # User should NOT be activated.
    integration_db.expire_all()
    refreshed = (
        await integration_db.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert refreshed.status == "pending"
    # Activation notice NOT sent.
    assert mailer_mocks["send_activation_notice"].calls == []


# ===========================================================================
# 16. GET /admin/approve — already-used token → approval_error.html
# ===========================================================================


async def test_get_admin_approve_used_token_renders_error(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_approval_token, integration_db
):
    from app.models import User

    user = await seeded_user("used-tok@example.com", status="pending")
    _tok, raw = await seed_approval_token(
        user.id, raw_token="used-raw-token", used_at=_utcnow_naive()
    )
    user_id = user.id

    resp = await app_client.get("/admin/approve", params={"token": raw})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")

    integration_db.expire_all()
    refreshed = (
        await integration_db.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert refreshed.status == "pending"
    assert mailer_mocks["send_activation_notice"].calls == []


# ===========================================================================
# 17. GET /admin/approve — unknown token → approval_error.html
# ===========================================================================


async def test_get_admin_approve_unknown_token_renders_error(
    app_client, mailer_mocks, pinned_admin_email
):
    resp = await app_client.get(
        "/admin/approve", params={"token": "totally-bogus-token-nobody-has"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert mailer_mocks["send_activation_notice"].calls == []


# ===========================================================================
# 18. GET /login — renders login template
# ===========================================================================


async def test_get_login_renders_login_template(app_client):
    resp = await app_client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "<title>" in body and "Method" in body
    # Login form must have an email input (spec §7.2 A)
    assert "email" in body.lower()
    assert "<input" in body.lower() or "<form" in body.lower()


# ===========================================================================
# 19. GET / — anonymous → 303 redirect to /login
# ===========================================================================


async def test_get_root_redirects_to_login_when_not_authed(app_client):
    app_client.cookies.clear()
    resp = await app_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/login"


# ===========================================================================
# 20. GET / — authed → 200 with landing page showing user email
# ===========================================================================


async def test_get_root_renders_placeholder_when_authed(
    app_client, mailer_mocks, pinned_admin_email, seeded_user, seed_session
):
    user = await seeded_user("lander@example.com", status="active")
    _row, raw = await seed_session(user.id, raw_token="lander-raw-token")

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/", follow_redirects=False)
    app_client.cookies.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # Landing template should surface the authenticated user's email.
    assert "lander@example.com" in resp.text


# ===========================================================================
# 21. End-to-end: new user → approve → verify_code → authenticated GET /
# ===========================================================================


async def test_full_flow_new_user_to_session(
    app_client, mailer_mocks, pinned_admin_email, integration_db
):
    from app.models import User

    # Step 1 — new user submits email → pending, admin gets approval email.
    r1 = await app_client.post(
        "/api/auth/request_code", json={"email": "e2e@example.com"}
    )
    assert r1.status_code == 200
    assert r1.json() == {"status": "pending"}
    assert len(mailer_mocks["send_approval_request"].calls) == 1
    args, _ = mailer_mocks["send_approval_request"].calls[0]
    approve_url = args[2]
    assert "?token=" in approve_url
    raw_approval = approve_url.split("?token=", 1)[1]

    # Step 2 — admin clicks approve link.
    r2 = await app_client.get("/admin/approve", params={"token": raw_approval})
    assert r2.status_code == 200

    # Step 3 — user requests a fresh code (now active path).
    r3 = await app_client.post(
        "/api/auth/request_code", json={"email": "e2e@example.com"}
    )
    assert r3.status_code == 200
    assert r3.json() == {"status": "sent"}
    assert len(mailer_mocks["send_login_code"].calls) == 1
    (_to, code), _ = mailer_mocks["send_login_code"].calls[0]

    # Step 4 — user verifies code, receives session cookie.
    r4 = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "e2e@example.com", "code": code},
    )
    assert r4.status_code == 200
    cookie_header = r4.headers.get("set-cookie")
    assert cookie_header is not None
    parsed = _parse_set_cookie(cookie_header)
    assert parsed["_name"] == "method_session"
    raw_session = parsed["_value"]

    # Step 5 — authenticated request to /.
    app_client.cookies.set("method_session", raw_session)
    r5 = await app_client.get("/", follow_redirects=False)
    app_client.cookies.clear()
    assert r5.status_code == 200
    assert "e2e@example.com" in r5.text

    # sanity: user is now active in DB
    integration_db.expire_all()
    u = (
        await integration_db.execute(
            select(User).where(User.email == "e2e@example.com")
        )
    ).scalar_one()
    assert u.status == "active"


# ===========================================================================
# 22. Admin full flow — self-registration skips approval step
# ===========================================================================


async def test_admin_full_flow_self_registration_skips_approval(
    app_client, mailer_mocks, pinned_admin_email
):
    r1 = await app_client.post(
        "/api/auth/request_code", json={"email": pinned_admin_email}
    )
    assert r1.status_code == 200
    assert r1.json() == {"status": "sent"}
    assert mailer_mocks["send_approval_request"].calls == []
    assert len(mailer_mocks["send_login_code"].calls) == 1
    (_to, code), _ = mailer_mocks["send_login_code"].calls[0]

    r2 = await app_client.post(
        "/api/auth/verify_code",
        json={"email": pinned_admin_email, "code": code},
    )
    assert r2.status_code == 200
    assert r2.json() == {"ok": True}
    assert r2.headers.get("set-cookie") is not None


# ===========================================================================
# 23. Cookie flag policy — HttpOnly + SameSite=Lax, Secure absent (HARNESS §4)
# ===========================================================================


async def test_cookie_has_httponly_samesite_lax_not_secure(
    app_client, mailer_mocks, pinned_admin_email, seeded_user
):
    # Pin cookie policy against auth_flow.COOKIE_FLAGS.
    from app.services import auth_flow as af

    assert af.COOKIE_FLAGS == {"httponly": True, "samesite": "lax", "secure": False}

    await seeded_user("cookie@example.com", status="active")
    r1 = await app_client.post(
        "/api/auth/request_code", json={"email": "cookie@example.com"}
    )
    assert r1.status_code == 200
    (_to, code), _ = mailer_mocks["send_login_code"].calls[0]

    r2 = await app_client.post(
        "/api/auth/verify_code",
        json={"email": "cookie@example.com", "code": code},
    )
    assert r2.status_code == 200

    raw_cookie = r2.headers.get("set-cookie")
    assert raw_cookie is not None
    parsed = _parse_set_cookie(raw_cookie)
    # HttpOnly attribute must be present.
    assert parsed.get("httponly") is True, f"HttpOnly missing from {raw_cookie!r}"
    # SameSite=Lax — case-insensitive match.
    samesite = parsed.get("samesite")
    assert isinstance(samesite, str) and samesite.lower() == "lax", (
        f"SameSite=Lax missing or wrong in {raw_cookie!r}"
    )
    # Secure must NOT be set (we're pre-HTTPS per HARNESS §4 / M5).
    assert parsed.get("secure") is not True, (
        f"Secure flag should not be set pre-M5, got {raw_cookie!r}"
    )


# ===========================================================================
# 24. CSRF — mismatched Origin on POST returns 403
# ===========================================================================


async def test_csrf_same_origin_check_rejects_cross_origin(
    app_client, mailer_mocks, pinned_admin_email
):
    resp = await app_client.post(
        "/api/auth/request_code",
        json={"email": "csrf@example.com"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403
    assert resp.json().get("error") == "bad_origin"
    # No email side effect from rejected request.
    assert mailer_mocks["send_approval_request"].calls == []
    assert mailer_mocks["send_login_code"].calls == []
