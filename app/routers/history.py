"""History HTTP boundary (Task 4.1 + 4.2 + 4.3).

Owns the user-facing HTML routes + one JSON list endpoint. The root handler
(``GET /``) lives here (moved from ``auth.py``) because its authed branch
renders the workspace ``index.html``.

Contract source: ``docs/design/issue-3-m4-frontend-ui.md`` §2.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from app.db import get_sessionmaker
from app.models import ResearchRequest, UploadedFile, User
from app.routers.auth import get_current_user, require_user

logger = logging.getLogger("method.routes")

router = APIRouter()


_BEIJING = timezone(timedelta(hours=8))


def format_beijing(dt: datetime | None) -> str:
    """Render a naive-UTC ``datetime`` to a Beijing-time display string.

    Returns ``""`` for ``None`` (template-friendly). DB rows store UTC naive
    via ``datetime.now(UTC).replace(tzinfo=None)`` — we treat them as UTC.
    """
    if dt is None:
        return ""
    # DB rows are naive UTC; attach UTC then convert to Beijing.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


# ---------------------------------------------------------------------------
# GET / — workspace (authed) or redirect to /login (unauthed)
# ---------------------------------------------------------------------------


@router.get("/")
async def root(
    request: Request,
    user: User | None = Depends(get_current_user),
) -> Response:
    if user is None:
        return RedirectResponse(
            url="/login", status_code=status.HTTP_303_SEE_OTHER
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "index.html",
        {"title": "Method", "user": user},
    )


# ---------------------------------------------------------------------------
# Helpers — shared by /history (template) and /api/history (json)
# ---------------------------------------------------------------------------


async def _list_user_items(user: User) -> list[dict]:
    """Return list of history items for ``user``, newest-first.

    Each item: {"request_id", "question", "status", "created_at" (dt),
    "completed_at" (dt|None), "n_files", "created_at_display",
    "completed_at_display", "cost_usd": None, "cost_display"}.

    The template (``history.html``) and JSON (``/api/history``) consume the
    same shape — JSON serialisation drops the ``_display`` fields.
    """
    async with get_sessionmaker()() as s:
        # LEFT JOIN + GROUP BY request_id to get n_files in one round-trip.
        n_files_subq = (
            select(
                UploadedFile.request_id.label("rid"),
                func.count(UploadedFile.id).label("n"),
            )
            .group_by(UploadedFile.request_id)
            .subquery()
        )
        stmt = (
            select(
                ResearchRequest,
                func.coalesce(n_files_subq.c.n, 0).label("n_files"),
            )
            .outerjoin(n_files_subq, n_files_subq.c.rid == ResearchRequest.id)
            .where(ResearchRequest.user_id == user.id)
            .order_by(ResearchRequest.created_at.desc())
        )
        result = await s.execute(stmt)
        rows = result.all()

    items: list[dict] = []
    for row in rows:
        req: ResearchRequest = row[0]
        n_files = int(row[1] or 0)
        items.append(
            {
                "request_id": req.id,
                "question": req.question,
                "status": req.status,
                "created_at": req.created_at,
                "completed_at": req.completed_at,
                "n_files": n_files,
                "cost_usd": None,
                "created_at_display": format_beijing(req.created_at),
                "completed_at_display": format_beijing(req.completed_at),
                "cost_display": "$0.00",
            }
        )
    return items


# ---------------------------------------------------------------------------
# GET /history — HTML list
# ---------------------------------------------------------------------------


@router.get("/history")
async def history_page(
    request: Request,
    user: User = Depends(require_user),
) -> Response:
    items = await _list_user_items(user)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "history.html",
        {"title": "Method — 历史", "user": user, "items": items},
    )


# ---------------------------------------------------------------------------
# GET /api/history — JSON list
# ---------------------------------------------------------------------------


@router.get("/api/history")
async def api_history(
    user: User = Depends(require_user),
) -> JSONResponse:
    items = await _list_user_items(user)
    payload = [
        {
            "request_id": it["request_id"],
            "question": it["question"],
            "status": it["status"],
            "created_at": _iso(it["created_at"]),
            "completed_at": _iso(it["completed_at"]),
            "n_files": it["n_files"],
            "cost_usd": it["cost_usd"],  # always None in M4
        }
        for it in items
    ]
    return JSONResponse(status_code=200, content={"items": payload})


# ---------------------------------------------------------------------------
# GET /history/<id> — HTML detail
# ---------------------------------------------------------------------------


@router.get("/history/{request_id}")
async def history_detail(
    request_id: str,
    request: Request,
    user: User = Depends(require_user),
) -> Response:
    async with get_sessionmaker()() as s:
        req = (
            await s.execute(
                select(ResearchRequest).where(
                    ResearchRequest.id == request_id,
                    ResearchRequest.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if req is None:
            # Missing + cross-user both 404 — no enumeration oracle.
            raise HTTPException(status_code=404, detail="not_found")
        files_rows = list(
            (
                await s.execute(
                    select(UploadedFile).where(
                        UploadedFile.request_id == request_id
                    )
                )
            )
            .scalars()
            .all()
        )

    # Load plan markdown (when done) so the template can seed
    # data-markdown-source for selection-anchored comments. Best-effort: a
    # missing file just leaves the attribute empty.
    plan_markdown = ""
    if req.status == "done" and req.plan_path:
        from pathlib import Path as _Path

        try:
            plan_markdown = _Path(req.plan_path).read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "history_detail plan_path unreadable rid=%s path=%s",
                request_id,
                req.plan_path,
            )

    ctx = {
        "title": f"Method — {req.id}",
        "user": user,
        "request_id": req.id,
        "question": req.question,
        "status": req.status,
        "files": [{"name": f.original_name} for f in files_rows],
        "error_message": req.error_message if req.status == "failed" else None,
        "plan_markdown": plan_markdown,
        "created_at_iso": _iso(req.created_at) or "",
        "completed_at_iso": _iso(req.completed_at),
        "created_at_display": format_beijing(req.created_at),
        "completed_at_display": format_beijing(req.completed_at),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "history_detail.html", ctx)
