# Code Review — Issue #5 (Frontend UX Error Copy Refresh)

- Reviewer: code-review sub-agent (independent)
- Date: 2026-04-20
- Branch: `feat/issue-5-error-copy`
- Design: `docs/design/issue-5-error-copy.md`
- Scope: 7 product files modified/new, 8 test files modified/new

---

## 1. Per-file findings

### 1.1 `app/services/error_copy.py` (NEW) — PASS

- 24 codes match design §4.1 verbatim (auth: 6, research: 12, shared: 1, file_processor: 6).
- `message_for("")` and `message_for(<unknown>)` both return `"操作失败，请稍后重试"` per design §5.
- `error_response_body(code)` helper added (a useful extra; doc-string credits the design).
- Strings spot-checked against design §4.1 — all 24 character-for-character matches confirmed by `tests/unit/test_error_copy.py::test_error_copy_values_match_design_exactly` (parameterised over the full set).

WARN (non-blocking): ruff I001 `app/services/error_copy.py:15` — unsorted import block (single-import file; cosmetic only). No action required for this issue.

---

### 1.2 `app/main.py` — PASS (with minor WARN)

- New `_http_exception_handler` (lines 44–82) wraps `StarletteHTTPException` so:
  - dict `detail` (LimitExceededError after migration) is bubbled verbatim;
  - str `detail` is wrapped to `{error, message}`;
  - special-cases FastAPI default 404 ("Not Found") → "not_found" canonical code.
- Verified by `test_global_404_handler_returns_error_and_message` and `test_research_download_not_found_returns_error_and_message` in `tests/integration/test_research_error_copy.py`.

WARN: `app/main.py:8` imports `HTTPException` and `app/main.py:9` imports `RequestValidationError` but neither is used (ruff F401, both confirmed). Likely leftover from a planned 422 handler that the design (§5 row 6, §8) explicitly de-scoped. Suggested fix: drop the two unused names.

---

### 1.3 `app/routers/auth.py` — PASS

- 4 explicit error returns migrated to `error_response_body(<code>)` (lines 165, 171, 177, 198).
- Marker handlers `_Unauthenticated` (line 277) and `_BadOrigin` (line 290) also migrated.
- BC contract preserved: `error` key is still emitted (test `test_error_field_still_present_after_message_addition` covers both keys).
- No new business logic moved into the router; boundary rule respected.

---

### 1.4 `app/routers/research.py` — PASS (with one minor logic-change concern)

Migrated paths (verified line-by-line vs design §4.1):

| Line | Code | Status |
|---|---|---|
| 102 | empty_question | OK (now also fires for whitespace-only via `q.strip()` already pre-existing) |
| 107 | question_too_long | OK |
| 112 | invalid_mode | OK |
| 121 | (bubble) files_too_many etc. | OK — `exc.detail` already in new shape |
| 172 | internal | OK |
| 365 | plan_missing | OK |
| 404 | request_busy | OK — replaced inline Chinese with helper; hard-coded copy in old code (`请求仍在处理中，请等它结束后再删除`) replaced by canonical (`请求仍在处理中，请等它结束后再操作`); test asserts the new wording and design table is the source of truth. PASS.
| 501 | request_not_finalized | OK |
| 514 | anchor_text_invalid | OK + new `not payload.anchor_text.strip()` check (whitespace-only). Design §6 doesn't mandate this exact behaviour, but the dev-loop reported deliberate behaviour change to fix UX (whitespace selection). Tester explicitly tests it. Existing `test_post_comment_anchor_text_too_long_returns_400` only asserts `"error" in body` — passes. **Risk**: any prior test that posted anchor_text as a non-empty string of whitespace expecting 201 would break — none found via grep. PASS.
| 519 | body_empty (NEW path) | OK — split out from the prior single `body_invalid` branch. Pre-existing `test_post_comment_body_too_long_returns_400` uses 2001-char body so still hits `body_invalid` (line 523). Empty body is now routed to `body_empty` (correct per design §4.1). Existing test passes the loose `"error" in body` assertion. PASS.
| 523 | body_invalid (length only) | OK |
| 531 | anchor_context_too_long | OK |
| 544 | body_empty (comment_runner.BodyEmptyError) | OK |
| 551 | internal | OK |
| 662 | ai_reply_not_deletable | OK |
| (404 paths via `HTTPException`) | not_found | wrapped by global handler (1.2). |

WARN: `app/routers/research.py:29` — `Field` imported but unused after Issue #4's removal of pydantic length-limits (ruff F401). Pre-existing, not introduced by Issue #5; cosmetic.

