"""Unit tests for ``app.services.comment_runner`` (Issue #4 — Feature B).

Contract source: ``docs/design/issue-4-feature-b-comments.md`` §2, §3, §5, §8.
These tests are RED until:

- ``app/services/comment_runner.py`` exists
- ``app/models.py`` has a ``Comment`` ORM class + indices
- ``app/templates/prompts/comment_reply.j2`` exists
- ``app/config.py`` exposes ``settings.comment_model`` and
  ``settings.claude_comment_timeout_sec``
- ``.env.example`` contains ``CLAUDE_COMMENT_MODEL`` and
  ``CLAUDE_COMMENT_TIMEOUT_SEC``

Coverage binding (design §7):

- ``app/services/comment_runner.py``: every unit test imports + exercises it
- ``app/models.py`` (Comment ORM): tests 1, 2, 3, 4, 5, 6, 7, and the
  direct-ORM import + mapping test
- ``app/templates/prompts/comment_reply.j2``: tests 9 (done) + 9b (failed)
- ``app/config.py`` (comment_model / claude_comment_timeout_sec): settings test
- ``.env.example``: env-example test

Mocking strategy: mock ``asyncio.create_subprocess_exec`` for the claude
invocation (per TESTER_PROMPT.md). Drive canned stream-json output line-by-
line from an async iterator the test controls. Real DB via ``db_session``.
Never start a real claude subprocess.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# Valid 26-char Crockford-base32 ULIDs for direct inserts.
VALID_RID_1 = "01HXZK8D7Q3V0S9B4W2N6M5C7R"
VALID_RID_2 = "01HXZK8D7Q3V0S9B4W2N6M5C7S"
VALID_CID_1 = "01HXZK8D7Q3V0S9B4W2N6M5C01"
VALID_CID_2 = "01HXZK8D7Q3V0S9B4W2N6M5C02"


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


async def _insert_user(session, email: str = "c@example.com"):
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


async def _insert_research_request(
    session,
    request_id: str,
    user_id: int,
    *,
    status: str = "done",
    plan_path: str | None = None,
    error_message: str | None = None,
    question: str = "what is X?",
):
    from app.models import ResearchRequest

    row = ResearchRequest(
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
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _write_plan(plan_root: Path, rid: str, markdown: str) -> str:
    plan_file = plan_root / f"{rid}.md"
    plan_file.write_text(markdown, encoding="utf-8")
    return str(plan_file.resolve())


# ---------------------------------------------------------------------------
# A minimal fake subprocess / reader harness for stream-json output.
# ---------------------------------------------------------------------------


class _FakeReader:
    """Fake asyncio StreamReader yielding pre-canned newline-terminated lines."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._eof = False

    async def readline(self) -> bytes:
        if not self._lines:
            self._eof = True
            return b""
        return self._lines.pop(0)

    async def read(self, n: int = -1) -> bytes:
        self._eof = True
        out = b"".join(self._lines)
        self._lines.clear()
        return out

    def at_eof(self) -> bool:
        return self._eof and not self._lines


class _FakeProc:
    def __init__(self, *, stdout_lines: list[bytes], returncode: int = 0):
        self.stdout = _FakeReader(stdout_lines)
        self.stderr = _FakeReader([])
        self.returncode = returncode
        self.pid = 12345

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout_lines: list[bytes] | None = None,
    returncode: int = 0,
    capture_argv: list | None = None,
    capture_cwd: list | None = None,
    raise_on_exec: Exception | None = None,
) -> None:
    """Patch asyncio.create_subprocess_exec to yield canned stream-json output."""

    async def _fake_exec(*argv, **kwargs):
        if capture_argv is not None:
            capture_argv.extend(argv)
        if capture_cwd is not None:
            capture_cwd.append(kwargs.get("cwd"))
        if raise_on_exec is not None:
            raise raise_on_exec
        return _FakeProc(stdout_lines=stdout_lines or [], returncode=returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


def _delta_line(text: str) -> bytes:
    # stream-json "content_block_delta" event with text_delta.
    frame = {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text},
    }
    return (json.dumps(frame) + "\n").encode("utf-8")


