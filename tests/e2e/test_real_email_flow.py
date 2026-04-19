"""Real-SMTP e2e test for Method mailer (Task 2.5).

Guarded by ``RUN_E2E=1`` (HARNESS §5). Exercises ``app.services.mailer._send``
against the real Gmail SMTP server configured in ``.env``. The test asserts
only delivery acceptance (SMTP server returned 250 OK, i.e. no exception was
raised); receipt is verified out-of-band by the main agent via Gmail MCP using
the marker subject printed to stdout.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_real_smtp_delivers_login_code_email() -> None:
    if os.environ.get("RUN_E2E") != "1":
        pytest.skip("e2e not requested (set RUN_E2E=1 to enable)")

    # Import inside the test so the skip path stays free of any app imports.
    from app.config import settings
    from app.services.mailer import _send

    marker = (
        f"[Method E2E {datetime.now(UTC).isoformat()}] "
        "login code test"
    )
    body = (
        "E2E test body from Method — Task 2.5 real-SMTP verification.\n"
        "If you see this, Gmail SMTP delivery succeeded.\n"
    )

    # Call the mailer's internal single-message sender directly, so this test
    # validates the raw SMTP path rather than any subject hardcoded by the
    # higher-level helpers (send_login_code / send_approval_request / ...).
    # A raised exception here means Gmail refused the message; no raise = 250 OK.
    await _send(settings.admin_email, marker, body)

    # Print the marker to stdout (pytest -s) so the main agent can search
    # Gmail MCP for the exact subject afterwards.
    print(f"\nE2E_MARKER_SUBJECT={marker}")
    print(f"E2E_MARKER_TO={settings.admin_email}")
    print(f"E2E_MARKER_SENT_AT={datetime.now(UTC).isoformat()}")
