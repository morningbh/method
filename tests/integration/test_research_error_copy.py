"""Integration tests for Issue #5: research + history routes + 404 handler.

Contract: ``docs/design/issue-5-error-copy.md`` §3.2, §4.1, §5, §7-note-1.

Every 4xx/5xx JSON body from ``app/routers/research.py`` and
``app/routers/history.py`` (and the global HTTPException handler in
``app/main.py``) must return ``{"error": <code>, "message": <中文>}``.

Strings below are transcribed verbatim from design §4.1.

These tests are RED until the routes + ``app/main.py`` exception handler
are updated.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


VALID_ULID = "01HXZK8D7Q3V0S9B4W2N6M5C7R"
MISSING_ULID = "01HXZK8D7Q3V0S9B4W2N6M5CZZ"


# ---------------------------------------------------------------------------
# Expected Chinese copy — verbatim from design §4.1.
# ---------------------------------------------------------------------------

MSG_EMPTY_QUESTION = "请输入研究问题"
MSG_QUESTION_TOO_LONG = "问题过长，请精简后再提交"
MSG_INVALID_MODE = "研究模式不合法，请刷新页面重试"
MSG_INTERNAL = "服务器开小差了，请稍后重试"
MSG_PLAN_MISSING = "方案文件缺失，请联系管理员"
MSG_REQUEST_BUSY = "请求仍在处理中，请等它结束后再操作"
MSG_REQUEST_NOT_FINALIZED = "当前请求还在生成中，请等它结束再评论"
MSG_ANCHOR_TEXT_INVALID = "选中的原文不合法，请重新框选"
MSG_BODY_INVALID = "评论内容不符合要求（长度或格式），请修改后重试"
MSG_ANCHOR_CONTEXT_TOO_LONG = "选中段落上下文过长，请缩短后重试"
MSG_BODY_EMPTY = "评论不能为空"
MSG_AI_REPLY_NOT_DELETABLE = "AI 回复不能被删除"
MSG_NOT_FOUND = "记录不存在或已被删除"
MSG_UNAUTHENTICATED = "登录已过期，请刷新页面重新登录"


# ---------------------------------------------------------------------------
# Seed helpers (duplicated minimally to keep this file self-contained and
# avoid cross-file imports from other test modules).
# ---------------------------------------------------------------------------


async def _seed_request(
    integration_db,
    *,
    user_id: int,
    request_id: str,
    status: str = "done",
    plan_path: str | None = None,
    error_message: str | None = None,
    question: str = "what is X?",
):
    from app.models import ResearchRequest

    req = ResearchRequest(
        id=request_id,
        user_id=user_id,
        question=question,
        status=status,
        plan_path=plan_path,
        error_message=error_message,
        model="claude-opus-4-7",
        created_at=_utcnow_naive(),
        completed_at=_utcnow_naive() if status in ("done", "failed") else None,
    )
    integration_db.add(req)
    await integration_db.commit()
    return req


def _write_plan(plan_root: Path, rid: str, markdown: str) -> str:
    p = plan_root / f"{rid}.md"
    p.write_text(markdown, encoding="utf-8")
    return str(p.resolve())


def _install_noop_stream(monkeypatch):
    """Swap research_runner.stream with a no-op so POST endpoint doesn't
    actually spawn any background machinery — we only exercise request-time
    validation paths here.
    """
    try:
        from app.services import research_runner as rr

        async def _fake(prompt, cwd):
            if False:
                yield None  # pragma: no cover

        monkeypatch.setattr(rr, "stream", _fake)
    except ModuleNotFoundError:
        # RED phase upstream; tests will fail on import elsewhere first.
        pass


# ===========================================================================
# 1. empty_question (research.py:101) — 400
# ===========================================================================


async def test_post_research_empty_question_returns_error_and_message(
    app_client, research_paths, auth_session, monkeypatch
):
    """Design §4.1: empty_question / 400 / "请输入研究问题"."""
    _install_noop_stream(monkeypatch)
    _, raw = await auth_session("eq@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post("/api/research", data={"question": "   "})
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "empty_question"
    assert body.get("message") == MSG_EMPTY_QUESTION


# ===========================================================================
# 2. question_too_long (research.py:106) — 400
# ===========================================================================


async def test_post_research_too_long_returns_error_and_message(
    app_client, research_paths, auth_session, monkeypatch
):
    """Design §4.1: question_too_long / 400."""
    _install_noop_stream(monkeypatch)
    _, raw = await auth_session("tl@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research", data={"question": "A" * 4001}
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "question_too_long"
    assert body.get("message") == MSG_QUESTION_TOO_LONG


# ===========================================================================
# 3. invalid_mode (research.py:111) — 400
# ===========================================================================


async def test_post_research_invalid_mode_returns_error_and_message(
    app_client, research_paths, auth_session, monkeypatch
):
    """Design §4.1: invalid_mode / 400."""
    _install_noop_stream(monkeypatch)
    _, raw = await auth_session("mo@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research", data={"question": "Q?", "mode": "bogus"}
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "invalid_mode"
    assert body.get("message") == MSG_INVALID_MODE


# ===========================================================================
# 4. plan_missing (research.py:364) — 500
#    Triggered by downloading a "done" request whose plan file was deleted.
# ===========================================================================


async def test_download_plan_missing_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: plan_missing / 500 / "方案文件缺失，请联系管理员".

    Seed a ``done`` request whose plan_path points at a file that no
    longer exists; GET /api/research/<rid>/download must convert the
    missing-file error to the plan_missing code with Chinese message.
    """
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("pm@example.com")

    _upload, plan_root = research_paths
    # Point plan_path at a file we will NOT create.
    ghost_path = str((plan_root / f"{VALID_ULID}.md").resolve())
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="done",
        plan_path=ghost_path,
    )
    # Make sure it really is absent.
    assert not Path(ghost_path).exists()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{VALID_ULID}/download")
    app_client.cookies.clear()

    assert resp.status_code == 500
    body = resp.json()
    assert body.get("error") == "plan_missing"
    assert body.get("message") == MSG_PLAN_MISSING


