# Task 2.4 — Auth Routes (design)

**Scope**: Issue #1 / Milestone M2. This document defines the FastAPI HTTP
boundary layer that consumes the `app/services/auth_flow` contract (Task 2.3)
and exposes it over HTTP + HTML. It owns the session-cookie header wiring,
exception-to-HTTP translation, the admin approval landing page, and the login
page. It does NOT touch business logic — `auth_flow` remains the single
writer of auth DB rows.

## 1. Purpose

Expose `auth_flow` over HTTP:

- JSON endpoints for the email → code → session dance.
- Admin GET link handler that renders HTML after server-side `approve_user`.
- Session-cookie middleware (`get_current_user` / `require_user`).
- HTML surfaces (`/login`, `/`) plus their Jinja2 templates + static mount.

Boundary rules (HARNESS §component-map): routers validate and assemble
responses; they do NOT commit — each call is wrapped in
`async with session.begin():` so any `MailerError` or `IntegrityError`
raised by the service rolls the row back (spec §4.3 + §6 of Task 2.3).
No blocking I/O on the event loop.

## 2. Endpoints

Reproduced from spec §3.1 (auth rows) verbatim:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | session optional | logged-in → placeholder landing; anon → 302 `/login` |
| GET | `/login` | — | renders login page |
| POST | `/api/auth/request_code` | — | send login code / register |
| POST | `/api/auth/verify_code` | — | verify code + set cookie |
| POST | `/api/auth/logout` | session required | clear cookie + delete sessions row |
| GET | `/admin/approve` | query `token=<raw>` | activate user, render HTML |

### 2.1 `POST /api/auth/request_code`

- **Request body** (pydantic): `class RequestCodeIn(BaseModel): email: EmailStr`.
- **Response body** (pydantic): `class RequestCodeOut(BaseModel): status: Literal["sent","pending","rejected"]`.
- **Handler flow**:
  1. `async with session.begin(): status = await request_login_code(session, payload.email)`
  2. return `{"status": status}` with HTTP 200.
- **Error translation**:
  - `RateLimitError` → HTTP 429 `{"error": "rate_limit"}`
  - `MailerError` → HTTP 503 `{"error": "mail_send_failed"}`. Transaction is rolled back automatically because the exception unwinds out of `session.begin()` before commit; login_codes/user rows created in the same txn are not persisted.
  - `sqlalchemy.exc.IntegrityError` (e.g. email-collision race from Task 2.3 §4) → HTTP 400 `{"error": "bad_request"}`.
  - Any other `AuthError` subclass bubbles → FastAPI default 500.

### 2.2 `POST /api/auth/verify_code`

- **Request body**: `class VerifyCodeIn(BaseModel): email: EmailStr; code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")`.
- **Response body**: `class VerifyCodeOut(BaseModel): ok: bool = True`.
- **Success**: call `raw = await verify_login_code(session, email, code)` inside `async with session.begin():`, then on the response set
  `Set-Cookie: method_session=<raw>; HttpOnly; SameSite=Lax; Max-Age=<session_ttl_days * 86400>; Path=/`.
  Cookie flags come from `auth_flow.COOKIE_FLAGS` (HARNESS §4). Cookie name from `auth_flow.COOKIE_NAME`. Use `response.set_cookie(...)` on a FastAPI `Response` parameter.
- **Error translation**: `InvalidCodeError` → HTTP 400 `{"error": "invalid_or_expired"}` (non-specific, prevents enumeration — Task 2.3 §12).

### 2.3 `POST /api/auth/logout`

- **Auth**: requires session (see §3). Enforced by `Depends(require_user)`.
- **Request body**: none.
- **Handler**:
  1. Read `method_session` cookie (via `Cookie(...)` dep).
  2. `async with session.begin(): await invalidate_session_cookie(session, raw)`.
  3. `response.set_cookie("method_session", value="", max_age=0, httponly=True, samesite="lax", path="/")` to clear.
- **Response**: `{"ok": True}` HTTP 200.
- If no cookie present → 401 `{"error": "unauthenticated"}` (same as any other auth-required endpoint).

### 2.4 `GET /admin/approve?token=<raw>`

- **Auth**: none. The raw token in the query string is itself the credential (Task 2.3 §4, design spec §4 "ADMIN_SECRET 不需要 — token 自身可防伪").
- **Request**: query param `token: str` (FastAPI `Query(...)`).
- **Handler**:
  1. `async with session.begin(): user = await approve_user(session, token)`.
  2. On success render `approved.html` with context `{"email": user.email}` — HTTP 200.
  3. On `ApprovalTokenError` render `approval_error.html` with generic copy — HTTP 200 (HTML success page; the error is user-visible, not a protocol error). Rationale: a 4xx would be crawled differently and is unnecessary for a human-facing page.
