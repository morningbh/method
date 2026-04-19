# Task 2.5 — Real-SMTP e2e Test (design)

**Scope**: Issue #1 / Milestone M2 verification. A single e2e test that proves
`app/services/mailer.py` can deliver a message through real Gmail SMTP using
the credentials configured in `.env`. The test does not start any router or
touch the DB; it exercises the mailer's private `_send` path directly.

## 1. Purpose

Close the integration gap between "unit tests with a fake SMTP" and "production
Gmail SMTP". HARNESS §5 requires a `RUN_E2E=1` gate for this class of test;
unit tests live in `tests/unit/test_mailer.py` and already cover retry/backoff
logic via a fake aiosmtpd server. This test adds the real-network leg.

## 2. Contract (what the test does)

`test_real_smtp_delivers_login_code_email` in `tests/e2e/test_real_email_flow.py`:

1. `pytest.skip("e2e not requested")` unless `os.environ["RUN_E2E"] == "1"`.
2. Loads `settings` from `.env` (pydantic-settings). Reads
   `settings.admin_email` as recipient and `settings.smtp_*` as credentials.
   Does not read / log `smtp_password`.
3. Builds a unique subject marker:
   `f"[Method E2E {datetime.now(timezone.utc).isoformat()}] login code test"`.
4. Imports the underscore-prefixed helper `_send` directly from
   `app.services.mailer` (module-private by convention; this test is the
   single authorized caller outside the module).
5. `await _send(to_email=settings.admin_email, subject=marker,
   body="E2E test body from Method — Task 2.5 real-SMTP verification.")`.
6. No exception raised → Gmail SMTP accepted the message (250 OK).
7. Prints the marker and timestamp to stdout (`-s` on pytest) so the main
   agent can look up the message in Gmail MCP.

Assertion surface: the awaited call returns `None` and does NOT raise
`MailerError` or any `aiosmtplib.SMTPException`. Receipt verification is out
of scope for pytest (requires Gmail MCP, only available to the main agent).

## 3. Files created

| Path | Action | Purpose |
|---|---|---|
| `docs/design/issue-1-task-2.5-e2e-smtp.md` | create | this document |
| `tests/e2e/__init__.py` | create | make `tests/e2e` a package |
| `tests/e2e/test_real_email_flow.py` | create | the one e2e test |

No production code changes. `_send` already exists in `app/services/mailer.py`
and accepts `(to_email, subject, body)` — the test calls it as-is.

## 4. How to run

```bash
cd /home/ubuntu/method
RUN_E2E=1 .venv/bin/python -c "import pytest; pytest.main(['-v', '-s', 'tests/e2e/test_real_email_flow.py'])"
```

Without `RUN_E2E=1` the test skips immediately, so `make test` still runs
fast. Do NOT run more than once per minute — Gmail rate-limits.

## 5. How to verify in Gmail MCP

After the test prints the marker to stdout, the main agent can run (outside
pytest, via MCP):

- `mcp__claude_ai_Gmail__search_threads` with query
  `subject:"[Method E2E <timestamp>]"` → expects exactly one matching thread
  in the admin inbox within a few seconds.

## 6. Test plan

One test: `test_real_smtp_delivers_login_code_email` (see §2).

## 7. Infrastructure dependency table

| Dependency | Required by | Failure mode | Degradation |
|---|---|---|---|
| Gmail SMTP `smtp.gmail.com:587` | `_send` call | network error / auth error → `aiosmtplib.SMTPException` → bubbles out as test FAIL | none at MVP — test just fails with the raised exception (HARNESS §1 "no silent failures") |
| `settings.smtp_password` (app password) | `_send` call | missing → pydantic-settings fails at import; wrong → Gmail 535 → mailer retries 3x then raises `MailerError` | retries handled by `mailer._send`; final failure fails the test loudly |
| `RUN_E2E` env var | test entry | absent → `pytest.skip` | intended — HARNESS §5 |