# ===========================================================================
# 5. request_busy (research.py:403) — 409
# ===========================================================================


async def test_delete_busy_request_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: request_busy / 409 / "请求仍在处理中，请等它结束后再操作".

    The current implementation already hardcodes a (slightly different)
    Chinese string; design §5 requires migration to ``message_for("request_busy")``.
    """
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("rb@example.com")

    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="running",
        plan_path=None,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(f"/api/research/{VALID_ULID}")
    app_client.cookies.clear()

    assert resp.status_code == 409
    body = resp.json()
    assert body.get("error") == "request_busy"
    assert body.get("message") == MSG_REQUEST_BUSY


# ===========================================================================
# 6. request_not_finalized (research.py:502) — 409
#    Posting a comment on a pending/running request.
# ===========================================================================


async def test_post_comment_on_unfinalized_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: request_not_finalized / 409 /
    "当前请求还在生成中，请等它结束再评论"."""
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("rnf@example.com")

    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="pending",
        plan_path=None,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{VALID_ULID}/comments",
        json={
            "anchor_text": "hello",
            "anchor_before": "",
            "anchor_after": "",
            "body": "a comment",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 409
    body = resp.json()
    assert body.get("error") == "request_not_finalized"
    assert body.get("message") == MSG_REQUEST_NOT_FINALIZED


# ===========================================================================
# 7. anchor_text_invalid (research.py:509) — 400
# ===========================================================================


async def test_post_comment_anchor_text_invalid_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: anchor_text_invalid / 400."""
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("ati@example.com")

    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, VALID_ULID, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="done",
        plan_path=plan_abs,
    )

    # Empty anchor_text (whitespace only) — rejects per design.
    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{VALID_ULID}/comments",
        json={
            "anchor_text": "   ",
            "anchor_before": "",
            "anchor_after": "",
            "body": "valid body",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "anchor_text_invalid"
    assert body.get("message") == MSG_ANCHOR_TEXT_INVALID


# ===========================================================================
# 8. body_invalid (research.py:513) — 400
#    body length > 2000 chars.
# ===========================================================================


async def test_post_comment_body_invalid_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: body_invalid / 400 /
    "评论内容不符合要求（长度或格式），请修改后重试"."""
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("bi@example.com")

    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, VALID_ULID, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="done",
        plan_path=plan_abs,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{VALID_ULID}/comments",
        json={
            "anchor_text": "hello",
            "anchor_before": "",
            "anchor_after": "",
            "body": "x" * 2001,  # > 2000 char cap.
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "body_invalid"
    assert body.get("message") == MSG_BODY_INVALID


# ===========================================================================
# 9. anchor_context_too_long (research.py:520) — 400
# ===========================================================================


async def test_post_comment_anchor_context_too_long_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: anchor_context_too_long / 400."""
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("actl@example.com")

    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, VALID_ULID, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="done",
        plan_path=plan_abs,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{VALID_ULID}/comments",
        json={
            "anchor_text": "hello",
            "anchor_before": "B" * 2000,  # exceed ±200 char cap.
            "anchor_after": "A" * 2000,
            "body": "ok",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "anchor_context_too_long"
    assert body.get("message") == MSG_ANCHOR_CONTEXT_TOO_LONG


# ===========================================================================
# 10. body_empty (research.py:533) — 400
#     POST on a reply endpoint with empty body.
# ===========================================================================


async def test_post_comment_body_empty_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: body_empty / 400 / "评论不能为空"."""
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("be@example.com")

    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, VALID_ULID, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="done",
        plan_path=plan_abs,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{VALID_ULID}/comments",
        json={
            "anchor_text": "hello",
            "anchor_before": "",
            "anchor_after": "",
            "body": "",  # empty.
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "body_empty"
    assert body.get("message") == MSG_BODY_EMPTY


# ===========================================================================
# 11. ai_reply_not_deletable (research.py:651) — 403
# ===========================================================================


async def test_delete_ai_reply_returns_error_and_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.1: ai_reply_not_deletable / 403 / "AI 回复不能被删除"."""
    from app.models import Comment

    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("arnd@example.com")

    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, VALID_ULID, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="done",
        plan_path=plan_abs,
    )

    now = _utcnow_naive()
    uc = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=VALID_ULID,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="u",
        created_at=now,
        deleted_at=None,
    )
    ai = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C02",
        request_id=VALID_ULID,
        user_id=user.id,
        parent_id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        author="ai",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="ai body",
        ai_status="done",
        created_at=now,
        deleted_at=None,
    )
    integration_db.add_all([uc, ai])
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(
        f"/api/research/{VALID_ULID}/comments/{ai.id}"
    )
    app_client.cookies.clear()

    assert resp.status_code == 403
    body = resp.json()
    assert body.get("error") == "ai_reply_not_deletable"
    assert body.get("message") == MSG_AI_REPLY_NOT_DELETABLE


# ===========================================================================
# 12. not_found on /api/research/<missing-id>/download — 404
#     via HTTPException(detail="not_found") → global handler.
# ===========================================================================


async def test_research_download_not_found_returns_error_and_message(
    app_client, research_paths, auth_session, monkeypatch
):
    """Design §4.1 + §7-note-1: a ``HTTPException(404, detail="not_found")``
    must be wrapped by the global handler into
    ``{"error": "not_found", "message": "记录不存在或已被删除"}``.
    """
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("nf1@example.com")

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{MISSING_ULID}/download")
    app_client.cookies.clear()

    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error") == "not_found", (
        f"expected error=not_found (design §7 note 1), got {body!r}"
    )
    assert body.get("message") == MSG_NOT_FOUND, (
        f"expected message={MSG_NOT_FOUND!r}, got {body!r}"
    )


# ===========================================================================
# 13. not_found on /history/<missing-id> and /api/history detail — 404
#     Covers app/routers/history.py (design §5 row 4).
# ===========================================================================


async def test_history_detail_not_found_returns_error_and_message(
    app_client, research_paths, auth_session, monkeypatch
):
    """Design §4.1 / §5: history.py's HTTPException(404, "not_found") must
    surface as ``{"error": "not_found", "message": "记录不存在或已被删除"}``.
    """
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("hnf@example.com")

    app_client.cookies.set("method_session", raw)
    # Try the API (JSON) path first — the handler applies uniformly.
    resp = await app_client.get(f"/api/history/{MISSING_ULID}")
    app_client.cookies.clear()

    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error") == "not_found"
    assert body.get("message") == MSG_NOT_FOUND


# ===========================================================================
# 14. Generic unknown 404 (any missing route) — 404 via global handler
#     Exercises the app/main.py exception handler registration explicitly.
# ===========================================================================


async def test_global_404_handler_returns_error_and_message(app_client):
    """Design §7 note 1: the global HTTPException handler wraps 404s from
    FastAPI's default "not found" path into the ``{error,message}`` shape.
    """
    # A route that definitely doesn't exist.
    resp = await app_client.get("/api/this-route-does-not-exist-xyz")
    assert resp.status_code == 404
    body = resp.json()
    # The machine code used for default 404 is "not_found" per design.
    assert body.get("error") == "not_found", (
        f"global 404 handler should emit error=not_found, got {body!r}"
    )
    assert body.get("message") == MSG_NOT_FOUND


# ===========================================================================
# 15. file_processor path: POST /api/research with > 20 files triggers
#     LimitExceededError → new ``{error, message}`` shape (§3.2), NOT the
#     legacy ``{code, message}`` shape.
# ===========================================================================


async def test_post_research_too_many_files_returns_new_shape(
    app_client, research_paths, auth_session, monkeypatch
):
    """Design §3.2 / §5: after migration the bubbled body is
    ``{"error": "files_too_many", "message": <中文>}`` — the legacy ``"code"``
    key is gone.
    """
    _install_noop_stream(monkeypatch)
    _, raw = await auth_session("tmf@example.com")
    app_client.cookies.set("method_session", raw)

    files = [
        ("files", (f"f{i}.md", b"# hi\n", "text/markdown"))
        for i in range(21)
    ]
    resp = await app_client.post(
        "/api/research", data={"question": "Q?"}, files=files
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "files_too_many"
    assert body.get("message") == (
        "上传文件数超出单次 8 个的上限，请删减后重试"
    ), f"unexpected message: {body!r}"
    # Legacy "code" key must NOT be present.
    assert "code" not in body, (
        f"legacy 'code' key still present after §3.2 migration: {body!r}"
    )


# ===========================================================================
# 16. 5xx internal (research.py:172 / :540) — 500
#     This is harder to trigger without mocking internals. We mark it as
#     xfail pending an intentional internal-error injection hook.
# ===========================================================================


import pytest  # noqa: E402 — placed after top-of-file imports to keep test body contiguous


@pytest.mark.xfail(
    reason=(
        "research.py internal 500 paths (:172 / :540) require invasive mocking "
        "of the request-finalization branch to simulate; skipped until a "
        "dedicated seam exists. Coverage target: assert body == "
        "{'error':'internal','message':'服务器开小差了，请稍后重试'}."
    )
)
async def test_research_internal_500_returns_error_and_message(app_client):
    resp = await app_client.get("/api/research/__force_internal_error__")
    body = resp.json()
    assert body == {"error": "internal", "message": MSG_INTERNAL}


# ===========================================================================
# 17. templates/history_detail.html — error banner fallback copy
#
# Design §4.3 / §5 row 8: when ``error_message`` is missing / None, the
# banner must render the fallback string "研究失败，原因未知，请重试".
# When ``error_message`` is a real string, it must render verbatim.
#
# We exercise the template through the real /history/<rid> route so the
# Jinja rendering path is authentic.
# ===========================================================================


async def test_history_detail_failed_with_error_message_renders_verbatim(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """When research_requests.error_message is populated, template renders it
    verbatim (no fallback)."""
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("hdvm@example.com")

    real_msg = "claude 进程超时：120 秒无输出"
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="failed",
        plan_path=None,
        error_message=real_msg,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{VALID_ULID}")
    app_client.cookies.clear()

    assert resp.status_code == 200
    body = resp.text
    assert real_msg in body, (
        f"failed banner missing the real error message: {real_msg!r}"
    )
    # The fallback MUST NOT appear when the real message is present.
    assert "研究失败，原因未知，请重试" not in body


async def test_history_detail_failed_with_null_error_message_renders_fallback(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4.3 / §5 row 8: ``error_message = None`` → banner shows
    "研究失败，原因未知，请重试" (the Jinja ``or`` fallback).

    Note: HARNESS §1 forbids silent failures in production, but tests may
    seed a NULL error_message directly to exercise the template fallback.
    """
    _install_noop_stream(monkeypatch)
    user, raw = await auth_session("hdnf@example.com")

    # Seed a failed request with error_message=None to exercise the
    # template-side defense. Direct DB insert bypasses the service-layer
    # guard that normally enforces a non-empty error_message.
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=VALID_ULID,
        status="failed",
        plan_path=None,
        error_message=None,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{VALID_ULID}")
    app_client.cookies.clear()

    assert resp.status_code == 200
    body = resp.text
    assert "研究失败，原因未知，请重试" in body, (
        "expected fallback banner copy when error_message is null; got HTML "
        "without the fallback string"
    )
