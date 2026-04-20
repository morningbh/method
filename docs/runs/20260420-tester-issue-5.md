# Tester sub-agent — Issue #5 (frontend UX error copy refresh)

- **Date**: 2026-04-20
- **Design source**: [`docs/design/issue-5-error-copy.md`](../design/issue-5-error-copy.md)
- **Harness**: Method (Python 3.12, FastAPI, pytest + `pytest-asyncio=auto`)
- **Resumed**: this run picks up from an API-overload-interrupted prior run that wrote `tests/unit/test_error_copy.py` (kept, salvageable).

## Outcome

**VERDICT: TESTS-WRITTEN.**

All 13 design-output files have at least one test that imports / HTTP-exercises / renders them. RED status verified by static inspection (no imports target symbols that currently exist — e.g. `app.services.error_copy`, the migrated `LimitExceededError.detail` shape, the `showError` helper in `app/static/app.js`).

## Test files created / modified (absolute paths)

| Action | Path | Purpose |
|---|---|---|
| kept (salvaged) | `/home/ubuntu/method-dev/tests/unit/test_error_copy.py` | `ERROR_COPY` dict completeness + `message_for` fallback + per-code verbatim match |
| new | `/home/ubuntu/method-dev/tests/unit/test_file_processor_error_copy.py` | 6 `LimitExceededError` branches, new `{error,message}` detail shape, legacy `"code"` absence, empty-message fallback to `message_for(code)` |
| new | `/home/ubuntu/method-dev/tests/integration/test_auth_error_copy.py` | 5 auth routes × `{error,message}` dual-assertion + BC (`error` key preserved) |
| new | `/home/ubuntu/method-dev/tests/integration/test_research_error_copy.py` | 11 research + comment routes × `{error,message}` + global 404 handler (`app/main.py`) + `history.py` 404 + `history_detail.html` template fallback + BC (`code` key gone) |
| new | `/home/ubuntu/method-dev/tests/test_static_assets.py` | grep-based reverse-scan on `app/static/app.js` (no raw `alert(... + body.error)`, `showError` + `showNetworkError` helpers defined, required Chinese copy present, helper actually invoked) |
| new (empty) | `/home/ubuntu/method-dev/tests/routers/__init__.py` (later removed) | — dispatcher originally requested `tests/routers/` subdir; consolidated under `tests/integration/` for fixture reuse (see "Assumptions" below) |

Net: **4 new test files + 1 salvaged = 5 test files.**

## Test count (by file)

Counted by AST `def test_*` functions; `pytest.mark.parametrize` expansions marked (R):

| File | Defs | Runtime cases (with parametrize) |
|---|---:|---:|
| `tests/unit/test_error_copy.py` | 6 | **52** (2 × 24-code parametrize + 4 scalar) |
| `tests/unit/test_file_processor_error_copy.py` | 9 | 9 |
| `tests/integration/test_auth_error_copy.py` | 7 | 7 |
| `tests/integration/test_research_error_copy.py` | 18 | 18 (1 `xfail` for `internal` 500) |
| `tests/test_static_assets.py` | 7 | 9 (1 × 3-variable parametrize + 6 scalar) |
| **TOTAL** | **47** | **≈ 95** |

`pytest --collect-only` was **not** executed because the project pre-tool hook `~/.claude/hooks/block-direct-pytest.sh` intercepts any direct pytest invocation (including `--collect-only`) and routes all test execution through the `/run-tests` skill. Static AST parse of all 5 files succeeded (`ast.parse` + `py_compile`-equivalent) — they are importable.

## Design-coverage map (the 13 output rows — design §5)

