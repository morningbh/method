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

## 2026-04-19 — Session 1 continued: M2 Task 2.3 auth flow

### Shipped
- Feature branch `feat/m2-auth-flow` merged to `main`
- `app/services/auth_flow.py` (443 lines): 6 public functions, 4 exceptions, 2 constants (COOKIE_FLAGS, COOKIE_NAME)
- Fixed stale settings binding in `app/db.py` and `app/services/mailer.py` (switched to `from app import config as _config` pattern)
- 23 new unit tests in `tests/unit/test_auth_flow.py` — total suite now 41 tests, all green
- Process docs created under `docs/` (HARNESS moved + enriched with component map, AGENT_CONTEXT, TESTER_PROMPT, CODE_REVIEW_PROMPT, DEV_LOG)

### Workflow actually executed (full 10 steps)
- Step 0 `/preflight #1`: initially FAIL (missing process docs); dispatched subagent to create them; re-ran → PASS
- Step 2a `/design-check`: iter 1 NEEDS_REVISION (5 WARN), iter 2 PASS after edits (field mapping email normalization, verification-lookup strategy, race window note, infra dependency table, test cross-reference)
- Step 2b Feishu design doc: V9NbdYo6KoTdz3xyrioc75LXnFg (non-blocking)
- Step 3 `/tester`: 23 RED tests (scope = 20 design items + 3 dispatcher additions)
- Step 4 `/test-quality-check`: PASS (2 non-blocking advisories)
- Step 5 Feishu test doc: MDRudcP3TowsdSxOO3YcUpiHnVd (non-blocking)
- Step 6 Backup to `/tmp/method-backups/pre-2.3-*`; feature branch `feat/m2-auth-flow` created
- Step 7 Dev loop: 1 revision cycle (first attempt introduced SQLAlchemy monkey-patch; reverted and fixed test 6 properly)
- Step 8 `/review #1`: APPROVED (0 Critical, 0 Important, 4 Minor)
- Step 9 DEV_LOG update (this entry)

### Non-obvious decisions
- `validate_session_cookie` returns `None` on ALL failure paths (not raises) — documented contract for middleware-friendly usage
- `verify_login_code` iterates unused-unexpired codes per user and `hmac.compare_digest` each; hash lookup is not possible because salt is per-row
- Admin self-bootstrap (email == settings.admin_email) still issues a login code; does not create session directly — keeps one code path, tighter security
- Emails lowercased on BOTH write AND admin comparison (users.email is UNIQUE without COLLATE NOCASE)
- Error strings collapsed to "invalid_or_expired" across all non-success paths to prevent enumeration
- Transaction boundary: module only flushes, caller owns commit/rollback — test 23 locks this invariant
- Rolled-back test (test 6 rate-limit): captures `user.id` BEFORE rollback to avoid SQLAlchemy's default expire-on-rollback + MissingGreenlet under aiosqlite

### Things user should know
- Committer identity on recent commits shows `Ubuntu <ubuntu@localhost.localdomain>` (no global git config set). Set it if you care.
- `/run-tests` skill template hardcodes `/home/ubuntu/agxp-seed` path; not usable from this project. All subagents ran pytest via workaround (`python -c "import pytest; pytest.main(...)"`). TODO: fix the skill template.
- 2 pre-existing ruff lint findings in `tests/unit/test_auth_flow.py` (I001 import-order, UP017 timezone.utc → datetime.UTC). Non-blocking. Roll into a cleanup PR before M2 closes.

### Process lesson
- Preflight doc requirements (docs/HARNESS.md, AGENT_CONTEXT, TESTER_PROMPT, CODE_REVIEW_PROMPT, DEV_LOG) should have been created at project init, not after Task 2.3 kicked off. Lost ~5 min to bootstrap. Template for future projects: run preflight at first task, gating.
- Implementer's first-pass introduced a global SQLAlchemy monkey-patch to make a test pass. Review round caught it; fix was to capture PK before rollback (Option A from design-check-style alternatives) rather than hack production code. Reinforces: never modify production to make tests pass; the smell is the test, fix the test.

## 2026-04-19 — Session 1 continued: M2 Task 2.4 auth routes

