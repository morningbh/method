# Human Smoke — Issue #5 Error Copy Refresh

**Environment**: `https://method-dev.xvc.com` (dev @ port 8002, commit `05783c0`)
**Tester**: Boyu (project owner)
**Date**: 2026-04-21
**Result**: PASS — proceed with prod deploy

## Scenarios tested

| # | Scenario | Actual | Pass? |
|---|----------|--------|-------|
| 1 | Rate limit — 60s 内连点 2 次「发送验证码」 | `429 {"error":"rate_limit","message":"请求过于频繁，请稍后再试"}` (server-side curl smoke, confirmed by tester) | ✓ |
| 2 | Invalid code — 输错验证码 | User-attested (tester UI smoke) | ✓ |
| 3 | File too large — 上传 > 50MB | User-attested | ✓ |
| 4 | Too many files — 上传 > 20 个 | User-attested | ✓ |
| 5 | Not found — `/history/<bad-id>` | `{"error":"not_found","message":"记录不存在或已被删除"}` (tester attested this exact body) | ✓ |

## Server-side shape smoke (curl, 2026-04-21)

```
1) rate_limit: 429  {"error":"rate_limit","message":"请求过于频繁，请稍后再试"}
2) /history/<bad>: 401 {"error":"unauthenticated","message":"登录已过期，请刷新页面重新登录"}
   (unauthenticated path — expected 401 before reaching not_found)
```

Unified `{error, message}` shape present across auth / rate-limit / not_found / unauthenticated — no raw English codes leaking to user.

## Verdict

GREEN. Safe to run `/deploy-prod` WITHOUT `--skip-human-smoke`.

## Evidence inputs

- Dev service: `method-dev.service` running commit `05783c0` (post-restart verified active)
- Dev URL: `https://method-dev.xvc.com/api/health = 200`
- `BASE_URL=https://method-dev.xvc.com` (dev `.env`, gitignored — confirmed via `verify_origin` accepting same-host requests)
