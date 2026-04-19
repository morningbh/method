"""Background research orchestration + pub/sub SSE bus (Task 3.3).

Contract source: ``docs/design/issue-2-task-3.3-research-routes.md`` §3, §4, §5.

Responsibilities
----------------
- ``run_research(request_id)``: router-facing dispatcher — creates a task,
  attaches a done-callback chain (GC-prevention ref set + exception logger).
- ``_run_research(request_id)``: the coroutine body. Two-session pattern
  (§3): A-session flips pending→running and snapshots inputs; it closes
  before the (minutes-long) claude stream. B-session writes the terminal
  state. A rescue C-session covers B-session failures (HARNESS §1).
- ``_render_prompt``: Jinja2 rendering with autoescape=False so untrusted
  user text is preserved literally (§4, §13). A dedicated ``Environment``
  is used — *not* ``app.state.templates`` (that one auto-escapes HTML).
- ``_write_plan``: writes ``{plan_dir}/{request_id}.md``, returning the
  absolute ``Path`` (HARNESS §2).
- Pub/sub: module-level ``_channels`` dict + ``_publish``/``subscribe``/
  ``unsubscribe``. Best-effort delivery (§5); DB is the ground truth.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import ulid
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select

from app import config as _config
from app.db import get_sessionmaker
from app.models import ResearchRequest, UploadedFile
from app.services.claude_runner import stream

logger = logging.getLogger("method.research_runner")

__all__ = [
    "run_research",
    "subscribe",
    "unsubscribe",
]


# ---------------------------------------------------------------------------
# Pub/sub registry (design §5)
# ---------------------------------------------------------------------------

# Each value is a list of subscriber queues for that request_id. asyncio
# single-threaded guarantee — no locks needed.
_channels: dict[str, list[asyncio.Queue]] = {}

# Active background tasks — hold strong references so tasks aren't GC'd
# while still running. Callback discards on completion.
_TASKS: set[asyncio.Task] = set()

# Queue size per design §5 — overflow drops silently.
_QUEUE_MAXSIZE = 256


def subscribe(request_id: str) -> asyncio.Queue:
    """Return a new subscriber queue attached to ``request_id``'s channel."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _channels.setdefault(request_id, []).append(q)
    return q


def unsubscribe(request_id: str, q: asyncio.Queue) -> None:
    """Remove ``q`` from ``request_id``'s channel; drop channel if empty."""
    lst = _channels.get(request_id)
    if not lst:
        return
    try:
        lst.remove(q)
    except ValueError:
        pass
    if not lst:
        _channels.pop(request_id, None)


def _publish(request_id: str, event) -> None:
    """Send ``event`` to every current subscriber of ``request_id``.

    Synchronous (design §5). On ``QueueFull`` we log WARNING once and drop
    silently — the DB is ground truth, clients refetch on reconnect.
    """
    for q in list(_channels.get(request_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "research_runner queue full (slow subscriber), dropping event "
                "rid=%s tag=%s",
                request_id,
                event[0] if isinstance(event, tuple) and event else "?",
            )


def _close_channel(request_id: str) -> None:
    """Publish the __close__ sentinel so subscribers disconnect."""
    _publish(request_id, ("__close__",))


# ---------------------------------------------------------------------------
# Prompt rendering (design §4)
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "prompts"

# Dedicated environment — autoescape=False so malicious user content renders
# as-is (HTML entities, nested jinja, etc.). Do NOT reuse app.state.templates
# because that environment auto-escapes for HTML safety.
_PROMPT_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
)


@dataclass(frozen=True)
class _PromptFile:
    """Shape consumed by ``research.j2``."""

    original_name: str
    local_path: str
    extraction_ok: bool


def _render_prompt(question: str, files: list[_PromptFile]) -> str:
    """Render the claude prompt from ``question`` + ``files``."""
    template = _PROMPT_ENV.get_template("research.j2")
    return template.render(question=question, uploaded_files=files)


# ---------------------------------------------------------------------------
# Plan writer (HARNESS §2)
# ---------------------------------------------------------------------------


def _write_plan(request_id: str, markdown: str) -> Path:
    """Write ``markdown`` to ``{plan_dir}/{request_id}.md``. Returns absolute Path."""
    plan_dir = Path(_config.settings.plan_dir).resolve()
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{request_id}.md"
    path.write_text(markdown, encoding="utf-8")
    return path.resolve()


# ---------------------------------------------------------------------------
# Time helper (matches Task 2.x convention — naive UTC)
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------


def ulid_new() -> str:
    """Return a fresh 26-char Crockford base32 ULID."""
    return str(ulid.new())


# ---------------------------------------------------------------------------
# DB load helpers
# ---------------------------------------------------------------------------


async def _load_for_update(session, request_id: str) -> ResearchRequest | None:
    """Load a research_requests row by PK within the given session."""
    result = await session.execute(
        select(ResearchRequest).where(ResearchRequest.id == request_id)
    )
    return result.scalar_one_or_none()


async def _load_files(session, request_id: str) -> list[UploadedFile]:
    """Load uploaded_files rows for ``request_id``."""
    result = await session.execute(
        select(UploadedFile).where(UploadedFile.request_id == request_id)
    )
    return list(result.scalars().all())