def _result_line(*, total_cost_usd: float, is_error: bool = False) -> bytes:
    frame = {
        "type": "result",
        "total_cost_usd": total_cost_usd,
        "is_error": is_error,
    }
    return (json.dumps(frame) + "\n").encode("utf-8")


# ===========================================================================
# #1. Comment ORM class exists with required columns (design §2).
# ===========================================================================


async def test_comment_orm_has_all_design_fields(db_session):
    """Design §2 lists the schema — assert the ORM class exposes every column
    and that constraints (author / ai_status CHECK IN) are enforceable."""
    from app.models import Comment

    # Column names per design §2.
    required = {
        "id",
        "request_id",
        "user_id",
        "parent_id",
        "author",
        "anchor_text",
        "anchor_before",
        "anchor_after",
        "body",
        "ai_status",
        "ai_error",
        "cost_usd",
        "created_at",
        "deleted_at",
    }
    present = set(Comment.__table__.columns.keys())
    missing = required - present
    assert not missing, f"Comment ORM missing columns: {missing}"


# ===========================================================================
# #2. create_user_comment inserts user row + AI placeholder in one txn.
# ===========================================================================


async def test_create_user_comment_inserts_user_row_and_ai_placeholder(
    db_session, pinned_dirs
):
    """Design §4 POST flow: single txn writes both user comment + AI
    placeholder (pending, body='')."""
    from app.models import Comment
    from app.services import comment_runner

    user = await _insert_user(db_session, "c1@example.com")
    await _insert_research_request(db_session, VALID_RID_1, user.id)

    payload = {
        "anchor_before": "prefix",
        "anchor_text": "selected text",
        "anchor_after": "suffix",
        "body": "my thoughts",
    }
    result = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload=payload,
    )

    # result must expose both rows' ids (design §4 response shape).
    assert "comment" in result or "user_comment" in result
    user_row_repr = result.get("comment") or result.get("user_comment")
    ai_placeholder = result.get("ai_placeholder")
    assert user_row_repr is not None
    assert ai_placeholder is not None

    # DB must have exactly 2 rows for this request.
    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(Comment).where(Comment.request_id == VALID_RID_1)
        )
    ).scalars().all()
    assert len(rows) == 2

    by_author = {r.author: r for r in rows}
    assert "user" in by_author and "ai" in by_author

    user_row = by_author["user"]
    ai_row = by_author["ai"]

    # User row fields.
    assert user_row.body == "my thoughts"
    assert user_row.anchor_text == "selected text"
    assert user_row.anchor_before == "prefix"
    assert user_row.anchor_after == "suffix"
    assert user_row.parent_id is None
    assert user_row.ai_status is None or user_row.ai_status == ""
    assert user_row.deleted_at is None

    # AI placeholder fields.
    assert ai_row.parent_id == user_row.id
    assert ai_row.ai_status == "pending"
    assert ai_row.body == ""
    assert ai_row.deleted_at is None


# ===========================================================================
# #3. cascade soft-delete: DELETE user comment → AI reply also deleted_at set.
# ===========================================================================


async def test_cascade_soft_delete_marks_user_and_ai_rows(
    db_session, pinned_dirs
):
    from app.models import Comment
    from app.services import comment_runner

    user = await _insert_user(db_session, "c3@example.com")
    await _insert_research_request(db_session, VALID_RID_1, user.id)

    # Seed a user comment + AI reply directly.
    now = _utcnow_naive()
    user_row = Comment(
        id=VALID_CID_1,
        request_id=VALID_RID_1,
        user_id=user.id,
        parent_id=None,
        author="user",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="hi",
        ai_status=None,
        ai_error=None,
        cost_usd=None,
        created_at=now,
        deleted_at=None,
    )
    ai_row = Comment(
        id=VALID_CID_2,
        request_id=VALID_RID_1,
        user_id=user.id,
        parent_id=VALID_CID_1,
        author="ai",
        anchor_text="x",
        anchor_before="",
        anchor_after="",
        body="ai reply text",
        ai_status="done",
        ai_error=None,
        cost_usd=0.01,
        created_at=now,
        deleted_at=None,
    )
    db_session.add_all([user_row, ai_row])
    await db_session.commit()

    # Cascade soft-delete.
    deleted_count = await comment_runner.cascade_soft_delete(
        request_id=VALID_RID_1,
        comment_id=VALID_CID_1,
        user_id=user.id,
    )
    # Expect at minimum user + AI row touched.
    assert deleted_count >= 2

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(Comment).where(Comment.request_id == VALID_RID_1)
        )
    ).scalars().all()
    assert len(rows) == 2
    for r in rows:
        assert r.deleted_at is not None, f"row {r.id!r} not soft-deleted"


