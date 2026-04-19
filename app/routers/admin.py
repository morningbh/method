"""Admin HTTP boundary (Task 2.4).

Exposes ``GET /admin/approve?token=<raw>`` — the link embedded in the
admin-approval email. Translates ``ApprovalTokenError`` into a rendered
``approval_error.html`` page (HTTP 200 by design §2.4, the error is a
user-visible condition, not a protocol error).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.auth import _db_session
from app.services import auth_flow

logger = logging.getLogger("method.routes")

router = APIRouter()


@router.get("/admin/approve")
async def approve(
    request: Request,
    token: str = Query(...),
    session: AsyncSession = Depends(_db_session),
) -> Response:
    templates = request.app.state.templates
    try:
        async with session.begin():
            user = await auth_flow.approve_user(session, token)
    except auth_flow.ApprovalTokenError:
        logger.info("admin.approve invalid_token")
        return templates.TemplateResponse(
            request,
            "approval_error.html",
            {"title": "Method — 审批失败"},
        )

    logger.info("admin.approve ok email=%s", user.email)
    return templates.TemplateResponse(
        request,
        "approved.html",
        {"title": "Method — 审批完成", "email": user.email},
    )