---

### 1.5 `app/services/file_processor.py` — PASS

- `LimitExceededError.__init__` signature changed to `(code, message="")` with `message_for(code)` as fallback (lines 131–136). Detail now `{"error": code, "message": <中文>}`. Legacy `"code"` key gone — verified by `tests/unit/test_file_processor_error_copy.py::test_no_legacy_code_key_across_all_branches` and `tests/integration/test_research_error_copy.py::test_post_research_too_many_files_returns_new_shape` (asserts `"code" not in body`).
- All 6 raise sites migrated: lines 151, 158, 161, 163, 167, 187, 193.
- `extracted_path is None`/`extraction_ok=False` handling unchanged.
- HARNESS rule 1 (no silent failure): not in scope — this module never writes `research_requests.status='failed'`.

---

### 1.6 `app/static/app.js` — PASS

- New `showError(body, status, fallback)` (lines 22–28) and `showNetworkError()` (line 29) helpers added per design §6.1.
- The 17 `alert(...)` migrations from design §7 verified line-by-line against the file:
  - Login email failure (line 49): `showError(body, r.status)` ✓
  - Login network (line 53): `showNetworkError()` ✓
  - Verify code failure (line 74): `showError(body, r.status)` ✓
  - Verify network (line 77): `showNetworkError()` ✓
  - Submit research banner path (line 275): `body.message || ("提交失败 (..." )` — no longer concatenates `body.error` ✓
  - History delete 409/404/other (lines 465, 467, 469): `showError(body, ..., <fallback>)` ✓
  - History delete network (line 473): `showNetworkError()` ✓
  - Comment compose 400/409/401/other (lines 688, 690, 692, 694): `showError(resBody, ...)` ✓
  - Comment compose network (line 697): `showNetworkError()` ✓
  - Comment delete other (line 721): `showError(body, r.status, ...)` ✓
  - Comment delete network (line 725): `showNetworkError()` ✓
- Pure-client alerts retained where appropriate (lines 52, 187, 260, 416, 659, 660 — all already-Chinese, never read `body.error`).
- Reverse-scan tripwire `tests/test_static_assets.py` regex `alert\(... + <obj>.error` returns ZERO hits (confirmed manually with `Grep`); helper-defined and ≥3 invocations checks all pass.

NOTE: line 352 `data.error_message` is the SSE error payload (not the same as `body.error`), and is the legitimate field that surfaces `research_requests.error_message` to the failed-banner. Not in scope.

---

### 1.7 `app/templates/history_detail.html` — PASS

Line 21: `{{ error_message or '研究失败，原因未知，请重试' }}` — exact design §4.3 / §5 row 8 string. Both `data-markdown-source` attribute and inner text use the same Jinja `or` fallback. Tests `test_history_detail_failed_with_null_error_message_renders_fallback` and `test_history_detail_failed_with_error_message_renders_verbatim` cover both branches.

---

## 2. Test file findings

### 2.1 `tests/unit/test_error_copy.py` (NEW) — PASS
Design oracle is duplicated verbatim into `EXPECTED_COPY` and parametrised tests assert dict equality. Will hard-fail on any future drift.

### 2.2 `tests/unit/test_file_processor_error_copy.py` (NEW) — PASS
Six branches plus two edge cases (empty-string message → fallback, explicit message respected) plus BC tripwire (no `"code"` key).

### 2.3 `tests/integration/test_auth_error_copy.py` (NEW) — PASS
Five auth codes + BC contract test (`error` and `message` both present).

### 2.4 `tests/integration/test_research_error_copy.py` (NEW) — PASS
17 cases covering every research/history error code in §4.1, the global 404 handler, the file_processor bubble shape, and the template fallback. The `internal` 500 case is `xfail` with a clear reason.

### 2.5 `tests/test_static_assets.py` (NEW) — PASS
Regex-based reverse-scan tests + helper-presence tests + verbatim-string presence tests. Implements design §5 row for `app.js` correctly.

### 2.6 BC migrations
- `tests/integration/test_auth_endpoints.py` — 7 strict equality assertions softened to `.get("error") == ...`. Correct minimal change.
- `tests/integration/test_research_endpoints.py` — 3 strict equalities softened. **WARN**: line 257 of `test_post_research_too_many_files_returns_400` still reads `body.get("code") or body.get("detail",{}).get("code") or body.get("error")`. After §3.2 migration, `"code"` key is **explicitly removed** from `LimitExceededError.detail` and asserted gone (`tests/integration/test_research_error_copy.py:636`). The legacy fallback chain is dead but harmless — `body.get("error")` is the only branch that fires now. **Recommendation (non-blocking)**: simplify to `body.get("error")` to match the BC tripwire and avoid confusing future readers. The reverse-scan rule (L4) flagged this as a lingering reader of the old key; treating as WARN, not FAIL, because the test still passes for the right reason and the BC contract is enforced by a sibling test in the new file.
- `tests/unit/test_file_processor.py` — 7 assertions migrated `["code"]` → `["error"]`. Correct.