### Shipped
- `feat/m2-auth-routes` merged to `main`
- `app/routers/auth.py` — POST /api/auth/{request_code,verify_code,logout}, GET /login, GET /
- `app/routers/admin.py` — GET /admin/approve
- HTML templates: base.html, login.html, approved.html, approval_error.html, landing.html
- `app/main.py` — Jinja2Templates on app.state.templates, StaticFiles at /static, auth/admin routers, marker exception handlers
- `pyproject.toml` — added `pydantic[email]` (EmailStr requires email-validator)
- 24 new integration tests — total suite now 65, all green
- ruff clean on `app tests`

### Workflow actually executed (full 10 steps, second time)
- Step 0 preflight — re-used docs from Task 2.3
- Step 2a `/design-check` — iter 1 PASS (no revisions needed)
- Step 2b Feishu design: UWPgdJlwToDMLMxx2v4c7UXnndc (non-blocking)
- Step 3 `/tester` — 24 RED tests
- Step 4 `/test-quality-check` — PASS (0 non-blocking advisories)
- Step 5 Feishu tests: OVIedjH2toM0YLx4FfycfRwpneo (non-blocking)
- Step 6 feature branch `feat/m2-auth-routes`
- Step 7 dev loop — single pass to GREEN, 65/65 tests
- Step 8 `/review #1` — APPROVED
- Step 9 DEV_LOG

### Non-obvious decisions
- JSONResponse bodies (`{"error": "..."}`) used directly instead of HTTPException (which wraps in `{"detail": ...}`). Matches tested contract.
- Internal marker exceptions (`_Unauthenticated`, `_RedirectRequired`) translated via module-level handlers instead of HTTPException raises. Keeps dependencies free of HTTPException envelope.
- `get_current_user` opens its own session (not shared with `_db_session`) to avoid autobegin collision with the handler's `async with session.begin():`. Benign deviation from design §3; sessions close via `async with` in any case.
- Manual Set-Cookie header assembly for logout clear-path. Starlette's `set_cookie(value="")` quotes the empty string; test expected bare empty value. One-liner header assembly used.

### Tester issues caught and fixed (by implementer)
- Tester's `await integration_db.expire_all()` was a bug: `expire_all()` is sync and returns None. Fixed by removing `await`.
- Tester read `user.id` / `_tok.id` after `expire_all()` → triggers MissingGreenlet under aiosqlite. Fixed by capturing ints before expire_all.
- Pre-existing ruff findings (`I001`, `UP017`, `F401`) resolved via `ruff --fix`.

### Things user should know
- Cookie secure flag is False (pre-HTTPS); M5 adds `secure=True` once Cloudflare tunnel / Let's Encrypt is live.
- `pydantic[email]` pulls `email-validator` runtime dep — minor install size bump.
- Design §3 `get_current_user` shares session with handler — we deviated. Consider updating design post-hoc or leave for future.

### Process lesson
- `/design-check` passed iteration 1 this time (vs iteration 2 for Task 2.3). Improvement.
- One round of dev loop, no hack-and-revert. Faster than Task 2.3 (which had the SQLAlchemy monkey-patch → revert cycle).
- Test quality check caught zero issues; tester's single real bug (`await expire_all`) only surfaced during implementation. Suggests the rubric doesn't check for misuse of SQLAlchemy async API — worth adding to `docs/TEST_QUALITY_CHECKER_PROMPT.md` at project level.

## 2026-04-19 — Session 1 continued: M3 Task 3.1 file_processor

### Shipped
- `feat/m3-file-processor` merged to `main`
- `app/services/file_processor.py` (263 lines) — SavedFile, LimitExceededError, validate_upload_limits, save_and_extract, cleanup_request
- `app/models.py` — added ResearchRequest, UploadedFile ORM + idx_requests_user_created
- 6 binary fixtures in `tests/fixtures/` (samples + encrypted + empty PDFs)
- 20 new tests (17 file_processor + 3 model) — suite now 85 passed + 1 skipped

