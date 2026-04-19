# Task 2.3 — Auth Flow Pure Logic (design)

**Scope**: Issue #1 / Milestone M2. This document defines `app/services/auth_flow.py`,
the FastAPI-free, unit-testable layer that implements registration, login-code
verification, admin approval, and session cookie issuance. HTTP routes that call
this module are Task 2.4 and out of scope here.

## 1. Purpose

`auth_flow` turns the spec §4 state machine into a set of coroutines over an
`AsyncSession`. It owns all secrets-touching logic: generating login codes,
approval tokens, and session tokens, and storing only their salted sha256
hashes. Callers (routers) receive either a well-typed value (`"sent"`, a raw
cookie token, a `User`) or a typed exception — **never a silent fallback**.
This matches HARNESS §1 ("failures must never be silent") and CLAUDE.md's
silent-failure-prevention rule: no bare `except`, no `return None` to mask
errors (with one exception — `validate_session_cookie`, whose None return IS
the contract and is reached only after explicit checks).

The module is the single writer of `users`/`login_codes`/`sessions`/
`approval_tokens` rows for the auth flow. Cookie header assembly is Task 2.4;
this module only produces the raw token and enforces HARNESS §4 policy
(`HttpOnly`, `SameSite=Lax`; `Secure` added at M5) via a documented constant
so the router layer cannot drift.

## 2. Public API (contract)

```python
# app/services/auth_flow.py

class AuthError(Exception): ...
class RateLimitError(AuthError): ...
class InvalidCodeError(AuthError): ...
class ApprovalTokenError(AuthError): ...

# HARNESS §4 policy — routers import this rather than hardcoding flags.
COOKIE_FLAGS: dict = {"httponly": True, "samesite": "lax", "secure": False}
COOKIE_NAME: str = "method_session"

async def request_login_code(
    session: AsyncSession, email: str,
) -> Literal["sent", "pending", "rejected"]:
    """Drive spec §4.1 row-1. New email → create pending user + approval_token
    + admin email → "pending". Pending/rejected user → return status, send
    nothing. Active user → issue login_code + send mail → "sent". If the
    submitted email equals settings.admin_email and no row exists, short-circuit
    to status='active' and immediately issue a login_code (admin self-bootstrap).
    Raises RateLimitError if a login_codes row for this user exists with
    created_at > now - 60s (even if previous status was pending — applies only
    once user is active, see §4 below)."""

async def verify_login_code(
    session: AsyncSession, email: str, code: str,
) -> str:
    """Return raw session cookie token (43-char urlsafe b64, no padding).
    Marks matched login_code.used_at=now and inserts a sessions row whose
    token_hash is sha256(raw). Raises InvalidCodeError if (a) user not found or
    not active, (b) no matching unused, unexpired code, (c) lockout active
    (≥5 unused-and-not-yet-expired codes recorded within last 15 min for this
    user — see §4 "Option B" below)."""

async def approve_user(session: AsyncSession, raw_token: str) -> User:
    """Look up approval_tokens by sha256(raw_token). Raise ApprovalTokenError
    if not found / expired / already used. On success: set users.status='active',
    users.approved_at=now, approval_tokens.used_at=now, and send activation
    notice. Returns the updated User."""

async def validate_session_cookie(
    session: AsyncSession, raw_token: str,
) -> User | None:
    """Return the owning User if sha256(raw_token) matches a sessions row with
    expires_at > now, else None. Must NOT raise: middleware treats None as
    'redirect to /login'. Empty/malformed input also returns None."""

async def invalidate_session_cookie(
    session: AsyncSession, raw_token: str,
) -> None:
    """Delete the matching sessions row. No-op if not found. No raise."""
```

## 3. Hashing & token generation policy

| Artifact | Generation | Stored | TTL |
|---|---|---|---|
| Login code | `secrets.randbelow(10**6)` zero-padded to 6 digits | `sha256(code + salt)`, per-row 32-hex salt (`secrets.token_hex(16)`) | `settings.login_code_ttl_min` = 10 min |
| Session token | `secrets.token_urlsafe(32)` → strip `=` → 43 chars | `sha256(raw)` | `settings.session_ttl_days` = 30 d |
| Approval token | `secrets.token_urlsafe(32)` → strip `=` → 43 chars | `sha256(raw)` | `settings.approval_token_ttl_days` = 7 d |

