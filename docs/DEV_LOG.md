# Method Dev Log

## 2026-04-19 — Session 1: M1 + M2 partial

### Shipped

- M1 scaffolding: FastAPI skeleton, `pyproject.toml`, `Makefile`, `/api/health` smoke test (commits `828a688` → `83b6197`)
- M2 Task 2.1: data models (`users`, `login_codes`, `sessions`, `approval_tokens`) + tests (commit `91a7434`)
- Bugfix: SQLite FK pragma enabled per-connection (commit `c8a0768`)
- M2 Task 2.2: mailer service + templates + tests using `aiosmtpd` fake SMTP (commit `d47ca26`)

### Non-obvious decisions

- DB engine made lazy (`get_engine` / `get_sessionmaker`) so tests can reset per-test without `sys.modules` surgery. Originally created eagerly; refactored during Task 1.1 code review.
- `tests/conftest.py` uses `app.router.lifespan_context(app)` (httpx 0.28 feature) to drive the real lifespan inside ASGI transport tests. Alternative was the `asgi-lifespan` dep — chose the stdlib path.
- Kept `test_cascade_or_no_cascade` to document the RESTRICT-not-CASCADE decision explicitly so future refactors don't silently add cascades.
- `aiosmtpd` `Controller(port=0)` triggers a refused-connection self-probe — reserve an ephemeral port via a throwaway socket bind first, then pass it explicitly to `Controller(port=<reserved>)`.
- Retry schedule in mailer is `(1s, 2s, 4s)` across 3 attempts: only 2 sleeps actually fire, but the tuple is kept at 3 entries for spec traceability (spec §8).

### Things the user should know

- `tests/unit/test_mailer.py` reserves random local ports; could get flaky if system port usage spikes — revisit if CI gets noisy.
- `google-chrome-stable` installed on the server for M4/M5 web e2e screenshots (snap chromium was broken by AppArmor + snap confinement interaction).
- `cloudflared` v2026.3.0 installed and ready for M5 tunnel.
- All 5 commits are pushed to `main` at github.com/morningbh/method.

### Process lesson

- M1, 2.1, 2.2 used a lightweight subagent-driven-development flow (spec review + code-quality review per task). The strict 10-step workflow (with `/preflight`, `/design-check`, `/tester`, `/test-quality-check`, `/review` skills) resumes from Task 2.3.
- `/preflight` flagged missing process docs (`AGENT_CONTEXT`, `TESTER_PROMPT`, `CODE_REVIEW_PROMPT`, `DEV_LOG`, `HARNESS.md` component map). Created them in this session.
