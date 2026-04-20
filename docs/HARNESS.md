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

## 10 步流程的项目级调整（2026-04-20 起）

全局 `CLAUDE.md` 的 10 步流程默认有两处人工审核门：Step 2b（设计评审）+ Step 5（测试评审）。**Method 项目把 Step 5 删掉**：

- **保留 Step 2b**：用户在飞书文档里对功能设计和技术方案审核通过。这一步就代表了用户对本轮工作的全部确认。
- **跳过 Step 5**：测试评审**不再需要人工 gate**。测试由 Step 3（tester sub-agent）+ Step 4（test-quality-checker，含设计覆盖度 BLOCKING）双重把关即可。

调整后的执行序列：

```
Step 0 preflight → Step 1 env check → Step 2a design + design-check
→ Step 2b 人工评审（飞书）→ Step 3 tester → Step 4 test-quality-check
→ Step 6 backup+branch → Step 7 dev loop → Step 8 code review → Step 9 DEV_LOG
```

（Step 2b 之后从 Step 3 直接跳到 Step 6，中间的 Step 5 省去。）

原则：Step 2b 已覆盖用户对"做什么 + 怎么做"的确认意图，测试是实现细节的一部分，不再需要二次人工确认。

## Component map

```
app/
├── main.py               FastAPI app assembly + lifespan
├── config.py             pydantic-settings reads .env
├── db.py                 async SQLA engine, init_db, get_session
├── models.py             ORM tables (users, login_codes, sessions, approval_tokens, research_requests, uploaded_files)
├── routers/
│   ├── health.py         /api/health                                  [M1 ✓]
│   ├── auth.py           /api/auth/*                                  [M2]
│   ├── admin.py          /admin/approve                               [M2]
│   ├── research.py       /api/research + SSE                          [M3]
│   └── history.py        /api/history, /history/<id>                  [M4]
├── services/             (pure, no FastAPI deps — unit-testable)
│   ├── mailer.py         SMTP + email templates                       [M2 ✓]
│   ├── auth_flow.py      register/verify/approve                      [M2]
│   ├── claude_runner.py  subprocess stream-json wrapper               [M3]
│   └── file_processor.py pdf/docx → .md + limits                      [M3]
└── templates/            Jinja2 (HTML + email .txt)
```

Boundary rule: routers validate and assemble responses only; business logic lives in `services/*`. Services must import without FastAPI.

## Interface / module table

| Module | Path | Responsibility | Depends on | Consumers |
|---|---|---|---|---|
| config | `app/config.py` | Load env settings | `.env` file | everyone |
| db | `app/db.py` | Async SQLA engine, `init_db`, `get_session` | config | models, routers, services |
| models | `app/models.py` | ORM tables | db | services, routers |
| services/mailer | `app/services/mailer.py` | SMTP send with templates | config, `templates/emails/` | auth_flow |
| services/auth_flow | `app/services/auth_flow.py` (M2.3) | register / verify / approve pure functions | models, mailer | routers/auth, routers/admin |
| services/claude_runner | `app/services/claude_runner.py` (M3) | subprocess wrapper, stream-json parser | config | routers/research |
| services/file_processor | `app/services/file_processor.py` (M3) | pdf/docx extract + size/count limits | — | routers/research |
| routers/health | `app/routers/health.py` | `/api/health` | — | main |
| routers/auth | `app/routers/auth.py` (M2.4) | auth endpoints | auth_flow | main |
| routers/admin | `app/routers/admin.py` (M2.4) | `/admin/approve` | auth_flow | main |
| routers/research | `app/routers/research.py` (M3) | research endpoints + SSE | claude_runner, file_processor | main |
| routers/history | `app/routers/history.py` (M4) | history list / detail | models | main |
| main | `app/main.py` | FastAPI app assembly + lifespan | all routers | uvicorn |

Legend: ✓ done, (Mx) pending at given milestone.

## Related docs

- Design spec: [`superpowers/specs/2026-04-19-method-research-planner-design.md`](superpowers/specs/2026-04-19-method-research-planner-design.md)
- Implementation plan: [`superpowers/plans/2026-04-19-method-implementation-plan.md`](superpowers/plans/2026-04-19-method-implementation-plan.md)
- Agent context: [`AGENT_CONTEXT.md`](AGENT_CONTEXT.md)
- Tester prompt: [`TESTER_PROMPT.md`](TESTER_PROMPT.md)
- Code review prompt: [`CODE_REVIEW_PROMPT.md`](CODE_REVIEW_PROMPT.md)
- Dev log: [`DEV_LOG.md`](DEV_LOG.md)