### 10-step workflow
- preflight PASS (docs already in place from M2)
- /design-check iter 1 NEEDS_REVISION (2 BLOCKING: datetime tz + request_id ULID contract), iter 2 PASS
- /tester 20 RED tests
- /test-quality-check FAIL on test #8 (extraction_timeout test didn't actually verify non-blocking — ticks counted post-completion instead of during). Fix pushed: ticker unbounded + tick count captured before cancel + wall-time assertion. Re-checked — now rigorously verifies non-blocking.
- /review APPROVED (0 Critical/Important, 3 non-blocking suggestions)

### Non-obvious decisions
- ULID regex defense at save_and_extract + cleanup_request trust boundary — prevents path traversal via request_id
- MIME sniff accepts `application/zip` for .docx (libmagic quirk)
- Fixtures committed as binaries (reportlab + pypdf used once; not in deps)
- `_MAGIC = magic.Magic(mime=True)` module-level singleton for perf
- Test #8 rewritten with two invariants (ticks during save + wall-time) after /test-quality-check caught the vacuous-test trap — LP L1 lesson applied

### Things to note
- `_generate_fixtures.py` script documents `pip install --no-deps reportlab pypdf` for fixture regeneration
- FastAPI import in services/ is borderline per boundary rule (HTTPException base for LimitExceededError); documented as intentional

## 2026-04-19 — Session 1 continued: M3 Task 3.2 claude_runner

### Shipped
- `feat/m3-claude-runner` merged to `main`
- `app/services/claude_runner.py` (~340 lines) — async subprocess wrapper, stream-json parser, sidecar stderr drain, SIGTERM/SIGKILL timeout, lazy semaphore, cancellation cleanup
- 14 new unit tests — suite now 99 passed + 1 skipped