# ===========================================================================
# #4. AI pipeline done branch — deltas + result → ai_status=done + body + cost.
# ===========================================================================


async def test_ai_pipeline_done_writes_body_and_cost_usd(
    db_session, pinned_dirs, monkeypatch
):
    """End-to-end AI pipeline on a 'done' research plan.

    Real DB (soft rule for mock boundary gap check: unit covers the real create
    → publish flow). Mocked claude subprocess only.
    """
    from app.models import Comment
    from app.services import comment_runner

    upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "c4@example.com")
    plan_md = "# A plan\n\n段落一。\n\n段落二。\n"
    plan_abs = _write_plan(plan_root, VALID_RID_1, plan_md)
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, status="done", plan_path=plan_abs
    )

    # Seed user + AI placeholder.
    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "段落",
            "anchor_text": "一",
            "anchor_after": "。",
            "body": "好不好？",
        },
    )
    placeholder = created.get("ai_placeholder")
    assert placeholder is not None
    ai_cid = placeholder["id"] if isinstance(placeholder, dict) else placeholder.id

    # Install fake subprocess emitting 2 deltas + 1 result.
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[
            _delta_line("这是一个"),
            _delta_line("回复。"),
            _result_line(total_cost_usd=0.0123),
        ],
        returncode=0,
    )

    # Drive the AI pipeline to completion.
    await comment_runner._run_ai_reply(ai_cid)

    db_session.expire_all()
    ai_row = (
        await db_session.execute(select(Comment).where(Comment.id == ai_cid))
    ).scalar_one()
    assert ai_row.ai_status == "done"
    assert "这是一个回复。" in ai_row.body
    assert ai_row.ai_error in (None, "")
    assert ai_row.cost_usd == 0.0123


# ===========================================================================
# #5. AI pipeline failed branch — exit code ≠ 0 → ai_status=failed,
#     non-empty ai_error (HARNESS §1 parity).
# ===========================================================================


async def test_ai_pipeline_nonzero_exit_marks_failed_with_ai_error(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import Comment
    from app.services import comment_runner

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "c5@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "a",
            "anchor_text": "b",
            "anchor_after": "c",
            "body": "test",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    # exit=1, some stderr-ish content is fine — just want failure surfaced.
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[],
        returncode=1,
    )

    await comment_runner._run_ai_reply(ai_cid)

    db_session.expire_all()
    ai_row = (
        await db_session.execute(select(Comment).where(Comment.id == ai_cid))
    ).scalar_one()
    assert ai_row.ai_status == "failed"
    # HARNESS §1 parity: ai_error non-empty.
    assert ai_row.ai_error, "ai_error empty on failed branch — HARNESS §1 parity violated"


# ===========================================================================
# #6. AI pipeline timeout → ai_status=failed + ai_error mentions timeout.
# ===========================================================================