---

## 3. Anti-gaming check (rubric items 8–12)

Looked for: hardcoded constants matching test expectations, test-specific branches, lookup-table-disguised-as-logic, code that exists only to satisfy a string equality.

- `error_copy.py` IS a lookup table — but the design explicitly demands a dict (§5 row 1, "单一事实源"). Both the test file and the implementation reference the same source-of-truth (design §4.1) independently — neither is generated from the other. PASS.
- The frontend `showError` helper has no test-specific branches; it is a thin priority cascade.
- Routers all use `error_response_body(<literal>)` — no conditional on test mode, no skipping in test paths.
- `tests/unit/test_error_copy.py` independently lists EXPECTED_COPY (not imported from the implementation). This is the right kind of duplication: catches drift, doesn't hide it.

Anti-gaming triggered: NONE.

---

## 4. Design completeness (rubric item 13 — BLOCKING)

Design §5 lists 13 output rows (9 product + 4 test). Verified existence on disk:

| # | Path | Exists? |
|---|---|---|
| 1 | `app/services/error_copy.py` | ✓ |
| 2 | `app/routers/auth.py` (modified) | ✓ |
| 3 | `app/routers/research.py` (modified) | ✓ |
| 4 | `app/routers/history.py` (modified) | ✓ — no edits needed; covered by global handler in main.py per design §7 note 1 |
| 5 | `app/services/file_processor.py` (modified) | ✓ |
| 6 | `app/main.py` (modified) | ✓ — added handler beyond the optional-422 minimum |
| 7 | `app/static/app.js` (modified) | ✓ |
| 8 | `app/templates/history_detail.html` (modified) | ✓ |
| 9 | `tests/test_error_copy.py` | ✓ — at `tests/unit/test_error_copy.py` (subdir, equivalent) |
| 10 | `tests/test_static_assets.py` | ✓ |
| 11 | `tests/routers/test_auth_error_copy.py` | ✓ — at `tests/integration/test_auth_error_copy.py` (subdir choice) |
| 12 | `tests/routers/test_research_error_copy.py` | ✓ — at `tests/integration/test_research_error_copy.py` |
| 13 | `tests/services/test_file_processor_error_copy.py` | ✓ — at `tests/unit/test_file_processor_error_copy.py` |

All 13/13 present. Subdirectory placement (`tests/unit/`, `tests/integration/`) differs from the design's nominal `tests/routers/` / `tests/services/` paths but matches Method's actual project layout (other tests follow the same convention). No missing files.

Design completeness: PASS (13/13 files).

---

## 5. Design symbol existence (rubric item 14 — BLOCKING)

| Symbol | Where | Exists? |
|---|---|---|
| `ERROR_COPY` dict | `app/services/error_copy.py:21` | ✓ |
| `message_for(code)` | `app/services/error_copy.py:59` | ✓ |
| `error_response_body(code)` (extra helper) | `app/services/error_copy.py:70` | ✓ |
| `LimitExceededError(code, message="")` | `app/services/file_processor.py:131` | ✓ |
| `_http_exception_handler` (StarletteHTTPException) | `app/main.py:44` | ✓ |
| `showError(body, status, fallback)` | `app/static/app.js:22` | ✓ |
| `showNetworkError()` | `app/static/app.js:29` | ✓ |
| Auth marker handlers `_Unauthenticated` / `_BadOrigin` migrated | `app/routers/auth.py:277, 290` | ✓ |

All design-named symbols exist at the named (or equivalent) location.

Symbol completeness: PASS.

---

## 6. Dangling reference scan (rubric item 15 / L4 reverse-scan)

- `Grep "from app.services.error_copy"` confirms `message_for` / `error_response_body` resolved at every import site (auth, research, file_processor, main).
- No call to a function that doesn't exist. No JS reference to a symbol not defined in `app.js`.
- Legacy `"code"` key:
  - Removed from product code in `file_processor.py` (verified: `Grep "code"` over `app/` returns only the marked.min.js vendor file).
  - Removed from frontend (no `body.code` / `data.code` / `resBody.code` anywhere in `app.js`).
  - Lingering REMNANT: `tests/integration/test_research_endpoints.py:257` still reads `body.get("code")`. This is a TEST-side compatibility fallback (not product code) and is the only `"code"` reader left in the repo; harmless because `body.get("error")` is the branch that fires after §3.2 migration. WARN, not FAIL.

