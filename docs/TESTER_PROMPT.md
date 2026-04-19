# Method — /tester Prompt Extensions

Project-specific rules that extend the default `/tester` flow. Read the global skill at `~/.claude/skills/tester/SKILL.md` first — the rules below are additions, not replacements.

## Hard rules for Method tests

### Fixtures (always use; do not roll your own)

- **DB access** → `db_session` fixture in `tests/conftest.py` (yields an async SA session bound to a per-test SQLite file)
- **HTTP client** → `app_client` fixture in `tests/conftest.py` (httpx `AsyncClient` with real lifespan via `app.router.lifespan_context(app)`)
- Never create engines, sessionmakers, or `AsyncClient` instances inline in a test. If the fixture you need doesn't exist, add it to `conftest.py` rather than inlining.

### SMTP

- Use `aiosmtpd.controller.Controller` exactly as demonstrated in `tests/unit/test_mailer.py` (reserve an ephemeral port with a throwaway socket first, then pass it explicitly — passing `port=0` triggers an aiosmtpd self-probe that gets refused).
- **Never** connect to real Gmail in unit or integration tests. Real SMTP only in `tests/e2e/` and only behind `RUN_E2E=1`.

### `claude` subprocess

- Mock `asyncio.create_subprocess_exec` (and the stream readers it returns). Drive stream-json output line-by-line from an `async` iterator the test controls.
- **Never** invoke the real `claude` CLI in unit/integration tests — it costs money and is slow/flaky. Real CLI only in `tests/e2e/test_real_claude_call.py` behind `RUN_E2E=1`.

### E2E tests

- Path: `tests/e2e/*.py`
- Must be guarded at module or test level with `pytest.mark.skipif(os.getenv("RUN_E2E") != "1", reason="E2E disabled")`
- E2E tests are free to use real SMTP, real `claude`, real Gmail MCP polling — that's the point

### Design coverage (BLOCKING)

The tester prompt you receive is mechanically generated from the design doc's "output files" / "产出文件" table. For every file, class, route, or function listed there:

- at least one test must **import**, **instantiate**, **call**, or **HTTP-exercise** it
- generic "smoke that the module imports" counts only if no other behavioral test references it
- if the design lists something you don't know how to test yet, write a `pytest.mark.xfail(reason="…")` placeholder rather than silently skipping

If the design table is missing or ambiguous, stop and flag it to the dispatcher. Do NOT hand-write a test list from memory.

## Naming conventions

- Test files: `test_<module>.py` mirroring the module under test (e.g. `test_auth_flow.py` for `app/services/auth_flow.py`)
- Test functions: `test_<behavior_being_verified>` — describe the behavior, not the implementation
  - Good: `test_verify_code_rejects_expired_code`
  - Bad: `test_verify_code_branch_2`
- Parametrized cases: use `pytest.param(..., id="...")` so failures point at the scenario

## Assertion style

- Prefer exact equality (`assert x == expected`) over truthy checks (`assert x`)
- Decode bytes / parse JSON before asserting (`assert resp.json() == {"status": "sent"}`, not `assert b"sent" in resp.content`)
- For error paths, assert on both the status code and the error text / field
- For DB assertions, re-fetch the row through the same session and assert on concrete column values

## What NOT to do

- Do not read any file under `app/` other than public interfaces (pydantic models, type stubs, ABCs, router signatures). No reading service implementations — you write tests from the contract, not the implementation.
- Do not copy-paste expected values out of the current code — derive them from the spec.
- Do not `try: ... except: pass` in tests. If a call can fail, assert on the failure.
- Do not skip without a reason string (`pytest.skip("…")` only with an explanation).