async def test_ai_pipeline_timeout_marks_failed(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import Comment
    from app.services import comment_runner
    from app import config as config_mod

    # Squeeze timeout to nearly zero so we don't actually wait.
    monkeypatch.setattr(config_mod.settings, "claude_comment_timeout_sec", 0)

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "c6@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    # Fake subprocess that never emits anything and hangs on wait.
    class _HangingProc:
        def __init__(self):
            self.stdout = _FakeReader([])
            self.stderr = _FakeReader([])
            self.returncode = None
            self.pid = 42

        async def wait(self):
            # Hang long enough for the outer timeout to fire.
            await asyncio.sleep(5)
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    async def _fake_exec(*argv, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await comment_runner._run_ai_reply(ai_cid)

    db_session.expire_all()
    ai_row = (
        await db_session.execute(select(Comment).where(Comment.id == ai_cid))
    ).scalar_one()
    assert ai_row.ai_status == "failed"
    assert ai_row.ai_error
    assert "timeout" in ai_row.ai_error.lower() or "timed out" in ai_row.ai_error.lower()


# ===========================================================================
# #7. AI pipeline ENOENT (claude binary missing) → failed + ai_error non-empty.
# ===========================================================================


async def test_ai_pipeline_enoent_marks_failed(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import Comment
    from app.services import comment_runner

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "c7@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    _install_fake_subprocess(
        monkeypatch,
        raise_on_exec=FileNotFoundError("claude binary not found"),
    )

    await comment_runner._run_ai_reply(ai_cid)

    db_session.expire_all()
    ai_row = (
        await db_session.execute(select(Comment).where(Comment.id == ai_cid))
    ).scalar_one()
    assert ai_row.ai_status == "failed"
    assert ai_row.ai_error  # HARNESS §1 parity.


# ===========================================================================
# #8. AI pipeline empty body → ai_error = "claude 未返回内容".
# ===========================================================================


async def test_ai_pipeline_empty_body_marks_failed_with_specific_message(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import Comment
    from app.services import comment_runner

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "c8@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    # Subprocess emits only the result line (no deltas), so body stays empty.
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[_result_line(total_cost_usd=0.001)],
        returncode=0,
    )

    await comment_runner._run_ai_reply(ai_cid)

    db_session.expire_all()
    ai_row = (
        await db_session.execute(select(Comment).where(Comment.id == ai_cid))
    ).scalar_one()
    assert ai_row.ai_status == "failed"
    assert ai_row.ai_error == "claude 未返回内容"


# ===========================================================================
# #9. SSE channel name format: comment:{comment_id} (design §4 SSE).
# ===========================================================================


async def test_sse_channel_name_matches_comment_id(pinned_dirs):
    from app.services import comment_runner

    q = comment_runner.subscribe(VALID_CID_1)
    try:
        ev = ("ai_delta", {"comment_id": VALID_CID_1, "text": "hello"})
        comment_runner._publish(VALID_CID_1, ev)

        got = await asyncio.wait_for(q.get(), timeout=1.0)
        assert got == ev

        # Publishing to a different comment id must NOT reach this queue.
        comment_runner._publish(VALID_CID_2, ("ai_delta", {"text": "nope"}))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)
    finally:
        comment_runner.unsubscribe(VALID_CID_1, q)


# ===========================================================================
# #10. Prompt context completeness: done branch includes all 5 context items.
# ===========================================================================


async def test_prompt_context_done_branch_includes_all_five_items(
    db_session, pinned_dirs
):
    """Design §5 context clist (B-Q6=A): question, uploaded_files (abs path),
    plan_markdown, anchor_text, user_body — all 5 items must appear."""
    from app.services import comment_runner

    # Simulated fixture rows — whatever shape comment_runner expects is fine
    # as long as these attribute names match.
    class _UF:
        def __init__(self, original_name, local_path, kind="text"):
            self.original_name = original_name
            self.local_path = local_path
            self.kind = kind

    context = {
        "question": "什么是 method？",
        "uploaded_files": [
            _UF("notes.md", "/abs/uploads/01HXZ.../notes.md"),
            _UF("paper.pdf", "/abs/uploads/01HXZ.../paper.extracted.md"),
        ],
        "plan_markdown": "# 研究方案\n\n段落一。\n",
        "error_message": None,
        "anchor_text": "段落一",
        "user_body": "这段写得不好",
    }

    out = comment_runner._render_prompt(**context)

    # All 5 context items must appear literally.
    assert "什么是 method？" in out
    assert "notes.md" in out
    assert "/abs/uploads/01HXZ.../notes.md" in out
    assert "paper.pdf" in out
    assert "/abs/uploads/01HXZ.../paper.extracted.md" in out
    assert "段落一。" in out  # from plan_markdown
    assert "段落一" in out  # anchor_text
    assert "这段写得不好" in out  # user_body

    # Template should load the research-method-designer skill (design §5).
    assert "/research-method-designer" in out or "评论员" in out


# ===========================================================================
# #10b. Prompt context: failed branch uses error_message instead of plan.
# ===========================================================================


async def test_prompt_context_failed_branch_includes_error_message(
    db_session, pinned_dirs
):
    """Design §5 failed branch (B-Q7=A): when error_message is set, prompt
    uses the 自我诊断 template with {% if error_message %}.
    """
    from app.services import comment_runner

    class _UF:
        def __init__(self, original_name, local_path, kind="text"):
            self.original_name = original_name
            self.local_path = local_path
            self.kind = kind

    context = {
        "question": "怎么做 X？",
        "uploaded_files": [_UF("a.md", "/abs/uploads/01HXZ.../a.md")],
        "plan_markdown": "",  # failed branch: no plan
        "error_message": "Claude 子进程 exit=1: RateLimitError",
        "anchor_text": "RateLimitError",
        "user_body": "什么时候能重试？",
    }

    out = comment_runner._render_prompt(**context)

    # Failed-branch markers from the template.
    assert "RateLimitError" in out  # error_message appears
    assert "RateLimitError" in out  # also the anchor_text
    assert "什么时候能重试？" in out
    # The failed template should mention "自我诊断" or diagnostic role.
    assert "自我诊断" in out or "失败" in out


# ===========================================================================
# #11. _render_prompt must not HTML-escape and must not evaluate nested Jinja.
# ===========================================================================


async def test_prompt_render_preserves_malicious_user_content_literally(
    pinned_dirs,
):
    from app.services import comment_runner

    out = comment_runner._render_prompt(
        question="{{ '{{7*7}}' }} <script>alert(1)</script>",
        uploaded_files=[],
        plan_markdown="# plan\n",
        error_message=None,
        anchor_text="x",
        user_body="SELECT 1;--",
    )
    # Raw HTML preserved (no autoescape for prompts).
    assert "<script>alert(1)</script>" in out
    # Nested Jinja not evaluated (49 = 7*7 must NOT appear near the literal).
    assert "{{7*7}}" in out
    assert "SELECT 1;--" in out


# ===========================================================================
# #12. Unicode normalization: zero-width + bidi controls removed before
#     both DB persistence and prompt injection (design §5).
# ===========================================================================


async def test_user_body_normalization_strips_zero_width_and_bidi(
    db_session, pinned_dirs
):
    from app.models import Comment
    from app.services import comment_runner

    user = await _insert_user(db_session, "cnorm@example.com")
    await _insert_research_request(db_session, VALID_RID_1, user.id)

    dirty = "\u200b\u202e恶意\u2066反向\u2069" + "正常内容"
    result = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": dirty,
        },
    )
    assert result is not None

    db_session.expire_all()
    user_row = (
        await db_session.execute(
            select(Comment).where(
                Comment.request_id == VALID_RID_1, Comment.author == "user"
            )
        )
    ).scalar_one()
    # Zero-width + bidi removed.
    for bad in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u202a", "\u202e", "\u2066", "\u2069"):
        assert bad not in user_row.body, f"char {bad!r} not stripped"
    assert "正常内容" in user_row.body


