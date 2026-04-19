# Method Implementation Plan (M1–M5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`. Main agent only dispatches and reviews; subagents read spec + do all implementation.

**Spec**: `docs/superpowers/specs/2026-04-19-method-research-planner-design.md` (v1.1)
**Goal**: Ship an MVP of Method — a research-planner web app — end-to-end, with both CLI-style and web (headless-chromium-screenshot) e2e tests, deployed via Cloudflare Tunnel, usable by the time the user lands.

**Architecture** (see spec §1): FastAPI + Jinja2 + SQLite + subprocess `claude -p` + Gmail SMTP. Single-page, SSE-streamed output, email-auth with admin approval.

**Tech Stack**:
- Python 3.12, FastAPI, SQLAlchemy 2.x async, aiosqlite
- pydantic-settings, aiosmtplib
- pdfplumber, python-docx, python-magic
- Jinja2, marked.js (frontend)
- pytest + pytest-asyncio + httpx
- headless chromium (screenshot-based web e2e)
- Cloudflare Tunnel (cloudflared)

**Working tree**: `/home/ubuntu/method` (this server = Tencent cloud, also production)

**Autonomy protocol (user-authorized 2026-04-19)**:
- All review gates produce Feishu docs for post-hoc review but do NOT block
- All spec §13 open questions defaulted per spec recommendations
- Main agent never reads implementation files; dispatches everything

---

## Milestone 1: Scaffolding

**Deliverable**: Runnable FastAPI app with `/api/health`; `pytest` green; pushed to `github.com/morningbh/method`.

### Task 1.1: Project skeleton

**Files created**:
- `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, `README.md`, `LICENSE`, `HARNESS.md`, `CLAUDE.md`
- `app/__init__.py`, `app/main.py`, `app/config.py`, `app/db.py`, `app/models.py`
- `app/routers/{__init__.py, health.py}`
- `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

**Subagent brief**: Read spec §11 (directory structure) and §10 (env vars). Create the full scaffold with:
- FastAPI app with lifespan that initializes DB, exposes `/api/health` → `{"ok": true, "version": "0.0.1"}`
- Async SQLAlchemy engine with `aiosqlite`, session factory
- Pydantic-settings config module loading from `.env` in the working dir
- Smoke test that boots the app, calls `/api/health` via httpx.AsyncClient, asserts 200
- `.gitignore` must exclude `.env`, `data/`, `__pycache__/`, `.venv/`, `*.pyc`
- `Makefile` with targets: `install`, `dev`, `test`, `lint`

Verify test is GREEN before reporting done.

### Task 1.2: GitHub repo + initial commit

Create repo `morningbh/method` (public), add remote, commit, push.

---

## Milestone 2: Auth loop (email + admin approval)

**Deliverable**: User can register (email) → admin gets approval email → admin clicks link → user is active → user gets login code → verifies → session cookie set. All tested (unit + integration + e2e).

### Task 2.1: Data models + migrations

**Files**: `app/models.py`, `scripts/init_db.py`, `tests/unit/test_models.py`

Implement `User`, `LoginCode`, `Session`, `ApprovalToken` per spec §2.1. `init_db.py` creates all tables. Unit test: each table's CRUD + constraints.

### Task 2.2: Mailer service

**Files**: `app/services/mailer.py`, `app/templates/emails/{login_code.txt, admin_approval.txt, activation.txt}`, `tests/unit/test_mailer.py`

`aiosmtplib`-based async mailer with 3 templates (spec §4.2). Test with `aiosmtpd` local fake SMTP.

### Task 2.3: Auth flow logic

**Files**: `app/services/auth_flow.py`, `tests/unit/test_auth_flow.py`

Pure functions:
- `request_login_code(email) -> Literal["sent","pending","rejected"]` (handles new-user registration, admin self-registration skip, rate limit)
- `verify_login_code(email, code) -> Session | AuthError`
- `approve_user(token) -> User | ApprovalError`
- `create_session_cookie(user) / validate_session_cookie(cookie)`

All code/token hashing with sha256+salt. Unit-tested in isolation (no FastAPI).

### Task 2.4: Auth routes

**Files**: `app/routers/auth.py`, `app/routers/admin.py`, `app/templates/{login.html, approved.html}`, `tests/integration/test_auth_endpoints.py`

Endpoints per spec §3.1. Integration tests exercise full HTTP round trips with mocked mailer. Also: cross-origin check, rate limit, wrong code lockout, session cookie flags.

### Task 2.5: Real SMTP e2e

**Files**: `tests/e2e/test_real_email_flow.py`

Guarded by `RUN_E2E=1`:
1. Register `h@xcptl.com` via `POST /api/auth/request_code`
2. Poll admin Gmail (`morningwilliam@gmail.com`) via Gmail MCP for approval email (via a test helper that calls `mcp__claude_ai_Gmail__search_threads`)
3. Extract approve link, hit it via httpx
4. Poll `h@xcptl.com` inbox for login code
5. `POST /api/auth/verify_code` with code, assert 200 + cookie set

**Note**: the Gmail MCP is available to the test runner because it runs in the same Claude session that invokes these tests. Pass Gmail reading results as test fixtures dispatched from main agent.

---

## Milestone 3: Research core (claude subprocess + SSE + files)

**Deliverable**: Logged-in user submits question + files; Claude CLI streams structured markdown plan; result persisted and streamed to browser.