Dangling references: NONE in product code; ONE harmless test-side relic.

---

## 7. Method-specific BLOCKING checks

| # | Check | Result |
|---|---|---|
| 1 | Chinese copy verbatim match (every code at every call site) | PASS — every router call uses `error_response_body(<code>)` which dispatches through `ERROR_COPY` dict whose values are tested for character-for-character equality with design §4.1 |
| 2 | HARNESS rule 1 (research_requests never silent: `failed` requires `error_message`) | PASS — Issue #5 did not touch any path that sets `status='failed'`. `Grep "status\s*=\s*['\"]failed"` shows the three sites in `research_runner.py` (385/392/419) plus `comment_runner.py` (644) — all pre-existing, all paired with non-empty error_message assignment per HARNESS §1 (already enforced before this issue). |
| 3 | HARNESS rule 3 (claude subprocess allowlist) | PASS — Issue #5 did not touch `claude_runner.py`; allowlist unchanged |
| 4 | BC tripwire (no lingering reader of `.code` on file_processor errors) | WARN — only `tests/integration/test_research_endpoints.py:257` still references `body.get("code")` as a fallback chain, but the actual passing branch is `body.get("error")`; product code is clean |
| 5 | Two router behavior changes (whitespace-only `anchor_text` → `anchor_text_invalid`; empty `body` → `body_empty`) | PASS — both paths are covered by NEW Issue #5 tests (`test_post_comment_anchor_text_invalid_returns_error_and_message` for whitespace-only, `test_post_comment_body_empty_returns_error_and_message` for empty body); existing `tests/integration/test_comment_endpoints.py` tests use loose `"error" in body` assertions and remain green. No other tests assert the old strict path. |

Method-specific BLOCKING: NONE triggered.

---

## 8. Boundary / hard-constraint scan (per CODE_REVIEW_PROMPT.md)

- **Component boundaries**: `app/services/error_copy.py` is pure Python (no FastAPI imports, no I/O). `app/services/file_processor.py:39 from fastapi import HTTPException, UploadFile` is pre-existing (HTTPException needed because `LimitExceededError` is itself an HTTPException — a known design tradeoff to preserve raise-and-bubble semantics into routers). Not introduced by Issue #5; out-of-scope WARN.
- **HARNESS rule 4 (cookie flags)**: `auth.py` cookie code unchanged by Issue #5. PASS.
- **HARNESS rule 5 (e2e gating)**: no e2e files added. PASS.
- **Secrets hygiene**: `Grep "ioppyngi"` over `app/` and `tests/` returns no hits. No password/SECRET literals introduced.
- **Lazy imports**: no new lazy imports. (`error_copy` imported at module top in all callers.)
- **`# pragma: no cover`**: none added.

---

## 9. Quality (non-blocking)

- ruff: 4 findings on Issue #5-touched files (`app/main.py` 2× F401 unused imports, `app/routers/research.py` 1× F401 unused `Field`, `app/services/error_copy.py` 1× I001 import sort). Total repo ruff: 39 findings (vast majority pre-existing). None safety-family. Recommend the `app/main.py` two F401s be cleaned up before merge as they were introduced by this issue (the `Field` one in research.py is pre-existing).
- Test count not run (per review-rule). Dev-loop report claims green.

---

## Final summary

- Anti-gaming check: NONE
- Design completeness: 13/13 files exist; 8/8 named symbols verified
- Method-specific BLOCKING: NONE
- WARNs: 3 (unused imports in `app/main.py`; ruff cosmetic in `error_copy.py`; legacy `"code"` fallback in one BC test)

### Final VERDICT: **PASS**

The implementation faithfully realises the design. The migration is consistent, the BC contract is preserved, and every machine code is paired with the design-mandated Chinese copy at the canonical source. No blocking issues; the three WARNs are cosmetic and may be addressed in a small follow-up commit before merge.

### Suggested follow-ups (non-blocking, can be deferred)

1. `app/main.py` lines 8–9: drop unused `HTTPException` and `RequestValidationError` imports (introduced by this issue).
2. `tests/integration/test_research_endpoints.py:257`: simplify the BC fallback chain to `body.get("error")` — the legacy `"code"` key is now contractually absent.
3. `app/services/error_copy.py:15`: ruff `--fix` to address the I001 single-import-block cosmetic.