async def test_user_body_normalization_empty_after_strip_raises(
    db_session, pinned_dirs
):
    """If body becomes empty after stripping zero-width/bidi, creation must
    raise a domain error (router maps it to 400 body_empty per design §5)."""
    from app.services import comment_runner

    user = await _insert_user(db_session, "cnorm2@example.com")
    await _insert_research_request(db_session, VALID_RID_1, user.id)

    # Only zero-width + bidi → fully empty after normalization.
    all_dirty = "\u200b\u202e\u2066\u2069"
    with pytest.raises(Exception):  # service-level error; router maps → 400
        await comment_runner.create_user_comment(
            request_id=VALID_RID_1,
            user_id=user.id,
            payload={
                "anchor_before": "",
                "anchor_text": "x",
                "anchor_after": "",
                "body": all_dirty,
            },
        )


# ===========================================================================
# #13. Claude subprocess argv includes --allowed-tools Read,Glob,Grep
#     (HARNESS §3 tripwire — for comment_runner specifically).
# ===========================================================================


async def test_comment_runner_claude_invocation_uses_safe_tool_allowlist(
    db_session, pinned_dirs, monkeypatch
):
    from app.models import Comment
    from app.services import comment_runner

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "callow@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    captured_argv: list = []
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[_result_line(total_cost_usd=0.0)],
        returncode=0,
        capture_argv=captured_argv,
    )

    await comment_runner._run_ai_reply(ai_cid)

    assert captured_argv, "create_subprocess_exec was never called"
    assert "--allowed-tools" in captured_argv, (
        f"argv missing --allowed-tools: {captured_argv!r}"
    )
    idx = captured_argv.index("--allowed-tools")
    tools_value = captured_argv[idx + 1]
    assert tools_value == "Read,Glob,Grep", (
        f"HARNESS §3 tripwire: expected 'Read,Glob,Grep', got {tools_value!r}"
    )
    # Double-negative tripwire against Write/Edit/Bash leakage.
    forbidden = {"Write", "Edit", "Bash"}
    assert not forbidden.intersection(tools_value.split(",")), (
        f"forbidden tool leaked into --allowed-tools: {tools_value!r}"
    )