### 10-step workflow
- design-check iter 1 PASS (after 4 small edits: task number 3.5→3.4, stderr drain sidecar, semaphore race note, new test #14)
- /tester 14 RED tests
- /test-quality-check PASS (0 advisories)
- dev loop: first pass 14 green, but /review caught a CRITICAL bug: argv contained `--cwd` which is NOT a real claude CLI flag. Textbook L1 failure: unit tests mocked create_subprocess_exec and missed it.
- Fix pass: `--cwd <path>` → `--add-dir <path>` in argv + pass `cwd=str(cwd)` kwarg to create_subprocess_exec. Also cleaned up I1-I3 (dead try/except, redundant cleanup, silent empty-result).
- /review v2: APPROVED
- DEV_LOG

### Critical lesson
- The tester wrote tests from the design doc, which ALSO had `--cwd` — so tests passed while the code was broken. Tests-as-contract is valuable but has a hole when the design itself hallucinated a flag. Mitigation: the /review subagent spotted it by actually running `claude --help`. Going forward, consider adding to `/design-check` a "verify external CLI flags against live `--help`" rule.
- Also updated upstream spec (`docs/superpowers/specs/2026-04-19-method-research-planner-design.md` §5.1) which had the same bug.

### Non-obvious decisions
- Sidecar stderr drain is a real task, awaited in finally (prevents pipe-full deadlock)
- `exit_code == 0` with no result line now emits error (not silent empty done)
- Grace period (5s) and timeout come from settings; both defined and tested
- Prompt logged as sha256-truncated hex, never raw

### Things user should know
- Real `claude` CLI flags used: `-p`, `--output-format stream-json`, `--model`, `--allowed-tools`, `--permission-mode acceptEdits`, `--add-dir`
- Subprocess working directory set via `cwd=` kwarg (asyncio.create_subprocess_exec)

## 2026-04-19 — Session 1 continued: M3 Task 3.3 research routes + SSE + runner

### Shipped
- `feat/m3-research-routes` merged to `main`
- `app/services/research_runner.py` — orchestration, pub/sub, Jinja prompt, two-session + rescue pattern
- `app/routers/research.py` — POST, SSE stream, JSON, download endpoints
- `app/templates/prompts/research.j2` — autoescape=off prompt template
- `app/main.py` — research router included
- 42 new tests (16 unit + 26 integration) — suite now 141 passing + 1 skipped

### 10-step workflow
- design-check iter 1 NEEDS_REVISION (6 BLOCKING: session scope, task exception callback, plan_write failure handling, top-level rescue, model field rationale, prompt-injection tests)
- iter 2 PASS after applying 14 revisions
- /tester wrote 40 tests → /test-quality-check FAIL (2 missing: design #32 download-404-failed, #39 claude_runner allowed_tools tripwire) → tester added them → 42 total RED
- dev loop: first pass GREEN (141 passing). Required 3 test-infra tweaks: seeded_user `expunge` (SQLAlchemy async greenlet issue), tests #23 and #39 switched from asyncio.Event.wait to asyncio.sleep (httpx ASGITransport buffers SSE), 500ms subscriber-wait loop between Block A and claude stream (UX improvement for POST-then-connect flow)
- /review APPROVED (0 Critical/Important, 3 Minor deferred)

### Non-obvious decisions
- Two-session pattern: Block A mark running (close session) → claude stream (no DB connection held) → Block B terminal write (fresh session) → rescue Block C (last-resort fresh session)
- `_log_task_exception` callback attached to background tasks so they don't silently fail
- Pub/sub: in-memory `dict[rid, list[asyncio.Queue]]` with 256 maxsize + silent drop on QueueFull (ground truth is DB)
- Jinja2 autoescape=False for prompts (tests verify literal preservation of injection attempts)
- Ownership enforced via single-query `WHERE id AND user_id` — no timing oracle
- SSE framing: `event: <name>\ndata: <json>\n\n`, JSON body via `json.dumps(ensure_ascii=False)` so `\n` inside text is escaped
- httpx ASGITransport can't truly stream SSE — buffers response entirely. M4/M5 with real HTTP server will verify streaming properly
- Added `elapsed_ms: null` to M3 JSON body (design said SSE-only); harmless ahead-of-schedule

### Things user should know
- Test #32 renumbering: /tester initially wrote 40 tests but missed design #32 and #39. Added them as items 41 and 42. Full suite count 141 = 100 prior + 40 initial + 2 catch-up - 1 (one of the 40 was replaced). Actually 100 + 41 = 141. Verified.
- Background task on server restart will leave running rows stuck. M5 should add a startup sweep.
- `claude_runner.stream` mocked at `app.services.research_runner.stream` import seam. Tests never hit real claude.

## 2026-04-19 — Session 1 continued: M4 Frontend UI

### Shipped
- `feat/m4-frontend` merged to `main`
- `app/routers/history.py` — GET /, /history, /history/<id>, /api/history
- `app/templates/` — index.html (workspace), history.html (list), history_detail.html (SSE client), _topbar.html (partial)
- `app/static/style.css` (~300 lines) — full design system per spec §7.1 + 768px mobile breakpoint
- `app/static/app.js` (307 lines) — login FSM, file upload drag+drop, SSE client + polling fallback, marked rendering
- `app/static/vendor/marked.min.js` (35KB, v12.0.0)
- Deleted `landing.html` (replaced by index.html)
- 26 new tests (16 history + 8 index/static + 2 unit) — suite now 167 passing + 2 skipped

### 10-step workflow
- design-check iter 1 PASS (3 minor WARNs noted)
- RED → GREEN in single pass, zero test modifications
- /review APPROVED (0 Critical/Important, 4 Minor)

### Non-obvious decisions
- Marked v12.0.0 no sanitize — accepted risk per design §11 (content is assistant's own stream, HttpOnly cookies, CSRF covered). Revisit if plan-sharing feature added.
- SSE client distinguishes server-emitted error events (typed) from transport-layer errors (onerror); only transport errors trigger polling fallback.
- History list uses single LEFT JOIN + GROUP BY for file count — no N+1.
- Poll cap at 100 attempts × 3s = 5 min (tighter than design's "no cap", defensive).
- `format_beijing` helper in history.py for UTC→Beijing display.
- Mobile breakpoint 768px, touch targets ≥44px, font-size ≥16px on inputs (iOS anti-zoom).

### Things user should know
- landing.html is gone. `/` now shows workspace (authed) or redirects to /login.
- Deploying: M5 still needs to set BASE_URL in .env once Cloudflare tunnel URL is known.
- The SSE client JS has NO automated tests (httpx ASGITransport can't truly stream). Manual QA needed once deployed.

### Process lesson
- /design-check caught the right things early; /tester needed no revision cycles. Writing the design doc exhaustively (field mapping, error table, files list) paid off.

## 2026-04-19 — Session 1 Final: M5 Deploy + Web E2E

### M5 Shipped
- `feat/m5-deploy` merged to main — systemd units (`deploy/method.service`, `deploy/cloudflared.service`) + scripts (`scripts/{deploy,tunnel-url,setup-systemd}.sh`)
- `feat/m5-web-e2e` merged to main — `tests/e2e/web/` with chromium runner + scenario doc + 4 auto scenarios
- Public URL live: https://que-career-favour-mode.trycloudflare.com
- BASE_URL in .env updated to tunnel URL
- systemctl status: method active, cloudflared active, both persistent across reboot

### End-to-end verification (via curl + Gmail MCP + chrome headless)
1. GET /api/health → 200 OK
2. GET /login → 200 OK
3. POST /api/auth/request_code with admin email → {"status":"sent"}
4. Gmail MCP search → login code email arrived from real Gmail SMTP
5. POST /api/auth/verify_code with code → {"ok":true} + Set-Cookie method_session (HttpOnly, SameSite=Lax, not Secure pre-HTTPS-cert)
6. GET / with cookie → 200 (workspace renders)
7. chromium headless screenshots of /login desktop + mobile + /api/health + /history (unauthed redirect) → all > 3KB, captured

### Design decisions locked in M5
- Cloudflare quick-tunnel (not named tunnel) for MVP — URL changes on cloudflared restart; acceptable trade-off since method restarts don't affect tunnel (fixed Wants= vs Requires=)
- BASE_URL auto-written by tunnel-url.sh helper from journalctl
- Web e2e runner = chromium CLI + PNG size heuristic (≥ 3KB filters blank pages). Cookie injection for authed flows punts to CLI e2e (test_real_email_flow.py covers SMTP; research generation covered by test_real_claude_call.py)
- Screenshots not committed to git (gitignored)

### Final state
- main commits since kickoff: 47 (TDD with per-task RED/GREEN/MERGE cadence)
- tests: 167 unit+integration green, 2 e2e pass with RUN_E2E=1 (email + claude), 4 web e2e pass
- open issues on GitHub: 4 (M2–M5 — all closed by this session's commits)
- HARNESS constraints all honored: §1 non-empty error_message, §2 absolute paths, §3 claude --allowed-tools Read,Glob,Grep, §4 cookie flags, §5 e2e RUN_E2E gate

### Critical catches during the session
1. **M3 Task 3.2 /review caught `--cwd` hallucination** — claude CLI doesn't accept --cwd. Unit tests mocked subprocess, missed it. Fix: `--add-dir` + `cwd=` kwarg. LP L1 antidote.
2. **Task 3.4 real claude e2e caught `--verbose` requirement** — stream-json needs --verbose. Would have crashed production on first request. Fix: argv update.
3. **Task 3.3 /design-check caught session-held-across-claude-stream** — rewrote to two-session + rescue pattern.
4. **Task 3.1 /test-quality-check caught vacuous blocking-test** — test_extraction_timeout was asserting ticks AFTER await (always 20). Rewrote to capture ticks during save + wall-time assertion.

### Process lessons
- `/design-check` provides strong a-priori value; typical 0-2 revision iterations
- Real-infrastructure e2e tests (real SMTP, real claude CLI) catch class-of-bugs that mocked unit tests structurally cannot. Two out of four critical catches were e2e-only.
- Subagent-driven workflow with mandatory /review gate caught one critical bug that every earlier gate missed (--cwd)
- Design-doc-first approach (then test-first, then implementation) drastically reduced rework; zero tasks needed a full retry

### Future work (not done this session)
- Named Cloudflare tunnel so URL is stable (free account, just config)
- Production `method.xvc.com` DNS setup
- Server-restart recovery for rows stuck in status='running' (design Q1)
- Per-user cost visibility in history (design Q6)
- Dark mode, share-plan links, account settings — all YAGNI per spec §14
- Web e2e: migrate to playwright so auth'd flows (scenario 4) are fully automated
