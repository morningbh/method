"""Unit tests for the mailer service (Task 2.2).

Uses a local fake SMTP server (aiosmtpd) — NEVER hits real Gmail. Covers
the three email templates from spec §4.2 and retry semantics from §4.3
(3 attempts, exponential backoff).
"""
from __future__ import annotations

import asyncio
import email
import socket
from email import policy
from email.parser import BytesParser

import pytest
import pytest_asyncio
from aiosmtpd.controller import Controller

# ---------------------------------------------------------------------------
# Fake SMTP infrastructure
# ---------------------------------------------------------------------------


class _RecordingHandler:
    """Async message handler that stores every received envelope."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 aiosmtpd convention
        self.messages.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "data": bytes(envelope.content),
            }
        )
        return "250 OK"


class _FailingHandler:
    """Handler that fails `fail_times` DATA commands, then starts succeeding."""

    def __init__(self, fail_times: int, always_fail: bool = False) -> None:
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.attempts = 0
        self.messages: list[dict] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        self.attempts += 1
        if self.always_fail or self.attempts <= self.fail_times:
            # 421 is a transient/temporary failure; aiosmtplib will raise.
            return "421 Temporary failure, try again later"
        self.messages.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "data": bytes(envelope.content),
            }
        )
        return "250 OK"


def _free_port() -> int:
    """Reserve an ephemeral port (aiosmtpd Controller needs an explicit port
    because its self-trigger logic cannot discover a port=0 assignment)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_smtp(handler) -> Controller:
    """Start a local SMTP server on a freshly reserved ephemeral port."""
    controller = Controller(handler, hostname="127.0.0.1", port=_free_port())
    controller.start()
    return controller


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_smtp(monkeypatch):
    """Start a recording SMTP on an ephemeral port and point settings at it."""
    handler = _RecordingHandler()
    controller = _start_smtp(handler)
    try:
        _patch_settings(monkeypatch, controller.hostname, controller.port)
        yield handler
    finally:
        controller.stop()


def _patch_settings(monkeypatch, host: str, port: int) -> None:
    """Point the mailer at a local fake SMTP and reset the jinja env."""
    from app import config as config_mod

    monkeypatch.setattr(config_mod.settings, "smtp_host", host)
    monkeypatch.setattr(config_mod.settings, "smtp_port", port)
    monkeypatch.setattr(config_mod.settings, "smtp_user", "")
    monkeypatch.setattr(config_mod.settings, "smtp_password", "")
    monkeypatch.setattr(config_mod.settings, "smtp_from", "method@example.com")
    monkeypatch.setattr(config_mod.settings, "smtp_from_name", "Method")


def _parse(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def _body(msg: email.message.EmailMessage) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


# ---------------------------------------------------------------------------
# Template tests
# ---------------------------------------------------------------------------


async def test_send_login_code_delivers_email(fake_smtp):
    from app.services.mailer import send_login_code

    await send_login_code("alice@example.com", "123456")

    assert len(fake_smtp.messages) == 1
    msg = _parse(fake_smtp.messages[0]["data"])
    assert "alice@example.com" in msg["To"]
    assert "验证码" in msg["Subject"]
    body = _body(msg)
    assert "123456" in body
    assert "10 分钟" in body


async def test_send_approval_request_contains_link(fake_smtp):
    from app.services.mailer import send_approval_request

    approve_url = "https://method.example.com/admin/approve?token=abcdef123"
    await send_approval_request(
        "admin@example.com", "newuser@example.com", approve_url
    )

    assert len(fake_smtp.messages) == 1
    msg = _parse(fake_smtp.messages[0]["data"])
    assert "admin@example.com" in msg["To"]
    assert "newuser@example.com" in msg["Subject"]
    body = _body(msg)
    assert approve_url in body
    assert "newuser@example.com" in body


async def test_send_activation_notice_contains_base_url(fake_smtp):
    from app.config import settings
    from app.services.mailer import send_activation_notice

    await send_activation_notice("bob@example.com")

    assert len(fake_smtp.messages) == 1
    msg = _parse(fake_smtp.messages[0]["data"])
    assert "bob@example.com" in msg["To"]
    assert "激活" in msg["Subject"]
    body = _body(msg)
    assert settings.base_url in body
    assert "bob@example.com" in body


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


async def test_retry_on_failure_then_success(monkeypatch):
    """Handler fails twice, third attempt succeeds — mailer must retry."""
    handler = _FailingHandler(fail_times=2)
    controller = _start_smtp(handler)
    try:
        _patch_settings(monkeypatch, controller.hostname, controller.port)
        # Skip the real sleep to keep the test fast; preserves call ordering.
        import app.services.mailer as mailer

        slept: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr(mailer.asyncio, "sleep", _fake_sleep)

        await mailer.send_login_code("carol@example.com", "654321")
    finally:
        controller.stop()

    assert handler.attempts == 3, f"expected 3 attempts, got {handler.attempts}"
    assert len(handler.messages) == 1
    # Two retries → two sleeps with exponential backoff 1s, 2s.
    assert slept == [1, 2]


async def test_raises_after_3_failures(monkeypatch):
    """Three consecutive failures must raise MailerError."""
    handler = _FailingHandler(fail_times=0, always_fail=True)
    controller = _start_smtp(handler)
    try:
        _patch_settings(monkeypatch, controller.hostname, controller.port)

        import app.services.mailer as mailer

        async def _fake_sleep(delay: float) -> None:
            return None

        monkeypatch.setattr(mailer.asyncio, "sleep", _fake_sleep)

        with pytest.raises(mailer.MailerError):
            await mailer.send_login_code("dave@example.com", "000000")
    finally:
        controller.stop()

    assert handler.attempts == 3, f"expected 3 attempts, got {handler.attempts}"


async def test_chinese_subject_and_body_encoded_correctly(fake_smtp):
    """Chinese characters must round-trip through SMTP without mojibake."""
    from app.services.mailer import send_login_code

    await send_login_code("eve@example.com", "987654")

    msg = _parse(fake_smtp.messages[0]["data"])
    # Decoded headers / body should contain the original Chinese glyphs.
    subject = str(msg["Subject"])
    assert "验证码" in subject
    assert "Method" in subject
    body = _body(msg)
    assert "你好" in body
    assert "登录验证码" in body
    assert "10 分钟" in body
    # Sanity: no "=?utf-8?" raw MIME fallback leaked into decoded form.
    assert "=?utf-8?" not in subject
    assert "=?utf-8?" not in body


# ---------------------------------------------------------------------------
# Sanity: asyncio is imported by mailer (used by tests monkeypatching sleep).
# If this import fails the module is malformed.
# ---------------------------------------------------------------------------


def test_mailer_module_importable():
    import app.services.mailer as m

    assert hasattr(m, "send_login_code")
    assert hasattr(m, "send_approval_request")
    assert hasattr(m, "send_activation_notice")
    assert issubclass(m.MailerError, Exception)
    assert asyncio is not None  # keep linter happy; also ensures import works