- **Missing / empty `token` param** → FastAPI auto-422 (pydantic validation on query) — acceptable.

### 2.5 `GET /login`

- **Auth**: none.
- **Handler**: render `login.html` with `{"state": "initial"}`. The page is otherwise static — the three visual states (initial / code-sent / pending) are switched client-side by fetch + JS in Task 4; the HTML contains all three regions with CSS classes so the template is identical across states.
- **Response**: HTML 200.

### 2.6 `GET /`

- **Auth**: session optional (via `Depends(get_current_user)`).
- **Handler**:
  - If user is `None` → `RedirectResponse("/login", status_code=303)`.
  - Else render a placeholder HTML body that says "logged in as `{email}` — workspace coming in Task 3.3" plus a logout link. Uses `base.html` as the shell, no separate template file needed (inline string or a `landing.html` — chosen: small dedicated template so the contract is explicit). Files list: `app/templates/landing.html`.

## 3. Session middleware

Two small FastAPI dependencies in `app/routers/auth.py` (or a shared `app/deps.py` — chosen: `app/routers/auth.py` keeps scope minimal; Task 3 will extract if research router needs it).

```python
async def get_current_user(
    method_session: str | None = Cookie(default=None),
    session: AsyncSession = Depends(_db_session),
) -> User | None:
    if not method_session:
        return None
    return await validate_session_cookie(session, method_session)

async def require_user(
    user: User | None = Depends(get_current_user),
    request: Request = ...,
) -> User:
    if user is None:
        # API routes: 401 JSON; HTML routes: 303 /login
        accept = request.headers.get("accept", "")
        if "text/html" in accept and "application/json" not in accept:
            raise HTTPException(
                status_code=303,
                detail="login required",
                headers={"Location": "/login"},
            )
        raise HTTPException(status_code=401, detail={"error": "unauthenticated"})
    return user
```

Cookie is read via FastAPI's `Cookie(default=None)` dependency — name must be `method_session` (the literal parameter name maps to the cookie key). `validate_session_cookie` never raises, so no try/except needed here.

The `_db_session` dependency is a thin wrapper over `app.db.get_session` converted into a FastAPI generator dep (`async def _db_session() -> AsyncIterator[AsyncSession]`).

## 4. Template contract

All templates live in `app/templates/`. `Jinja2Templates(directory=Path(__file__).parent.parent / "templates")` is instantiated once in `app/main.py` and imported by routers (or registered on `app.state.templates`).

| Template | Variables | Renders |
|---|---|---|
| `base.html` | `title: str` (default "Method"); block `content` | `<!doctype html>`, `<meta charset="utf-8">`, `<meta name="viewport" content="width=device-width, initial-scale=1">` (spec §7.3 rule 1), `<title>{{ title }}</title>`, flash region `<div id="flash"></div>`, `{% block content %}{% endblock %}` |
| `login.html` | none | spec §7.2 A. Renders the three region divs (initial, code-sent, pending-approval) toggled client-side by data-state |
| `approved.html` | `email: str` | spec §7.2 E. "✓ 已批准 — {{ email }} 已激活" |
| `approval_error.html` | none | "✗ 链接无效或已过期" generic copy (spec §4.1 "链接无效或已过期") |
| `landing.html` | `user_email: str` | placeholder for logged-in `/`. Shows email + logout link, notes workspace is Task 3.3 |
| `emails/*.txt` | untouched | already exist from Task 2.2 |

No backend-rendered flash-error content for login — all error UX is client-side JS reading the JSON response (Task 4). This document does not introduce server-side session flashes.

## 5. Transaction ownership

HARNESS + Task 2.3 §2 contract: `auth_flow` does not call `session.commit()`. Every router call therefore wraps the service call like:

```python
async with session.begin():
    status = await request_login_code(session, payload.email)
```

`session.begin()` commits on clean exit and rolls back on any exception. Exceptions (`RateLimitError`, `MailerError`, `IntegrityError`) propagate out of the block, trigger rollback, then the FastAPI exception handler translates them. This is the only correct way to satisfy HARNESS §1 ("failures must never be silent") and Task 2.3 §12 ("Task 2.4 owns the transaction boundary") simultaneously.

## 6. Templates & static discovery

- **Jinja2**: `templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))` in `app/main.py`. Exposed as `app.state.templates`. Routers use `request.app.state.templates.TemplateResponse(...)`.
- **StaticFiles**: `app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")`. Directory `app/static/` is created in this task with a `.gitkeep`; Task 4 populates it with CSS/JS. If the directory does not exist, `StaticFiles` raises at startup — fast-fail per HARNESS §1.

## 7. Error-to-HTTP mapping table

