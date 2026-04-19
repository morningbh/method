"""Async SMTP mailer for Method auth flows.

Renders plain-text Jinja templates in ``app/templates/emails/`` and sends
them via ``aiosmtplib``. Retries transient failures with exponential
backoff (1s, 2s, 4s). On final failure raises ``MailerError``.

Public functions:
- ``send_login_code(to_email, code)``
- ``send_approval_request(admin_email, pending_user_email, approve_url)``
- ``send_activation_notice(to_email)``

All functions are coroutines. They never send real email in tests: tests
monkeypatch ``settings.smtp_host``/``smtp_port`` to a local fake SMTP.
"""
from __future__ import annotations

import asyncio
import logging
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

logger = logging.getLogger("method.mailer")

# ---------------------------------------------------------------------------
# Template environment (cached at module level)
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(disabled_extensions=("txt",), default=False),
    keep_trailing_newline=True,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MailerError(Exception):
    """Raised after retry budget is exhausted for an outgoing email."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_login_code(to_email: str, code: str) -> None:
    """Send the 6-digit login verification code (spec §4.2)."""
    body = _env.get_template("login_code.txt").render(code=code)
    await _send(to_email, "Method 登录验证码", body)


async def send_approval_request(
    admin_email: str, pending_user_email: str, approve_url: str
) -> None:
    """Send the admin the approve-this-user link (spec §4.2)."""
    body = _env.get_template("admin_approval.txt").render(
        user_email=pending_user_email, approve_url=approve_url
    )
    subject = f"[Method] 新用户注册待审批：{pending_user_email}"
    await _send(admin_email, subject, body)


async def send_activation_notice(to_email: str) -> None:
    """Tell a newly-approved user their account is active (spec §4.2)."""
    body = _env.get_template("activation.txt").render(
        user_email=to_email, base_url=settings.base_url
    )
    await _send(to_email, "Method 账号已激活", body)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1, 2, 4)  # sleep after attempt N before N+1


def _build_message(to_email: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    from_name = settings.smtp_from_name or ""
    from_addr = settings.smtp_from
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    return msg


async def _send(to_email: str, subject: str, body: str) -> None:
    """Send a single message, retrying transient SMTP failures."""
    msg = _build_message(to_email, subject, body)
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_port == 587,
                use_tls=settings.smtp_port == 465,
                timeout=30,
            )
            logger.info(
                "mailer.send ok to=%s subject=%r attempt=%d", to_email, subject, attempt
            )
            return
        except (aiosmtplib.SMTPException, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                backoff = _BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "mailer.send retry to=%s subject=%r attempt=%d err=%r sleep=%ds",
                    to_email,
                    subject,
                    attempt,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
            else:
                logger.error(
                    "mailer.send failed to=%s subject=%r attempts=%d err=%r",
                    to_email,
                    subject,
                    attempt,
                    exc,
                )

    raise MailerError(
        f"failed to send email to {to_email!r} after {_MAX_ATTEMPTS} attempts: {last_exc!r}"
    ) from last_exc