All sha256 via `hashlib.sha256(x.encode("utf-8")).hexdigest()` → 64-char lower hex,
matching `String(64)` columns in `app/models.py`. Raw tokens are never logged
(per CLAUDE.md secrets rule); the module logs only the hash prefix (first 8
chars) for traceability.

**Decision — session tokens are unsalted.** Rationale: a 32-byte urlsafe
random already has 256 bits of entropy; per-row salt adds no guessing
resistance and complicates lookup (must scan rows). Login codes are salted
because 6 digits = 20 bits of entropy and a global rainbow table would be
trivial otherwise.

**Verification lookup for login codes.** Because each `login_codes` row has its own salt, the stored `code_hash` cannot be computed from input alone. `verify_login_code` therefore fetches all rows for the user where `used_at IS NULL AND expires_at > now`, and `hmac.compare_digest(row.code_hash, sha256(submitted + row.salt))` iterates until a match or exhaustion. Bounded at ≤5 rows by the lockout rule in §4.

## 4. Rate limiting & lockout

**`request_login_code` 60-second rate limit**: before issuing a new code for
an *active* user, query `SELECT 1 FROM login_codes WHERE user_id=:u AND
created_at > :cutoff LIMIT 1` with cutoff = now − 60s. If a row exists, raise
`RateLimitError`. Not applied to the pending/rejected/admin-bootstrap
branches (they don't create codes). Matches spec §4.3.

**`verify_login_code` 5-wrong-in-15min lockout — Option B (chosen)**: before
attempting to match, count login_codes rows for this user where
`created_at > now - 15 min AND used_at IS NULL AND expires_at < now` (i.e.
issued-then-expired-unused within window). If count ≥ 5, raise
`InvalidCodeError` without checking the code. A correct match resets the
window implicitly (that row gets `used_at` stamped, dropping out of the
"unused" count).

> *Rejected alternative (Option A — "failed attempts counter" column on
> users)*: requires a migration and is harder to reason about across
> request_code→verify_code racing. Option B uses only existing columns.
> *Caveat*: a user who requests 5 codes legitimately but never uses any will
> lock themselves out for 15 min. Acceptable — request_code is already 60s
> rate-limited, so reaching 5 unused codes takes ≥ 4 min of deliberate action.

**Race window note.** Two concurrent `request_login_code` calls for the same active user may each pass the 60s pre-check and insert two `login_codes` rows. Accepted trade-off: the lockout counter absorbs this, and low-concurrency deployment (one FastAPI worker per HARNESS §5) makes it unlikely. If the new-user branch races, the `UNIQUE(users.email)` constraint raises `IntegrityError`, which the router translates to HTTP 400.

## 5. State machine (restatement of spec §4.1 in function terms)

| Trigger | Entry state | Function | Exit state | Side effects |
|---|---|---|---|---|
| visitor submits email | no user row | `request_login_code` | user(pending) + approval_token row | email to admin; return `"pending"` |
| visitor submits email | user(pending) or (rejected) | `request_login_code` | unchanged | none; return `status` |
| visitor submits email | user(active) | `request_login_code` | + login_codes row | email to user; return `"sent"` |
| admin email submits | no user row | `request_login_code` (short-circuit) | user(active) + login_codes row | email to admin himself; return `"sent"` |
| admin clicks link | approval_token unused & not expired | `approve_user` | user→active; token→used | activation notice email; return `User` |
| user submits code | user(active), valid code | `verify_login_code` | + sessions row; code→used | return raw token |
| browser sends cookie | sessions row valid | `validate_session_cookie` | unchanged | return `User` |
| logout | any | `invalidate_session_cookie` | sessions row deleted | return `None` |

## 6. Error handling boundaries (informs Task 2.4)

| Function | Exception | Router should translate to |
|---|---|---|
| `request_login_code` | `RateLimitError` | HTTP 429 `{"error": "rate_limit"}` |
| `request_login_code` | `mailer.MailerError` (bubbled) | HTTP 503 `{"error": "mail_send_failed"}`, rollback |
| `verify_login_code` | `InvalidCodeError` | HTTP 400 `{"error": "invalid_or_expired"}` (deliberately does not distinguish wrong-code / expired / lockout — prevents enumeration) |
| `approve_user` | `ApprovalTokenError` | HTML page "链接无效或已过期" (spec §4.1) |
| `validate_session_cookie` | — | never raises; `None` → 302 `/login` |
| `invalidate_session_cookie` | — | never raises |

## 7. Timezone & datetime policy

**Decision — naive UTC.** All `datetime` written to DB uses
`datetime.now(timezone.utc).replace(tzinfo=None)`. The SQLite column type is
`DATETIME` (SQLAlchemy `DateTime` without `timezone=True`) matching existing
`app/models.py`. All comparisons (`expires_at > now`) are naive-naive. A single
module-private helper `_utcnow() -> datetime` centralises this so future
migration to tz-aware is a one-line change. No `datetime.utcnow()` —
deprecated in Python 3.12.

## 8. Field mapping table (design-check Category 1)

| Field | Input | Storage (table.col) | Query return | Hash policy |
|---|---|---|---|---|
| email | `request_login_code(email)`, `verify_login_code(email)` | `users.email` | N/A (normalised to lowercase on read) | plain — Write path lowercases before insert; admin-bootstrap comparison (`email == settings.admin_email`) also lowercases both sides. Rationale: `users.email` is `UNIQUE` without `COLLATE NOCASE`. |
| user_status | computed in `request_login_code` | `users.status` | returned as `"sent"`/`"pending"`/`"rejected"` | enum |
| login_code | generated in `request_login_code` | `login_codes.code_hash` + `login_codes.salt` | N/A (sent via email) | `sha256(code + salt)` |
| login_code.expires_at | `now + login_code_ttl_min` | `login_codes.expires_at` | N/A | naive UTC |
| session_token | generated in `verify_login_code` | `sessions.token_hash` | returned to caller as raw 43-char b64 | `sha256(raw)` |
| session_token.expires_at | `now + session_ttl_days` | `sessions.expires_at` | validated in `validate_session_cookie` | naive UTC |
| approval_token | generated in `request_login_code` (pending path) | `approval_tokens.token_hash` | embedded as `?token=<raw>` in admin email URL | `sha256(raw)` |
| approval_token.expires_at | `now + approval_token_ttl_days` | `approval_tokens.expires_at` | checked in `approve_user` | naive UTC |
| cookie flags | constant `COOKIE_FLAGS` in this module | N/A | imported by router | — |

## 9. Files created / modified (tester coverage audit binding)

| Path | Action | Purpose |
|---|---|---|
| `app/services/auth_flow.py` | create | All public functions + exceptions defined in §2 |
| `tests/unit/test_auth_flow.py` | create | TDD tests per §11 |

Routes (`routers/auth.py`, `routers/admin.py`), HTML templates, and
integration tests are Task 2.4, not 2.3.

## 10. Dependencies on existing code

- `app/models.py` — `User`, `LoginCode`, `Session`, `ApprovalToken`
- `app/services/mailer.py` — `send_login_code(to, code)`,
  `send_approval_request(admin_email, user_email, approve_url)`,
  `send_activation_notice(to)`, `MailerError`
- `app/config.py` — `settings.admin_email`, `settings.login_code_ttl_min`,
  `settings.approval_token_ttl_days`, `settings.session_ttl_days`,
  `settings.base_url`
- stdlib — `secrets`, `hashlib`, `datetime`, `typing.Literal`
- SQLAlchemy 2.x async — `AsyncSession`, `select`, `delete`, `func`

No FastAPI import (enforced by HARNESS component map: services must import
without FastAPI).

### 10.1 Infrastructure dependency table (design-check Category 11)

| Dependency | Required by | Failure mode | Degradation |
|---|---|---|---|
| SQLite via `app/db.py` | all queries | SQLAlchemy raises | propagate → router 500 |
| SMTP (Gmail) via `aiosmtplib` | send_login_code, send_approval_request, send_activation_notice | `MailerError` after 3 retries (1s/2s/4s backoff) | §6 → HTTP 503, transaction rolled back by router |
| `settings.admin_email` | new-user branch, admin-bootstrap | missing env var → pydantic fails at startup | fast-fail |
| `settings.base_url` | approve-url composition | empty → email link broken | not validated; manual smoke-test covers |
| `settings.login_code_ttl_min`, `session_ttl_days`, `approval_token_ttl_days` | token expiry | missing env → pydantic fails at startup | fast-fail |

## 11. Test plan (hint for /tester)

Every public function and every raised exception gets a test. Test file:
`tests/unit/test_auth_flow.py`. Fixture `db_session` provides an in-memory
aiosqlite session with `init_db()` applied (see `tests/conftest.py`). Mailer
is monkeypatched at the module level (`app.services.auth_flow.send_*`) to
record calls without hitting SMTP.

1. `test_request_login_code_new_user_creates_pending` — row inserted,
   approval_token row inserted, admin email sent, returns `"pending"`.
2. `test_request_login_code_admin_short_circuit_activates_directly` — email
   == `settings.admin_email`, returns `"sent"`, user row has `status='active'`.
3. `test_request_login_code_pending_returns_pending_no_email` — no new
   approval_token, no mailer call.
4. `test_request_login_code_rejected_returns_rejected_no_email`.
5. `test_request_login_code_active_sends_code` — login_codes row inserted,
   `send_login_code` called with plaintext 6-digit code.
6. `test_request_login_code_rate_limit_within_60s` — second call within 60s
   raises `RateLimitError`; no new login_codes row.
7. `test_verify_login_code_success_returns_token_and_marks_used` — returned
   token is 43 chars urlsafe; sessions row exists; login_code.used_at set.
8. `test_verify_login_code_wrong_code_raises_invalid`.
9. `test_verify_login_code_expired_raises_invalid` — expires_at in past.
10. `test_verify_login_code_reused_raises_invalid` — used_at already set.
11. `test_verify_login_code_lockout_after_5_wrong` — insert 5 expired-unused
    login_codes for user in last 15 min; next call raises `InvalidCodeError`
    even with correct code.
12. `test_approve_user_success_activates_and_sends_notice`.
13. `test_approve_user_expired_token_raises`.
14. `test_approve_user_reused_token_raises`.
15. `test_approve_user_unknown_token_raises`.
16. `test_validate_session_cookie_valid_returns_user`.
17. `test_validate_session_cookie_invalid_returns_none` — also covers empty
    string and malformed input; asserts no exception raised.
18. `test_validate_session_cookie_expired_returns_none`.
19. `test_invalidate_session_cookie_removes_row` — also: second call is
    no-op (no raise).
20. `test_cookie_flags_policy_documented` — asserts
    `auth_flow.COOKIE_FLAGS == {"httponly": True, "samesite": "lax",
    "secure": False}` and `COOKIE_NAME == "method_session"`. Pins HARNESS §4.
21. **Cross-reference.** Integration tests exercising `auth_flow` through the router layer (`/api/auth/*`, `/admin/approve`) belong to Task 2.4. Real-SMTP E2E belongs to Task 2.5 and is guarded by `RUN_E2E=1` per HARNESS §5. This test file (`tests/unit/test_auth_flow.py`) must NOT hit real SMTP — `send_*` functions are monkeypatched at module level.

## 12. Security notes

- **No silent failure**: every error path raises a typed `AuthError` subclass
  or returns a contractually-defined sentinel (`None` for
  `validate_session_cookie`). No bare `except:` or `except Exception: pass`.
  HARNESS §1 applies: if a DB write fails mid-flow (e.g. mailer raises after
  login_code insert), the SQLAlchemy transaction is rolled back by the caller
  — this module does not `session.commit()` itself, it only flushes; Task 2.4
  owns the transaction boundary.
- **Constant-time code comparison**: use `hmac.compare_digest(stored_hash,
  sha256(submitted + salt))` to block timing side channels on the hash
  compare in `verify_login_code`. Cheap, standard.
- **No plaintext secrets in logs or errors**: `InvalidCodeError` carries
  only the string `"invalid_or_expired"` (the string the router emits);
  never the submitted code, never the stored hash. Raw session/approval
  tokens are logged only as `hash_prefix=<first-8-hex>`.
