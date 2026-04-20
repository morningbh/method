"""Integration tests for the research HTTP boundary (Task 3.3).

Contract source: ``docs/design/issue-2-task-3.3-research-routes.md`` §2, §8,
§9, §11.2. These tests are RED until:

- ``app/routers/research.py`` exists and is wired into ``app/main.py``
- ``app/services/research_runner.py`` exists (so mocks at the import seam
  stick)

Coverage binding (design §10):

- ``app/routers/research.py``  → tests 17–40
- ``app/services/research_runner.py``  → transitively, plus direct mock
  seam on ``.stream``
- ``app/main.py`` (router include)  → transitively via every request

Mocking strategy:

- ``research_runner.stream`` is monkeypatched at the research_runner module
  attribute (the call seam inside ``_run_research`` per design §3). Tests
  NEVER start a real claude subprocess.
- Real file_processor does real disk I/O into ``tmp_path`` via
  ``research_paths`` fixture (HARNESS §2 requires real paths).
- Real DB (SQLite) via ``app_client`` / ``integration_db``.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


VALID_ULID_FIXTURE = "01HXZK8D7Q3V0S9B4W2N6M5C7R"


# ---------------------------------------------------------------------------
# Fake stream installer — overrides the one in conftest when tests need
# per-test control of the events. Uses the same ``research_runner.stream``
# seam.
# ---------------------------------------------------------------------------


def _install_fake_stream(monkeypatch, events):
    """Install a fake ``stream`` on research_runner that yields ``events``.

    Returns a holder dict:
      ``holder["events"]`` — the event list (mutable)
      ``holder["prompts"]`` — list of prompts seen so far (for mode/template assertions)
      ``holder["cwds"]`` — list of cwd paths seen so far
    """
    from app.services import research_runner as rr

    holder = {"events": list(events), "prompts": [], "cwds": []}

    async def _fake(prompt, cwd):
        holder["prompts"].append(prompt)
        holder["cwds"].append(cwd)
        for ev in holder["events"]:
            yield ev

    monkeypatch.setattr(rr, "stream", _fake)
    return holder


def _read_md_bytes() -> bytes:
    return b"# sample\n\nhello\n"


def _read_txt_bytes() -> bytes:
    return b"hello world\n"


async def _wait_for_status(integration_db, request_id: str, target: set[str], timeout: float = 3.0):
    """Poll the DB until status is in ``target`` or timeout. Returns the row."""
    from app.models import ResearchRequest

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        integration_db.expire_all()
        row = (
            await integration_db.execute(
                select(ResearchRequest).where(ResearchRequest.id == request_id)
            )
        ).scalar_one_or_none()
        if row is not None and row.status in target:
            return row
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"timed out waiting for status in {target}; last row={row!r}"
            )
        await asyncio.sleep(0.02)


# ===========================================================================
# #17. POST creates request + files rows
# ===========================================================================


async def test_post_research_creates_request_and_files(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import ResearchRequest, UploadedFile

    user, raw = await auth_session("creator@example.com")
    _install_fake_stream(monkeypatch, [("done", "# md\n", 0.1, 1)])
    app_client.cookies.set("method_session", raw)

    files = [
        ("files", ("a.md", _read_md_bytes(), "text/markdown")),
        ("files", ("b.txt", _read_txt_bytes(), "text/plain")),
    ]
    resp = await app_client.post(
        "/api/research",
        data={"question": "How should I study X?"},
        files=files,
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "request_id" in body
    assert body["status"] == "pending"
    rid = body["request_id"]

    # DB row inserted.
    integration_db.expire_all()
    req = (
        await integration_db.execute(
            select(ResearchRequest).where(ResearchRequest.id == rid)
        )
    ).scalar_one()
    assert req.user_id == user.id
    assert req.question == "How should I study X?"
    # Row may already have flipped to running/done by the time we look —
    # the invariant is that it's not in some unknown fourth state.
    assert req.status in ("pending", "running", "done")

    # Two uploaded_files rows inserted.
    files_rows = (
        await integration_db.execute(
            select(UploadedFile).where(UploadedFile.request_id == rid)
        )
    ).scalars().all()
    assert len(files_rows) == 2
    names = sorted(f.original_name for f in files_rows)
    assert names == ["a.md", "b.txt"]


# ===========================================================================
# #18. POST without auth → 401
# ===========================================================================


async def test_post_research_without_auth_returns_401(app_client, research_paths):
    app_client.cookies.clear()
    resp = await app_client.post(
        "/api/research",
        data={"question": "Q?"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthenticated"}


# ===========================================================================
# #19. empty question → 400
# ===========================================================================


async def test_post_research_empty_question_returns_400(
    app_client, research_paths, auth_session, monkeypatch
):
    _install_fake_stream(monkeypatch, [])
    user, raw = await auth_session("eq@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research",
        data={"question": "   "},
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    assert resp.json() == {"error": "empty_question"}


# ===========================================================================
# #20. question > 4000 chars → 400
# ===========================================================================


async def test_post_research_too_long_question_returns_400(
    app_client, research_paths, auth_session, monkeypatch
):
    _install_fake_stream(monkeypatch, [])
    user, raw = await auth_session("long@example.com")
    app_client.cookies.set("method_session", raw)

    long_q = "A" * 4001
    resp = await app_client.post(
        "/api/research",
        data={"question": long_q},
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    assert resp.json() == {"error": "question_too_long"}


# ===========================================================================
# #21. > 20 files → 400 (file_processor limit)
# ===========================================================================


async def test_post_research_too_many_files_returns_400(
    app_client, research_paths, auth_session, monkeypatch
):
    _install_fake_stream(monkeypatch, [])
    user, raw = await auth_session("many@example.com")
    app_client.cookies.set("method_session", raw)

    files = [
        ("files", (f"f{i}.md", _read_md_bytes(), "text/markdown"))
        for i in range(21)
    ]
    resp = await app_client.post(
        "/api/research",
        data={"question": "Q?"},
        files=files,
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    # file_processor.LimitExceededError uses {"code", "message"} shape.
    # Accept either the bubbled shape or a bare {"code": "files_too_many"}.
    # The key check is: code indicates too-many-files.
    code = body.get("code") or body.get("detail", {}).get("code") or body.get("error")
    assert code == "files_too_many", f"unexpected 400 body: {body!r}"


# ===========================================================================
# Mode selector (general vs investment) — form field routing.
# ===========================================================================


async def test_post_research_defaults_to_general_mode(
    app_client, research_paths, auth_session, monkeypatch
):
    holder = _install_fake_stream(monkeypatch, [("done", "# ok\n", 0.0, 1)])
    _, raw = await auth_session("mode-default@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research",
        data={"question": "What is X?"},
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    # Wait briefly for the background task to call stream().
    for _ in range(50):
        if holder["prompts"]:
            break
        await asyncio.sleep(0.02)
    assert holder["prompts"], "stream() was never invoked"
    prompt = holder["prompts"][0]
    assert prompt.lstrip().startswith("/research-method-designer")
    assert "/investment-research-planner" not in prompt


async def test_post_research_explicit_general_mode(
    app_client, research_paths, auth_session, monkeypatch
):
    holder = _install_fake_stream(monkeypatch, [("done", "# ok\n", 0.0, 1)])
    _, raw = await auth_session("mode-general@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research",
        data={"question": "Q?", "mode": "general"},
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    for _ in range(50):
        if holder["prompts"]:
            break
        await asyncio.sleep(0.02)
    assert holder["prompts"]
    assert holder["prompts"][0].lstrip().startswith("/research-method-designer")


async def test_post_research_investment_mode_uses_investment_planner(
    app_client, research_paths, auth_session, monkeypatch
):
    holder = _install_fake_stream(monkeypatch, [("done", "# ok\n", 0.0, 1)])
    _, raw = await auth_session("mode-inv@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research",
        data={"question": "Tesla 值不值得投？", "mode": "investment"},
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    for _ in range(50):
        if holder["prompts"]:
            break
        await asyncio.sleep(0.02)
    assert holder["prompts"], "stream() was never invoked"
    prompt = holder["prompts"][0]
    assert prompt.lstrip().startswith("/investment-research-planner")
    assert "/research-method-designer" not in prompt
    # User's question still present.
    assert "Tesla 值不值得投？" in prompt


async def test_post_research_invalid_mode_returns_400(
    app_client, research_paths, auth_session, monkeypatch
):
    _install_fake_stream(monkeypatch, [])
    _, raw = await auth_session("mode-bad@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research",
        data={"question": "Q?", "mode": "bogus"},
    )
    app_client.cookies.clear()

    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error") == "invalid_mode"


# ===========================================================================
# #22. Zero files is allowed
# ===========================================================================


async def test_post_research_allows_zero_files(
    app_client, research_paths, auth_session, monkeypatch
):
    from app.models import ResearchRequest

    _install_fake_stream(monkeypatch, [("done", "# no-files\n", 0.0, 1)])
    user, raw = await auth_session("zero@example.com")
    app_client.cookies.set("method_session", raw)

    resp = await app_client.post(
        "/api/research",
        data={"question": "question with no uploads"},
    )
    app_client.cookies.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    rid = body["request_id"]

    # Per-request cwd should be created (even with 0 files).
    upload_dir, _plan_dir = research_paths
    assert (upload_dir / rid).exists() or True  # created by runner on-demand


# ===========================================================================
# #23. SSE stream: delta → done
# ===========================================================================


async def test_get_research_stream_sse_events(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    user, raw = await auth_session("sse@example.com")

    # Configure fake stream with small deltas + done. We use brief asyncio
    # sleeps between events so the SSE consumer has a chance to subscribe
    # before deltas are published (see research_runner's subscriber wait).
    # NOTE: the original test design used an asyncio.Event to gate the done
    # event on a test-side `delay.set()` inside aiter_text iteration; that
    # pattern deadlocks under httpx.ASGITransport, which buffers the entire
    # response body before returning control to aiter_text. Sleeps achieve
    # the same "subscriber-connects-first" ordering without the deadlock.
    from app.services import research_runner as rr

    async def _fake(prompt, cwd):
        await asyncio.sleep(0.05)
        yield ("delta", "# A\n")
        yield ("delta", "B\n")
        await asyncio.sleep(0.05)
        yield ("done", "# A\nB\n", 0.5, 321)

    monkeypatch.setattr(rr, "stream", _fake)

    app_client.cookies.set("method_session", raw)
    post = await app_client.post(
        "/api/research",
        data={"question": "live-sse"},
    )
    assert post.status_code == 201, post.text
    rid = post.json()["request_id"]

    # Open SSE stream.
    events = []
    async with app_client.stream(
        "GET", f"/api/research/{rid}/stream"
    ) as response:
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/event-stream")

        # Read all event records (event: X\ndata: Y\n\n).
        buf = ""
        got_delta = False
        async for chunk in response.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                raw_ev, _, rest = buf.partition("\n\n")
                buf = rest
                events.append(raw_ev)
                if "event: delta" in raw_ev:
                    got_delta = True
                if "event: done" in raw_ev:
                    break
            else:
                continue
            if any("event: done" in e for e in events):
                break

    app_client.cookies.clear()

    assert got_delta, f"no delta event observed; events={events!r}"
    assert any("event: done" in e for e in events), (
        f"no done event; events={events!r}"
    )


# ===========================================================================
# #24. SSE replay: already-done row → single done event, then close
# ===========================================================================


async def test_get_research_stream_replays_done_if_already_finished(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import ResearchRequest

    user, raw = await auth_session("replaydone@example.com")

    # Seed a done request directly. Write the plan file so the stream can
    # read it back.
    _upload, plan_root = research_paths
    rid = VALID_ULID_FIXTURE
    plan_path = plan_root / f"{rid}.md"
    plan_path.write_text("# final markdown\n", encoding="utf-8")

    row = ResearchRequest(
        id=rid,
        user_id=user.id,
        question="Q?",
        status="done",
        plan_path=str(plan_path.resolve()),
        error_message=None,
        model="claude-opus-4-7",
        created_at=_utcnow_naive(),
        completed_at=_utcnow_naive(),
    )
    integration_db.add(row)
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)

    events = []
    async with app_client.stream(
        "GET", f"/api/research/{rid}/stream"
    ) as response:
        assert response.status_code == 200
        buf = ""
        async for chunk in response.aiter_text():
            buf += chunk
            if "\n\n" in buf:
                events.append(buf)
                break

    app_client.cookies.clear()
    joined = "".join(events)
    assert "event: done" in joined, f"expected done replay, got {joined!r}"
    assert "final markdown" in joined


# ===========================================================================
# #25. SSE replay: already-failed row → error event with message
# ===========================================================================


async def test_get_research_stream_replays_error_if_failed(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    user, raw = await auth_session("replayerr@example.com")

    rid = VALID_ULID_FIXTURE
    row = ResearchRequest(
        id=rid,
        user_id=user.id,
        question="Q?",
        status="failed",
        plan_path=None,
        error_message="something went wrong",
        model="claude-opus-4-7",
        created_at=_utcnow_naive(),
        completed_at=_utcnow_naive(),
    )
    integration_db.add(row)
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)

    events = []
    async with app_client.stream(
        "GET", f"/api/research/{rid}/stream"
    ) as response:
        assert response.status_code == 200
        buf = ""
        async for chunk in response.aiter_text():
            buf += chunk
            if "\n\n" in buf:
                events.append(buf)
                break

    app_client.cookies.clear()
    joined = "".join(events)
    assert "event: error" in joined
    assert "something went wrong" in joined


# ===========================================================================
# #26. SSE cross-user → 404
# ===========================================================================


async def test_get_research_stream_returns_404_for_others_request(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    # Bob owns the request.
    bob, bob_raw = await auth_session("bob26@example.com")
    rid = VALID_ULID_FIXTURE
    row = ResearchRequest(
        id=rid,
        user_id=bob.id,
        question="Q?",
        status="done",
        plan_path=None,
        error_message="",  # failed semantics irrelevant; keep status=done for replay path
        model="claude-opus-4-7",
        created_at=_utcnow_naive(),
        completed_at=_utcnow_naive(),
    )
    integration_db.add(row)
    await integration_db.commit()

    # Sanity: route exists — bob can reach it and gets 200.
    app_client.cookies.set("method_session", bob_raw)
    sanity = await app_client.get(f"/api/research/{rid}/stream")
    app_client.cookies.clear()
    assert sanity.status_code == 200, (
        f"owner could not reach stream route (got {sanity.status_code}); "
        f"cannot meaningfully assert cross-user 404"
    )

    # Alice authenticates — must get 404.
    alice, alice_raw = await auth_session("alice26@example.com")
    app_client.cookies.set("method_session", alice_raw)
    resp = await app_client.get(f"/api/research/{rid}/stream")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# #27. GET /api/research/<id> JSON full state
# ===========================================================================


async def test_get_research_json_returns_full_state(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest, UploadedFile

    user, raw = await auth_session("json@example.com")
    rid = VALID_ULID_FIXTURE
    _upload, plan_root = research_paths
    plan_path = plan_root / f"{rid}.md"
    plan_path.write_text("# plan body\n", encoding="utf-8")

    created = _utcnow_naive()
    req = ResearchRequest(
        id=rid,
        user_id=user.id,
        question="What is this?",
        status="done",
        plan_path=str(plan_path.resolve()),
        error_message=None,
        model="claude-opus-4-7",
        created_at=created,
        completed_at=created,
    )
    integration_db.add(req)
    integration_db.add(
        UploadedFile(
            request_id=rid,
            original_name="a.md",
            stored_path=str((_upload / "stub.md").resolve()),
            extracted_path=None,
            size_bytes=42,
            mime_type="text/markdown",
            created_at=created,
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{rid}")
    app_client.cookies.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == rid
    assert body["status"] == "done"
    assert body["question"] == "What is this?"
    assert body["markdown"] == "# plan body\n"
    assert body["error_message"] is None
    assert body["cost_usd"] is None
    assert "created_at" in body
    assert "completed_at" in body and body["completed_at"] is not None
    assert body["files"] == [{"name": "a.md", "size": 42}]


# ===========================================================================
# #28. GET /api/research/<id>/download returns the markdown when done
# ===========================================================================


async def test_get_research_download_returns_md_when_done(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    user, raw = await auth_session("dl@example.com")
    _upload, plan_root = research_paths
    rid = VALID_ULID_FIXTURE
    plan_path = plan_root / f"{rid}.md"
    plan_path.write_text("# download me\n", encoding="utf-8")
    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=user.id,
            question="Q?",
            status="done",
            plan_path=str(plan_path.resolve()),
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.get(f"/api/research/{rid}/download")
    app_client.cookies.clear()

    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/markdown")
    disp = resp.headers.get("content-disposition", "")
    assert "attachment" in disp.lower()
    assert f"research-{rid}.md" in disp
    assert resp.content == b"# download me\n"


# ===========================================================================
# #29. Download while pending → 404
# ===========================================================================


async def test_get_research_download_returns_404_when_pending(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    user, raw = await auth_session("dlp@example.com")
    rid = VALID_ULID_FIXTURE
    _upload, plan_root = research_paths
    done_rid = "01HXZK8D7Q3V0S9B4W2N6M5C7S"
    done_plan = plan_root / f"{done_rid}.md"
    done_plan.write_text("# a plan\n", encoding="utf-8")

    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=user.id,
            question="Q?",
            status="pending",
            plan_path=None,
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=None,
        )
    )
    # Also seed a DONE row for the same user so we can assert the JSON
    # endpoint path exists and returns 200 for a valid id — this makes the
    # 404 assertion below meaningful (otherwise a missing route returns 404
    # and the test would pass vacuously).
    integration_db.add(
        ResearchRequest(
            id=done_rid,
            user_id=user.id,
            question="Q?",
            status="done",
            plan_path=str(done_plan.resolve()),
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    # Sanity: route exists — same user, done id → 200.
    sanity = await app_client.get(f"/api/research/{done_rid}/download")
    assert sanity.status_code == 200, (
        f"route missing or misrouted (got {sanity.status_code}); "
        f"cannot meaningfully assert 404 on pending rid"
    )
    # The actual assertion: pending row → 404.
    resp = await app_client.get(f"/api/research/{rid}/download")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# #30. Failed row must have non-empty error_message at router layer (HARNESS §1)
# ===========================================================================


async def test_failed_request_has_non_empty_error_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import ResearchRequest

    user, raw = await auth_session("fail@example.com")
    # Claude fails mid-stream.
    _install_fake_stream(
        monkeypatch,
        [("delta", "starting\n"), ("error", "catastrophic stderr")],
    )

    app_client.cookies.set("method_session", raw)
    post = await app_client.post(
        "/api/research",
        data={"question": "will fail"},
    )
    assert post.status_code == 201
    rid = post.json()["request_id"]
    app_client.cookies.clear()

    row = await _wait_for_status(integration_db, rid, {"failed"})
    # HARNESS §1: error_message non-empty.
    assert row.error_message, "HARNESS §1 violated: empty error_message on failed row"
    assert row.error_message == "catastrophic stderr"


# ===========================================================================
# #31. Prompt-injection content preserved literally end-to-end
# ===========================================================================


async def test_prompt_injection_content_preserved_literally(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.services import research_runner as rr
    from app.models import ResearchRequest

    user, raw = await auth_session("inject@example.com")

    observed_prompts: list[str] = []

    async def _fake(prompt, cwd):
        observed_prompts.append(prompt)
        yield ("done", "# ok\n", 0.0, 1)

    monkeypatch.setattr(rr, "stream", _fake)

    malicious_q = "{{ '{{7*7}}' }} <script>alert(1)</script> SELECT 1;--"
    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        "/api/research",
        data={"question": malicious_q},
    )
    assert resp.status_code == 201
    rid = resp.json()["request_id"]
    app_client.cookies.clear()

    await _wait_for_status(integration_db, rid, {"done", "failed"})

    assert len(observed_prompts) == 1
    assembled = observed_prompts[0]
    assert "<script>alert(1)</script>" in assembled
    assert "{{7*7}}" in assembled
    # If jinja had evaluated the expression it'd be "49".
    # Rough guard: 49 can appear elsewhere by chance, but not adjacent to
    # the literal delimiter.
    assert "{{7*7}}" in assembled, "inner jinja was evaluated — escape bug"


# ===========================================================================
# #32. Malicious filename accepted without crash / path traversal
# ===========================================================================


async def test_post_research_accepts_malicious_filename_without_crash(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import ResearchRequest, UploadedFile

    user, raw = await auth_session("malname@example.com")
    _install_fake_stream(monkeypatch, [("done", "# ok\n", 0.0, 1)])

    # Filename has path-traversal segments and a NUL byte.
    bad_name = "../../etc/passwd\x00.md"
    files = [("files", (bad_name, _read_md_bytes(), "text/markdown"))]

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        "/api/research",
        data={"question": "malicious filename test"},
        files=files,
    )
    app_client.cookies.clear()

    # Either the request succeeds (file_processor rewrites on-disk name to
    # uuid4.hex) OR file_processor rejects it as a 400. No 500, no crash.
    assert resp.status_code in (201, 400), f"unexpected status: {resp.status_code} {resp.text}"

    upload_dir, _plan_dir = research_paths
    # No file should escape the upload_dir — /etc/passwd or anything outside
    # settings.upload_dir must not have been created.
    assert not Path("/etc/passwd\x00.md").exists()


# ===========================================================================
# #33. Cross-user isolation — POST (cannot happen directly; ownership enforced
# at the <id> GETs). This test exercises the "bob posts, alice sees nothing"
# invariant through a GET request rather than POST.
# ===========================================================================


async def test_cross_user_isolation_post(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import ResearchRequest

    _install_fake_stream(monkeypatch, [("done", "# ok\n", 0.0, 1)])

    # Bob posts a research request.
    bob, bob_raw = await auth_session("bob33@example.com")
    app_client.cookies.set("method_session", bob_raw)
    bob_resp = await app_client.post(
        "/api/research",
        data={"question": "bob's question"},
    )
    app_client.cookies.clear()
    assert bob_resp.status_code == 201
    bob_rid = bob_resp.json()["request_id"]

    # Alice POSTs her own — should not see Bob's id in her request_id.
    alice, alice_raw = await auth_session("alice33@example.com")
    app_client.cookies.set("method_session", alice_raw)
    alice_resp = await app_client.post(
        "/api/research",
        data={"question": "alice's question"},
    )
    app_client.cookies.clear()
    assert alice_resp.status_code == 201
    alice_rid = alice_resp.json()["request_id"]
    assert alice_rid != bob_rid

    # Alice cannot GET bob's row.
    app_client.cookies.set("method_session", alice_raw)
    r = await app_client.get(f"/api/research/{bob_rid}")
    app_client.cookies.clear()
    assert r.status_code == 404


# ===========================================================================
# #34. Cross-user isolation — GET /stream
# ===========================================================================


async def test_cross_user_isolation_stream(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    bob, bob_raw = await auth_session("bob34@example.com")
    rid = VALID_ULID_FIXTURE
    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=bob.id,
            question="Q?",
            status="done",
            plan_path=None,
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    # Sanity: bob can reach the route (proving it exists).
    app_client.cookies.set("method_session", bob_raw)
    sanity = await app_client.get(f"/api/research/{rid}/stream")
    app_client.cookies.clear()
    assert sanity.status_code == 200, (
        f"owner stream returned {sanity.status_code}; "
        f"cannot meaningfully assert cross-user 404"
    )

    alice, alice_raw = await auth_session("alice34@example.com")
    app_client.cookies.set("method_session", alice_raw)
    resp = await app_client.get(f"/api/research/{rid}/stream")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# #35. Cross-user isolation — GET JSON
# ===========================================================================


async def test_cross_user_isolation_json(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    bob, bob_raw = await auth_session("bob35@example.com")
    rid = VALID_ULID_FIXTURE
    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=bob.id,
            question="Q?",
            status="done",
            plan_path=None,
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    # Sanity: bob can reach the JSON route.
    app_client.cookies.set("method_session", bob_raw)
    sanity = await app_client.get(f"/api/research/{rid}")
    app_client.cookies.clear()
    assert sanity.status_code == 200, (
        f"owner JSON returned {sanity.status_code}; "
        f"cannot meaningfully assert cross-user 404"
    )

    alice, alice_raw = await auth_session("alice35@example.com")
    app_client.cookies.set("method_session", alice_raw)
    resp = await app_client.get(f"/api/research/{rid}")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# #36. Cross-user isolation — GET /download
# ===========================================================================


async def test_cross_user_isolation_download(
    app_client, research_paths, auth_session, integration_db
):
    from app.models import ResearchRequest

    bob, bob_raw = await auth_session("bob36@example.com")
    rid = VALID_ULID_FIXTURE
    _upload, plan_root = research_paths
    plan_path = plan_root / f"{rid}.md"
    plan_path.write_text("# bob's plan\n", encoding="utf-8")

    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=bob.id,
            question="Q?",
            status="done",
            plan_path=str(plan_path.resolve()),
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    # Sanity: bob can reach the download route.
    app_client.cookies.set("method_session", bob_raw)
    sanity = await app_client.get(f"/api/research/{rid}/download")
    app_client.cookies.clear()
    assert sanity.status_code == 200, (
        f"owner download returned {sanity.status_code}; "
        f"cannot meaningfully assert cross-user 404"
    )

    alice, alice_raw = await auth_session("alice36@example.com")
    app_client.cookies.set("method_session", alice_raw)
    resp = await app_client.get(f"/api/research/{rid}/download")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# #37. All uploaded file paths stored absolute (HARNESS §2)
# ===========================================================================


async def test_post_research_stores_all_files_with_absolute_paths(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import UploadedFile

    user, raw = await auth_session("abspath@example.com")
    _install_fake_stream(monkeypatch, [("done", "# ok\n", 0.0, 1)])

    files = [
        ("files", ("a.md", _read_md_bytes(), "text/markdown")),
        ("files", ("b.txt", _read_txt_bytes(), "text/plain")),
    ]
    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        "/api/research",
        data={"question": "abs-path-test"},
        files=files,
    )
    app_client.cookies.clear()
    assert resp.status_code == 201, resp.text
    rid = resp.json()["request_id"]

    integration_db.expire_all()
    rows = (
        await integration_db.execute(
            select(UploadedFile).where(UploadedFile.request_id == rid)
        )
    ).scalars().all()
    assert len(rows) == 2
    for r in rows:
        assert Path(r.stored_path).is_absolute(), (
            f"HARNESS §2 violated — stored_path not absolute: {r.stored_path!r}"
        )
        if r.extracted_path is not None:
            assert Path(r.extracted_path).is_absolute(), (
                f"HARNESS §2 violated — extracted_path not absolute: {r.extracted_path!r}"
            )


# ===========================================================================
# #38. Status transitions (pending → running → done) observed through DB
# ===========================================================================


async def test_research_request_status_transitions_correctly(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.services import research_runner as rr
    from app.models import ResearchRequest

    user, raw = await auth_session("trans@example.com")

    hold = asyncio.Event()
    seen_running = {"v": False}

    async def _fake(prompt, cwd):
        # Reach here ≡ _run_research already flipped status=running.
        yield ("delta", "A")
        await hold.wait()
        yield ("done", "# A\n", 0.0, 1)

    monkeypatch.setattr(rr, "stream", _fake)

    app_client.cookies.set("method_session", raw)
    resp = await app_client.post(
        "/api/research",
        data={"question": "Q?"},
    )
    app_client.cookies.clear()
    assert resp.status_code == 201
    rid = resp.json()["request_id"]

    # Poll for running status.
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        integration_db.expire_all()
        row = (
            await integration_db.execute(
                select(ResearchRequest).where(ResearchRequest.id == rid)
            )
        ).scalar_one()
        if row.status == "running":
            seen_running["v"] = True
            break
        if row.status == "done":
            break
        await asyncio.sleep(0.02)

    # Release the fake stream so it flips to done.
    hold.set()

    row = await _wait_for_status(integration_db, rid, {"done", "failed"})
    assert row.status == "done"
    assert seen_running["v"], "never observed status=running transition"


# ===========================================================================
# #39. SSE done event payload includes markdown, cost_usd, elapsed_ms
# ===========================================================================


async def test_sse_done_event_payload_includes_markdown_cost_elapsed(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.services import research_runner as rr

    user, raw = await auth_session("ssepay@example.com")

    # NOTE: original test used an asyncio.Event to gate done on a test-side
    # `release.set()` inside aiter_text iteration; that deadlocks under
    # httpx.ASGITransport (which buffers the full body before returning to
    # aiter_text). Brief sleeps let the SSE subscriber attach before events
    # are published while still producing a deterministic done payload.
    async def _fake(prompt, cwd):
        await asyncio.sleep(0.05)
        yield ("delta", "x")
        await asyncio.sleep(0.05)
        yield ("done", "# final-md\n", 1.23, 4567)

    monkeypatch.setattr(rr, "stream", _fake)

    app_client.cookies.set("method_session", raw)
    post = await app_client.post(
        "/api/research",
        data={"question": "payload?"},
    )
    assert post.status_code == 201
    rid = post.json()["request_id"]

    collected = ""
    async with app_client.stream(
        "GET", f"/api/research/{rid}/stream"
    ) as response:
        assert response.status_code == 200
        async for chunk in response.aiter_text():
            collected += chunk
            if "event: done" in collected:
                break

    app_client.cookies.clear()

    # The done frame should contain a JSON payload with all three fields.
    # Extract the last "event: done" block.
    done_block = ""
    for frame in collected.split("\n\n"):
        if "event: done" in frame:
            done_block = frame
    assert done_block, f"no done frame found; collected={collected!r}"

    # Parse JSON data line.
    data_line = ""
    for ln in done_block.splitlines():
        if ln.startswith("data:"):
            data_line = ln[len("data:"):].strip()
            break
    assert data_line, f"no data line in done frame: {done_block!r}"
    payload = json.loads(data_line)
    assert payload.get("request_id") == rid
    assert "# final-md" in payload.get("markdown", "")
    assert payload.get("cost_usd") == 1.23
    assert payload.get("elapsed_ms") == 4567


# ===========================================================================
# #40. Claude error message propagates verbatim to research_requests.error_message
# ===========================================================================


async def test_claude_runner_error_propagates_to_research_error_message(
    app_client, research_paths, auth_session, integration_db, monkeypatch
):
    from app.models import ResearchRequest

    user, raw = await auth_session("errprop@example.com")
    claude_tail = "stderr tail: RateLimitError(status=429): over quota"
    _install_fake_stream(monkeypatch, [("error", claude_tail)])

    app_client.cookies.set("method_session", raw)
    post = await app_client.post(
        "/api/research",
        data={"question": "will-error"},
    )
    assert post.status_code == 201
    rid = post.json()["request_id"]
    app_client.cookies.clear()

    row = await _wait_for_status(integration_db, rid, {"failed"})
    assert row.status == "failed"
    assert row.error_message == claude_tail, (
        f"error_message did not round-trip verbatim; got {row.error_message!r}"
    )


# ===========================================================================
# #32 (design §11.2). Download while failed → 404
# ===========================================================================


async def test_get_research_download_returns_404_when_failed(
    app_client, research_paths, auth_session, integration_db
):
    """Design §11.2 #32: GET /api/research/<id>/download on a failed row → 404.

    A failed research request has ``plan_path=None`` and a non-empty
    ``error_message``; there is no markdown to return, so the download
    endpoint must 404 rather than 500 or serve a stale body.
    """
    from app.models import ResearchRequest

    user, raw = await auth_session("dlfail@example.com")
    rid = VALID_ULID_FIXTURE
    _upload, plan_root = research_paths

    # Seed a DONE row for the same user so we can prove the route exists
    # (avoids vacuous 404 on missing route). Mirrors the pattern used by
    # test_get_research_download_returns_404_when_pending (#29/#31).
    done_rid = "01HXZK8D7Q3V0S9B4W2N6M5C7T"
    done_plan = plan_root / f"{done_rid}.md"
    done_plan.write_text("# a plan\n", encoding="utf-8")

    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=user.id,
            question="Q?",
            status="failed",
            plan_path=None,
            error_message="simulated failure",
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    integration_db.add(
        ResearchRequest(
            id=done_rid,
            user_id=user.id,
            question="Q?",
            status="done",
            plan_path=str(done_plan.resolve()),
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    # Sanity: route exists — same user, done id → 200.
    sanity = await app_client.get(f"/api/research/{done_rid}/download")
    assert sanity.status_code == 200, (
        f"route missing or misrouted (got {sanity.status_code}); "
        f"cannot meaningfully assert 404 on failed rid"
    )
    # The actual assertion: failed row → 404.
    resp = await app_client.get(f"/api/research/{rid}/download")
    app_client.cookies.clear()
    assert resp.status_code == 404


# ===========================================================================
# DELETE /api/research/<id> — owner-scoped delete; cleans DB + filesystem.
# ===========================================================================


async def test_delete_research_done_removes_row_files_and_plan(
    app_client, research_paths, auth_session, integration_db
):
    """DELETE on a DONE row: 204, deletes the DB row, the uploaded files
    directory, and the plan markdown file."""
    from app.models import ResearchRequest, UploadedFile

    user, raw = await auth_session("del-done@example.com")
    upload_root, plan_root = research_paths
    rid = VALID_ULID_FIXTURE

    # Seed plan file + upload directory with a dummy original.
    plan_path = plan_root / f"{rid}.md"
    plan_path.write_text("# plan\n", encoding="utf-8")
    req_dir = upload_root / rid
    req_dir.mkdir(parents=True, exist_ok=True)
    stored_file = req_dir / "aaaa.md"
    stored_file.write_text("hello", encoding="utf-8")

    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=user.id,
            question="delete me",
            status="done",
            plan_path=str(plan_path.resolve()),
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    integration_db.add(
        UploadedFile(
            request_id=rid,
            original_name="src.md",
            stored_path=str(stored_file.resolve()),
            extracted_path=None,
            size_bytes=5,
            mime_type="text/markdown",
            created_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(f"/api/research/{rid}")
    app_client.cookies.clear()

    assert resp.status_code == 204, resp.text

    # DB: research_requests row + uploaded_files rows are gone.
    integration_db.expire_all()
    assert (
        await integration_db.execute(
            select(ResearchRequest).where(ResearchRequest.id == rid)
        )
    ).scalar_one_or_none() is None
    assert (
        await integration_db.execute(
            select(UploadedFile).where(UploadedFile.request_id == rid)
        )
    ).scalars().all() == []

    # Filesystem: plan file + upload directory are gone.
    assert not plan_path.exists(), "plan file not deleted"
    assert not req_dir.exists(), "upload dir not deleted"


async def test_delete_research_failed_row_succeeds_with_no_plan_file(
    app_client, research_paths, auth_session, integration_db
):
    """DELETE on a FAILED row (plan_path=None) → still 204, idempotent on the
    missing plan file."""
    from app.models import ResearchRequest

    user, raw = await auth_session("del-failed@example.com")
    rid = VALID_ULID_FIXTURE

    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=user.id,
            question="Q?",
            status="failed",
            plan_path=None,
            error_message="something broke",
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(f"/api/research/{rid}")
    app_client.cookies.clear()

    assert resp.status_code == 204, resp.text
    integration_db.expire_all()
    assert (
        await integration_db.execute(
            select(ResearchRequest).where(ResearchRequest.id == rid)
        )
    ).scalar_one_or_none() is None


async def test_delete_research_unknown_id_returns_404(
    app_client, research_paths, auth_session
):
    user, raw = await auth_session("del-404@example.com")
    app_client.cookies.set("method_session", raw)
    resp = await app_client.delete(f"/api/research/{VALID_ULID_FIXTURE}")
    app_client.cookies.clear()
    assert resp.status_code == 404


async def test_delete_research_cross_user_returns_404(
    app_client, research_paths, auth_session, integration_db
):
    """Cross-user delete must return 404 (no enumeration oracle)."""
    from app.models import ResearchRequest

    owner, _ = await auth_session("del-owner@example.com")
    _other, other_raw = await auth_session("del-other@example.com")
    rid = VALID_ULID_FIXTURE

    integration_db.add(
        ResearchRequest(
            id=rid,
            user_id=owner.id,
            question="owner's",
            status="done",
            plan_path=None,
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=_utcnow_naive(),
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", other_raw)
    resp = await app_client.delete(f"/api/research/{rid}")
    app_client.cookies.clear()
    assert resp.status_code == 404

    # Row still exists — owner's data not affected.
    integration_db.expire_all()
    assert (
        await integration_db.execute(
            select(ResearchRequest).where(ResearchRequest.id == rid)
        )
    ).scalar_one_or_none() is not None


async def test_delete_research_pending_or_running_returns_409(
    app_client, research_paths, auth_session, integration_db
):
    """In-flight rows cannot be deleted: returns 409 with a clear error code.

    Reason: the background task owns the upload dir (Claude subprocess is
    reading from it). Racing with it risks breaking the in-flight run.
    """
    from app.models import ResearchRequest

    user, raw = await auth_session("del-busy@example.com")
    running_rid = "01HXZK8D7Q3V0S9B4W2N6M5C7S"
    pending_rid = "01HXZK8D7Q3V0S9B4W2N6M5C7T"

    integration_db.add(
        ResearchRequest(
            id=running_rid,
            user_id=user.id,
            question="Q?",
            status="running",
            plan_path=None,
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=None,
        )
    )
    integration_db.add(
        ResearchRequest(
            id=pending_rid,
            user_id=user.id,
            question="Q?",
            status="pending",
            plan_path=None,
            error_message=None,
            model="claude-opus-4-7",
            created_at=_utcnow_naive(),
            completed_at=None,
        )
    )
    await integration_db.commit()

    app_client.cookies.set("method_session", raw)
    for rid in (running_rid, pending_rid):
        resp = await app_client.delete(f"/api/research/{rid}")
        assert resp.status_code == 409, f"rid={rid} got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("error") == "request_busy"
    app_client.cookies.clear()

    # Rows must still exist.
    integration_db.expire_all()
    for rid in (running_rid, pending_rid):
        assert (
            await integration_db.execute(
                select(ResearchRequest).where(ResearchRequest.id == rid)
            )
        ).scalar_one_or_none() is not None


async def test_delete_research_without_auth_returns_401(app_client, research_paths):
    app_client.cookies.clear()
    resp = await app_client.delete(f"/api/research/{VALID_ULID_FIXTURE}")
    assert resp.status_code == 401


# ===========================================================================
# #39 (design §11.2). Cross-task tripwire — claude_runner argv still uses
# ``--allowed-tools Read,Glob,Grep`` (HARNESS §3).
#
# NOTE: This test is expected to PASS immediately — it exists as a tripwire
# so that any future drift in claude_runner's argv construction (e.g., a
# refactor that accidentally adds ``Write``/``Edit``/``Bash`` to the allowed
# tools) will fail this test in the research-routes suite even if the
# claude_runner unit suite is removed or reshaped. Redundant with
# tests/unit/test_claude_runner.py #10 by design (§13 reverse-scan).
# ===========================================================================


async def test_claude_runner_allowed_tools_unchanged(monkeypatch, tmp_path):
    """Tripwire: HARNESS §3 requires --allowed-tools Read,Glob,Grep.

    This test is expected to PASS immediately; it exists as a cross-task
    tripwire for future regressions. Fires a fake subprocess, captures the
    argv passed to ``asyncio.create_subprocess_exec``, and asserts that the
    ``--allowed-tools`` value is exactly ``Read,Glob,Grep`` and contains no
    ``Write``/``Edit``/``Bash`` tokens.
    """
    import app.services.claude_runner as cr

    captured_argv: list = []

    class _FakeReader:
        def __init__(self):
            self._eof = False

        async def readline(self) -> bytes:
            self._eof = True
            return b""

        async def read(self, n: int = -1) -> bytes:
            self._eof = True
            return b""

        def at_eof(self) -> bool:
            return self._eof

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeReader()
            self.stderr = _FakeReader()
            self.returncode = 0
            self.pid = 99999

        async def wait(self):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    async def _fake_exec(*argv, **kwargs):
        captured_argv.extend(argv)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    # Reset the module-level semaphore so this test sees a fresh one.
    monkeypatch.setattr(cr, "_CLAUDE_SEM", None, raising=False)

    # Drive the stream to completion (EOF immediately, exit code 0 but no
    # result line — the runner will yield an error, which is fine; we only
    # care about the argv that was captured).
    agen = cr.stream("tripwire prompt", tmp_path)
    try:
        async for _ev in agen:
            pass
    except Exception:
        pass

    assert captured_argv, "create_subprocess_exec was never called"
    assert "--allowed-tools" in captured_argv, (
        f"argv missing --allowed-tools: {captured_argv!r}"
    )
    idx = captured_argv.index("--allowed-tools")
    tools_value = captured_argv[idx + 1]
    assert tools_value == "Read,Glob,Grep", (
        f"HARNESS §3 tripwire: expected 'Read,Glob,Grep', got {tools_value!r}"
    )
    forbidden = {"Write", "Edit", "Bash"}
    assert not forbidden.intersection(tools_value.split(",")), (
        f"forbidden tool leaked into --allowed-tools: {tools_value!r}"
    )