| Exception (from auth_flow) | HTTP status | Response body | UI effect |
|---|---|---|---|
| `RateLimitError` | 429 | `{"error": "rate_limit"}` | login page shows "too frequent, retry in 60s" |
| `InvalidCodeError` | 400 | `{"error": "invalid_or_expired"}` | login page shows "验证码无效或已过期" |
| `ApprovalTokenError` | HTML 200 | renders `approval_error.html` | admin sees error page |
| `MailerError` | 503 | `{"error": "mail_send_failed"}` | login page shows retry prompt; transaction rolled back |
| `sqlalchemy.exc.IntegrityError` | 400 | `{"error": "bad_request"}` | rare (email-collision race, Task 2.3 §4) |
| no cookie / invalid cookie on protected route | 401 (JSON) or 303 (HTML) | `{"error": "unauthenticated"}` / redirect `/login` | — |

Exception handlers are registered either as per-route `try/except` inside handlers or as a module-level `@app.exception_handler(AuthError)`. Chosen: per-route try/except in `routers/auth.py` — the mapping differs per endpoint (e.g. `MailerError` is only raised by `request_code`, not `verify_code`), and a global handler would obscure that.

## 8. Field mapping table (/design-check Cat 1)

| Field | Input (HTTP) | Service call | Response / Cookie | UI display |
|---|---|---|---|---|
| email | JSON body (`request_code`, `verify_code`) | `request_login_code(email)` / `verify_login_code(email, code)` | status echo only (`{"status": ...}`) | login page input + pending message |
| code | JSON body (`verify_code`) | `verify_login_code(email, code)` | N/A (consumed server-side) | login page input box |
| session raw token | produced by `verify_login_code` return value | — | `Set-Cookie: method_session=<raw>; HttpOnly; SameSite=Lax; Max-Age=<ttl*86400>; Path=/` | HTTP-only cookie; UI sees only logged-in state |
| session cookie read | `Cookie(method_session)` | `validate_session_cookie(raw)` / `invalidate_session_cookie(raw)` | None / `Set-Cookie: method_session=; Max-Age=0` | session-driven page branching |
| approval raw token | URL query param `?token=<raw>` | `approve_user(raw)` | server-side → `approved.html` or `approval_error.html` | admin landing page |
| user email (on approval) | — (from `User` return) | — | rendered into `approved.html` | admin sees "{email} 已激活" |
| logout | no input | `invalidate_session_cookie(raw)` | `{"ok": true}` + clear cookie | login page |

## 9. Files created / modified

| Path | Action | Purpose |
|---|---|---|
| `app/routers/auth.py` | create | `POST /api/auth/request_code`, `/verify_code`, `/logout`; `GET /login`, `/`; `get_current_user`, `require_user` deps |
| `app/routers/admin.py` | create | `GET /admin/approve` |
| `app/main.py` | modify | include `auth` + `admin` routers; mount Jinja2 templates; mount `StaticFiles` at `/static` |
| `app/templates/base.html` | create | shared HTML shell + viewport meta |
| `app/templates/login.html` | create | login/register page (3 states: initial / code-sent / pending) |
| `app/templates/approved.html` | create | admin success landing |
| `app/templates/approval_error.html` | create | admin error landing |
| `app/templates/landing.html` | create | placeholder logged-in `/` body |
| `app/static/.gitkeep` | create | reserve directory; Task 4 fills CSS/JS |
| `tests/integration/__init__.py` | create | new integration package marker |
| `tests/integration/conftest.py` | create | `app_client` (httpx.AsyncClient) + fake mailer fixtures |
| `tests/integration/test_auth_endpoints.py` | create | HTTP-level tests (see §10) |

Note — `tests/integration/conftest.py` is not called out in the brief's file table but is required for `app_client`/mailer fixtures; flagging here for human review (§12 ambiguities).

## 10. Test plan (hint for /tester)

All tests live in `tests/integration/test_auth_endpoints.py` unless noted. Use `httpx.AsyncClient` driving the FastAPI app with an ASGI transport. Mailer is monkeypatched at `app.services.auth_flow.send_*` (the same call-seam the Task 2.3 unit tests use). DB is the same in-memory aiosqlite fixture as `tests/conftest.py`.

