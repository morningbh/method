# Code Review — Issue #4 Feature B (选文评论 + AI 回复)

## Scope

- **Branch**: `feat/issue-4-comments` (HEAD: `3f51a5b`)
- **Base**: `main`
- **Design doc**: `/home/ubuntu/method-dev/docs/design/issue-4-feature-b-comments.md`
  (canonical Feishu link in doc; v2.1, 2026-04-20)
- **Files reviewed (vs main)**:
  - `.env.example`
  - `app/config.py`
  - `app/models.py`
  - `app/routers/history.py`
  - `app/routers/research.py`
  - `app/services/comment_runner.py` (NEW)
  - `app/static/app.js`
  - `app/static/style.css`
  - `app/templates/history_detail.html`
  - `app/templates/prompts/comment_reply.j2` (NEW)
  - `tests/integration/test_comment_endpoints.py` (NEW)
  - `tests/unit/test_comment_runner.py` (NEW)
- **Skipped**: `docs/*` (notes, not code).

---

## Per-file findings

### `.env.example` — PASS

Lines 27-30 add `CLAUDE_COMMENT_MODEL` (empty default = fall back to `CLAUDE_MODEL`) and `CLAUDE_COMMENT_TIMEOUT_SEC=60`. Matches design §7 row "`.env.example`" and design §5 (60 s default, escape hatch for B-Q8). Default values consistent with `app/config.py` defaults. No secrets.

### `app/config.py` — PASS

Lines 38-42 add `comment_model: str = ""` and `claude_comment_timeout_sec: int = 60`. Field names exactly match what `comment_runner.py` reads (`_config.settings.comment_model` / `claude_comment_timeout_sec`). Empty-string default for `comment_model` is the design-mandated B-Q8 fallback signal — `comment_runner.py` line 449 implements it (`settings.comment_model or settings.claude_model`). pydantic-settings `extra="ignore"` will accept the new env keys without breaking older `.env` files.

### `app/models.py` — PASS

- New `Comment` ORM (lines 137-181) covers all 14 columns from design §2 (id, request_id, user_id, parent_id, author, anchor_text, anchor_before, anchor_after, body, ai_status, ai_error, cost_usd, created_at, deleted_at).
- `__table_args__` (lines 144-154) enforces `author IN ('user','ai')` and `ai_status IN ('pending','streaming','done','failed') OR NULL` per design §2 CHECK constraints.
- Two indices match design §2 §"索引"：
  - `idx_comments_request_created` on `(request_id, created_at)` — line 194
  - `idx_comments_parent` on `(parent_id)` — line 195
- `Comment` exported in `__all__` and re-exposed via `app.models.Comment` (router import succeeds).
- Self-referential FK (`parent_id` → `comments.id`) is correct for AI replies pointing at user comments.

WARN (non-blocking): SQLite `init_db()` uses `CREATE TABLE IF NOT EXISTS`, so existing prod DBs will pick up the new table on next boot; the FK on `parent_id` self-reference is allowed by SQLite but not enforced by default (no `PRAGMA foreign_keys=ON`). Not a regression — same posture as existing tables.

### `app/routers/history.py` — PASS

Lines 219-230 read `plan_path` (already absolute, set by research_runner via `settings.plan_dir` which is absolute) and pass `plan_markdown` into the template, so the template can seed `data-markdown-source`. OSError is logged (not silently swallowed). `error_message` for failed plans is also threaded into context (line 239). No new logic that touches DB writes.

### `app/routers/research.py` — PASS with WARN

Boundary discipline OK: business logic delegated to `comment_runner` (create / cascade-soft-delete / SSE pubsub).

PASS items:
- POST endpoint (lines 486-555):
  - Owner check via `_load_owned` returns 404 for both missing and cross-user (no enumeration oracle) — design §4.
  - Status gate (lines 499-503) returns 409 `request_not_finalized` for pending/running, allows done + failed (B-Q7=A).
  - Manual length validation as 400 with `{"error": ...}` (lines 507-521), avoiding 422 default — matches design §4 contract that other tests assert on.
  - `BodyEmptyError` mapped to 400 `body_empty` (line 530-534).
  - Generic exception → 500 `{"error": "internal"}` (line 535-541) — matches design §4 step 1 ("DB 失败 500 约定").
  - Background AI task spawned via `comment_runner.run_ai_reply` (line 547) — uses the `_TASKS`-managed dispatcher.
