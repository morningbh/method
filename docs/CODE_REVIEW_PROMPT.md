# Method — /review Prompt Extensions

Project-specific checks that extend the default `/review` flow. Read the global skill at `~/.claude/skills/review/SKILL.md` first — the items below are additions, not replacements. Treat everything marked **BLOCKING** as FAIL (not WARN).

## BLOCKING — design completeness

(From global CLAUDE.md Sub-Agent Rules, repeated here so you can't miss them.)

- Every file listed in the design doc's "output files" / "产出文件" table MUST exist on disk.
- Every public function / class / route listed in the design MUST exist in code.
- Any code reference to a symbol that does not exist elsewhere in the codebase is a FAIL.
- Rationale: incomplete implementation is worse than buggy implementation — a bug surfaces, missing code is invisible to the caller.

## BLOCKING — component boundaries (spec §1.2)

- `app/routers/*` must not contain business logic beyond input validation, auth dispatch, and response assembly.
- `app/services/*` must be importable and unit-testable with zero FastAPI imports. Grep services for `from fastapi` / `import fastapi` — any hit is a FAIL unless the symbol is a type-only alias.

## BLOCKING — hard constraint #1 (no silent research failures)

`HARNESS.md` constraint 1: every code path that sets `research_requests.status = "failed"` must also set a non-empty `error_message` in the same transaction.

Procedure:
```
grep -rn 'status\s*=\s*["'\'']failed["'\'']' app/
grep -rn "status='failed'" app/  # just in case
```
For each hit, verify the surrounding block assigns `error_message = <non-empty>`. Any occurrence without an adjacent error_message assignment → FAIL.

## BLOCKING — hard constraint #2 (absolute paths in DB)

`research_requests.plan_path`, `uploaded_files.stored_path`, `uploaded_files.extracted_path` must be absolute.

Procedure: grep for assignments to these three columns. Each source expression must be a `Path.resolve()`, an `os.path.abspath(...)`, or a variable already proven absolute (e.g. derived from `settings.PLAN_DIR` which is absolute in `config.py`). Relative path literals / `os.path.join("uploads", ...)` without a preceding abs → FAIL.

## BLOCKING — hard constraint #3 (claude allowlist)

Every `claude` subprocess invocation must pass `--allowed-tools Read,Glob,Grep`. Any presence of `Write`, `Bash`, or `Edit` in the allowlist is FAIL.

Procedure:
```
grep -rn 'claude' app/services/claude_runner.py app/
grep -rn '--allowed-tools' app/
```

## BLOCKING — hard constraint #4 (cookie flags)

Session cookies set with `response.set_cookie(...)` must include `httponly=True` and `samesite="lax"`. Grep for `set_cookie(` and audit every call.

## BLOCKING — hard constraint #5 (e2e gating)

Every test file under `tests/e2e/` must have a top-level or per-test `pytest.mark.skipif(os.getenv("RUN_E2E") != "1", ...)` guard. Unguarded tests → FAIL.

## BLOCKING — secrets hygiene

Grep the tree (excluding `.env` which is gitignored) for:

- the SMTP password substring `ioppyngi` (fragment of the Gmail app password) — must NOT appear anywhere tracked
- the session secret fragment from `.env` — must NOT appear anywhere tracked
- other suspicious patterns: `password\s*=\s*"[^"]+"`, `SECRET\s*=\s*"[^"]+"` in non-`.env.example` files

Any hit in tracked files → FAIL (and the branch needs a history rewrite, not just a fix).

## Non-blocking quality checks

- `make lint` (ruff) must be clean. `.venv/bin/ruff check app tests` → WARN if any finding (escalate to FAIL if the rule is in the "safety" family: mutable default args, bare except, unused imports that hide a dead feature).
- Tests run via `/run-tests` must all pass; any skipped test without a reason string → WARN.
- Lazy imports (imports inside a function body) must follow L3 from global CLAUDE.md: either a startup `importlib.util.find_spec` check OR a test that imports the same path.
- New `# pragma: no cover` lines must have a `# why:` comment AND a named manual-verification command next to them (L2).

## Reverse-scan discipline (L4)

If the change migrates callers from an old symbol to a new one, run:
```
grep -rn "<old_symbol>" app/ tests/
```
and confirm every hit is either migrated or explicitly exempted with a reason in the PR description. Silent misses → FAIL.

## Output

Report FAIL / WARN / PASS with the list of findings. For each BLOCKING fail, include the exact grep line so the main agent can jump to it.
