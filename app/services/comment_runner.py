"""Comment AI pipeline + pub/sub for Issue #4 (Feature B).

Contract source: ``docs/design/issue-4-feature-b-comments.md`` §2, §4, §5.

Responsibilities
----------------
- ``create_user_comment``: validate + normalize body; insert user comment +
  AI placeholder in a single transaction; spawn background AI task.
- ``cascade_soft_delete``: owner-scoped soft delete of a user comment + its
  AI reply in the same transaction. AI replies themselves cannot be deleted
  directly — callers must check the ``author`` field first.
- ``_run_ai_reply``: the coroutine body that spawns a ``claude`` subprocess
  and drives it through the stream-json protocol; publishes ``ai_delta`` /
  ``ai_done`` events to the comment's pub/sub channel; writes the terminal
  state (body / ai_status / ai_error / cost_usd) to DB.
- Pub/sub: module-level ``_channels`` + ``subscribe`` / ``unsubscribe`` /
  ``_publish``; channel key is the AI reply's comment id.

Design decisions:
- Model fallback (B-Q8): ``settings.comment_model`` empty → use
  ``settings.claude_model``. Exercised by two feature-flag branch tests.
- failed branch (B-Q7): prompts for failed plans use the same template with
  ``{% if error_message %}`` switching to the "self-diagnosis" wording.
- Unicode normalization (design §5): zero-width + bidi control characters
  are stripped before storage and before injection into the prompt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ulid
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select

from app import config as _config
from app.db import get_sessionmaker
from app.models import Comment, ResearchRequest, UploadedFile

logger = logging.getLogger("method.comment_runner")

__all__ = [
    "BodyEmptyError",
    "cascade_soft_delete",
    "create_user_comment",
    "subscribe",
    "unsubscribe",
]


# ---------------------------------------------------------------------------
# Pub/sub registry (design §4 SSE)
# ---------------------------------------------------------------------------

# channel_id == ai reply comment_id. asyncio is single-threaded; no locks.
_channels: dict[str, list[asyncio.Queue]] = {}

# Active background tasks — hold strong refs so tasks aren't GC'd while still
# running; done_callback removes them. Mirrors research_runner._TASKS (design
# §4 "复用 research_runner 的 pub/sub 基础设施").
_TASKS: set[asyncio.Task] = set()

_QUEUE_MAXSIZE = 256


def subscribe(comment_id: str) -> asyncio.Queue:
    """Return a new subscriber queue attached to ``comment_id``'s channel."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _channels.setdefault(comment_id, []).append(q)
    return q


def unsubscribe(comment_id: str, q: asyncio.Queue) -> None:
    """Remove ``q`` from ``comment_id``'s channel; drop channel if empty."""
    lst = _channels.get(comment_id)
    if not lst:
        return
    try:
        lst.remove(q)
    except ValueError:
        pass
    if not lst:
        _channels.pop(comment_id, None)


def _publish(comment_id: str, event) -> None:
    """Send ``event`` to every current subscriber. On overflow, drop silently."""
    for q in list(_channels.get(comment_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "comment_runner queue full (slow subscriber), drop cid=%s tag=%s",
                comment_id,
                event[0] if isinstance(event, tuple) and event else "?",
            )


def _close_channel(comment_id: str) -> None:
    _publish(comment_id, ("__close__",))


# ---------------------------------------------------------------------------
# Prompt rendering (design §5)
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "prompts"
_PROMPT_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
)


def _render_prompt(
    *,
    question: str,
    uploaded_files: list,
    plan_markdown: str,
    error_message: str | None,
    anchor_text: str,
    user_body: str,
) -> str:
    """Render the comment AI prompt.

    Branches on ``error_message``: truthy → "self-diagnosis" role for failed
    research plans; falsy → "commentator" role for done plans.
    """
    template = _PROMPT_ENV.get_template("comment_reply.j2")
    return template.render(
        question=question,
        uploaded_files=uploaded_files,
        plan_markdown=plan_markdown,
        error_message=error_message,
        anchor_text=anchor_text,
        user_body=user_body,
    )


# ---------------------------------------------------------------------------
# Input normalization (design §5)
# ---------------------------------------------------------------------------