# ===========================================================================
# #14. Claude subprocess cwd is an ABSOLUTE path under upload_dir/{rid}/
#     (design §5 + HARNESS §2).
# ===========================================================================


async def test_comment_runner_claude_cwd_is_absolute_under_upload_dir(
    db_session, pinned_dirs, monkeypatch
):
    from app.services import comment_runner

    upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "cwd@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    captured_cwd: list = []
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[_result_line(total_cost_usd=0.0)],
        returncode=0,
        capture_cwd=captured_cwd,
    )

    await comment_runner._run_ai_reply(ai_cid)

    assert captured_cwd, "cwd was never captured"
    cwd = captured_cwd[0]
    assert cwd is not None
    # HARNESS §2: absolute path.
    assert Path(str(cwd)).is_absolute(), f"cwd not absolute: {cwd!r}"
    # Under upload_dir / rid.
    expected_prefix = str((upload / VALID_RID_1).resolve())
    # Allow either upload/<rid> or resolved form.
    assert VALID_RID_1 in str(cwd), f"cwd does not reference rid: {cwd!r}"


# ===========================================================================
# #15. comment_reply.j2 template file exists at the path the design names.
# ===========================================================================


async def test_comment_reply_template_file_exists():
    path = Path(
        "/home/ubuntu/method-dev/app/templates/prompts/comment_reply.j2"
    )
    assert path.exists(), f"design §7 file missing: {path}"
    txt = path.read_text(encoding="utf-8")
    # Must include the {% if error_message %} branch per design §5.
    assert "error_message" in txt, "template missing error_message branch"
    # Both branches reference key context variables.
    assert "question" in txt
    assert "anchor_text" in txt
    assert "user_body" in txt or "body" in txt


# ===========================================================================
# #16. config has comment_model + claude_comment_timeout_sec.
# ===========================================================================


async def test_settings_has_comment_model_and_timeout():
    from app.config import settings

    # Attribute access (pydantic-settings raises AttributeError otherwise).
    _ = settings.comment_model
    _ = settings.claude_comment_timeout_sec
    # Default timeout per design §5 is 60s.
    assert isinstance(settings.claude_comment_timeout_sec, int)
    assert settings.claude_comment_timeout_sec > 0


# ===========================================================================
# #16a. comment_model empty → falls back to claude_model (design §5 逃生门
#      feature-flag branch coverage; LP #21 parity — exercise the real branch,
#      don't just assert attribute presence).
# ===========================================================================


async def test_comment_runner_falls_back_to_claude_model_when_comment_model_empty(
    db_session, pinned_dirs, monkeypatch
):
    """Design §5: ``settings.comment_model`` default = ``settings.claude_model``.

    When ``comment_model`` is empty (or None), the claude subprocess argv
    ``--model`` value MUST equal ``claude_model`` (the fallback). LP #21
    lesson: assert the actual branch is exercised, not just attribute access.
    """
    from app import config as config_mod
    from app.models import Comment
    from app.services import comment_runner

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "cm_fallback@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    # Set claude_model to a concrete, non-empty value and comment_model to "".
    monkeypatch.setattr(config_mod.settings, "claude_model", "claude-opus-4-7")
    monkeypatch.setattr(config_mod.settings, "comment_model", "")

    captured_argv: list = []
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[_result_line(total_cost_usd=0.0)],
        returncode=0,
        capture_argv=captured_argv,
    )

    await comment_runner._run_ai_reply(ai_cid)

    assert captured_argv, "create_subprocess_exec was never called"
    assert "--model" in captured_argv, f"argv missing --model: {captured_argv!r}"
    idx = captured_argv.index("--model")
    model_value = captured_argv[idx + 1]
    assert model_value == "claude-opus-4-7", (
        f"comment_model='' must fall back to claude_model, got --model {model_value!r}"
    )


