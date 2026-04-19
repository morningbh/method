# Method — Project Harness

This file inherits all rules from the global harness at `/home/ubuntu/.claude/CLAUDE.md`.
Project-specific hard constraints are listed below. In case of conflict, the stricter rule wins.

## Project-specific hard constraints

1. **`research_requests` failures must never be silent.**
   Any code path that moves a `research_requests` row out of `pending`/`running`
   to `failed` MUST write a non-empty `error_message`. Silent failures
   (`status=failed, error_message=NULL`) are forbidden; tests must assert on
   the error text.

2. **All file paths stored in the database must be absolute.**
   `research_requests.plan_path`, `uploaded_files.stored_path`, and
   `uploaded_files.extracted_path` must be absolute paths on disk. Relative
   paths are forbidden — they break when the CWD changes (e.g. between the
   FastAPI worker and the `claude` subprocess).

3. **`claude` subprocess tool allowlist.**
   Every invocation of the `claude` CLI from this codebase MUST pass
   `--allowed-tools Read,Glob,Grep`. No `Write`, `Bash`, or `Edit`. The
   research-method-designer skill is a pure planner; it must not mutate the
   filesystem or spawn child processes.

4. **Session cookie flags.**
   Auth cookies MUST be set with `HttpOnly` and `SameSite=Lax`. The `Secure`
   flag is added once the deployment is behind HTTPS (M5).

5. **E2E tests are opt-in.**
   Any test in `tests/e2e/` must be guarded by `RUN_E2E=1` env var (skip
   otherwise). These hit real SMTP and real `claude` subprocess and are too
   slow/expensive for the default `make test` loop.

## Related docs

- Design spec: `docs/superpowers/specs/2026-04-19-method-research-planner-design.md`
- Implementation plan: `docs/superpowers/plans/2026-04-19-method-implementation-plan.md`