# Zero-width + bidi control characters. Stripped before DB persistence and
# before prompt injection to prevent Unicode-based role-reversal tricks.
_DANGER_CHARS_RE = re.compile(
    "["
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\ufeff"  # zero-width no-break space (BOM)
    "\u202a-\u202e"  # LRE / RLE / PDF / LRO / RLO
    "\u2066-\u2069"  # LRI / RLI / FSI / PDI
    "]"
)


class BodyEmptyError(ValueError):
    """Raised when a comment body is empty after Unicode normalization.

    Router maps this to ``400 {"error": "body_empty"}`` (design §5).
    """


def _normalize_body(raw: str) -> str:
    """Strip dangerous zero-width + bidi chars; raise if result is empty."""
    cleaned = _DANGER_CHARS_RE.sub("", raw).strip()
    if not cleaned:
        raise BodyEmptyError("comment body empty after normalization")
    return cleaned


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _new_ulid() -> str:
    return str(ulid.new())


# ---------------------------------------------------------------------------
# Row serialization
# ---------------------------------------------------------------------------


def _row_to_dict(row: Comment) -> dict[str, Any]:
    """Serialize a Comment row for API responses.

    Per design §4: returns all fields except ``user_id`` (internal) and
    ``deleted_at`` (filter-only). ``created_at`` is ISO-8601.
    """
    return {
        "id": row.id,
        "request_id": row.request_id,
        "parent_id": row.parent_id,
        "author": row.author,
        "anchor_text": row.anchor_text,
        "anchor_before": row.anchor_before,
        "anchor_after": row.anchor_after,
        "body": row.body,
        "ai_status": row.ai_status,
        "ai_error": row.ai_error,
        "cost_usd": row.cost_usd,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# Public: create + cascade-soft-delete
# ---------------------------------------------------------------------------


async def create_user_comment(
    *,
    request_id: str,
    user_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert the user comment + AI placeholder in a single transaction.

    Returns ``{"comment": {...}, "ai_placeholder": {...}}`` (design §4).

    Both rows share the same anchor fields; AI placeholder has ``author='ai'``,
    ``ai_status='pending'``, ``body=''``, ``parent_id`` pointing at the user
    comment's id.
    """
    body = _normalize_body(payload["body"])
    anchor_text = payload["anchor_text"]
    anchor_before = payload.get("anchor_before", "")
    anchor_after = payload.get("anchor_after", "")

    now = _utcnow()
    user_cid = _new_ulid()
    ai_cid = _new_ulid()

    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            user_row = Comment(
                id=user_cid,
                request_id=request_id,
                user_id=user_id,
                parent_id=None,
                author="user",
                anchor_text=anchor_text,
                anchor_before=anchor_before,
                anchor_after=anchor_after,
                body=body,
                ai_status=None,
                ai_error=None,
                cost_usd=None,
                created_at=now,
                deleted_at=None,
            )
            ai_row = Comment(
                id=ai_cid,
                request_id=request_id,
                user_id=user_id,
                parent_id=user_cid,
                author="ai",
                anchor_text=anchor_text,
                anchor_before=anchor_before,
                anchor_after=anchor_after,
                body="",
                ai_status="pending",
                ai_error=None,
                cost_usd=None,
                created_at=now,
                deleted_at=None,
            )
            session.add_all([user_row, ai_row])
        # After commit, return copies safe to serialize outside the session.
        return {
            "comment": _row_to_dict(user_row),
            "ai_placeholder": _row_to_dict(ai_row),
        }


async def cascade_soft_delete(
    *,
    request_id: str,
    comment_id: str,
    user_id: int,
) -> int:
    """Soft-delete a user comment + its AI reply within a single txn.

    Returns the number of rows touched (≥2 on success).

    Caller must verify ``author='user'`` and ``user_id`` ownership BEFORE
    calling — this function is best-effort at the ORM layer and will simply
    return 0 if no matching live user comment is found.
    """
    now = _utcnow()
    touched = 0
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            user_row = (
                await session.execute(
                    select(Comment).where(
                        Comment.id == comment_id,
                        Comment.request_id == request_id,
                        Comment.user_id == user_id,
                        Comment.author == "user",
                        Comment.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if user_row is None:
                return 0
            user_row.deleted_at = now
            touched += 1

            ai_rows = (
                await session.execute(
                    select(Comment).where(
                        Comment.parent_id == comment_id,
                        Comment.author == "ai",
                        Comment.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            for r in ai_rows:
                r.deleted_at = now
                touched += 1
    return touched


# ---------------------------------------------------------------------------
# AI reply pipeline (design §5)
# ---------------------------------------------------------------------------


_STDERR_TAIL = 1000


async def _run_ai_reply(comment_id: str) -> None:
    """Drive one AI reply: spawn claude subprocess, stream deltas, finalize row.

    Design §5: all failure paths write ``ai_status='failed'`` + non-empty
    ``ai_error`` (HARNESS §1 parity).
    """
    sm = get_sessionmaker()

    # Snapshot everything we need from DB up-front so we don't hold a session
    # across the subprocess lifetime.
    async with sm() as session:
        ai_row = (
            await session.execute(
                select(Comment).where(Comment.id == comment_id)
            )
        ).scalar_one_or_none()
        if ai_row is None:
            logger.error(
                "comment_runner._run_ai_reply: AI row not found cid=%s",
                comment_id,
            )
            return
        if ai_row.author != "ai" or ai_row.ai_status not in (
            None, "pending", "streaming",
        ):
            # Not in a runnable state (already done / failed / manual row).
            return

        request_id = ai_row.request_id
        anchor_text = ai_row.anchor_text
        parent_id = ai_row.parent_id

        user_row = None
        if parent_id:
            user_row = (
                await session.execute(
                    select(Comment).where(Comment.id == parent_id)
                )
            ).scalar_one_or_none()
        user_body = user_row.body if user_row is not None else ""

        req = (
            await session.execute(
                select(ResearchRequest).where(ResearchRequest.id == request_id)
            )
        ).scalar_one_or_none()
        if req is None:
            await _mark_ai_failed(
                comment_id, "research request not found"
            )
            return
        question = req.question
        plan_path = req.plan_path
        error_message = req.error_message

        upload_rows = (
            await session.execute(
                select(UploadedFile).where(
                    UploadedFile.request_id == request_id
                )
            )
        ).scalars().all()
        uploaded_files = [
            {
                "original_name": f.original_name,
                "local_path": f.extracted_path or f.stored_path,
                "kind": "text",
            }
            for f in upload_rows
        ]

    # Build prompt context outside the session.
    plan_markdown = ""
    if plan_path:
        try:
            plan_markdown = Path(plan_path).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "comment_runner: plan_path unreadable rid=%s err=%r",
                request_id,
                exc,
            )

    prompt = _render_prompt(
        question=question,
        uploaded_files=uploaded_files,
        plan_markdown=plan_markdown,
        error_message=error_message if error_message else None,
        anchor_text=anchor_text,
        user_body=user_body,
    )

    # Per upload dir for claude cwd (so Read tool scope covers user files).
    cwd = (Path(_config.settings.upload_dir) / request_id).resolve()
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        await _mark_ai_failed(comment_id, f"cwd mkdir failed: {exc!r}")
        return

    # Pick model per B-Q8: explicit comment_model wins; else fall back.
    model = _config.settings.comment_model or _config.settings.claude_model

    argv = [
        _config.settings.claude_bin,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--allowed-tools", "Read,Glob,Grep",
        "--permission-mode", "acceptEdits",
        "--add-dir", str(cwd),
    ]

    timeout = max(_config.settings.claude_comment_timeout_sec, 1)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
    except FileNotFoundError as exc:
        await _mark_ai_failed(comment_id, f"claude binary not found: {exc!r}")
        _close_channel(comment_id)
        return
    except PermissionError as exc:
        await _mark_ai_failed(comment_id, f"claude not executable: {exc!r}")
        _close_channel(comment_id)
        return
    except Exception as exc:  # noqa: BLE001 — any spawn failure surfaces
        await _mark_ai_failed(comment_id, f"subprocess spawn failed: {exc!r}")
        _close_channel(comment_id)
        return

    start = time.monotonic()
    deadline = start + timeout
    body_parts: list[str] = []
    cost_usd = 0.0
    saw_result = False
    timed_out = False

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining
                )
            except TimeoutError:
                timed_out = True
                break
            if not line:
                break  # EOF

            try:
                obj = json.loads(
                    line.decode("utf-8", errors="replace").rstrip()
                )
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")
            if msg_type == "content_block_delta":
                # Anthropic-style delta frame (what tester seeds).
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        body_parts.append(text)
                        _publish(
                            comment_id,
                            ("ai_delta", {"comment_id": comment_id, "text": text}),
                        )
            elif msg_type == "assistant":
                # Alternate frame shape used elsewhere in the project.
                content = obj.get("message", {}).get("content", []) or []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            body_parts.append(text)
                            _publish(
                                comment_id,
                                (
                                    "ai_delta",
                                    {"comment_id": comment_id, "text": text},
                                ),
                            )
            elif msg_type == "result":
                try:
                    cost_usd = float(obj.get("total_cost_usd") or 0.0)
                except (TypeError, ValueError):
                    cost_usd = 0.0
                saw_result = True
    except Exception as exc:  # noqa: BLE001
        await _terminate(proc)
        await _mark_ai_failed(comment_id, f"stream error: {exc!r}")
        _close_channel(comment_id)
        return

    if timed_out:
        await _terminate(proc)
        await _mark_ai_failed(
            comment_id,
            f"claude timeout after {timeout}s",
        )
        _close_channel(comment_id)
        return

    # Reap and decide.
    exit_code: int | None = proc.returncode
    if exit_code is None:
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            await _terminate(proc)
            await _mark_ai_failed(
                comment_id,
                f"claude timeout after {timeout}s (post-EOF wait)",
            )
            _close_channel(comment_id)
            return

    body = "".join(body_parts)

    if exit_code != 0:
        stderr_tail = b""
        try:
            stderr_tail = await proc.stderr.read()
        except Exception:  # noqa: BLE001
            pass
        tail = stderr_tail[-_STDERR_TAIL:].decode("utf-8", errors="replace")
        await _mark_ai_failed(
            comment_id,
            tail.strip() or f"claude exit {exit_code}",
        )
        _close_channel(comment_id)
        return

    if not body:
        # Empty output even on exit 0 — explicit failure per design §5.
        await _mark_ai_failed(comment_id, "claude 未返回内容")
        _close_channel(comment_id)
        return

    # Success — finalize DB row.
    async with sm() as session:
        async with session.begin():
            ai_row = (
                await session.execute(
                    select(Comment).where(Comment.id == comment_id)
                )
            ).scalar_one_or_none()
            if ai_row is None:
                _close_channel(comment_id)
                return
            ai_row.body = body
            ai_row.ai_status = "done"
            ai_row.ai_error = None
            ai_row.cost_usd = cost_usd if saw_result else None

    _publish(
        comment_id,
        (
            "ai_done",
            {
                "comment_id": comment_id,
                "body": body,
                "ai_status": "done",
                "cost_usd": cost_usd if saw_result else None,
            },
        ),
    )
    _close_channel(comment_id)


async def _mark_ai_failed(comment_id: str, error_text: str) -> None:
    """Write ``ai_status='failed'`` + non-empty ``ai_error`` (HARNESS §1)."""
    if not error_text:
        error_text = "unknown failure"
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(Comment).where(Comment.id == comment_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return
                row.ai_status = "failed"
                row.ai_error = error_text
    except Exception as exc:  # noqa: BLE001 — last-resort logging
        logger.exception(
            "comment_runner._mark_ai_failed failed cid=%s err=%r",
            comment_id,
            exc,
        )
    # Publish terminal event too so SSE subscribers see the failure.
    _publish(
        comment_id,
        (
            "ai_done",
            {
                "comment_id": comment_id,
                "body": "",
                "ai_status": "failed",
                "ai_error": error_text,
            },
        ),
    )


async def _terminate(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Task lifecycle helper
# ---------------------------------------------------------------------------


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: surface any uncaught exception from ``_run_ai_reply``."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("comment_runner task failed", exc_info=exc)


async def run_ai_reply(comment_id: str) -> None:
    """Router-facing dispatcher — spawn a background task for ``comment_id``."""
    task = asyncio.create_task(_run_ai_reply(comment_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_log_task_exception)