1. `test_post_request_code_new_user_returns_pending_and_sends_admin_email`
2. `test_post_request_code_active_user_returns_sent`
3. `test_post_request_code_admin_self_registration_returns_sent`
4. `test_post_request_code_pending_user_returns_pending`
5. `test_post_request_code_rejected_user_returns_rejected`
6. `test_post_request_code_rate_limit_returns_429`
7. `test_post_request_code_mailer_failure_returns_503_and_rolls_back`
8. `test_post_verify_code_correct_sets_cookie_returns_ok`
9. `test_post_verify_code_wrong_returns_400`
10. `test_post_verify_code_expired_returns_400`
11. `test_post_verify_code_reused_returns_400`
12. `test_post_logout_clears_cookie_and_deletes_session`
13. `test_post_logout_without_session_returns_401`
14. `test_get_admin_approve_valid_token_activates_user_and_renders_approved`
15. `test_get_admin_approve_expired_token_renders_error`
16. `test_get_admin_approve_used_token_renders_error`
17. `test_get_admin_approve_unknown_token_renders_error`
18. `test_get_login_renders_login_template` (asserts `<title>` + viewport meta present)
19. `test_get_root_redirects_to_login_when_not_authed`
20. `test_get_root_renders_placeholder_when_authed`
21. `test_full_flow_new_user_to_session` (register → mocked approve → verify code → cookie set → subsequent request finds user)
22. `test_admin_full_flow_self_registration_skips_approval`
23. `test_cookie_has_httponly_samesite_lax_not_secure` (pins HARNESS §4)
24. `test_csrf_same_origin_check_rejects_cross_origin` (spec §4.3 "校验 Origin 头")

Each test must import/exercise at least one of the files listed in §9 (design-coverage audit requirement from global CLAUDE.md Step 4).

## 11. Concurrency / security notes

- **Cookie flags**: always sourced from `auth_flow.COOKIE_FLAGS` (HARNESS §4: `HttpOnly=True`, `SameSite=Lax`, `Secure=False` until M5 HTTPS). Router must NOT hardcode.
- **CSRF**: All mutation endpoints are POST JSON with `SameSite=Lax`, so cross-site form posts cannot carry the cookie. Additional defense: an `Origin` header check on POST — if `Origin` is present and its scheme+host does not equal `settings.base_url`'s scheme+host, return 403 `{"error": "bad_origin"}`. `Origin` absent is allowed (some clients strip it). Implemented as a small dependency `verify_origin` on the three POST endpoints. Spec §4.3 bullet: "表单外调用全部 JSON，且会校验 Origin 头".
- **Rate-limit**: `/api/auth/request_code` rate-limit is enforced at the service layer (Task 2.3 §4, 60s window). The router's only job is to translate `RateLimitError` → HTTP 429. No separate router-level limit.
- **Enumeration**: `InvalidCodeError` always maps to the same body; the router does not log email on invalid-code paths at level above DEBUG.
- **No blocking I/O** in handlers; all DB + mailer calls are already async (Task 2.3 + Task 2.2).

## 12. Infrastructure dependency table (Cat 11)

| Dependency | Required by | Failure mode | Degradation |
|---|---|---|---|
| `app/services/auth_flow` | every endpoint | raises `AuthError` subclasses | mapped to HTTP per §7 |
| `Jinja2Templates(directory=...)` | GET /login, /admin/approve, / | missing template file → Jinja raises `TemplateNotFound` → FastAPI 500 | fail-fast on template syntax; missing-file fails on first render |
| `StaticFiles("/static", directory=...)` | mount at app startup | directory missing → `RuntimeError` at mount | create `app/static/` + `.gitkeep` in this task |
| `app/db.get_session` | every endpoint | SQLAlchemy raises → router 500 | propagate |
| `settings.session_ttl_days` | cookie `Max-Age` computation | missing → pydantic-settings raises at startup | fast-fail |
| `settings.base_url` | Origin check | empty → Origin check falls back to allow-all | acceptable for MVP; documented in §11 |
| Session-cookie secret | N/A — cookie value is a DB-backed hash lookup, not a signed payload. `settings.session_secret` is reserved for future CSRF token signing | — | — |

## 13. Open design ambiguities (for human review)

1. **`/` placeholder location**: chose to add `landing.html` now rather than leaving `/` as a 303 to `/login` for all users. Alternative: always 303 to `/login` when not logged in, but 303 to `/workspace` (Task 3.3) when logged in — since `/workspace` doesn't exist yet, a placeholder is needed. Confirm placeholder template vs. plain-text body is OK.
2. **`tests/integration/conftest.py`**: not listed in brief's file table but needed for `app_client` fixture. Flagged in §9.
3. **Origin check behavior when header absent**: allow (many legit clients strip `Origin`). Alternative: require `Origin` on POST and reject when absent. Chosen: permissive; `SameSite=Lax` is the primary CSRF control.
4. **401 vs 303 for HTML routes on `require_user`**: currently dispatched by `Accept` header. If the user is on a JSON-Accept client hitting an HTML route, they'd get 401 not 303 — acceptable because HTML routes `/` already handle no-auth via `get_current_user` returning None (never reaches `require_user`). `require_user` is only hit by API routes (logout + future research endpoints).