- GET endpoint (lines 558-617): single SELECT then Python nests AI replies under user parents (avoid N+1 per design §2). Hard cap of 200 enforced (line 594-597, oldest-first slice keeps newest 200). `X-Comments-Truncated: true` header on truncation. Response shape matches design §4 example exactly (user_id and deleted_at excluded; ai_status/ai_error/cost_usd popped from the user record because it's NULL there but kept under `ai_reply`).
- DELETE endpoint (lines 620-672): owner scope (404 cross-user, 403 ai_reply_not_deletable, 204 success). Race between lookup and cascade re-checked at touched-count (line 661).
- SSE endpoint (lines 675-746): `comment_id` is a query param (not in path), declared as required function arg per FastAPI conventions; matches both test usage (`?comment_id=...`) and JS client. Live-replay split for terminal states avoids holding subscribers when the row is already done/failed.

WARN (Bopundary, non-blocking):
- Line 588: chained boolean `(r.created_at and prior.created_at and r.created_at >= prior.created_at)` is correct but long; not a defect.
- Line 131: `from app import config as _config` performed inside the POST research handler — pre-existing pattern in the file, not a new violation.

### `app/services/comment_runner.py` — PASS with WARN

PASS items:
- HARNESS §3: argv at lines 451-460 contains `"--allowed-tools", "Read,Glob,Grep"`. No `Write/Bash/Edit`. Identical posture to `app/services/claude_runner.py` (line 75).
- HARNESS §1 parity: every failure path writes a non-empty `ai_error` via `_mark_ai_failed` (line 629-664). Cases covered: ENOENT (472), PermissionError (476), generic spawn (480), stream error (549), timeout (557, 571), exit≠0 (586), empty body (594, hard-codes the design §5 string `"claude 未返回内容"`). Empty `error_text` defensively falls back to `"unknown failure"` (line 631-632).
- Absolute path discipline: `cwd = (Path(_config.settings.upload_dir) / request_id).resolve()` (line 441) — `upload_dir` is enforced absolute by `config.py` consumers; `.resolve()` makes the final value absolute regardless. Matches HARNESS §2 in spirit.
- Single transaction for create_user_comment (lines 248-282) — both rows live or die together; the design §4 "two rows must succeed or fail together" requirement holds.
- Pub/sub (lines 62-106) mirrors research_runner; queue overflow drops with WARN log (no silent swallow).
- Unicode normalization (lines 152-176): zero-width + bidi controls stripped before DB persistence and before prompt injection. `BodyEmptyError` raised on empty-after-normalize.
- Model fallback (line 449): `settings.comment_model or settings.claude_model`. Both branches covered by tests (test #16a fallback, #16b override).
- Stream-json parsing accepts both `content_block_delta` (Anthropic-style) and `assistant` (project-local) frames. Cost from `result` event.
- `_TASKS` set + `_log_task_exception` done-callback (lines 692-706) prevent GC of background tasks and surface uncaught exceptions.

WARN (non-blocking):
- Line 184: `_utcnow` returns `datetime.now(UTC).replace(tzinfo=None)` — naive UTC, consistent with project convention.
- Line 105: `_close_channel` publishes a `("__close__",)` tuple but never an explicit `None`; the SSE consumer in `routers/research.py` line 735 handles `__close__` correctly. Mostly a documentation-friendly choice; `None` sentinel would also work but isn't needed.
- Line 547 (research router) → comment_runner.run_ai_reply: this is `await`-ed but the underlying function only schedules a task and returns immediately — the `await` is harmless but slightly misleading. No fix required.

### `app/static/app.js` — PASS

- `initComments()` (line 465) is a self-contained init function added to `init()` (line 724) per design §6.
- HTML escaping for all user-controlled fields (anchor_text, body, ai_error) via `escapeHTML` (line 484-488). Defense against XSS — combined with backend's plain-text policy (no markdown render) this matches design §9 risk row "XSS".
- Selection capture (lines 576-600) computes anchor_before/anchor_after from `data-markdown-source` (preferred) or DOM textContent fallback. Ignores selections outside registered sources.
- POST/DELETE/SSE wiring matches the new endpoints exactly. Uses `?comment_id=` query for SSE (matches the route's signature).
- SSE error handling (line 557): single-shot `es.close()`, no reconnect loop — fine for the design's 60 s budget.

WARN (UX, non-blocking):
- Line 532-535: when subscribing to an existing AI placeholder, the body element is reset to empty regardless of current content. If the AI is already streaming (race) and partial body is rendered, this discards it. Minor; would be visible only on refresh during streaming.
- Line 644: `alert("评论不能为空")` — pre-empts the backend's `body_empty` 400. OK.

### `app/static/style.css` — PASS

Lines 820-987 add the Feature B section: `.selection-tool`, `.comments`, `.comment-card`, `.comment-card .excerpt`, `.comment-card .ai-reply[data-status="..."]`, `.comment-compose` + `::backdrop`. Matches design §6 requirements. Uses existing tokens; consistent with the prior style.

### `app/templates/history_detail.html` — PASS

- Line 23: `<article id="markdown-body" class="markdown-body" data-markdown-source="{{ plan_markdown|default('') }}">` — design §6 + integration test #13 expectation.
- Line 21: `<div class="error-banner" data-markdown-source="{{ error_message }}">` for failed plans (B-Q7=A) — design §3 + integration test #13b expectation.
- Comments region (lines 25-46) gated on `status in ("done", "failed")` per B-Q7. Compose dialog uses `<dialog>` (modern, accessible).
- `app.js` and `marked.min.js` referenced via `<script src=...>` (lines 49-50).

WARN (security, non-blocking):
- Line 23 `data-markdown-source="{{ plan_markdown|default('') }}"`: Jinja autoescape protects against `"` and `<` breaking the attribute, but very long plan markdowns will inflate the HTML payload. Acceptable for MVP-1 — design §3 explicitly accepts this.
- Line 21 `data-markdown-source="{{ error_message }}"`: same caveat. Autoescaped, no XSS.

### `app/templates/prompts/comment_reply.j2` — PASS

- `{% if error_message %}` switches to the "自我诊断员" failed branch; else loads the "评论员" done branch — matches design §5 prompt templates verbatim.
- Variables referenced (`question`, `uploaded_files`, `plan_markdown`, `error_message`, `anchor_text`, `user_body`) match the call site `_render_prompt(...)` keyword args.
- `autoescape=False` on the prompt environment (comment_runner.py line 116) — appropriate for prompt text (Jinja autoescape would mangle `<` etc).

### `tests/unit/test_comment_runner.py` — PASS

- 19+ tests covering: ORM columns (#1), create user+AI placeholder (#2), cascade soft-delete (#3), AI pipeline done (#4), nonzero-exit failure (#5), timeout (#6), ENOENT (#7), empty body (#8), SSE channel scope (#9), prompt context completeness done branch (#10), failed branch (#10b), prompt-injection literal preservation (#11), Unicode normalization stripping (#12) and empty-after-strip (#12 second test), `--allowed-tools Read,Glob,Grep` argv tripwire (#13 — HARNESS §3), absolute cwd (#14 — HARNESS §2), template file existence (#15), settings fields (#16), model fallback branch (#16a — LP #21 lesson), explicit model override branch (#16b — LP #21 lesson), `.env.example` keys (#17), pubsub unsubscribe (#18), `_TASKS` set type (#19).
- Tests don't read implementation files (per HARNESS sub-agent rules).

### `tests/integration/test_comment_endpoints.py` — PASS

- Tests #1-#15 cover: POST done (#1), POST failed (#2 — B-Q7), POST pending/running 409 (#3a/#3b), POST cross-user 404 (#4), anchor_text 400 (#5), body 400 (#5b), GET nested (#6), GET all design fields (#6b — v2.1 BLOCKING fix), GET soft-deleted filtered (#7), GET 200-cap + truncated header (#7b), DELETE owner cascades (#8), DELETE cross-user 404 (#9), DELETE AI 403 (#10), DELETE/POST/GET unauth 401 (#11), SSE delta+done (#12), template renders data-markdown-source (#13a/#13b), CSS classes present (#14), `initComments` symbol in app.js (#15).
- Mocking strictly at `comment_runner._run_ai_reply` boundary; no real claude subprocess invoked.

---

## Design completeness (items 13–15)

### #13 — Output files (design §7) all exist

| Design §7 row | Present | Path |
|---|---|---|
| `app/models.py` Comment ORM + 2 Index | YES | models.py L137-195 |
| `app/services/comment_runner.py` (NEW) | YES | full file |
| `app/routers/research.py` 4 endpoints | YES | research.py L486-746 |
| `app/templates/prompts/comment_reply.j2` (NEW) | YES | full file |
| `app/templates/history_detail.html` modifications | YES | L21-46 |
| `app/static/app.js` initComments | YES | L465-716 |
| `app/static/style.css` `.comments` etc. | YES | L820-987 |
| `app/config.py` comment_model + timeout | YES | L38-42 |
| `.env.example` two new env keys | YES | L27-30 |
| `tests/unit/test_comment_runner.py` (NEW) | YES | full file |
| `tests/integration/test_comment_endpoints.py` (NEW) | YES | full file |

All 11 rows present.

### #14 — Public symbols (design §4 + §5)

| Design symbol | Defined |
|---|---|
| `POST /api/research/{rid}/comments` | research.py L486 |
| `GET /api/research/{rid}/comments` | research.py L558 |
| `DELETE /api/research/{rid}/comments/{cid}` | research.py L620 |
| `GET /api/research/{rid}/comments/stream` | research.py L675 |
| `comment_runner.create_user_comment` | comment_runner.py L224 |
| `comment_runner.cascade_soft_delete` | comment_runner.py L290 |
| `comment_runner._run_ai_reply` | comment_runner.py L348 |
| `comment_runner.run_ai_reply` (dispatcher) | comment_runner.py L701 |
| `comment_runner.subscribe / unsubscribe / _publish` | comment_runner.py L72 / L79 / L92 |
| `comment_runner._TASKS` | comment_runner.py L67 |
| `comment_runner.BodyEmptyError` | comment_runner.py L164 |
| `Comment` ORM | models.py L137 |
| `_render_prompt` (uses comment_reply.j2) | comment_runner.py L121 |

All present.

### #15 — Dangling refs

Reverse-grepped all symbol usages in `app/services/comment_runner.py` and `app/routers/research.py`. Each call site resolves to a defined symbol in the same module or in `app.models` / `app.db` / `app.routers.auth` / `app.config`. No dangling import or call. PASS.

---

## Method hard-constraints (HARNESS 1–5 + LP L1–L6)

| Constraint | Result | Evidence |
|---|---|---|
| HARNESS §1 — `research_requests` failed must set `error_message` | PASS (parity for `comments.ai_error`) | comment_runner.py `_mark_ai_failed` line 629-664 always writes non-empty `ai_error`. The two new tests (#5, #6, #7, #8 in unit) explicitly assert `ai_row.ai_error` is truthy / specific. No new code path moves a `research_requests` row to `failed`. |
| HARNESS §2 — absolute paths in DB | PASS | New code does not write to `research_requests.plan_path`, `uploaded_files.stored_path`, `uploaded_files.extracted_path`. `comment_runner._run_ai_reply` cwd uses `Path(...).resolve()` (line 441). Comments don't store paths in DB. |
| HARNESS §3 — claude allowlist | PASS | comment_runner.py line 457: `"--allowed-tools", "Read,Glob,Grep"`. No `Write`/`Bash`/`Edit`. Test #13 (unit) is a tripwire that asserts the exact value AND that `Write/Edit/Bash` are not in it. |
| HARNESS §4 — cookie flags | PASS (no change) | No new `set_cookie` call in this PR. Existing `auth_flow.COOKIE_FLAGS` (referenced from auth.py L207) sets HttpOnly + SameSite=Lax. |
| HARNESS §5 — e2e gating | PASS (no new e2e tests) | No new tests under `tests/e2e/`. Existing files all have `RUN_E2E=1` skipif. |
| LP L1 — feature-flag branch tested unmocked | PASS | The `comment_model` env override (B-Q8 escape hatch) has BOTH branches exercised by real-call tests #16a (fallback) and #16b (override) with subprocess argv assertion — not just attribute access. |
| LP L2 — `# pragma: no cover` discipline | PASS | No new pragmas added. |
| LP L3 — lazy import verification | N/A | No new lazy imports in this PR. (Existing inline `from app import config as _config` in research.py L131 is pre-existing, not a "lazy" import in the L3 sense — the module is always installed.) |
| LP L4 — refactor reverse-scan | N/A | No symbol migrations. |
| LP L5 — config-page coverage audit | N/A | No new UI enumeration page. |
| LP L6 — user-visible incompleteness | N/A | No related symptom. |

---

## Summary

- **PASS**: 12 files
- **WARN**: 0 files (a few non-blocking notes inside PASS sections; nothing requiring change)
- **FAIL**: 0 files

No FAIL items. No HARNESS or LP violations.

---

## Recommended next actions

1. (Optional, post-merge) Document the SSE `comment_id` query-parameter convention in the design doc §4 — currently the parameter is implicit from "channel_id = `comment:{comment_id}`" but the URL form `?comment_id=...` is only visible in code/tests. Easy follow-up.
2. (Optional) Add a one-line `# why: Jinja autoescape sanitises into the attribute` comment next to the `data-markdown-source="{{ plan_markdown }}"` on history_detail.html L23 to inoculate future reviewers against the "isn't this XSS?" reflex.
3. Proceed to Step 9 (DEV_LOG entry).
