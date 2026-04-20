# DESIGN CHECK REPORT — Issue #5 (Frontend UX Error Copy Refresh)

- Design doc: `/home/ubuntu/method-dev/docs/design/issue-5-error-copy.md`
- Skill spec applied: `/home/ubuntu/.claude/skills/design-check/SKILL.md` (14 categories)
- Codebase scanned: `/home/ubuntu/method-dev`
- Date: 2026-04-20

---

## FIELD MAPPING TABLE (Category 1)

The design's "primary data model" is the error-response envelope `{error, message}`. The mapping table below tracks every surface a single error code passes through.

| Field             | Defined in design | Source of truth (file) | JSONResponse body | HTTPException detail | Frontend renderer | Test asserts | Notes |
|-------------------|-------------------|------------------------|--------------------|----------------------|--------------------|---------------|-------|
| `error` (code)    | §3.2, §4.1        | `app/services/error_copy.py::ERROR_COPY` keys | YES (24 codes)     | YES (`not_found` via main.py exc handler, §7 注 1) | NO (do not concat raw) | tests/routers/* assert `body["error"]==<code>` | BC contract preserved |
| `message` (中文)  | §3.2, §4.1        | `app/services/error_copy.py::ERROR_COPY` values | YES (every JSONResponse + LimitExceededError detail) | YES (wrapped by main.py handler) | YES (preferred render via `showError`) | tests/routers/* assert `body["message"]==<copy>` | Single source of truth |
| HTTP status       | §4.1              | unchanged (24 codes; column 2 of §4.1 table) | preserved          | preserved            | n/a                | preserved by tests | "HTTP status code 不变" §3.2 |
| `code` key (legacy) | §3.2, §5         | `file_processor.LimitExceededError`         | RETIRED            | RETIRED              | n/a                | tests/services/* assert NOT in detail | BC-breaking but only internal consumer (research.py:121 bubble) |
| `error_message` (research_requests row → template) | §4.3, §5 | `research_requests.error_message` column writers (`research_runner` / `claude_runner`) — out of scope, only fallback added | n/a | n/a | template `history_detail.html:21` with `or "研究失败，原因未知，请重试"` | template render test asserts both branches | Out-of-scope flag for non-fallback path |
| frontend fallback (network / 5xx / unknown) | §4.2, §6.1 | `app/static/app.js::showError`/`showNetworkError` | n/a | n/a | YES | review-by-grep test (`tests/test_static_assets.py`) | 7 fallback rows in §4.2 |

All design rows have `error code` + `HTTP status` + `中文 message` + `where copy lives` filled. No `???` cells. **PASS.**

---

## Category-by-category results

| # | Category                                  | Result | Notes |
|---|-------------------------------------------|--------|-------|
| 1 | Field Completeness                        | PASS   | See above. 24 backend codes + 7 frontend fallbacks all mapped. |
| 2 | Interface / API Contract                  | PASS   | Response shape `{error, message, …}` defined §3.2. HTTP statuses preserved. No new params hallucinated; `JSONResponse(content=…)` is real FastAPI. |
| 3 | Data Type Contracts at Boundaries         | PASS   | `error: str`, `message: str` only. No new datetime/UUID/Decimal surfaces. Empty-`message` fallback explicitly defined (`message_for(code)` returns "操作失败，请稍后重试"). |
| 4 | SQL / Query Safety                        | PASS   | No SQL changes proposed. |
| 5 | Concurrency / Threading                   | PASS   | No new concurrency surface; this is a copy refactor. |
| 6 | Error Handling Design                     | PASS   | §6.1 helper centralizes failure rendering; design forbids bare `body.error` concat (anti-pattern explicitly named); §9 risk #3 covers "missing message" regression with a CI test (`test_all_routes_use_known_codes`). |
| 7 | Security                                  | PASS   | §3.2 BC contract keeps `error` machine code unchanged → no new auth surface. No secrets. Cookie/HTTPS unaffected. |
| 8 | Performance / Scalability                 | PASS   | Pure constants dict + helper function. O(1). No new I/O. |
| 9 | Integration Boundaries                    | PASS   | `file_processor.LimitExceededError` shape change is BC-breaking but only consumed by `research.py:121`; design §9 risk #2 calls out the grep-and-update plan. |
| 10| State Management                          | PASS   | `ERROR_COPY` is a module-level dict, immutable, single source of truth. |
| 11| Infrastructure Dependencies               | PASS   | No new infra deps. `app/services/error_copy.py` is in-process Python. |
| 12| End-to-End Scenarios                      | PASS   | §5 mandates integration tests for every error code (`tests/routers/test_auth_error_copy.py`, `tests/routers/test_research_error_copy.py`); §5 also covers a CI grep test (`tests/test_static_assets.py`) for the JS path. Manual smoke acknowledged as the e2e for `app.js`. |
| 13a| Config-Surface Coverage (BLOCKING)       | PASS   | Reverse-scan verified independently — see "Category 14 reverse-scan results" below. Every backend `{"error": "<code>"}` site appears in §4.1; every `app.js` `alert(...)` line is in §7 sweep table or explicitly retained. |
| 13| Operational Readiness                     | PASS   | No new env vars / dirs / services. |
| 14| User-Facing Copy (BLOCKING)               | PASS   | This issue *is* the Category-14 retro fix. Every code has Chinese copy (§4.1), copy居所 (`error_copy.py`) is named, anti-pattern is forbidden three layers deep (helper comment + sweep table + CI grep test), empty-state and 5xx/network fallbacks defined. |

---

## Category 14 reverse-scan results (mandatory raw paste)

### Scan A — backend `{"error": "<code>"}` literals

Command: `grep -rnE '\{"error":\s*"[a-z_]+"' app/routers app/services`

```
app/routers/auth.py:164:            content={"error": "rate_limit"},
app/routers/auth.py:170:            content={"error": "mail_send_failed"},
app/routers/auth.py:176:            content={"error": "bad_request"},
app/routers/auth.py:197:            content={"error": "invalid_or_expired"},
app/routers/auth.py:276:            content={"error": "unauthenticated"},
app/routers/auth.py:289:            content={"error": "bad_origin"},
app/routers/research.py:101:            content={"error": "empty_question"},
app/routers/research.py:106:            content={"error": "question_too_long"},
app/routers/research.py:111:            content={"error": "invalid_mode"},
app/routers/research.py:172:            status_code=500, content={"error": "internal"}
app/routers/research.py:364:        return JSONResponse(status_code=500, content={"error": "plan_missing"})
app/routers/research.py:502:            content={"error": "request_not_finalized"},
app/routers/research.py:509:            status_code=400, content={"error": "anchor_text_invalid"}
app/routers/research.py:513:            status_code=400, content={"error": "body_invalid"}
app/routers/research.py:520:            status_code=400, content={"error": "anchor_context_too_long"}
app/routers/research.py:533:            content={"error": "body_empty"},
app/routers/research.py:540:            status_code=500, content={"error": "internal"}
app/routers/research.py:651:            content={"error": "ai_reply_not_deletable"},
```

Plus the multiline literal at `app/routers/research.py:403` (`{"error": "request_busy", "message": "请求仍在处理中，请等它结束后再删除"}`) — already in design §4.1 row "request_busy".

Plus the legacy `code` key at `app/services/file_processor.py:128` (`detail={"code": code, "message": message}`) — design §3.2 + §5 explicitly retires this and migrates to `{"error": code, "message": …}`.

The string `"error":` in `app/services/comment_runner.py:167` is a docstring reference to the router behavior, not a runtime body — out of scope.

**Coverage cross-check vs design §4.1:**

| File:line                             | Code in source              | Row in design §4.1                       | Covered |
|---------------------------------------|-----------------------------|-------------------------------------------|---------|
| auth.py:164                           | rate_limit                  | rate_limit (auth.py:164)                  | YES     |
| auth.py:170                           | mail_send_failed            | mail_send_failed (auth.py:170)            | YES     |
| auth.py:176                           | bad_request                 | bad_request (auth.py:176)                 | YES     |
| auth.py:197                           | invalid_or_expired          | invalid_or_expired (auth.py:197)          | YES     |
| auth.py:276                           | unauthenticated             | unauthenticated (auth.py:276)             | YES     |
| auth.py:289                           | bad_origin                  | bad_origin (auth.py:289)                  | YES     |
| research.py:101                       | empty_question              | empty_question (research.py:101)          | YES     |
| research.py:106                       | question_too_long           | question_too_long (research.py:106)       | YES     |
| research.py:111                       | invalid_mode                | invalid_mode (research.py:111)            | YES     |
| research.py:172                       | internal                    | internal (research.py:172, 540)           | YES     |
| research.py:364                       | plan_missing                | plan_missing (research.py:364)            | YES     |
| research.py:403 (multiline literal)   | request_busy                | request_busy (research.py:403)            | YES     |
| research.py:502                       | request_not_finalized       | request_not_finalized (research.py:502)   | YES     |
| research.py:509                       | anchor_text_invalid         | anchor_text_invalid (research.py:509)     | YES     |
| research.py:513                       | body_invalid                | body_invalid (research.py:513)            | YES     |
| research.py:520                       | anchor_context_too_long     | anchor_context_too_long (research.py:520) | YES     |
| research.py:533                       | body_empty                  | body_empty (research.py:533)              | YES     |
| research.py:540                       | internal                    | internal (research.py:172, 540)           | YES     |
| research.py:651                       | ai_reply_not_deletable      | ai_reply_not_deletable (research.py:651)  | YES     |
| file_processor.py:128 (`code` key)    | (6 codes via LimitExceededError) | files_too_many / unsupported_type / empty_file / file_too_large / total_too_large / mime_mismatch (rows in §4.1) | YES — §3.2 + §5 explicitly migrate this site |
| HTTPException(404, "not_found") × 14  | not_found                   | not_found (research.py / history.py 多处) | YES — §7 注 1 wraps via global handler |

**No code-side error code is missing from the design's table.**

### Scan B — frontend `alert(... + body.error)`

Command: `grep -nE 'alert\([^)]*\+ *body\.error' app/static/`

```
(no matches — the literal regex from the skill spec returns 0)
```

**Note on the 0-match result:** the regex in the skill spec misses the actual occurrences because the source uses `... + (body.error || r.status)` (parenthesized). A loosened scan (broader pattern `body\.error|data\.error|resBody\.error` in `app/static/`) returns:

```
app/static/app.js:35:        if (!r.ok) { alert("发送失败：" + (body.error || r.status)); return; }
app/static/app.js:60:          alert("验证失败：" + (body.error || r.status));
app/static/app.js:261:        const msg = body.message || body.error || ("提交失败 (" + r.status + ")");
app/static/app.js:338:          setStatus("failed", data.error_message || "unknown error");
app/static/app.js:673:          alert("提交失败：" + (resBody.error || r.status));
```

**Coverage cross-check vs design §7 sweep:**

| File:line              | Current pattern                                     | Design §7 row | Disposition           |
|------------------------|-----------------------------------------------------|----------------|-----------------------|
| app/static/app.js:35   | `alert("发送失败：" + (body.error || r.status))`    | line 35        | replaced by `showError(body, r.status)` |
| app/static/app.js:60   | `alert("验证失败：" + (body.error || r.status))`    | line 60        | replaced by `showError(body, r.status)` |
| app/static/app.js:261  | `const msg = body.message || body.error || …`      | implicit (line 263 row in §7 says "上游 msg 计算改用 `body.message || …`") | covered, see §6.2 |
| app/static/app.js:338  | `data.error_message || "unknown error"`            | NOT in §7 — but this reads `error_message` (different field, sourced from `research_requests.error_message`) — design §4.3 + §8 explicitly call this out-of-scope (the row's source field is the runner's responsibility per HARNESS rule 1). | OUT-OF-SCOPE (declared) |
| app/static/app.js:673  | `alert("提交失败：" + (resBody.error || r.status))` | line 673       | replaced by `showError(resBody, r.status)` |

### Scan C — frontend `alert(... + data.error)`

Command: `grep -nE 'alert\([^)]*\+ *data\.error' app/static/`

```
(no matches)
```

The only `data.error*` occurrence is line 338 reading `data.error_message` — explicitly out-of-scope per design §4.3 (`history_detail.html` banner field).

### Scan D — full `alert(` enumeration in app.js

Command: `grep -nE 'alert\(' app/static/`

```
app/static/app.js:35:        if (!r.ok) { alert("发送失败：" + (body.error || r.status)); return; }
app/static/app.js:38:        else if (body.status === "rejected") alert("该邮箱已被拒绝");
app/static/app.js:39:      } catch (e) { alert("网络错误，请稍后再试"); }
app/static/app.js:60:          alert("验证失败：" + (body.error || r.status));
app/static/app.js:63:          alert("网络错误，请稍后再试");
app/static/app.js:173:      if (rejects.length) alert(rejects.join("\n"));
app/static/app.js:246:      if (!q) { alert("请输入研究问题"); return; }
app/static/app.js:263:        else alert(msg);
app/static/app.js:402:            () => { alert("复制失败，请手动选择"); }
app/static/app.js:450:          alert("请求仍在处理中，请等它结束后再删除");
app/static/app.js:452:          alert("记录不存在或已被删除");
app/static/app.js:454:          alert("删除失败 (" + r.status + ")");
app/static/app.js:458:        alert("网络错误，请稍后再试");
app/static/app.js:644:      if (!body) { alert("评论不能为空"); return; }
app/static/app.js:645:      if (!currentSelection) { alert("请先选中方案里的一段文字"); return; }
app/static/app.js:673:          alert("提交失败：" + (resBody.error || r.status));
app/static/app.js:675:          alert("当前请求还在生成中，请等它结束再评论");
app/static/app.js:677:          alert("登录已过期，请刷新页面");
app/static/app.js:679:          alert("提交失败 (" + r.status + ")");
app/static/app.js:682:        alert("网络错误，请稍后再试");
app/static/app.js:705:          alert("删除失败 (" + r.status + ")");
app/static/app.js:709:        alert("网络错误");
```

22 lines total. Cross-checked vs design §7 table:

| Line | In design §7 | Disposition matches grep |
|------|--------------|---------------------------|
| 35   | YES          | YES (replace with `showError`) |
| 38   | YES          | YES (keep — pure literal) |
| 39   | YES          | YES (replace with `showNetworkError`) |
| 60   | YES          | YES |
| 63   | YES          | YES |
| 173  | YES          | YES (keep — client-side) |
| 246  | YES          | YES (keep — client-side) |
| 263  | YES          | YES |
| 402  | YES          | YES |
| 450  | YES          | YES |
| 452  | YES          | YES |
| 454  | YES          | YES |
| 458  | YES          | YES |
| 644  | YES          | YES (keep — client-side) |
| 645  | YES          | YES (keep — client-side) |
| 673  | YES          | YES |
| 675  | YES          | YES |
| 677  | YES          | YES |
| 679  | YES          | YES |
| 682  | YES          | YES |
| 705  | YES          | YES |
| 709  | YES          | YES |

**Every alert in app.js is enumerated in §7. Zero misses.**

The design §5 line for `app/static/app.js` cites "17 处" alert paths to migrate plus several pure client-side / pure literal alerts (35/38/39/60/63/173/246/263/402/450/452/454/458/644/645/673/675/677/679/682/705/709 = 22 lines, of which §7 marks 5 as "保持" and the rest as migrate/replace). Math is consistent.

---

## BLOCKING

None.

## WARN

None.

## SUGGEST

- **(Category 14, optional)** Skill spec's reverse-scan regex `\+ *body\.error` failed to match `+ (body.error || ...)`. The design defends against this gap by adding `tests/test_static_assets.py` with a stronger pattern. SUGGEST upstreaming an improved skill regex (e.g. `\+ *\(?\s*\w*\.error\b`) so future design-checks aren't dependent on the design author noticing the gap.
- **(Category 6, optional)** §6.3 decides not to ship a frontend `messageForCode` dictionary. That's a clean call, but if Issue #5 lands and a future BC client *is* discovered later, reviving `messageForCode` would cost a new round trip. Document this fork point in `docs/TODO.md` so future maintainers find it.
- **(Category 12, optional)** §5 says `app.js` has no unit tests; review-by-grep covers regression of the anti-pattern but doesn't cover positive rendering. SUGGEST one Playwright/light browser smoke test for the critical "登录验证码失败" path — defer if cost is high, but flag in TODO.

---

## VERDICT: PASS

All 14 categories pass. The design's claimed inventory (§4.1 backend codes, §7 frontend alerts) is verified by independent grep. The Category 14 issue this design addresses is itself fully covered. Ready for Step 2b (Feishu human review).