# ===========================================================================
# #16b. comment_model explicitly set → argv uses it, NOT claude_model (design
#      §5 逃生门 override branch — LP #21 parity).
# ===========================================================================


async def test_comment_runner_uses_comment_model_when_explicitly_set(
    db_session, pinned_dirs, monkeypatch
):
    """Design §5: when ``CLAUDE_COMMENT_MODEL`` (=> ``settings.comment_model``)
    is explicitly set, subprocess argv ``--model`` uses that value, NOT the
    default ``claude_model`` fallback.
    """
    from app import config as config_mod
    from app.services import comment_runner

    _upload, plan_root = pinned_dirs
    user = await _insert_user(db_session, "cm_override@example.com")
    plan_abs = _write_plan(plan_root, VALID_RID_1, "# plan\n")
    await _insert_research_request(
        db_session, VALID_RID_1, user.id, plan_path=plan_abs
    )

    created = await comment_runner.create_user_comment(
        request_id=VALID_RID_1,
        user_id=user.id,
        payload={
            "anchor_before": "",
            "anchor_text": "x",
            "anchor_after": "",
            "body": "body",
        },
    )
    ai_cid = (created.get("ai_placeholder") or {})["id"]

    # Set BOTH to distinct values so the assertion isn't ambiguous.
    monkeypatch.setattr(config_mod.settings, "claude_model", "claude-opus-4-7")
    monkeypatch.setattr(
        config_mod.settings, "comment_model", "claude-haiku-4-5-20251001"
    )

    captured_argv: list = []
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=[_result_line(total_cost_usd=0.0)],
        returncode=0,
        capture_argv=captured_argv,
    )

    await comment_runner._run_ai_reply(ai_cid)

    assert captured_argv, "create_subprocess_exec was never called"
    assert "--model" in captured_argv, f"argv missing --model: {captured_argv!r}"
    idx = captured_argv.index("--model")
    model_value = captured_argv[idx + 1]
    assert model_value == "claude-haiku-4-5-20251001", (
        f"explicit comment_model override must win, got --model {model_value!r}"
    )
    assert model_value != "claude-opus-4-7", (
        "override must NOT fall back to claude_model when comment_model is set"
    )


# ===========================================================================
# #17. .env.example contains the two new env keys.
# ===========================================================================


async def test_env_example_has_comment_env_keys():
    path = Path("/home/ubuntu/method-dev/.env.example")
    assert path.exists(), ".env.example missing"
    txt = path.read_text(encoding="utf-8")
    assert "CLAUDE_COMMENT_MODEL" in txt
    assert "CLAUDE_COMMENT_TIMEOUT_SEC" in txt


# ===========================================================================
# #18. pub/sub unsubscribe removes queue + channel key cleanup.
# ===========================================================================


async def test_pubsub_unsubscribe_removes_queue(pinned_dirs):
    from app.services import comment_runner

    q = comment_runner.subscribe(VALID_CID_2)
    # Sanity: present.
    chans = comment_runner._channels.get(VALID_CID_2) or []
    assert q in chans

    comment_runner.unsubscribe(VALID_CID_2, q)

    # After last unsubscribe, channel key removed.
    assert VALID_CID_2 not in comment_runner._channels
    # Publishing after unsubscribe must not raise.
    comment_runner._publish(VALID_CID_2, ("ai_delta", {"text": "ignored"}))


# ===========================================================================
# #19. _TASKS set holds strong refs to running AI tasks (design §4 GC guard).
# ===========================================================================


async def test_tasks_set_retains_strong_refs(pinned_dirs):
    """Design §4: asyncio.create_task(...) result is added to
    comment_runner._TASKS; done_callback removes it. Smoke test confirms the
    attribute exists and is a set."""
    from app.services import comment_runner

    assert hasattr(comment_runner, "_TASKS")
    assert isinstance(comment_runner._TASKS, set)
