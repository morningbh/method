"""Research HTTP boundary (Task 3.3).

Translates ``app.services.research_runner`` into FastAPI routes + SSE. Business
logic lives in ``research_runner``; this module validates input, persists the
request + uploaded files, spawns the background task, and streams events back
to the subscriber as Server-Sent Events.

Contract source: ``docs/design/issue-2-task-3.3-research-routes.md`` §2.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.db import get_sessionmaker
from app.models import Comment, ResearchRequest, UploadedFile, User
from app.routers.auth import require_user, verify_origin
from app.services import comment_runner, file_processor, research_runner

logger = logging.getLogger("method.routes")

router = APIRouter()


_MAX_QUESTION_CHARS = 4000

# Accepted values for the ``mode`` form field. ``general`` drives the
# research-method-designer router skill (default); ``investment`` drives the
# investment-research-planner skill (beta). Unknown values → 400 invalid_mode.
_ALLOWED_MODES = frozenset({"general", "investment"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso(dt) -> str | None:
    """Render a naive-UTC datetime to ISO-8601 string, or None."""
    if dt is None:
        return None
    return dt.isoformat()


async def _load_owned(request_id: str, user: User) -> ResearchRequest | None:
    """Load a ResearchRequest by id+user_id (single ownership-scoped query).

    Returns ``None`` for both missing and cross-user rows — routers MUST NOT
    distinguish (spec §8: no enumeration oracle).
    """
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(ResearchRequest).where(
                ResearchRequest.id == request_id,
                ResearchRequest.user_id == user.id,
            )
        )
        return result.scalar_one_or_none()


def _sse_frame(event: str, data: dict) -> str:
    """Format a single SSE event frame. data is JSON-serialised."""
    body = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


# ---------------------------------------------------------------------------
# POST /api/research — create a new research request
# ---------------------------------------------------------------------------


@router.post("/api/research", dependencies=[Depends(verify_origin)])
async def post_research(
    request: Request,  # noqa: ARG001 — kept for parity/future use
    question: str = Form(...),
    mode: str = Form("general"),
    files: list[UploadFile] = File(default_factory=list),
    user: User = Depends(require_user),
) -> JSONResponse:
    q = question.strip()
    if q == "":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "empty_question"},
        )
    if len(q) > _MAX_QUESTION_CHARS:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "question_too_long"},
        )
    if mode not in _ALLOWED_MODES:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_mode"},
        )

    # file_processor validates count/size/extension. Raises HTTPException(400)
    # with detail={"code","message"} on violation — bubble unchanged.
    try:
        await file_processor.validate_upload_limits(files)
    except file_processor.LimitExceededError as exc:
        # Return the {"code","message"} detail at top level so tests can read
        # body["code"] directly (design §2.1 step 3 — bubble the shape).
        return JSONResponse(status_code=400, content=exc.detail)

    request_id = research_runner.ulid_new()
    created_at = research_runner._utcnow()

    # Persist request + files in a single transaction. If anything fails the
    # txn rolls back and the background task is never spawned.
    try:
        async with get_sessionmaker()() as s:
            async with s.begin():
                from app import config as _config

                req = ResearchRequest(
                    id=request_id,
                    user_id=user.id,
                    question=q,
                    status="pending",
                    plan_path=None,
                    error_message=None,
                    model=_config.settings.claude_model,
                    created_at=created_at,
                    completed_at=None,
                )
                s.add(req)

                for f in files:
                    content = await f.read()
                    saved = await file_processor.save_and_extract(
                        request_id, f.filename or "", content
                    )
                    s.add(
                        UploadedFile(
                            request_id=request_id,
                            original_name=f.filename or "",
                            stored_path=str(saved.stored_path),
                            extracted_path=(
                                str(saved.extracted_path)
                                if saved.extracted_path
                                else None
                            ),
                            size_bytes=saved.size_bytes,
                            mime_type=saved.mime_type,
                            created_at=created_at,
                        )
                    )
    except HTTPException:
        # LimitExceededError from save_and_extract (e.g. mime_mismatch).
        raise
    except Exception:
        logger.exception("post_research persistence failed rid=%s", request_id)
        return JSONResponse(
            status_code=500, content={"error": "internal"}
        )

    # Spawn background task — txn already committed.
    await research_runner.run_research(request_id, mode)

    logger.info(
        "research.post_research created rid=%s user_id=%s mode=%s files=%d",
        request_id,
        user.id,
        mode,
        len(files),
    )
    return JSONResponse(
        status_code=201,
        content={"request_id": request_id, "status": "pending"},
    )


# ---------------------------------------------------------------------------
# GET /api/research/<id>/stream — SSE
# ---------------------------------------------------------------------------


@router.get("/api/research/{request_id}/stream")
async def get_research_stream(
    request_id: str,
    user: User = Depends(require_user),
) -> StreamingResponse:
    row = await _load_owned(request_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")

    # Replay-only paths for already-terminal rows (design §2.2 steps 2–3).
    if row.status == "done":
        markdown = ""
        if row.plan_path:
            try:
                markdown = Path(row.plan_path).read_text(encoding="utf-8")
            except OSError:
                logger.error(
                    "research.stream done row but plan_path unreadable rid=%s",
                    request_id,
                )
                markdown = ""

        async def _replay_done() -> AsyncIterator[str]:
            yield _sse_frame(
                "done",
                {
                    "request_id": request_id,
                    "markdown": markdown,
                    "cost_usd": None,
                },
            )

        return StreamingResponse(
            _replay_done(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if row.status == "failed":
        msg = row.error_message or "unknown error"

        async def _replay_failed() -> AsyncIterator[str]:
            yield _sse_frame("error", {"message": msg})

        return StreamingResponse(
            _replay_failed(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Live stream for pending/running rows.
    queue = research_runner.subscribe(request_id)

    async def _live() -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                tag = event[0] if isinstance(event, tuple) else None
                if tag == "delta":
                    yield _sse_frame("delta", {"text": event[1]})
                elif tag == "done":
                    markdown = event[1]
                    cost = event[2] if len(event) > 2 else None
                    elapsed = event[3] if len(event) > 3 else None
                    yield _sse_frame(
                        "done",
                        {
                            "request_id": request_id,
                            "markdown": markdown,
                            "cost_usd": cost,
                            "elapsed_ms": elapsed,
                        },
                    )
                    break
                elif tag == "error":
                    yield _sse_frame("error", {"message": event[1]})
                    break
                elif tag == "__close__":
                    break
                # Unknown tag — skip (forward-compatible).
        except asyncio.CancelledError:
            # Client disconnect — unwind finally and re-raise so Starlette
            # knows the response was aborted cleanly.
            raise
        finally:
            research_runner.unsubscribe(request_id, queue)

    return StreamingResponse(
        _live(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# GET /api/research/<id> — JSON
# ---------------------------------------------------------------------------


@router.get("/api/research/{request_id}")
async def get_research_json(
    request_id: str,
    user: User = Depends(require_user),
) -> JSONResponse:
    row = await _load_owned(request_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")

    # Load associated files.
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(UploadedFile).where(UploadedFile.request_id == request_id)
        )
        files_rows = list(result.scalars().all())

    markdown: str | None = None
    if row.status == "done" and row.plan_path:
        try:
            markdown = Path(row.plan_path).read_text(encoding="utf-8")
        except OSError:
            logger.error(
                "research.json done row but plan_path unreadable rid=%s",
                request_id,
            )
            markdown = None

    body = {
        "request_id": row.id,
        "status": row.status,
        "question": row.question,
        "markdown": markdown,
        "error_message": row.error_message if row.status == "failed" else None,
        "cost_usd": None,  # M3: transient only, not persisted (design §9)
        "elapsed_ms": None,
        "created_at": _utcnow_iso(row.created_at),
        "completed_at": _utcnow_iso(row.completed_at),
        "files": [
            {"name": f.original_name, "size": f.size_bytes}
            for f in files_rows
        ],
    }
    return JSONResponse(status_code=200, content=body)


# ---------------------------------------------------------------------------
# GET /api/research/<id>/download — markdown attachment
# ---------------------------------------------------------------------------


@router.get("/api/research/{request_id}/download")
async def get_research_download(
    request_id: str,
    user: User = Depends(require_user),
):
    row = await _load_owned(request_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    if row.status != "done" or not row.plan_path:
        raise HTTPException(status_code=404, detail="not_found")
    plan_path = Path(row.plan_path)
    if not plan_path.exists():
        logger.error(
            "research.download plan_path missing rid=%s path=%s",
            request_id,
            plan_path,
        )
        return JSONResponse(status_code=500, content={"error": "plan_missing"})

    return FileResponse(
        str(plan_path),
        media_type="text/markdown",
        filename=f"research-{request_id}.md",
    )


# ---------------------------------------------------------------------------
# DELETE /api/research/<id> — owner-scoped delete; cleans DB + filesystem.
# ---------------------------------------------------------------------------


@router.delete(
    "/api/research/{request_id}", dependencies=[Depends(verify_origin)]
)
async def delete_research(
    request_id: str,
    user: User = Depends(require_user),
) -> Response:
    """Delete a research request owned by ``user``.

    - 404 if missing OR owned by another user (no enumeration oracle).
    - 409 if the request is still ``pending`` / ``running`` (background task
      owns the upload dir; racing with the live claude subprocess would break
      its reads and leak partial state).
    - 204 on success. Cascades: ``uploaded_files`` rows → DB commit, then
      ``{upload_dir}/{request_id}/`` directory + ``plan_path`` file on disk.
      Post-commit filesystem cleanup is best-effort (logged, not fatal) so a
      transient fs error cannot leave a row in an inconsistent state.
    """
    row = await _load_owned(request_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    if row.status in ("pending", "running"):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "request_busy",
                "message": "请求仍在处理中，请等它结束后再删除",
            },
        )

    plan_path_str = row.plan_path
    async with get_sessionmaker()() as s:
        async with s.begin():
            await s.execute(
                delete(UploadedFile).where(UploadedFile.request_id == request_id)
            )
            await s.execute(
                delete(ResearchRequest).where(
                    ResearchRequest.id == request_id,
                    ResearchRequest.user_id == user.id,
                )
            )

    # Post-commit filesystem cleanup — best-effort.
    try:
        await file_processor.cleanup_request(request_id)
    except Exception:
        logger.exception(
            "research.delete cleanup_request failed rid=%s (row already deleted)",
            request_id,
        )

    if plan_path_str:
        try:
            p = Path(plan_path_str)
            if p.exists():
                p.unlink()
        except OSError:
            logger.exception(
                "research.delete plan file unlink failed rid=%s path=%s "
                "(row already deleted)",
                request_id,
                plan_path_str,
            )

    logger.info(
        "research.delete rid=%s user_id=%s", request_id, user.id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Comments (Issue #4 — Feature B). See docs/design/issue-4-feature-b-comments.md
# ---------------------------------------------------------------------------

_MAX_COMMENTS_RETURNED = 200  # design §4 hard cap
_MAX_ANCHOR_LEN = 2000
_MAX_ANCHOR_AROUND = 50
_MAX_BODY_LEN = 2000


class CommentCreateIn(BaseModel):
    # No pydantic-level length limits: over-limit values must surface as
    # 400 {"error": "..."} per design §4, not as 422 default validation errors.
    anchor_before: str = ""
    anchor_text: str
    anchor_after: str = ""
    body: str


def _serialize_comment(row: Comment) -> dict:
    """API shape per design §4 — excludes user_id and deleted_at."""
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
        "created_at": _utcnow_iso(row.created_at),
    }


@router.post(
    "/api/research/{request_id}/comments",
    dependencies=[Depends(verify_origin)],
)
async def post_comment(
    request_id: str,
    payload: CommentCreateIn,
    user: User = Depends(require_user),
) -> JSONResponse:
    # Owner check + status gate.
    req = await _load_owned(request_id, user)
    if req is None:
        raise HTTPException(status_code=404, detail="not_found")
    if req.status not in ("done", "failed"):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "request_not_finalized"},
        )

    # Manual length validation — must surface as 400 with structured error
    # body, not as 422 default validation response.
    if not payload.anchor_text or len(payload.anchor_text) > _MAX_ANCHOR_LEN:
        return JSONResponse(
            status_code=400, content={"error": "anchor_text_invalid"}
        )
    if not payload.body or len(payload.body) > _MAX_BODY_LEN:
        return JSONResponse(
            status_code=400, content={"error": "body_invalid"}
        )
    if (
        len(payload.anchor_before) > _MAX_ANCHOR_AROUND
        or len(payload.anchor_after) > _MAX_ANCHOR_AROUND
    ):
        return JSONResponse(
            status_code=400, content={"error": "anchor_context_too_long"}
        )

    # Normalize body via comment_runner; empty → 400 body_empty (design §5).
    try:
        result = await comment_runner.create_user_comment(
            request_id=request_id,
            user_id=user.id,
            payload=payload.model_dump(),
        )
    except comment_runner.BodyEmptyError:
        return JSONResponse(
            status_code=400,
            content={"error": "body_empty"},
        )
    except Exception:
        logger.exception(
            "post_comment create failed rid=%s user_id=%s", request_id, user.id
        )
        return JSONResponse(
            status_code=500, content={"error": "internal"}
        )

    # Spawn AI reply generation (background task).
    ai_placeholder = result.get("ai_placeholder") or {}
    ai_cid = ai_placeholder.get("id")
    if ai_cid:
        await comment_runner.run_ai_reply(ai_cid)

    logger.info(
        "research.post_comment rid=%s user_id=%s cid=%s",
        request_id,
        user.id,
        (result.get("comment") or {}).get("id"),
    )
    return JSONResponse(status_code=201, content=result)


@router.get("/api/research/{request_id}/comments")
async def get_comments(
    request_id: str,
    user: User = Depends(require_user),
) -> JSONResponse:
    req = await _load_owned(request_id, user)
    if req is None:
        raise HTTPException(status_code=404, detail="not_found")

    # Design §2: single SELECT (all rows for the request, filtered deleted),
    # then Python nests AI replies under their user parents (avoid N+1).
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(Comment)
            .where(
                Comment.request_id == request_id,
                Comment.deleted_at.is_(None),
            )
            .order_by(Comment.created_at.asc())
        )
        rows = list(result.scalars().all())

    # Partition: user comments in order; AI replies by parent_id.
    ai_by_parent: dict[str, Comment] = {}
    user_rows: list[Comment] = []
    for r in rows:
        if r.author == "ai" and r.parent_id is not None:
            # If multiple AI rows point at the same parent (shouldn't happen
            # in MVP-1 but the schema allows it), keep the latest.
            prior = ai_by_parent.get(r.parent_id)
            if prior is None or (r.created_at and prior.created_at and r.created_at >= prior.created_at):
                ai_by_parent[r.parent_id] = r
        elif r.author == "user":
            user_rows.append(r)

    # Hard cap at 200; oldest-first already, so slice from the end.
    truncated = len(user_rows) > _MAX_COMMENTS_RETURNED
    if truncated:
        # Design §4: "超过按 created_at DESC 截断" — keep the newest 200.
        user_rows = user_rows[-_MAX_COMMENTS_RETURNED:]

    comments_payload = []
    for u in user_rows:
        item = _serialize_comment(u)
        item.pop("ai_status", None)
        item.pop("ai_error", None)
        item.pop("cost_usd", None)
        ai = ai_by_parent.get(u.id)
        item["ai_reply"] = _serialize_comment(ai) if ai is not None else None
        comments_payload.append(item)

    headers = {}
    if truncated:
        headers["X-Comments-Truncated"] = "true"

    return JSONResponse(
        status_code=200,
        content={"comments": comments_payload},
        headers=headers,
    )


@router.delete(
    "/api/research/{request_id}/comments/{comment_id}",
    dependencies=[Depends(verify_origin)],
)
async def delete_comment(
    request_id: str,
    comment_id: str,
    user: User = Depends(require_user),
) -> Response:
    req = await _load_owned(request_id, user)
    if req is None:
        raise HTTPException(status_code=404, detail="not_found")

    # Look up the comment. If it's an AI reply, 403. If missing / cross-user,
    # 404 (no enumeration oracle).
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                select(Comment).where(
                    Comment.id == comment_id,
                    Comment.request_id == request_id,
                    Comment.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    if row.author == "ai":
        return JSONResponse(
            status_code=403,
            content={"error": "ai_reply_not_deletable"},
        )
    if row.user_id != user.id:
        raise HTTPException(status_code=404, detail="not_found")

    touched = await comment_runner.cascade_soft_delete(
        request_id=request_id,
        comment_id=comment_id,
        user_id=user.id,
    )
    if touched == 0:
        # Race: row disappeared between lookup and delete.
        raise HTTPException(status_code=404, detail="not_found")

    logger.info(
        "research.delete_comment rid=%s cid=%s user_id=%s touched=%d",
        request_id,
        comment_id,
        user.id,
        touched,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/research/{request_id}/comments/stream")
async def get_comments_stream(
    request_id: str,
    comment_id: str,
    user: User = Depends(require_user),
) -> StreamingResponse:
    """SSE channel for a single AI reply's deltas + done event."""
    req = await _load_owned(request_id, user)
    if req is None:
        raise HTTPException(status_code=404, detail="not_found")

    # Verify comment_id belongs to this request.
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                select(Comment).where(
                    Comment.id == comment_id,
                    Comment.request_id == request_id,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")

    # If the AI reply is already terminal, replay one event and close.
    if row.ai_status in ("done", "failed"):
        terminal = {
            "comment_id": comment_id,
            "body": row.body,
            "ai_status": row.ai_status,
            "ai_error": row.ai_error,
            "cost_usd": row.cost_usd,
        }

        async def _replay() -> AsyncIterator[str]:
            yield _sse_frame("ai_done", terminal)

        return StreamingResponse(
            _replay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Live subscribe for pending/streaming rows.
    queue = comment_runner.subscribe(comment_id)

    async def _live() -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                if not isinstance(event, tuple) or not event:
                    continue
                tag = event[0]
                if tag == "ai_delta":
                    yield _sse_frame("ai_delta", event[1])
                elif tag == "ai_done":
                    yield _sse_frame("ai_done", event[1])
                    break
                elif tag == "__close__":
                    break
        except asyncio.CancelledError:
            raise
        finally:
            comment_runner.unsubscribe(comment_id, queue)

    return StreamingResponse(
        _live(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
