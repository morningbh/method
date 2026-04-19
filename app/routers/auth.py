"""Auth HTTP boundary (Task 2.4).

Translates ``app.services.auth_flow`` into FastAPI routes + session-cookie
handling. Business logic lives in ``auth_flow``; this module only validates
input, wraps each service call in a transaction, translates typed exceptions
into HTTP responses, and assembles the session-cookie header per HARNESS §4.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config
from app.db import get_sessionmaker
from app.models import User
from app.services import auth_flow

logger = logging.getLogger("method.routes")

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def _db_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession without starting a transaction.

    Each handler starts its own ``async with session.begin():`` so that the
    commit/rollback boundary is owned at the HTTP layer (design §5).
    """
    async with get_sessionmaker()() as session:
        yield session


async def get_current_user(
    method_session: str | None = Cookie(default=None),
) -> User | None:
    """Return the ``User`` owning the ``method_session`` cookie, or ``None``.

    Uses a short-lived dedicated session so it doesn't pollute a handler's
    own transaction scope (the handler opens ``_db_session`` separately).
    """
    if not method_session:
        return None
    async with get_sessionmaker()() as session:
        return await auth_flow.validate_session_cookie(session, method_session)


async def require_user(
    request: Request,
    user: User | None = Depends(get_current_user),
) -> User:
    """Enforce auth. Returns the ``User`` or short-circuits with a JSON 401
    / HTML 303 response."""
    if user is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept and "application/json" not in accept:
            raise _RedirectRequired("/login")
        raise _Unauthenticated()
    return user


class _Unauthenticated(Exception):
    """Marker raised by ``require_user`` for JSON 401 translation."""


class _RedirectRequired(Exception):
    """Marker raised by ``require_user`` for HTML 303 translation."""

    def __init__(self, location: str) -> None:
        self.location = location


async def verify_origin(request: Request) -> None:
    """CSRF defence — reject POSTs whose ``Origin`` header doesn't match ``base_url``.

    Absent ``Origin`` is permitted (some clients strip it); ``SameSite=Lax`` is
    the primary cross-site cookie defence. Design §11.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    base = _config.settings.base_url
    if not base:
        # Permissive when base_url not configured — documented in design §12.
        return
    parsed_origin = urlparse(origin)
    parsed_base = urlparse(base)
    if (parsed_origin.scheme, parsed_origin.netloc) != (
        parsed_base.scheme,
        parsed_base.netloc,
    ):
        logger.warning(
            "auth.verify_origin reject origin=%r base=%r", origin, base
        )
        raise _BadOrigin()


class _BadOrigin(Exception):
    """Marker raised by ``verify_origin`` when the Origin header mismatches."""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RequestCodeIn(BaseModel):
    email: EmailStr


class RequestCodeOut(BaseModel):
    status: Literal["sent", "pending", "rejected"]


class VerifyCodeIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class VerifyCodeOut(BaseModel):
    ok: bool = True


class LogoutOut(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# Routes — JSON API
# ---------------------------------------------------------------------------


@router.post("/api/auth/request_code", dependencies=[Depends(verify_origin)])
async def request_code(
    payload: RequestCodeIn,
    session: AsyncSession = Depends(_db_session),
) -> JSONResponse:
    try:
        async with session.begin():
            result = await auth_flow.request_login_code(session, payload.email)
    except auth_flow.RateLimitError:
        logger.info("auth.request_code rate_limited email=%s", payload.email)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "rate_limit"},
        )
    except auth_flow.MailerError:
        logger.error("auth.request_code mail_send_failed email=%s", payload.email)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": "mail_send_failed"},
        )
    except IntegrityError:
        logger.warning("auth.request_code integrity_error email=%s", payload.email)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "bad_request"},
        )

    logger.info("auth.request_code ok email=%s status=%s", payload.email, result)
    return JSONResponse(status_code=200, content={"status": result})


@router.post("/api/auth/verify_code", dependencies=[Depends(verify_origin)])
async def verify_code(
    payload: VerifyCodeIn,
    session: AsyncSession = Depends(_db_session),
) -> Response:
    try:
        async with session.begin():
            raw = await auth_flow.verify_login_code(
                session, payload.email, payload.code
            )
    except auth_flow.InvalidCodeError:
        logger.info("auth.verify_code invalid email=%s", payload.email)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_or_expired"},
        )

    max_age = _config.settings.session_ttl_days * 86400
    resp = JSONResponse(status_code=200, content={"ok": True})
    resp.set_cookie(
        key=auth_flow.COOKIE_NAME,
        value=raw,
        max_age=max_age,
        path="/",
        **auth_flow.COOKIE_FLAGS,
    )
    logger.info("auth.verify_code ok email=%s", payload.email)
    return resp


@router.post("/api/auth/logout", dependencies=[Depends(verify_origin)])
async def logout(
    user: User = Depends(require_user),
    method_session: str | None = Cookie(default=None),
    session: AsyncSession = Depends(_db_session),
) -> Response:
    if method_session:
        async with session.begin():
            await auth_flow.invalidate_session_cookie(session, method_session)

    resp = JSONResponse(status_code=200, content={"ok": True})
    # Manually assemble the clearing Set-Cookie header so the empty value
    # isn't quoted (http.cookies would emit `name=""` which breaks some
    # clients' parsers). Design §2.3: ``value=""; Max-Age=0; Path=/`` plus
    # the HARNESS §4 flags.
    parts = [f"{auth_flow.COOKIE_NAME}=", "Max-Age=0", "Path=/"]
    if auth_flow.COOKIE_FLAGS.get("httponly"):
        parts.append("HttpOnly")
    samesite = auth_flow.COOKIE_FLAGS.get("samesite")
    if samesite:
        parts.append(f"SameSite={samesite.capitalize()}")
    if auth_flow.COOKIE_FLAGS.get("secure"):
        parts.append("Secure")
    resp.headers["set-cookie"] = "; ".join(parts)
    logger.info("auth.logout ok user_id=%s", user.id)
    return resp


# ---------------------------------------------------------------------------
# Routes — HTML
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Method — 登录"},
    )


# Note: ``GET /`` used to live here and render ``landing.html``. It moved to
# ``app/routers/history.py::root`` in M4 (design §2.1) — the authed branch
# now renders the workspace ``index.html``, unauthed redirects unchanged.


# ---------------------------------------------------------------------------
# Module-level exception handlers for auth-related markers
# ---------------------------------------------------------------------------


def install_exception_handlers(app) -> None:
    """Register JSON/redirect responses for the internal marker exceptions.

    Invoked from ``app/main.py`` after the app is constructed.
    """

    @app.exception_handler(_Unauthenticated)
    async def _handle_unauth(request: Request, exc: _Unauthenticated):  # noqa: ARG001
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "unauthenticated"},
        )

    @app.exception_handler(_RedirectRequired)
    async def _handle_redirect(request: Request, exc: _RedirectRequired):  # noqa: ARG001
        return RedirectResponse(
            url=exc.location, status_code=status.HTTP_303_SEE_OTHER
        )

    @app.exception_handler(_BadOrigin)
    async def _handle_bad_origin(request: Request, exc: _BadOrigin):  # noqa: ARG001
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "bad_origin"},
        )