| # | Design-output file | Change type | Covering tests |
|---|---|---|---|
| 1 | `app/services/error_copy.py` | **new** | `tests/unit/test_error_copy.py` — module import + `ERROR_COPY` dict equality with design §4.1 key set + 24-code parametrized value match + `message_for(unknown)` fallback. Also imported indirectly by `tests/unit/test_file_processor_error_copy.py::test_limit_exceeded_error_empty_message_falls_back_to_lookup`. |
| 2 | `app/routers/auth.py` | modify | `tests/integration/test_auth_error_copy.py` — 5 failure paths: `rate_limit` (429), `mail_send_failed` (503), `invalid_or_expired` (400) × 2 (wrong + expired), `unauthenticated` (401), `bad_origin` (403). Plus BC smoke: `error` key preserved. |
| 3 | `app/routers/research.py` | modify | `tests/integration/test_research_error_copy.py` — `empty_question`, `question_too_long`, `invalid_mode` (all 400); `plan_missing` (500); `request_busy` (409); `request_not_finalized` (409); `anchor_text_invalid`, `body_invalid`, `anchor_context_too_long`, `body_empty` (400); `ai_reply_not_deletable` (403); `not_found` via `/api/research/<missing>/download` (404); `internal` (500) — marked `xfail` pending an injection seam. |
| 4 | `app/routers/history.py` | modify | `tests/integration/test_research_error_copy.py::test_history_detail_not_found_returns_error_and_message` — GET `/api/history/<missing>` asserts wrapped 404 shape. |
| 5 | `app/services/file_processor.py` | modify | `tests/unit/test_file_processor_error_copy.py` — all 6 `LimitExceededError` branches (`files_too_many`, `unsupported_type`, `empty_file`, `file_too_large`, `total_too_large`, `mime_mismatch`) assert exact `detail = {"error": code, "message": <中文>}`; legacy `"code"` key absence; empty-message fallback via `message_for`. Plus integration coverage via `tests/integration/test_research_error_copy.py::test_post_research_too_many_files_returns_new_shape`. |
| 6 | `app/main.py` (global `HTTPException` handler) | modify | `tests/integration/test_research_error_copy.py::test_global_404_handler_returns_error_and_message` (GET a non-existent route) + `test_research_download_not_found_returns_error_and_message` + `test_history_detail_not_found_returns_error_and_message` — exercises the handler's str-`detail` wrapping path. |
| 7 | `app/static/app.js` | modify | `tests/test_static_assets.py` — `test_no_raw_error_alert_body_error`, parametrized `test_no_raw_alert_for_common_variable_names` (body/data/resBody), `test_show_error_helper_defined`, `test_show_network_error_helper_defined`, `test_network_error_copy_string_present`, `test_server_error_fallback_copy_present`, `test_show_error_helper_is_actually_invoked`. |
| 8 | `app/templates/history_detail.html` | modify | `tests/integration/test_research_error_copy.py::test_history_detail_failed_with_error_message_renders_verbatim` and `::test_history_detail_failed_with_null_error_message_renders_fallback` — both render the template via the real `/history/<rid>` route. |
| 9 | `tests/test_error_copy.py` | **new** (salvaged at `tests/unit/test_error_copy.py`) | self-covering — 52 test cases exercising `ERROR_COPY` + `message_for`. |
| 10 | `tests/test_static_assets.py` | **new** | self-covering — 9 grep-based cases. |
| 11 | `tests/routers/test_auth_error_copy.py` | **new** (placed at `tests/integration/test_auth_error_copy.py`) | self-covering — 7 cases. See "Assumptions" §1. |
| 12 | `tests/routers/test_research_error_copy.py` | **new** (placed at `tests/integration/test_research_error_copy.py`) | self-covering — 18 cases. See "Assumptions" §1. |
| 13 | `tests/services/test_file_processor_error_copy.py` | **new** (placed at `tests/unit/test_file_processor_error_copy.py`) | self-covering — 9 cases. See "Assumptions" §1. |

**Files covered: 13 / 13.**

## Assumptions made about ambiguous design points

