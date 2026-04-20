"""Integration tests for comment HTTP endpoints (Issue #4 — Feature B).

Contract source: ``docs/design/issue-4-feature-b-comments.md`` §2, §4, §6, §8.
These tests are RED until:

- ``app/services/comment_runner.py`` exists (mocks stick at the import seam)
- ``app/routers/research.py`` exposes 4 new endpoints (POST/GET/DELETE/SSE)
  under ``/api/research/{rid}/comments``
- ``app/models.py`` has the ``Comment`` ORM class
- ``app/templates/history_detail.html`` renders ``data-markdown-source``
  attributes on ``.markdown-body`` (done) and ``.error-banner`` (failed)
- ``app/static/app.js`` is referenced by history_detail.html (initComments)
- ``app/static/style.css`` has ``.comments`` / ``.comment-card`` rules

Coverage binding (design §7):

- ``app/routers/research.py`` (4 new endpoints): tests 1–12
- ``app/services/comment_runner.py`` (at the mock seam): tests 1, 2, 12
- ``app/models.py`` (Comment): tests that directly query Comment rows
- ``app/templates/history_detail.html``: template-render test
- ``app/static/app.js`` / ``style.css``: referenced-from-template test

Mocking strategy: monkeypatch ``comment_runner._run_ai_reply`` (or the
comment_runner equivalent of ``research_runner.stream``) so no real claude
subprocess is ever spawned. Real DB (SQLite via ``app_client``) and real HTTP
via httpx.AsyncClient.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# Valid 26-char Crockford base32 ULIDs.
RID_DONE = "01HXZK8D7Q3V0S9B4W2N6M5C7R"
RID_FAILED = "01HXZK8D7Q3V0S9B4W2N6M5C7S"
RID_PENDING = "01HXZK8D7Q3V0S9B4W2N6M5C7T"
RID_RUNNING = "01HXZK8D7Q3V0S9B4W2N6M5C7U"
RID_OTHER = "01HXZK8D7Q3V0S9B4W2N6M5C7V"


async def _seed_request(
    integration_db,
    *,
    user_id: int,
    request_id: str,
    question: str = "what is X?",
    status: str = "done",
    plan_path: str | None = None,
    error_message: str | None = None,
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
    path = plan_root / f"{rid}.md"
    path.write_text(markdown, encoding="utf-8")
    return str(path.resolve())


def _install_fake_ai_reply(monkeypatch, *, events: list | None = None, status: str = "done"):
    """Patch comment_runner._run_ai_reply so the AI task is a no-op that just
    publishes ``events`` to the SSE channel and flips the placeholder row to
    the requested terminal ``status``.

    Returns the holder dict capturing invocations.
    """
    from app.services import comment_runner
    from app.db import get_sessionmaker
    from app.models import Comment

    holder = {"invocations": [], "events": list(events or [])}

    async def _fake_run(comment_id: str):
        holder["invocations"].append(comment_id)
        # Publish canned events to the SSE channel.
        for ev in holder["events"]:
            comment_runner._publish(comment_id, ev)
        # Close channel sentinel (consistent with research_runner pattern).
        comment_runner._publish(comment_id, ("__close__",))

        # Flip DB row to terminal status.
        sm = get_sessionmaker()
        async with sm() as session:
            ai_row = (
                await session.execute(
                    select(Comment).where(Comment.id == comment_id)
                )
            ).scalar_one_or_none()
            if ai_row is None:
                return
            if status == "done":
                ai_row.ai_status = "done"
                # Body is built from deltas in ``events``.
                ai_row.body = "".join(
                    (ev[1].get("text", "") if isinstance(ev, tuple) and len(ev) > 1 and isinstance(ev[1], dict) else "")
                    for ev in holder["events"]
                    if isinstance(ev, tuple) and ev and ev[0] == "ai_delta"
                ) or "fake ai reply"
                ai_row.cost_usd = 0.0123
                ai_row.ai_error = None
            elif status == "failed":
                ai_row.ai_status = "failed"
                ai_row.ai_error = "simulated failure"
            await session.commit()

    monkeypatch.setattr(comment_runner, "_run_ai_reply", _fake_run)
    return holder


# ===========================================================================
# #1. POST creates user comment + AI placeholder on a DONE plan (201).
# ===========================================================================


async def test_post_comment_done_plan_creates_user_and_ai_rows(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    _upload, plan_root = research_paths
    user, raw = await auth_session("p1@example.com")
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan body\n\n段落一。\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    _install_fake_ai_reply(monkeypatch, events=[("ai_delta", {"text": "OK"})], status="done")

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{RID_DONE}/comments",
        json={
            "anchor_before": "段落",
            "anchor_text": "一",
            "anchor_after": "。",
            "body": "这里不对",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    user_obj = body.get("comment") or body.get("user_comment")
    ai_obj = body.get("ai_placeholder")
    assert user_obj is not None, f"no 'comment' in response: {body!r}"
    assert ai_obj is not None, f"no 'ai_placeholder' in response: {body!r}"
    assert user_obj["author"] == "user"
    assert user_obj["body"] == "这里不对"
    assert user_obj["anchor_text"] == "一"
    assert ai_obj["author"] == "ai"
    assert ai_obj["ai_status"] == "pending"
    assert ai_obj["body"] == ""

    # DB rows created.
    integration_db.expire_all()
    rows = (
        await integration_db.execute(
            select(Comment).where(Comment.request_id == RID_DONE)
        )
    ).scalars().all()
    assert len(rows) == 2
    by_author = {r.author: r for r in rows}
    assert by_author["user"].body == "这里不对"
    assert by_author["ai"].parent_id == by_author["user"].id


# ===========================================================================
# #2. POST on FAILED plan also succeeds — error_message is the anchor source
#     (B-Q7 = A).
# ===========================================================================


async def test_post_comment_failed_plan_creates_rows(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p2@example.com")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_FAILED,
        status="failed",
        plan_path=None,
        error_message="Claude exit=1: RateLimitError",
    )

    _install_fake_ai_reply(
        monkeypatch, events=[("ai_delta", {"text": "这是我们的 bug"})], status="done"
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{RID_FAILED}/comments",
        json={
            "anchor_before": "Claude ",
            "anchor_text": "RateLimitError",
            "anchor_after": "",
            "body": "什么情况？",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body.get("comment") or body.get("user_comment")) is not None
    assert body.get("ai_placeholder") is not None

    integration_db.expire_all()
    rows = (
        await integration_db.execute(
            select(Comment).where(Comment.request_id == RID_FAILED)
        )
    ).scalars().all()
    assert len(rows) == 2


# ===========================================================================
# #3. POST on pending / running request → 409 request_not_finalized.
# ===========================================================================


async def test_post_comment_on_pending_returns_409(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    user, raw = await auth_session("p3a@example.com")
    await _seed_request(
        integration_db, user_id=user.id, request_id=RID_PENDING, status="pending"
    )
    _install_fake_ai_reply(monkeypatch)

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{RID_PENDING}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "hi",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body.get("error") == "request_not_finalized"


async def test_post_comment_on_running_returns_409(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    user, raw = await auth_session("p3b@example.com")
    await _seed_request(
        integration_db, user_id=user.id, request_id=RID_RUNNING, status="running"
    )
    _install_fake_ai_reply(monkeypatch)

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{RID_RUNNING}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "hi",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 409
    assert resp.json().get("error") == "request_not_finalized"


# ===========================================================================
# #4. POST cross-user → 404 (no enumeration oracle, matches DELETE style).
# ===========================================================================


async def test_post_comment_cross_user_returns_404(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    bob, _ = await auth_session("bobp4@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# bob's plan\n")
    await _seed_request(
        integration_db,
        user_id=bob.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    alice, alice_raw = await auth_session("alicep4@example.com")
    _install_fake_ai_reply(monkeypatch)

    app_client.cookies.set("method_session", alice_raw)
    resp = await app_client.post(
        f"/api/research/{RID_DONE}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "hi",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 404


# ===========================================================================
# #5. POST anchor_text > 2000 chars → 400.
# ===========================================================================


async def test_post_comment_anchor_text_too_long_returns_400(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    user, raw = await auth_session("p5@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )
    _install_fake_ai_reply(monkeypatch)

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{RID_DONE}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "A" * 2001,
            "anchor_after": "",
            "body": "body",
        },
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    # Design §4 doesn't name the exact validation error code, but other router
    # errors use structured {"error": "<name>"} (see 409 request_not_finalized
    # above) — require the same shape so the response isn't a bare 400.
    assert "error" in body, (
        f"400 response must carry structured 'error' key, got {body!r}"
    )


async def test_post_comment_body_too_long_returns_400(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    """Design §4 also bounds body to 1..2000."""
    user, raw = await auth_session("p5b@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )
    _install_fake_ai_reply(monkeypatch)

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        f"/api/research/{RID_DONE}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "A" * 2001,
        },
    )
    app_client.cookies.clear()
    assert resp.status_code == 400
    body = resp.json()
    # Structured-error shape parity with other router errors (design §4).
    assert "error" in body, (
        f"400 response must carry structured 'error' key, got {body!r}"
    )


# ===========================================================================
# #6. GET returns nested structure: user → ai_reply.
# ===========================================================================


async def test_get_comments_returns_nested_user_with_ai_reply(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p6@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    # Seed one user comment + one AI reply directly.
    now = _utcnow_naive()
    uc = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="选中的原文",
        anchor_before="前 50",
        anchor_after="后 50",
        body="用户评论正文",
        ai_status=None,
        ai_error=None,
        cost_usd=None,
        created_at=now,
        deleted_at=None,
    )
    ai = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C02",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        author="ai",
        anchor_text="选中的原文",
        anchor_before="前 50",
        anchor_after="后 50",
        body="AI 回复正文",
        ai_status="done",
        ai_error=None,
        cost_usd=0.0123,
        created_at=now,
        deleted_at=None,
    )
    integration_db.add_all([uc, ai])
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{RID_DONE}/comments")
    app_client.cookies.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "comments" in body
    assert isinstance(body["comments"], list)
    assert len(body["comments"]) == 1
    item = body["comments"][0]
    assert item["id"] == uc.id
    assert item["author"] == "user"
    assert item["body"] == "用户评论正文"
    # Nested ai_reply.
    assert "ai_reply" in item
    nested = item["ai_reply"]
    assert nested["id"] == ai.id
    assert nested["author"] == "ai"
    assert nested["ai_status"] == "done"
    assert nested["body"] == "AI 回复正文"
    assert nested["cost_usd"] == 0.0123


# ===========================================================================
# #6b. GET response includes ALL design §2 fields (except user_id, deleted_at)
#     — v2.1 BLOCKING fix.
# ===========================================================================


async def test_get_comments_response_includes_all_design_fields(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p6b@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    now = _utcnow_naive()
    uc = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="选中",
        anchor_before="前",
        anchor_after="后",
        body="user body",
        ai_status=None,
        ai_error=None,
        cost_usd=None,
        created_at=now,
        deleted_at=None,
    )
    ai = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C02",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        author="ai",
        anchor_text="选中",
        anchor_before="前",
        anchor_after="后",
        body="ai body",
        ai_status="done",
        ai_error=None,
        cost_usd=0.0001,
        created_at=now,
        deleted_at=None,
    )
    integration_db.add_all([uc, ai])
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{RID_DONE}/comments")
    app_client.cookies.clear()
    assert resp.status_code == 200
    body = resp.json()
    item = body["comments"][0]

    # Required fields on top-level user comment (design §2 minus user_id/deleted_at).
    required = {
        "id",
        "request_id",
        "author",
        "anchor_text",
        "anchor_before",
        "anchor_after",
        "body",
        "created_at",
    }
    missing = required - set(item.keys())
    assert not missing, f"user-comment response missing: {missing}"
    # Internal fields MUST NOT leak.
    assert "user_id" not in item, "user_id must not leak to client (design §4)"
    assert "deleted_at" not in item, "deleted_at must not leak (filter-only)"

    # AI reply must expose ai_status, ai_error, cost_usd (design §4 example).
    nested = item["ai_reply"]
    ai_required = {
        "id",
        "author",
        "anchor_text",
        "anchor_before",
        "anchor_after",
        "body",
        "ai_status",
        "ai_error",
        "cost_usd",
        "created_at",
    }
    ai_missing = ai_required - set(nested.keys())
    assert not ai_missing, f"ai_reply response missing: {ai_missing}"
    assert "user_id" not in nested
    assert "deleted_at" not in nested


# ===========================================================================
# #7. GET filters soft-deleted rows.
# ===========================================================================


async def test_get_comments_filters_soft_deleted_rows(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p7@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    now = _utcnow_naive()
    live = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="a",
        anchor_before="",
        anchor_after="",
        body="live",
        created_at=now,
        deleted_at=None,
    )
    dead = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C02",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="b",
        anchor_before="",
        anchor_after="",
        body="dead",
        created_at=now,
        deleted_at=now,
    )
    integration_db.add_all([live, dead])
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{RID_DONE}/comments")
    app_client.cookies.clear()

    assert resp.status_code == 200
    body = resp.json()
    ids = [c["id"] for c in body["comments"]]
    assert live.id in ids
    assert dead.id not in ids


# ===========================================================================
# #7b. GET hard-cap at 200 rows + X-Comments-Truncated header.
# ===========================================================================


async def test_get_comments_caps_at_200_with_truncated_header(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p7b@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    # Seed 205 user comments (no AI replies so rows are independent; simpler math).
    now = _utcnow_naive()
    from datetime import timedelta
    rows = []
    # Crockford base32 alphabet (no I/L/O/U).
    _ALPH = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    for i in range(205):
        # 26-char ULID-like id, unique across iterations. Fixed 24-char prefix + 2-char suffix.
        a = _ALPH[i // 32]
        b = _ALPH[i % 32]
        cid = f"01HXZK8D7Q3V0S9B4W2N6M5C{a}{b}"
        assert len(cid) == 26
        rows.append(
            Comment(
                id=cid,
                request_id=RID_DONE,
                user_id=user.id,
                parent_id=None,
                author="user",
                anchor_text="x",
                anchor_before="",
                anchor_after="",
                body=f"c{i}",
                created_at=now + timedelta(seconds=i),
                deleted_at=None,
            )
        )
    # Assert ids are unique.
    assert len({r.id for r in rows}) == 205, "seeded ids collided"
    integration_db.add_all(rows)
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{RID_DONE}/comments")
    app_client.cookies.clear()
    assert resp.status_code == 200

    body = resp.json()
    assert len(body["comments"]) <= 200, (
        f"expected ≤200 comments (design §4 hard cap), got {len(body['comments'])}"
    )
    # Header present and truthy.
    truncated = resp.headers.get("X-Comments-Truncated") or resp.headers.get(
        "x-comments-truncated"
    )
    assert truncated in ("true", "True", "1"), (
        f"X-Comments-Truncated header missing / falsy: {truncated!r}"
    )


# ===========================================================================
# #8. DELETE owner succeeds 204 and cascades soft-delete.
# ===========================================================================


async def test_delete_comment_owner_soft_deletes_user_and_ai(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p8@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    now = _utcnow_naive()
    uc = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="user",
        created_at=now,
        deleted_at=None,
    )
    ai = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C02",
        request_id=RID_DONE,
        user_id=user.id,
        parent_id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        author="ai",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="ai body",
        ai_status="done",
        cost_usd=0.01,
        created_at=now,
        deleted_at=None,
    )
    integration_db.add_all([uc, ai])
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(
        f"/api/research/{RID_DONE}/comments/{uc.id}"
    )
    app_client.cookies.clear()

    assert resp.status_code == 204, resp.text

    # Both rows soft-deleted.
    integration_db.expire_all()
    rows = (
        await integration_db.execute(
            select(Comment).where(Comment.request_id == RID_DONE)
        )
    ).scalars().all()
    assert len(rows) == 2  # physical rows remain
    for r in rows:
        assert r.deleted_at is not None, f"row {r.id!r} not soft-deleted"


# ===========================================================================
# #9. DELETE cross-user → 404 (no enumeration).
# ===========================================================================


async def test_delete_comment_cross_user_returns_404(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    bob, _ = await auth_session("bobp9@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=bob.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )
    now = _utcnow_naive()
    uc = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=RID_DONE,
        user_id=bob.id,
        parent_id=None,
        author="user",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="bob",
        created_at=now,
        deleted_at=None,
    )
    integration_db.add(uc)
    await integration_db.commit()

    uc_id = uc.id  # Capture before expire_all; ORM attr access would fire
                   # a sync lazy SELECT and MissingGreenlet under aiosqlite.
    alice, alice_raw = await auth_session("alicep9@example.com")
    app_client.cookies.set("method_session", alice_raw)
    resp = await app_client.delete(
        f"/api/research/{RID_DONE}/comments/{uc_id}"
    )
    app_client.cookies.clear()
    assert resp.status_code == 404

    # Row untouched.
    integration_db.expire_all()
    row = (
        await integration_db.execute(
            select(Comment).where(Comment.id == uc_id)
        )
    ).scalar_one()
    assert row.deleted_at is None


# ===========================================================================
# #10. DELETE AI reply directly → 403 ai_reply_not_deletable.
# ===========================================================================


async def test_delete_ai_reply_directly_returns_403(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import Comment

    user, raw = await auth_session("p10@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    now = _utcnow_naive()
    uc = Comment(
        id="01HXZK8D7Q3V0S9B4W2N6M5C01",
        request_id=RID_DONE,
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
        request_id=RID_DONE,
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
    ai_id = ai.id  # Capture before expire_all (MissingGreenlet guard).

    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(
        f"/api/research/{RID_DONE}/comments/{ai_id}"
    )
    app_client.cookies.clear()

    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body.get("error") == "ai_reply_not_deletable"

    # AI row still live.
    integration_db.expire_all()
    row = (
        await integration_db.execute(
            select(Comment).where(Comment.id == ai_id)
        )
    ).scalar_one()
    assert row.deleted_at is None


# ===========================================================================
# #11. DELETE unauthenticated → 401.
# ===========================================================================


async def test_delete_comment_unauthenticated_returns_401(
    app_client, research_paths
):
    app_client.cookies.clear()
    resp = await app_client.delete(
        f"/api/research/{RID_DONE}/comments/01HXZK8D7Q3V0S9B4W2N6M5C01"
    )
    assert resp.status_code == 401


async def test_post_comment_unauthenticated_returns_401(
    app_client, research_paths
):
    app_client.cookies.clear()
    resp = await app_client.post(
        f"/api/research/{RID_DONE}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "hi",
        },
    )
    assert resp.status_code == 401


async def test_get_comments_unauthenticated_returns_401(
    app_client, research_paths
):
    app_client.cookies.clear()
    resp = await app_client.get(f"/api/research/{RID_DONE}/comments")
    assert resp.status_code == 401


# ===========================================================================
# #12. SSE: POST → subscribe to comments/stream sees ai_delta → ai_done events.
# ===========================================================================


async def test_sse_stream_receives_ai_delta_and_ai_done(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.services import comment_runner

    user, raw = await auth_session("p12@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    # Install a fake AI task that publishes a delta, sleeps briefly so the
    # SSE consumer can attach, then publishes ai_done.
    holder: dict = {"cid": None}

    async def _fake_run(comment_id: str):
        holder["cid"] = comment_id
        await asyncio.sleep(0.05)
        comment_runner._publish(
            comment_id, ("ai_delta", {"comment_id": comment_id, "text": "hi"})
        )
        await asyncio.sleep(0.05)
        comment_runner._publish(
            comment_id,
            (
                "ai_done",
                {
                    "comment_id": comment_id,
                    "body": "hi",
                    "ai_status": "done",
                    "cost_usd": 0.01,
                },
            ),
        )
        comment_runner._publish(comment_id, ("__close__",))

        # Also flip DB row to done so subsequent GETs see the terminal state.
        from app.db import get_sessionmaker
        from app.models import Comment
        sm = get_sessionmaker()
        async with sm() as session:
            ai_row = (
                await session.execute(
                    select(Comment).where(Comment.id == comment_id)
                )
            ).scalar_one_or_none()
            if ai_row is not None:
                ai_row.ai_status = "done"
                ai_row.body = "hi"
                ai_row.cost_usd = 0.01
                await session.commit()

    monkeypatch.setattr(comment_runner, "_run_ai_reply", _fake_run)

    # POST a new comment — this spawns the AI task.
    app_client.cookies.set("method_session", raw)
    post = await app_client.post(
        f"/api/research/{RID_DONE}/comments",
        json={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    assert post.status_code == 201
    post_body = post.json()
    ai_cid = (post_body.get("ai_placeholder") or {}).get("id")
    assert ai_cid

    # Subscribe to SSE for this comment.
    collected = ""
    async with app_client.stream(
        "GET", f"/api/research/{RID_DONE}/comments/stream?comment_id={ai_cid}"
    ) as response:
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/event-stream")
        async for chunk in response.aiter_text():
            collected += chunk
            if "event: ai_done" in collected:
                break

    app_client.cookies.clear()

    assert "event: ai_delta" in collected, f"no ai_delta event; got {collected!r}"
    assert "event: ai_done" in collected, f"no ai_done event; got {collected!r}"


# ===========================================================================
# #13. history_detail.html renders data-markdown-source on .markdown-body
#     (done) and .error-banner (failed). Covers design §6 + §7 templates.
# ===========================================================================


async def test_history_detail_done_renders_markdown_body_with_data_source(
    app_client, research_paths, auth_session, integration_db
):
    user, raw = await auth_session("td1@example.com")
    _upload, plan_root = research_paths
    plan_abs = _write_plan(plan_root, RID_DONE, "# plan body\n\n段落一。\n")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_DONE,
        status="done",
        plan_path=plan_abs,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{RID_DONE}")
    app_client.cookies.clear()
    assert resp.status_code == 200, resp.text
    html = resp.text
    # Design §6: .markdown-body carries data-markdown-source for the plan.
    assert 'class="markdown-body"' in html
    assert "data-markdown-source" in html
    # Comments region present.
    assert 'class="comments"' in html or "id=\"comment-list\"" in html
    # JS hook for initComments must be loaded.
    assert "/static/app.js" in html or "app.js" in html
    # Stylesheet loaded.
    assert "/static/style.css" in html or "style.css" in html


async def test_history_detail_failed_renders_error_banner_with_data_source(
    app_client, research_paths, auth_session, integration_db
):
    user, raw = await auth_session("td2@example.com")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=RID_FAILED,
        status="failed",
        plan_path=None,
        error_message="Claude exit=1: RateLimitError",
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{RID_FAILED}")
    app_client.cookies.clear()
    assert resp.status_code == 200, resp.text
    html = resp.text
    # Design §3 + §6: .error-banner carries data-markdown-source for
    # error_message so selections can anchor against it.
    assert "error-banner" in html
    assert "data-markdown-source" in html
    # Comments region still present on failed pages (B-Q7=A).
    assert 'class="comments"' in html or "id=\"comment-list\"" in html


# ===========================================================================
# #14. CSS classes from design §6 present in style.css (covers product
#     output file app/static/style.css).
# ===========================================================================


async def test_static_style_css_has_comment_selectors(app_client):
    resp = await app_client.get("/static/style.css")
    assert resp.status_code == 200, f"style.css not served: {resp.status_code}"
    css = resp.text
    # Design §6: required CSS class names.
    for selector in (".comments", ".comment-card"):
        assert selector in css, f"CSS missing {selector}"


# ===========================================================================
# #15. app.js contains initComments() entrypoint (design §6).
# ===========================================================================


async def test_static_app_js_exposes_init_comments(app_client):
    resp = await app_client.get("/static/app.js")
    assert resp.status_code == 200, f"app.js not served: {resp.status_code}"
    js = resp.text
    assert "initComments" in js, "app.js missing initComments entrypoint"
