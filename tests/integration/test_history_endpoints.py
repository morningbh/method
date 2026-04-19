"""Integration tests for history endpoints (Task 4.1 + 4.2).

Contract source: ``docs/design/issue-3-m4-frontend-ui.md`` §2 + §9.

RED until:
- ``app/routers/history.py`` exists with 4 routes
- ``app/main.py`` includes history router
- Templates ``index.html``, ``history.html``, ``history_detail.html``,
  ``_topbar.html`` exist
- ``app/routers/auth.py::root`` handler has been removed

Tests 1-15 cover history/detail/api routes; tests 16-24 cover index page +
topbar + XSS escape.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_request(
    integration_db,
    *,
    user_id: int,
    request_id: str,
    question: str = "How do I study X?",
    status: str = "done",
    plan_path: str | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
    completed_at: datetime | None = None,
    model: str = "claude-opus-4-7",
):
    from app.models import ResearchRequest

    req = ResearchRequest(
        id=request_id,
        user_id=user_id,
        question=question,
        status=status,
        plan_path=plan_path,
        error_message=error_message,
        model=model,
        created_at=created_at or _utcnow_naive(),
        completed_at=completed_at,
    )
    integration_db.add(req)
    await integration_db.commit()
    return req


async def _seed_file(
    integration_db,
    *,
    request_id: str,
    original_name: str = "notes.md",
    stored_path: str = "/tmp/fake/stored",
    extracted_path: str | None = None,
    size_bytes: int = 100,
    mime_type: str = "text/markdown",
):
    from app.models import UploadedFile

    f = UploadedFile(
        request_id=request_id,
        original_name=original_name,
        stored_path=stored_path,
        extracted_path=extracted_path,
        size_bytes=size_bytes,
        mime_type=mime_type,
        created_at=_utcnow_naive(),
    )
    integration_db.add(f)
    await integration_db.commit()
    return f


# ===========================================================================
# #1. authed GET / renders index.html
# ===========================================================================


async def test_get_root_authed_renders_index_with_textarea(
    app_client, auth_session
):
    _user, raw = await auth_session("rooter@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/")
    app_client.cookies.clear()

    assert resp.status_code == 200, resp.text
    body = resp.text
    # Index page workspace markers.
    assert 'id="research-form"' in body
    assert 'id="question"' in body
    assert 'id="drop-zone"' in body


# ===========================================================================
# #2. unauthed GET / → 303 redirect to /login
# ===========================================================================


async def test_get_root_unauthed_redirects_to_login(app_client):
    app_client.cookies.clear()
    resp = await app_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ===========================================================================
# #3. GET /history lists user research (ordered newest-first)
# ===========================================================================


async def test_get_history_lists_user_research(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("lister@example.com")
    older = _utcnow_naive() - timedelta(hours=2)
    newer = _utcnow_naive() - timedelta(minutes=5)
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id="01HOLDER0000000000000000AA",
        question="older-question",
        status="done",
        created_at=older,
        completed_at=older,
    )
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id="01HNEWER000000000000000000",
        question="newer-question",
        status="pending",
        created_at=newer,
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/history")
    app_client.cookies.clear()

    assert resp.status_code == 200, resp.text
    body = resp.text
    # Both questions rendered.
    assert "older-question" in body
    assert "newer-question" in body
    # Newer appears before older (newest-first ordering).
    assert body.index("newer-question") < body.index("older-question")


# ===========================================================================
# #4. empty state renders 还没有研究记录
# ===========================================================================


async def test_get_history_empty_state(app_client, auth_session):
    _user, raw = await auth_session("empty@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/history")
    app_client.cookies.clear()
    assert resp.status_code == 200
    assert "还没有研究记录" in resp.text


# ===========================================================================
# #5. cross-user isolation — bob sees empty when alice seeds
# ===========================================================================


async def test_get_history_cross_user_isolation(
    app_client, auth_session, integration_db
):
    alice, _alice_raw = await auth_session("alice@example.com")
    bob, bob_raw = await auth_session("bob@example.com")
    await _seed_request(
        integration_db,
        user_id=alice.id,
        request_id="01HALICEMARKER000000000000",
        question="alice-only-question",
        status="done",
    )
    app_client.cookies.set("method_session", bob_raw)
    resp = await app_client.get("/history")
    app_client.cookies.clear()

    assert resp.status_code == 200
    assert "alice-only-question" not in resp.text
    assert "还没有研究记录" in resp.text


# ===========================================================================
# #6. GET /api/history returns JSON with expected shape
# ===========================================================================


async def test_get_api_history_returns_json_list(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("jsonlist@example.com")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id="01HJSONLIST00000000000000A",
        question="q?",
        status="done",
        completed_at=_utcnow_naive(),
    )
    await _seed_file(
        integration_db,
        request_id="01HJSONLIST00000000000000A",
        original_name="x.md",
    )
    await _seed_file(
        integration_db,
        request_id="01HJSONLIST00000000000000A",
        original_name="y.md",
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/api/history")
    app_client.cookies.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    items = body["items"]
    assert len(items) == 1
    item = items[0]
    for k in (
        "request_id",
        "question",
        "status",
        "created_at",
        "completed_at",
        "n_files",
        "cost_usd",
    ):
        assert k in item, f"missing key {k!r} in {item!r}"
    assert item["n_files"] == 2
    assert item["status"] == "done"


# ===========================================================================
# #7. /api/history ordered newest-first
# ===========================================================================


async def test_get_api_history_ordered_newest_first(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("ordered@example.com")
    older = _utcnow_naive() - timedelta(hours=5)
    newer = _utcnow_naive() - timedelta(minutes=1)
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id="01HORDEROLD00000000000000A",
        question="qold",
        status="done",
        created_at=older,
    )
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id="01HORDERNEW00000000000000B",
        question="qnew",
        status="pending",
        created_at=newer,
    )

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/api/history")
    app_client.cookies.clear()

    body = resp.json()
    ids = [it["request_id"] for it in body["items"]]
    assert ids == ["01HORDERNEW00000000000000B", "01HORDEROLD00000000000000A"]


# ===========================================================================
# #8. /api/history cost_usd always null in M4
# ===========================================================================


async def test_get_api_history_cost_usd_always_null_in_m4(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("cost@example.com")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id="01HCOSTTEST00000000000000X",
        question="qx",
        status="done",
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/api/history")
    app_client.cookies.clear()

    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["cost_usd"] is None


# ===========================================================================
# #9. GET /history/<id> shows question and file list
# ===========================================================================


async def test_get_history_detail_shows_question_and_files(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("detail@example.com")
    rid = "01HDETAIL0000000000000000A"
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=rid,
        question="please design a research plan",
        status="pending",
    )
    await _seed_file(
        integration_db,
        request_id=rid,
        original_name="evidence.pdf",
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "please design a research plan" in body
    assert "evidence.pdf" in body


# ===========================================================================
# #10. detail includes data-initial-status="pending"
# ===========================================================================


async def test_get_history_detail_includes_sse_url_for_pending(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("pending@example.com")
    rid = "01HPENDINGMARKER0000000000"
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=rid,
        question="pending-q",
        status="pending",
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()

    assert resp.status_code == 200
    body = resp.text
    assert 'data-initial-status="pending"' in body
    assert f'data-request-id="{rid}"' in body


# ===========================================================================
# #11. detail: download enabled when done (no aria-disabled)
# ===========================================================================


async def test_get_history_detail_includes_download_button_when_done(
    app_client, auth_session, integration_db, tmp_path
):
    user, raw = await auth_session("done@example.com")
    rid = "01HDONE000000000000000000A"
    plan = tmp_path / "plan.md"
    plan.write_text("# done\n", encoding="utf-8")
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=rid,
        question="done-q",
        status="done",
        plan_path=str(plan),
        completed_at=_utcnow_naive(),
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()

    body = resp.text
    # Download anchor for /api/research/<rid>/download must be present and
    # NOT carry aria-disabled="true".
    assert f"/api/research/{rid}/download" in body
    # Locate the download link segment and assert no aria-disabled on it.
    # A crude but effective assertion: the literal substring
    # 'aria-disabled="true"' should not appear on the detail page when done.
    assert 'aria-disabled="true"' not in body


# ===========================================================================
# #12. detail: download disabled when pending (aria-disabled="true")
# ===========================================================================


async def test_get_history_detail_hides_download_button_when_pending(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("aria@example.com")
    rid = "01HARIAPENDING000000000000"
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=rid,
        question="pending-q",
        status="pending",
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()

    body = resp.text
    assert 'aria-disabled="true"' in body


# ===========================================================================
# #13. detail: error banner when failed
# ===========================================================================


async def test_get_history_detail_shows_error_banner_when_failed(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("failed@example.com")
    rid = "01HFAILED00000000000000000"
    err_text = "claude subprocess timed out after 600s"
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=rid,
        question="failed-q",
        status="failed",
        error_message=err_text,
        completed_at=_utcnow_naive(),
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()

    body = resp.text
    assert err_text in body
    assert "error-banner" in body


# ===========================================================================
# #14. detail cross-user → 404
# ===========================================================================


async def test_get_history_detail_cross_user_returns_404(
    app_client, auth_session, integration_db
):
    alice, _ = await auth_session("alice-d@example.com")
    bob, bob_raw = await auth_session("bob-d@example.com")
    rid = "01HCROSSUSER00000000000000"
    await _seed_request(
        integration_db,
        user_id=alice.id,
        request_id=rid,
        question="alice-private",
        status="pending",
    )
    app_client.cookies.set("method_session", bob_raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# #15. detail unknown id → 404
# ===========================================================================


async def test_get_history_detail_404_for_unknown_id(
    app_client, auth_session
):
    _user, raw = await auth_session("unk@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get("/history/01HNOSUCHIDXXXXXXXXXXXXXXX")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# Extra: XSS escape on question render
# ===========================================================================


async def test_history_detail_escapes_question_html(
    app_client, auth_session, integration_db
):
    user, raw = await auth_session("xss@example.com")
    rid = "01HXSSTEST0000000000000000"
    await _seed_request(
        integration_db,
        user_id=user.id,
        request_id=rid,
        question="<script>alert(1)</script>",
        status="pending",
    )
    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/history/{rid}")
    app_client.cookies.clear()

    body = resp.text
    # Raw script tag must NOT appear (Jinja2 autoescape).
    assert "<script>alert(1)</script>" not in body
    # Escaped form must appear.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
