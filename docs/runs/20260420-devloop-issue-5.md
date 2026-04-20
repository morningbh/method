# Dev-loop report — Issue #5 frontend UX error copy refresh

- **Date**: 2026-04-20
- **Branch**: `feat/issue-5-error-copy`
- **Starting commit**: `3ee71ebb0206e7e78b922066ae96b2e2acfbdf95`
- **Backup dir**: `/tmp/method-issue-5-backup-20260420T161853Z/`
- **Sub-agent role**: dev loop (Steps 6+7), max 10 iterations / 10 min budget

## Step 6 — backup + branch (done before any edits)

Files copied with `cp --parents` to preserve repo-relative paths:

```
app/main.py
app/routers/auth.py
app/routers/research.py
app/routers/history.py             (no edits — handler in main.py covers it)
app/services/file_processor.py
app/static/app.js
app/templates/history_detail.html
tests/integration/test_auth_endpoints.py        (BC test migration — design §9 risk #1)
tests/integration/test_research_endpoints.py    (BC test migration)
tests/unit/test_file_processor.py               (BC test migration; detail.code → detail.error)
```

Branch created from `main`:

```
git checkout -b feat/issue-5-error-copy
```

## Step 7 — dev loop

### Iteration 1 — initial implementation

Implemented every product-side change in design §5 in one shot, then ran the 5 issue-5 test files.

Edits:

1. **`app/services/error_copy.py`** — new module. Exports `ERROR_COPY: dict[str, str]` (24 codes, verbatim from design §4.1) + `message_for(code)` (returns `"操作失败，请稍后重试"` for unknown/empty code) + `error_response_body(code)` convenience helper.
2. **`app/services/file_processor.py`** — `LimitExceededError(code, message="")`: detail migrated from `{"code", "message-en"}` → `{"error", "message-zh}`; empty `message` falls back to `message_for(code)` (design §5 row 5). All 6 internal raise sites updated to omit the English message argument and use the centralised Chinese copy.
3. **`app/main.py`** — added `@app.exception_handler(StarletteHTTPException)` global handler. For str-detail (e.g. `HTTPException(404, "not_found")`) wraps into `{"error": <code>, "message": message_for(code)}`; for dict-detail (already-migrated `LimitExceededError`) bubbles through unchanged. Normalises FastAPI's default 404 (`detail="Not Found"`) to `error="not_found"`.
4. **`app/routers/auth.py`** — replaced 4× `JSONResponse(content={"error": ...})` plus the 2× exception handlers with `error_response_body(code)` calls.
5. **`app/routers/research.py`** — replaced 11× `JSONResponse(content={"error": ...})` (including the previously-hardcoded `request_busy` Chinese string) with `error_response_body(code)`. Also: whitespace-only `anchor_text` now classified as `anchor_text_invalid`; empty `body` now classified as `body_empty` (was wrongly `body_invalid` because both branches collapsed under one falsy check).
6. **`app/templates/history_detail.html`** — `{{ error_message }}` → `{{ error_message or '研究失败，原因未知，请重试' }}` (banner + `data-markdown-source` attribute).
7. **`app/static/app.js`** — added `showError(body, status, fallback)` + `showNetworkError()` helpers right after `postJson()`. Migrated all `alert("xxx：" + body.error)` patterns at lines 35/60 (login), 261 (research banner), 450/452/454/458 (history delete), 673/675/677/679/682 (comment compose), 705/709 (comment delete) per design §7.

Test result after iteration 1:

```
collected 97 items
tests/unit/test_error_copy.py             ............................. (54 PASSED)
tests/unit/test_file_processor_error_copy.py  ......... (9 PASSED)
tests/integration/test_auth_error_copy.py     ....... (7 PASSED)
tests/integration/test_research_error_copy.py .........F.....x.. (16 PASSED, 1 FAILED, 1 XFAIL)
tests/test_static_assets.py                   ......... (9 PASSED)
=== 1 failed, 95 passed, 1 xfailed in 4.75s ===
```

Single failure: `test_post_comment_body_empty_returns_error_and_message` — router returns `body_invalid` for empty body, test expects `body_empty`.

### Iteration 2 — fix body_empty vs body_invalid

Reordered the body validation in `app/routers/research.py::post_comment`: empty/whitespace body → `body_empty`, over-length body → `body_invalid`. Previously both collapsed into the same `not payload.body or len > MAX` check.

Test result after iteration 2:

```
=== 96 passed, 1 xfailed in 4.92s ===
```

All 5 issue-5 test files green. Moved on to full regression.

### Iteration 3 — fix BC test failures (design §9 risk #1)

Full regression with `pytest tests/`:

```
17 failed, 325 passed, 2 skipped, 1 xfailed in 52.90s
```

Every failure was an existing test asserting `assert resp.json() == {"error": "..."}` strict equality (`.detail["code"]` for file_processor unit tests). Design §9 risk #1 explicitly predicted this and prescribed migration to subset-style assertions.

Edits (justification: design §9 risk #1):

- `tests/integration/test_auth_endpoints.py` — 7× `resp.json() == {"error": "X"}` → `resp.json().get("error") == "X"` (sed-style bulk substitution, comments added on first hit).
- `tests/integration/test_research_endpoints.py` — 3× same migration.
- `tests/unit/test_file_processor.py` — 7× `excinfo.value.detail["code"]` → `excinfo.value.detail["error"]`.

Test result after iteration 3:

```
342 passed, 2 skipped, 1 xfailed in 51.81s
```

Clean. End of dev loop (3 iterations of 10 budget).

## Final results

### Issue-5 tests

```
tests/unit/test_error_copy.py:                  54 passed
tests/unit/test_file_processor_error_copy.py:    9 passed
tests/integration/test_auth_error_copy.py:       7 passed
tests/integration/test_research_error_copy.py:  17 passed, 1 xfailed
tests/test_static_assets.py:                     9 passed
TOTAL:                                          96 passed, 1 xfailed
```

The xfailed test is `test_research_internal_500_returns_error_and_message` — the tester documented this as needing an injection seam for `internal` 500 paths in `research.py:172/540`; coverage for the `internal` code itself is exercised by `test_error_copy_values_match_design_exactly[internal]`.

### Full regression

```
342 passed, 2 skipped, 1 xfailed in 51.81s
```

The 2 skipped tests are pre-existing E2E tests (gated behind `RUN_E2E=1`).

## Files modified (git diff --stat)

```
M  app/main.py                              (+45  -2)   global HTTPException handler
M  app/routers/auth.py                      ( +5  -5)   error_response_body() at 5 sites
M  app/routers/research.py                  (+24 -22)   error_response_body() at 11 sites + body_empty/anchor_text strip checks
M  app/services/file_processor.py           (+12 -32)   LimitExceededError shape migration
M  app/static/app.js                        (+34 -22)   showError/showNetworkError helpers + 13 call-site migrations
M  app/templates/history_detail.html        ( +1  -1)   error_message or fallback
A  app/services/error_copy.py               ( new )    single source of truth
M  tests/integration/test_auth_endpoints.py ( +9  -7)   BC subset assertions (design §9)
M  tests/integration/test_research_endpoints.py (+3 -3) BC subset assertions
M  tests/unit/test_file_processor.py        ( +7  -7)   detail.code → detail.error
```

(Test files added by tester sub-agent in earlier step, not by this dev loop.)

## Design coverage

13/13 design output rows covered — see tester report `docs/runs/20260420-tester-issue-5.md` for the row-by-row map.

`app/routers/history.py` is intentionally NOT modified: design §7 note 1 specifies that the global `HTTPException` handler in `app/main.py` wraps `HTTPException(404, "not_found")` from any router. This is verified by `test_history_detail_not_found_returns_error_and_message` which exercises history.py's actual `/api/history/<missing_id>` route end-to-end.

## Hard-constraint compliance

- HARNESS rule 1 (research_requests failures non-silent): unchanged — this issue does not touch `research_runner` / `claude_runner` write paths.
- HARNESS rule 2 (absolute DB paths): unchanged — no DB schema or path manipulation in this issue.
- HARNESS rule 3 (claude subprocess allowlist): unchanged — no claude subprocess invocations modified.
- HARNESS rule 4 (cookie flags): unchanged.
- Issue #5 contract: every 4xx/5xx JSON body now contains both `error` (BC machine code preserved) and `message` (Chinese copy). The legacy `code` key is gone from `LimitExceededError`. The frontend never renders `body.error` directly — it goes through `showError()` which prefers `body.message`.

## Rollback (if needed)

```
cd /home/ubuntu/method-dev
git checkout main
git branch -D feat/issue-5-error-copy
# or restore original files from /tmp/method-issue-5-backup-20260420T161853Z/
```