### Task 3.1: file_processor

**Files**: `app/services/file_processor.py`, `tests/unit/test_file_processor.py`, `tests/fixtures/{sample.pdf, sample.docx, sample.md, sample.txt}`

Functions:
- `save_upload(request_id, original_name, stream) -> (stored_path, size, mime_type)`
- `extract_text(stored_path) -> extracted_path | None` (pdfplumber / python-docx; md/txt no-op)
- `validate_limits(files) -> raise HTTPException` (20 files / 30 MB each / 100 MB total / allowed exts)
- `cleanup_request(request_id)` — invoked on failure only (spec §8: keep files on failure)

### Task 3.2: claude_runner

**Files**: `app/services/claude_runner.py`, `tests/unit/test_claude_runner.py`

Async generator `stream(prompt: str, cwd: Path) -> AsyncIterator[Event]` where `Event = ("delta", str) | ("done", str, float, int) | ("error", str)`.

Invokes `claude -p <prompt> --output-format stream-json --model claude-opus-4-7 --allowed-tools Read,Glob,Grep --permission-mode acceptEdits --cwd <cwd>`. Parses stream-json line-by-line. Timeout 600s. Cancels on caller disconnect.

Test with `monkeypatch.setattr(asyncio.create_subprocess_exec, ...)` to feed canned stdout.

### Task 3.3: Research routes + SSE

**Files**: `app/routers/research.py`, `app/templates/index.html`, `app/templates/history_detail.html`, `app/static/{style.css, app.js}`, `tests/integration/test_research_endpoints.py`

- `POST /api/research` — multipart (question + files[]); creates request row, saves files, kicks off background task, returns `{request_id}`
- `GET /api/research/<id>/stream` — SSE; bridges `claude_runner.stream()` → `data: {...}\n\n` chunks; writes final md to `plan_path`
- `GET /api/research/<id>` — JSON with final markdown
- `GET /api/research/<id>/download` — `.md` attachment

Concurrency: global `asyncio.Semaphore(3)`.

### Task 3.4: Real claude e2e

**Files**: `tests/e2e/test_real_claude_call.py`

Guarded `RUN_E2E=1`. Login → POST short research question ("test 1+1 is 2") with tiny prompt → connect SSE → collect deltas → assert `done` event received with non-empty markdown containing at least one spec-required section like "# 1." or "问题重述".

---

## Milestone 4: History UI + mobile

**Deliverable**: History list, detail page, download button, mobile-friendly per spec §7.3.

### Task 4.1: History routes + templates

**Files**: `app/routers/history.py`, `app/templates/{history.html, base.html}`, `tests/integration/test_history_endpoints.py`

Spec §3.1 + §7.2 D.

### Task 4.2: Frontend polish — mobile + SSE client

**Files**: `app/static/style.css`, `app/static/app.js`

Implement §7.3 breakpoints, touch targets, iOS SSE background fallback (EventSource → polling `/api/research/<id>` when `readyState === CLOSED`).

### Task 4.3: Index page (search box + upload)

**Files**: `app/templates/index.html`, `app/static/app.js` (continued)

Spec §7.2 B. Drag-drop (desktop) + click-select (both). File chip list. Submit → POST /api/research → redirect to detail page, start SSE.

---

## Milestone 5: Deployment

**Deliverable**: `cloudflared tunnel` exposes the app via `*.trycloudflare.com`; systemd keeps app alive; real URL written into `.env` + baked into email templates.

### Task 5.1: systemd unit + launch scripts

**Files**: `deploy/method.service`, `scripts/{start.sh, deploy.sh}`

Per spec §10.2. `systemctl --user` variant if root not available.

### Task 5.2: Cloudflared tunnel

**Files**: `deploy/cloudflared.yml`, `scripts/tunnel.sh`

Start a named quick tunnel; capture the `*.trycloudflare.com` URL; rewrite `.env`'s `BASE_URL`; restart app to pick up.

### Task 5.3: Full e2e smoke via public URL

**Files**: `tests/e2e/test_public_deploy.py`

httpx hit `$BASE_URL/api/health` → 200.

### Task 5.4: Web e2e with chromium screenshots

**Files**: `tests/e2e/web/scenarios.md`, `tests/e2e/web/test_web_flows.py`, `tests/e2e/web/screenshots/`

`scenarios.md` is natural-language: user actions + expected screenshots. Test runner uses `chromium --headless --screenshot` + `playwright` or a minimal `pyppeteer`-like driver to execute each scenario, capture PNG, diff against reference images (or just archive for manual review).

---

## Cross-cutting: Review gates

Per user-authorized protocol:
- After each milestone: dispatch `/review`-style subagent against the milestone's changes
- After tests are drafted per task: dispatch test-quality-check (light, spec-coverage only)
- Feishu docs get generated per milestone for user to review post-flight — never blocks

## DEV_LOG

Append per milestone to `docs/DEV_LOG.md`:
- What shipped
- Non-obvious decisions
- Things the user should know (e.g. "SSE timeout set to 600s; hitting that limit will mark request failed")

## Out of scope for this plan (deferred)

- `/var/method/...` production paths (using `/home/ubuntu/method/data/` for MVP)
- Proper TLS with own cert (Cloudflare handles HTTPS via tunnel)
- Multi-admin UI (YAGNI — single admin)
- All items in spec §14 YAGNI list