def _files_to_prompt_files(rows: list[UploadedFile]) -> list[_PromptFile]:
    """Convert UploadedFile rows into _PromptFile entries for the template.

    Uses extracted_path when available (pdf/docx after successful extraction),
    otherwise the stored_path (md/txt, or pdf/docx with extraction failure).
    """
    ext_needs_extract = {".pdf", ".docx"}
    out: list[_PromptFile] = []
    for row in rows:
        ext = Path(row.original_name).suffix.lower()
        if row.extracted_path:
            local = row.extracted_path
            ok = True
        else:
            local = row.stored_path
            # md/txt never need extraction — always OK. pdf/docx without
            # extracted_path means extraction failed.
            ok = ext not in ext_needs_extract
        out.append(
            _PromptFile(
                original_name=row.original_name,
                local_path=local,
                extraction_ok=ok,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Task lifecycle callbacks
# ---------------------------------------------------------------------------


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: surface any uncaught exception from ``_run_research``."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("research task failed for request", exc_info=exc)


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


async def run_research(request_id: str) -> None:
    """Router-facing dispatcher — spawn background task for ``request_id``."""
    task = asyncio.create_task(_run_research(request_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_log_task_exception)


# ---------------------------------------------------------------------------
# Main coroutine — two-session pattern (§3)
# ---------------------------------------------------------------------------


async def _run_research(request_id: str) -> None:
    """Drive one research request through pending→running→{done,failed}.

    Design §3: Block A flips status to running (short-lived session); Block B
    writes the terminal state (separate session); Block C is a rescue-write
    invoked only if Block B itself blows up (HARNESS §1 safety net).
    """
    # --------------- Block A: mark running, snapshot inputs ---------------
    try:
        async with get_sessionmaker()() as s1:
            async with s1.begin():
                req = await _load_for_update(s1, request_id)
                if req is None or req.status != "pending":
                    return  # idempotent
                req.status = "running"
                question = req.question
                file_rows = await _load_files(s1, request_id)
                prompt_files = _files_to_prompt_files(file_rows)
            # s1 closed here — no DB connection held across claude stream.
    except Exception as e:
        logger.exception("research_runner Block A failed rid=%s", request_id)
        # Last-resort rescue: open a fresh session to mark failed.
        await _rescue_mark_failed(request_id, f"internal: {e!r}")
        _close_channel(request_id)
        return

    # Build prompt outside any session.
    prompt = _render_prompt(question, prompt_files)
    cwd = (Path(_config.settings.upload_dir) / request_id).resolve()
    cwd.mkdir(parents=True, exist_ok=True)  # 0-file case

    # Give SSE subscribers a brief window to connect before we start streaming
    # events. Design §5 is "best-effort" — missed events are tolerated — but
    # typical clients POST and then immediately open /stream, so a small
    # cooperative yield keeps the common case lossless. Bounded at ~500ms
    # total so unsubscribed POSTs aren't penalised.
    for _ in range(50):
        if _channels.get(request_id):
            break
        await asyncio.sleep(0.01)

    # -------------- Stream claude output (no DB session held) --------------
    # NOTE: cost_usd / elapsed_ms are carried in the ``done`` event and
    # re-published to subscribers here (§9); they aren't persisted in M3.
    final_md = ""
    error_msg: str | None = None
    saw_terminal = False
    try:
        async for ev in stream(prompt, cwd):
            _publish(request_id, ev)
            tag = ev[0]
            if tag == "delta":
                final_md += ev[1]
            elif tag == "done":
                final_md = ev[1]
                saw_terminal = True
            elif tag == "error":
                error_msg = ev[1] or "unknown claude error"
                saw_terminal = True
    except Exception as e:
        # Claude stream crashed — surface as a failure.
        error_msg = error_msg or f"internal: {e!r}"
        saw_terminal = True

    # Safety net: neither done nor error arrived (HARNESS §1).
    if not saw_terminal and error_msg is None:
        error_msg = "claude produced no output"

    # --------------- Block B: terminal write (fresh session) ---------------
    try:
        async with get_sessionmaker()() as s2:
            async with s2.begin():
                req = await _load_for_update(s2, request_id)
                if req is None:
                    return
                if error_msg:
                    req.status = "failed"
                    req.error_message = error_msg
                    req.completed_at = _utcnow()
                else:
                    try:
                        plan_path = _write_plan(request_id, final_md)
                    except OSError as e:
                        req.status = "failed"
                        req.error_message = f"plan_write_failed: {e!r}"
                        req.completed_at = _utcnow()
                    else:
                        req.status = "done"
                        req.plan_path = str(plan_path)
                        req.completed_at = _utcnow()
    except Exception as e:
        logger.error("research_runner Block B failed rid=%s err=%r", request_id, e)
        await _rescue_mark_failed(request_id, f"internal: {e!r}")
    finally:
        _close_channel(request_id)


async def _rescue_mark_failed(request_id: str, message: str) -> None:
    """Last-resort session to flip ``running`` (or ``pending``) → ``failed``.

    Used when Block A or Block B crashes before it could write a terminal
    state — HARNESS §1 requires a non-empty error_message on every failed row.
    """
    try:
        async with get_sessionmaker()() as s3:
            async with s3.begin():
                req = await _load_for_update(s3, request_id)
                if req is None:
                    return
                if req.status in ("running", "pending"):
                    req.status = "failed"
                    req.error_message = message
                    req.completed_at = _utcnow()
    except Exception as e2:
        logger.error(
            "research_runner rescue session failed rid=%s err=%r",
            request_id,
            e2,
        )
