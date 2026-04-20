"""Unit tests for ``app.services.research_runner`` (Task 3.3).

Contract source: ``docs/design/issue-2-task-3.3-research-routes.md`` §3, §4, §5.
These tests are RED until ``app/services/research_runner.py`` and
``app/templates/prompts/research.j2`` exist.

Coverage binding (design §10 + §11.1):

- ``app/services/research_runner.py``:
    tests 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15
- ``app/templates/prompts/research.j2``:
    tests 7, 8, 9, 16

Strategy: mock ``claude_runner.stream`` at the research_runner import seam
(``app.services.research_runner.stream``). Drive canned events from an
async generator. Real DB via ``db_session``; real filesystem via
``settings.upload_dir`` / ``settings.plan_dir`` pinned to ``tmp_path``.
Never spawn a real claude subprocess.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _utcnow_naive():
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None)


# A valid 26-char Crockford-base32 ULID for tests that insert rows directly.
VALID_ULID_1 = "01HXZK8D7Q3V0S9B4W2N6M5C7R"
VALID_ULID_2 = "01HXZK8D7Q3V0S9B4W2N6M5C7S"


@pytest.fixture
def pinned_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Pin settings.upload_dir / settings.plan_dir to a tmp tree."""
    from app import config as config_mod

    upload = tmp_path / "uploads"
    plan = tmp_path / "plans"
    upload.mkdir(parents=True, exist_ok=True)
    plan.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod.settings, "upload_dir", str(upload))
    monkeypatch.setattr(config_mod.settings, "plan_dir", str(plan))
    return upload, plan


def make_fake_stream(events: list):
    """Return an async generator coroutine mimicking ``claude_runner.stream``."""

    async def _gen(prompt, cwd):
        for ev in events:
            yield ev

    return _gen


async def _insert_pending_request(session, request_id: str, user_id: int, question: str = "Q"):
    """Insert a pending ResearchRequest and commit. Returns the row."""
    from app.models import ResearchRequest

    row = ResearchRequest(
        id=request_id,
        user_id=user_id,
        question=question,
        status="pending",
        plan_path=None,
        error_message=None,
        model="claude-opus-4-7",
        created_at=_utcnow_naive(),
        completed_at=None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _insert_user(session, email: str = "u@example.com"):
    from app.models import User

    u = User(
        email=email,
        status="active",
        created_at=_utcnow_naive(),
        approved_at=_utcnow_naive(),
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


# ===========================================================================
# #1. run_research flips pending → running → done (observe terminal state)
# ===========================================================================


async def test_run_research_marks_status_running_then_done(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import ResearchRequest
    from app.services import research_runner

    user = await _insert_user(db_session, "s1@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id, "what is X?")

    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("delta", "# Plan\n"), ("done", "# Plan\nBody\n", 0.42, 5000)]),
    )

    # _run_research is the coroutine body; run_research spawns via create_task.
    # Unit tests drive the coroutine directly so we can await completion.
    await research_runner._run_research(VALID_ULID_1)

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(ResearchRequest).where(ResearchRequest.id == VALID_ULID_1)
        )
    ).scalar_one()
    assert row.status == "done"
    assert row.completed_at is not None
    assert row.error_message in (None, "")


# ===========================================================================
# #2. plan_path is absolute, file exists, content matches done-event markdown
# ===========================================================================


async def test_run_research_writes_plan_path_on_done(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import ResearchRequest
    from app.services import research_runner

    upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "s2@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    final_md = "# Research plan\n\n## 1. Question\n...\n"
    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("done", final_md, 0.1, 1234)]),
    )

    await research_runner._run_research(VALID_ULID_1)

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(ResearchRequest).where(ResearchRequest.id == VALID_ULID_1)
        )
    ).scalar_one()
    assert row.status == "done"
    assert row.plan_path is not None
    # HARNESS §2: absolute path.
    plan_path = Path(row.plan_path)
    assert plan_path.is_absolute(), f"plan_path not absolute: {row.plan_path!r}"
    assert plan_path.exists()
    assert plan_path.read_text(encoding="utf-8") == final_md


# ===========================================================================
# #3. claude error → status=failed, non-empty error_message (HARNESS §1)
# ===========================================================================