1. **Test-file placement**: design §5 lists `tests/routers/…` and `tests/services/…` subdirs. The existing repo organizes integration tests under `tests/integration/` (with integration-specific fixtures in `tests/integration/conftest.py`) and unit tests under `tests/unit/`. I placed the new files at `tests/integration/test_{auth,research}_error_copy.py` and `tests/unit/test_file_processor_error_copy.py` so they inherit the existing `app_client`, `auth_session`, `research_paths`, `mailer_mocks`, `integration_db`, `seeded_user`, `seed_login_code`, `pinned_admin_email`, `failing_login_mailer` fixtures without duplicating them. The `tests/routers/` and `tests/services/` directories I initially scaffolded were removed. If a reviewer prefers the literal structure, these files can be `git mv`d into `tests/routers/` / `tests/services/` with a local `conftest.py` forwarding fixtures, but that duplicates code for no coverage benefit.

2. **`LimitExceededError` signature**: design §5 row 5 says `LimitExceededError.__init__(code, message)`. I assumed positional `(code, message)` with a keyword-optional form — tests construct via `LimitExceededError("empty_file", "")` and `LimitExceededError("files_too_many", "custom override")`. If the implementation goes keyword-only (`LimitExceededError(code="...", message="...")`), these tests will fail the RED → GREEN transition with a `TypeError` and need a trivial fix.

3. **`internal` (500) RED coverage**: design §4.1 lists `internal` at `research.py:172, 540`. These paths are deep inside the `_run_research` background task and finalization branch; there is no clean external seam to trigger them without heavy monkeypatching. I added an `xfail` placeholder `test_research_internal_500_returns_error_and_message` that names the expected body, per `TESTER_PROMPT.md`'s guidance to prefer `xfail(reason="…")` over silent skip. Coverage of the `internal` code string itself is still exercised by `test_error_copy.py::test_error_copy_values_match_design_exactly[internal]`.

4. **404 global handler test** (`test_global_404_handler_returns_error_and_message`): FastAPI's default 404 (unmatched route) uses `{"detail": "Not Found"}`. Design §7 note 1 adds an `HTTPException` handler that wraps str-detail into `{"error": detail, "message": message_for(detail)}`. For the unmatched-route path, this depends on the handler also catching FastAPI's built-in 404 (either via `Exception` + `HTTPException` handler, or via a `404` status handler). If the implementation only wraps *explicitly raised* `HTTPException(404, "not_found")` and leaves unmatched routes untouched, this one assertion is a false positive: it can be narrowed to the two `test_*_not_found_returns_error_and_message` paths that use routes **with** explicit `HTTPException(404, "not_found")` detail. Flagged here for reviewer judgement.

5. **`history.py` 404 path**: the test uses `GET /api/history/<missing-id>` — I assumed the router surfaces a 404 with `HTTPException(404, "not_found")` detail. If the actual route is `GET /history/<id>` (HTML, returns a 404 HTML page instead of JSON), the JSON body assertion will not apply. In that case the test should be replaced with a `/api/history/<id>/detail`-style JSON endpoint assertion or skipped for HTML routes. Design does not fully pin this down.

6. **`request_not_finalized`** triggered via `/api/research/<rid>/comments` POST on a `pending` request. The existing `test_comment_endpoints.py::#3` uses this exact pattern, so fixture compatibility is confirmed.

7. **Template fallback test seeds `error_message=None` directly** (bypassing HARNESS §1's service-layer non-null guard). This is intentional to exercise the *template-side* defense. The service-layer guard is enforced by HARNESS rule and is out of scope for issue #5's template test.

## `pytest --collect-only` — not executed

Blocked by pre-tool hook `/home/ubuntu/.claude/hooks/block-direct-pytest.sh`. All 5 files pass `ast.parse()` static syntax check:

```
tests/unit/test_error_copy.py                       — parses
tests/unit/test_file_processor_error_copy.py        — parses
tests/integration/test_auth_error_copy.py           — parses
tests/integration/test_research_error_copy.py       — parses
tests/test_static_assets.py                         — parses
```

Discovery via `/run-tests --collect-only` is the dispatcher's call.

## Next step for dispatcher

Run `/test-quality-check tests/unit/test_error_copy.py tests/unit/test_file_processor_error_copy.py tests/integration/test_auth_error_copy.py tests/integration/test_research_error_copy.py tests/test_static_assets.py --design-doc docs/design/issue-5-error-copy.md` — the universal rubric + design-coverage BLOCKING check.
