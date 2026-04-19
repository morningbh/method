# Method — Agent Context

Everything an agent needs to know before touching this repo. Read this before loading the full spec.

## What Method is

A single-page research-planner web app. A user types a research question and optionally uploads background docs; the backend calls `claude -p` running the `research-method-designer` skill; the resulting structured markdown research plan streams back to the browser via SSE.

Admin-approval invite model. Admin = `morningwilliam@gmail.com`.

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2.x async, `aiosqlite`
- `pydantic-settings`, `aiosmtplib` (Gmail SMTP), Jinja2 templates
- `pdfplumber`, `python-docx`, `python-magic` for file ingestion
- `claude` CLI subprocess (`--output-format stream-json`, `--model claude-opus-4-7`)
- `pytest` + `pytest-asyncio` + `httpx`; `aiosmtpd` for fake SMTP
- Headless `google-chrome-stable` for web e2e screenshots
- Cloudflare Tunnel (`cloudflared`) for deploy

## Environment

- Working dir: `/home/ubuntu/method`
- This server (Tencent Cloud) is BOTH dev and production
- Repo: `github.com/morningbh/method`
- `.venv/` at repo root; `make install`, `make dev`, `make test`, `make lint`
- Runtime dirs (created lazily): `/var/method/{db,uploads,plans,logs,backups}`
- For tests a local `data/` dir is used (see `tests/conftest.py`)

## Key docs

- Design spec: [`superpowers/specs/2026-04-19-method-research-planner-design.md`](superpowers/specs/2026-04-19-method-research-planner-design.md) — the single source of truth for API, data model, UX, error handling
- Implementation plan: [`superpowers/plans/2026-04-19-method-implementation-plan.md`](superpowers/plans/2026-04-19-method-implementation-plan.md) — M1–M5 task breakdown
- Harness: [`HARNESS.md`](HARNESS.md) — 5 hard constraints + component map
- Dev log: [`DEV_LOG.md`](DEV_LOG.md) — session-level decisions and lessons

## Workflow

Follows the 10-step workflow in `/home/ubuntu/.claude/CLAUDE.md`. Sub-skills used:

- `/preflight #N` — step 0
- `/design-check` — step 2a (loops until PASS, max 10)
- `/tester #N` — step 3 (reads design + ABCs only, never implementation)
- `/test-quality-check` — step 4 (blocks on design coverage)
- `/run-tests` — all test execution (main agent must not run pytest directly)
- `/review #N` — step 8

### Autonomy protocol (agreed 2026-04-19)

Review gates (steps 2b, 5) produce Feishu docs for post-hoc review but do NOT block. The agent continues through the loop; the user reviews out-of-band.

## Test accounts

Available through the Gmail MCP (no manual inbox juggling needed):

- `morningwilliam@gmail.com` — admin, receives approval emails
- `h@xcptl.com` — ordinary test user, receives login codes + activation notices

E2E email tests search these inboxes via `mcp__claude_ai_Gmail__search_threads`.

## Secrets

`.env` at repo root holds real `SMTP_PASSWORD` (Gmail app password) and `SESSION_SECRET`.

- `.env` is gitignored; NEVER commit it
- NEVER log secret values — not in prints, not in error messages, not in test fixtures
- Review step greps the tree for known secret substrings to make sure

## Milestones

| M | Status | Summary |
|---|---|---|
| M1 | done | Scaffolding, `/api/health`, pyproject, Makefile |
| M2 | in progress | Auth loop (models ✓, mailer ✓, auth_flow + routes next) |
| M3 | pending | `claude` subprocess + SSE research endpoint |
| M4 | pending | History list + detail UI |
| M5 | pending | Cloudflare Tunnel deploy |

GitHub issues: [#1 M2](https://github.com/morningbh/method/issues/1), [#2 M3](https://github.com/morningbh/method/issues/2), [#3 M4](https://github.com/morningbh/method/issues/3), [#4 M5](https://github.com/morningbh/method/issues/4).