async def test_run_research_marks_failed_with_error_on_claude_error(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import ResearchRequest
    from app.services import research_runner

    user = await _insert_user(db_session, "s3@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("delta", "partial\n"), ("error", "boom")]),
    )

    await research_runner._run_research(VALID_ULID_1)

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(ResearchRequest).where(ResearchRequest.id == VALID_ULID_1)
        )
    ).scalar_one()
    assert row.status == "failed"
    # HARNESS §1: failed rows MUST have non-empty error_message.
    assert row.error_message, "error_message empty — HARNESS §1 violation"
    assert row.error_message == "boom"
    assert row.completed_at is not None


# ===========================================================================
# #4. timeout-shaped error bubbles to failed + exact message
# ===========================================================================


async def test_run_research_timeout_marks_failed(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import ResearchRequest
    from app.services import research_runner

    user = await _insert_user(db_session, "s4@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("error", "timeout after 600s")]),
    )

    await research_runner._run_research(VALID_ULID_1)

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(ResearchRequest).where(ResearchRequest.id == VALID_ULID_1)
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error_message == "timeout after 600s"


# ===========================================================================
# #5. pub/sub: one publish reaches all current subscribers
# ===========================================================================


async def test_pubsub_publishes_to_subscribers(pinned_dirs):
    from app.services import research_runner

    q1 = research_runner.subscribe(VALID_ULID_1)
    q2 = research_runner.subscribe(VALID_ULID_1)

    ev = ("delta", "hello")
    # _publish is sync (per design §5). It takes rid + event.
    research_runner._publish(VALID_ULID_1, ev)

    got1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    got2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert got1 == ev
    assert got2 == ev

    # Cleanup so later tests see an empty registry.
    research_runner.unsubscribe(VALID_ULID_1, q1)
    research_runner.unsubscribe(VALID_ULID_1, q2)


# ===========================================================================
# #6. unsubscribe removes the queue from the channel list
# ===========================================================================


async def test_pubsub_unsubscribe_removes_queue(pinned_dirs):
    from app.services import research_runner

    q = research_runner.subscribe(VALID_ULID_2)
    # Confirm present.
    chans = research_runner._channels.get(VALID_ULID_2) or []
    assert q in chans

    research_runner.unsubscribe(VALID_ULID_2, q)

    # After last unsubscribe the channel key should be removed (design §5).
    assert VALID_ULID_2 not in research_runner._channels

    # Publishing after unsubscribe must not raise.
    research_runner._publish(VALID_ULID_2, ("delta", "ignored"))


# ===========================================================================
# #7. Prompt template: uploaded_files list renders names + local_paths
# ===========================================================================


async def test_prompt_template_includes_uploaded_files(pinned_dirs):
    from app.services import research_runner

    # _PromptFile is the dataclass described in design §4. Tolerate small
    # naming variation by building the expected shape the template consumes.
    pf_cls = research_runner._PromptFile
    files = [
        pf_cls(
            original_name="report.pdf",
            local_path="/abs/uploads/01HXZ.../aaa.extracted.md",
            kind="text",
        ),
        pf_cls(
            original_name="notes.md",
            local_path="/abs/uploads/01HXZ.../bbb.md",
            kind="text",
        ),
    ]
    out = research_runner._render_prompt("my question", files)
    assert "my question" in out
    assert "report.pdf" in out
    assert "/abs/uploads/01HXZ.../aaa.extracted.md" in out
    assert "notes.md" in out
    assert "/abs/uploads/01HXZ.../bbb.md" in out
    # Chinese header from the template is present.
    assert "用户上传了以下资料" in out
    # text-kind files get no suffix annotation.
    assert "解析失败" not in out
    assert "PDF 文本层为空" not in out


# ===========================================================================
# #8. Prompt template: no files → no uploaded-files section
# ===========================================================================


async def test_prompt_template_omits_files_section_when_empty(pinned_dirs):
    from app.services import research_runner

    out = research_runner._render_prompt("hello", [])
    assert "hello" in out
    assert "用户上传了以下资料" not in out


# ===========================================================================
# #9. Prompt template: kind="failed" (e.g. docx extraction failed) →
#     marked as (解析失败，已忽略)
# ===========================================================================


async def test_prompt_template_notes_extraction_failed_files(pinned_dirs):
    from app.services import research_runner

    pf_cls = research_runner._PromptFile
    files = [
        pf_cls(
            original_name="weird.docx",
            local_path="/abs/uploads/01HXZ.../weird.docx",
            kind="failed",
        ),
    ]
    out = research_runner._render_prompt("Q?", files)
    assert "weird.docx" in out
    assert "解析失败，已忽略" in out


# ===========================================================================
# #9b. Prompt template: kind="pdf_scan" (scanned PDF) → planner is told to
#      Read the original PDF directly, NOT to ignore it.
# ===========================================================================


async def test_prompt_template_instructs_direct_read_for_scanned_pdf(pinned_dirs):
    from app.services import research_runner

    pf_cls = research_runner._PromptFile
    files = [
        pf_cls(
            original_name="scan.pdf",
            local_path="/abs/uploads/01HXZ.../scan.pdf",
            kind="pdf_scan",
        ),
    ]
    out = research_runner._render_prompt("Q?", files)
    assert "scan.pdf" in out
    assert "/abs/uploads/01HXZ.../scan.pdf" in out
    # Scanned PDFs are NOT ignored — planner is instructed to Read them.
    assert "解析失败，已忽略" not in out
    assert "PDF 文本层为空" in out
    assert "Read" in out


# ===========================================================================
# #9c. _files_to_prompt_files: an UploadedFile for a .pdf with
#      extracted_path=None (scanned PDF path) → kind="pdf_scan" with
#      local_path pointing at the original PDF.
# ===========================================================================


async def test_files_to_prompt_files_classifies_scanned_pdf_as_pdf_scan(pinned_dirs):
    from app.services import research_runner

    class _Row:
        def __init__(self, original_name, stored_path, extracted_path):
            self.original_name = original_name
            self.stored_path = stored_path
            self.extracted_path = extracted_path

    rows = [
        _Row("scan.pdf", "/abs/uploads/rid/x.pdf", None),          # scanned pdf
        _Row("paper.pdf", "/abs/uploads/rid/y.pdf", "/abs/uploads/rid/y.extracted.md"),
        _Row("notes.md", "/abs/uploads/rid/n.md", None),
        _Row("resume.docx", "/abs/uploads/rid/r.docx", None),      # docx ext failed
    ]
    out = research_runner._files_to_prompt_files(rows)
    by_name = {pf.original_name: pf for pf in out}
    assert by_name["scan.pdf"].kind == "pdf_scan"
    assert by_name["scan.pdf"].local_path == "/abs/uploads/rid/x.pdf"
    assert by_name["paper.pdf"].kind == "text"
    assert by_name["paper.pdf"].local_path == "/abs/uploads/rid/y.extracted.md"
    assert by_name["notes.md"].kind == "text"
    assert by_name["notes.md"].local_path == "/abs/uploads/rid/n.md"
    assert by_name["resume.docx"].kind == "failed"


# ===========================================================================
# #10. _log_task_exception logs an exception when the task raised
# ===========================================================================


async def test_run_research_logs_exception_in_task_callback(
    pinned_dirs, caplog
):
    from app.services import research_runner

    async def _boom() -> None:
        raise RuntimeError("kaboom")

    task = asyncio.create_task(_boom())
    try:
        await task
    except RuntimeError:
        pass

    with caplog.at_level(logging.ERROR):
        research_runner._log_task_exception(task)

    # Some text describing the failure must have been emitted.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "kaboom" in joined or "RuntimeError" in joined or "research task failed" in joined


# ===========================================================================
# #11. Two sessions (A and B) — no DB session held during claude stream
# ===========================================================================


async def test_run_research_two_sessions_not_held_across_claude(
    db_session, pinned_dirs, monkeypatch
):
    """Design §3 requires the A-session to be closed before entering the
    claude stream, and a fresh B-session for the terminal write. We verify
    this by tracking get_sessionmaker()() calls and asserting the first
    session is closed before the stream is iterated.
    """
    from app.services import research_runner

    user = await _insert_user(db_session, "s11@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    state = {"stream_entered_while_sessions_open": []}
    # Track currently-open sessions (opened via __aenter__ minus __aexit__).
    open_sessions: list[Any] = []

    real_sessionmaker_factory = research_runner.get_sessionmaker

    class TrackingSession:
        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            await self._inner.__aenter__()
            open_sessions.append(self)
            return self._inner

        async def __aexit__(self, et, ev, tb):
            if self in open_sessions:
                open_sessions.remove(self)
            return await self._inner.__aexit__(et, ev, tb)

    def _tracking_sessionmaker_factory():
        sm = real_sessionmaker_factory()

        def _make():
            return TrackingSession(sm())

        return _make

    monkeypatch.setattr(
        research_runner, "get_sessionmaker", _tracking_sessionmaker_factory
    )

    async def _observing_stream(prompt, cwd):
        # Snapshot how many sessions are open the moment claude starts.
        state["stream_entered_while_sessions_open"].append(len(open_sessions))
        yield ("done", "# md\n", 0.0, 1)

    monkeypatch.setattr(research_runner, "stream", _observing_stream)

    await research_runner._run_research(VALID_ULID_1)

    assert state["stream_entered_while_sessions_open"] == [0], (
        "claude stream entered while a DB session was still open — "
        "design §3 requires s1 to be closed before streaming."
    )


# ===========================================================================
# #12. __close__ sentinel is published at end so subscribers disconnect
# ===========================================================================


async def test_run_research_close_sentinel_publishes_to_disconnect_subscribers(
    db_session, pinned_dirs, monkeypatch
):
    from app.services import research_runner

    user = await _insert_user(db_session, "s12@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("done", "x", 0.0, 1)]),
    )

    q = research_runner.subscribe(VALID_ULID_1)
    try:
        await research_runner._run_research(VALID_ULID_1)

        # Drain everything from the queue with a bounded wait.
        items: list = []
        while True:
            try:
                it = await asyncio.wait_for(q.get(), timeout=0.5)
            except TimeoutError:
                break
            items.append(it)

        # Somewhere in the stream we must see the __close__ sentinel. The
        # design uses ``("__close__",)`` per §3 finally-block.
        found = any(
            (i == ("__close__",)) or (i is None) for i in items
        )
        assert found, f"no close sentinel among published events: {items!r}"
    finally:
        research_runner.unsubscribe(VALID_ULID_1, q)


# ===========================================================================
# #13. Queue maxsize=256 — overflow drops silently, publisher does not block
# ===========================================================================


async def test_queue_maxsize_drops_silently(pinned_dirs):
    from app.services import research_runner

    q = research_runner.subscribe(VALID_ULID_1)
    try:
        # Queue has maxsize=256 per design §5.
        assert q.maxsize == 256

        # Fill the queue.
        for i in range(256):
            research_runner._publish(VALID_ULID_1, ("delta", f"d{i}"))

        # Next publish must not raise (QueueFull swallowed per design §5).
        research_runner._publish(VALID_ULID_1, ("delta", "overflow"))

        # Queue should still be full but intact (no deadlock / no raise).
        assert q.qsize() == 256
    finally:
        research_runner.unsubscribe(VALID_ULID_1, q)


# ===========================================================================
# #14. plan write failure (OSError) → failed, error_message starts with
#     "plan_write_failed:"
# ===========================================================================


async def test_run_research_plan_write_failure_marks_failed_with_message(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import ResearchRequest
    from app.services import research_runner

    user = await _insert_user(db_session, "s14@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("done", "# md\n", 0.1, 1)]),
    )

    def _boom_write(request_id, markdown):
        raise OSError("disk full")

    monkeypatch.setattr(research_runner, "_write_plan", _boom_write)

    await research_runner._run_research(VALID_ULID_1)

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(ResearchRequest).where(ResearchRequest.id == VALID_ULID_1)
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error_message is not None
    assert row.error_message.startswith("plan_write_failed:"), (
        f"expected plan_write_failed: prefix, got {row.error_message!r}"
    )
    # HARNESS §1: error_message non-empty.
    assert row.error_message != ""


# ===========================================================================
# #15. Terminal-session failure in Block B → rescue Block C still flips failed
# ===========================================================================


async def test_run_research_terminal_session_failure_marks_failed_via_rescue(
    db_session, pinned_dirs, monkeypatch
):
    """Simulate s2 blowing up during terminal write. The rescue session (s3)
    must still move the row to failed with an error_message starting with
    ``internal:``. See design §3 outer try/except and HARNESS §1.
    """
    from app.models import ResearchRequest
    from app.services import research_runner

    user = await _insert_user(db_session, "s15@example.com")
    await _insert_pending_request(db_session, VALID_ULID_1, user.id)

    monkeypatch.setattr(
        research_runner,
        "stream",
        make_fake_stream([("done", "# md\n", 0.0, 1)]),
    )

    real_get_sm = research_runner.get_sessionmaker
    calls = {"n": 0}

    def _fake_get_sessionmaker():
        real_sm = real_get_sm()

        def _factory():
            calls["n"] += 1
            if calls["n"] == 2:
                # Second session (terminal write) raises when opened.
                class _Broken:
                    async def __aenter__(self):
                        raise RuntimeError("db down for terminal write")

                    async def __aexit__(self, et, ev, tb):
                        return False

                return _Broken()
            return real_sm()

        return _factory

    monkeypatch.setattr(
        research_runner, "get_sessionmaker", _fake_get_sessionmaker
    )

    await research_runner._run_research(VALID_ULID_1)

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(ResearchRequest).where(ResearchRequest.id == VALID_ULID_1)
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error_message is not None and row.error_message.startswith(
        "internal:"
    ), f"expected internal: prefix, got {row.error_message!r}"


# ===========================================================================
# #16. Malicious content in question + filenames is preserved literally
# ===========================================================================


async def test_prompt_template_preserves_malicious_user_content_literally(
    pinned_dirs,
):
    """The dedicated Environment(autoescape=False) must not HTML-escape and
    must not evaluate nested Jinja expressions the user typed. Filenames
    containing path-traversal sequences must appear literally, not expanded.
    """
    from app.services import research_runner

    pf_cls = research_runner._PromptFile
    malicious_q = "{{ '{{7*7}}' }} <script>alert(1)</script>"
    files = [
        pf_cls(
            original_name="../../etc/passwd",
            local_path="/abs/uploads/01HXZ.../attack.md",
            kind="text",
        ),
    ]
    out = research_runner._render_prompt(malicious_q, files)

    # The template must echo these strings verbatim.
    assert "<script>alert(1)</script>" in out, "autoescape leaked HTML escaping"
    assert "{{7*7}}" in out or "{% " in malicious_q, "nested Jinja evaluated"
    assert "49" not in out.split("{{7*7}}")[0] if "{{7*7}}" in out else True
    assert "../../etc/passwd" in out


# ===========================================================================
# Mode selector (general vs investment) — prompt template routing.
# ===========================================================================


async def test_render_prompt_default_mode_uses_research_method_designer(pinned_dirs):
    from app.services import research_runner

    out = research_runner._render_prompt("Q?", [])
    assert out.lstrip().startswith("/research-method-designer")
    assert "/investment-research-planner" not in out


async def test_render_prompt_mode_general_uses_research_method_designer(pinned_dirs):
    from app.services import research_runner

    out = research_runner._render_prompt("Q?", [], mode="general")
    assert out.lstrip().startswith("/research-method-designer")
    assert "/investment-research-planner" not in out


async def test_render_prompt_mode_investment_uses_investment_research_planner(pinned_dirs):
    from app.services import research_runner

    out = research_runner._render_prompt("Q?", [], mode="investment")
    # Investment template must load the investment-research-planner skill,
    # NOT the generic router.
    assert out.lstrip().startswith("/investment-research-planner")
    assert "/research-method-designer" not in out
    # Question still rendered, Chinese output constraints still apply.
    assert "Q?" in out
    assert "全中文输出" in out


async def test_render_prompt_rejects_unknown_mode(pinned_dirs):
    from app.services import research_runner

    with pytest.raises(ValueError):
        research_runner._render_prompt("Q?", [], mode="bogus")


# ---------------------------------------------------------------------------
# User-friendliness: the prompt must instruct the planner to AVOID internal
# classification labels ("Type A/B/C/D" / "类型 A/B/C/D") and the "问题类型
# 分类" standalone section. These drift silently when someone edits the
# template, so pin them with string-level assertions.
# ---------------------------------------------------------------------------


async def test_general_template_forbids_internal_type_labels(pinned_dirs):
    from app.services import research_runner

    out = research_runner._render_prompt("Q?", [], mode="general")
    # Rule 4: forbid Type A/B/C/D labels.
    assert "Type A" in out and "类型 A" in out, (
        "general template must explicitly list the forbidden type labels"
    )
    # Rule 5: mention jargon-softening.
    assert "术语口语化" in out
    # Rule 6: skip the standalone classification section.
    assert "不要单独开" in out or "不单独开" in out
    assert "问题类型分类" in out


async def test_investment_template_forbids_internal_axis_labels(pinned_dirs):
    from app.services import research_runner

    out = research_runner._render_prompt("Q?", [], mode="investment")
    # Rule 4: forbid Axis / thesis labels.
    assert "Axis" in out
    assert "Long thesis" in out or "Short thesis" in out
    # Rule 5: mention implementation-term softening.
    assert "playbook" in out  # named as forbidden
    assert "术语口语化" in out
    # Rule 6: skip the standalone "投资决策类型分类" section.
    assert "投资决策类型分类" in out


async def test_prompt_template_handles_image_kind(pinned_dirs):
    """Images are surfaced with a hint telling the planner to Read them
    visually, same pattern as ``pdf_scan`` but labeled as image."""
    from app.services import research_runner

    pf_cls = research_runner._PromptFile
    files = [
        pf_cls(
            original_name="shot.png",
            local_path="/abs/uploads/rid/s.png",
            kind="image",
        ),
    ]
    out = research_runner._render_prompt("Q?", files)
    assert "shot.png" in out
    assert "图片文件" in out
    assert "Read" in out
    # NOT marked as failed or as a PDF.
    assert "解析失败，已忽略" not in out
    assert "PDF 文本层为空" not in out


async def test_files_to_prompt_files_classifies_images_as_image_kind(pinned_dirs):
    from app.services import research_runner

    class _Row:
        def __init__(self, original_name, stored_path, extracted_path):
            self.original_name = original_name
            self.stored_path = stored_path
            self.extracted_path = extracted_path

    rows = [
        _Row("a.png", "/abs/a.png", None),
        _Row("b.jpg", "/abs/b.jpg", None),
        _Row("c.jpeg", "/abs/c.jpeg", None),
        _Row("d.webp", "/abs/d.webp", None),
        _Row("e.gif", "/abs/e.gif", None),
        _Row("f.pdf", "/abs/f.pdf", None),  # should still be pdf_scan
        _Row("g.md", "/abs/g.md", None),    # still text
    ]
    out = research_runner._files_to_prompt_files(rows)
    by_name = {pf.original_name: pf for pf in out}
    for img in ("a.png", "b.jpg", "c.jpeg", "d.webp", "e.gif"):
        assert by_name[img].kind == "image", f"{img} → {by_name[img].kind}"
    assert by_name["f.pdf"].kind == "pdf_scan"
    assert by_name["g.md"].kind == "text"


async def test_investment_template_surfaces_uploaded_files_same_as_general(pinned_dirs):
    """Investment template must render uploaded files with the same three
    kinds (text / pdf_scan / failed), so users get identical file handling
    regardless of research mode."""
    from app.services import research_runner

    pf_cls = research_runner._PromptFile
    files = [
        pf_cls("notes.md", "/abs/uploads/rid/n.md", "text"),
        pf_cls("scan.pdf", "/abs/uploads/rid/s.pdf", "pdf_scan"),
        pf_cls("resume.docx", "/abs/uploads/rid/r.docx", "failed"),
    ]
    out = research_runner._render_prompt("Q?", files, mode="investment")
    assert "notes.md" in out
    assert "scan.pdf" in out
    assert "PDF 文本层为空" in out
    assert "resume.docx" in out
    assert "解析失败，已忽略" in out
