# Issue #3 — M4 Frontend UI (Tasks 4.1 + 4.2 + 4.3)

Date: 2026-04-19
Status: Draft (pre-review)
Scope: Milestone 4 — consolidated frontend. History routes + templates (4.1), mobile polish + SSE client (4.2), workspace index page + upload UI (4.3). After M4 a user can register → login → submit research → watch streaming result → see past history → download markdown in one browser session.

---

## 1. Purpose

Minimal but complete browser UI. M3 shipped all backend JSON/SSE endpoints; the app's `/` still renders `landing.html` (placeholder) and there is no HTML client for the research/history flow. M4 fills exactly that gap and nothing else.

Non-goals: realtime history refresh, settings page, password/PWA/dark-mode, admin dashboard. Spec §14 YAGNI remains in force. Deployment (M5) is separate.

---

## 2. New HTTP surface

A new `app/routers/history.py` owns three HTML routes plus one JSON list endpoint. The root `GET /` handler moves out of `auth.py` into `history.py` because its "authed → workspace" branch now needs the `index.html` template (workspace), not `landing.html` (placeholder, removed).

All four endpoints require a valid session; `require_user` is reused from `auth.py`. Ownership-scoped loads follow the same pattern as `research.py` §2 (missing + cross-user share 404, no enumeration oracle).

### 2.1 `GET /` (moved)

- Unauthed → `RedirectResponse("/login", 303)` (unchanged from current behaviour).
- Authed → `TemplateResponse("index.html", {"title": "Method", "user": user})`.
- Replaces the `landing.html`-rendering branch currently in `app/routers/auth.py::root`. The old handler is removed; `landing.html` is deleted.

### 2.2 `GET /history`

- `TemplateResponse("history.html", {"title": "Method — 历史", "user": user, "items": items})` where `items` is a list of dicts identical in shape to §2.4 (same SQL query — template iterates, JSON endpoint serialises).
- Items ordered `created_at DESC`. No pagination in M4 (spec §14 / scope note in §12).

### 2.3 `GET /history/<id>`

- Ownership-scoped `SELECT` on `research_requests` + associated `uploaded_files`; missing or cross-user → `HTTPException(404)`.
- Renders `history_detail.html` with context:
  - `request_id: str`
  - `question: str`
  - `status: Literal["pending","running","done","failed"]`
  - `files: list[{"name": str}]` (name only — stored paths never leak to the browser)
  - `error_message: str | None` (only populated when `status == "failed"`; per HARNESS §1 we know it is non-empty there)
  - `created_at_iso: str`, `completed_at_iso: str | None`
- Template decides client behaviour from `status` (see §6).

### 2.4 `GET /api/history`

- Returns JSON `{"items": [...]}` for the current user, sorted newest-first.
- Item shape: `{"request_id": str, "question": str, "status": str, "created_at": ISO-8601, "completed_at": ISO-8601 | null, "n_files": int, "cost_usd": null}`.
- `cost_usd` is always `null` in M4 — not yet persisted (spec §12 open question resolved in M3 design §9). Keeping the key present so M5+ can fill it without a client change.
- `n_files` computed via correlated subquery or a `LEFT JOIN … GROUP BY request_id` in one round-trip; the template uses the same query (§2.2).

---

## 3. Templates

All templates extend `base.html`. Jinja2 autoescape is on by default (FastAPI `Jinja2Templates` default), so all user text (`question`, email, filenames, error_message) is HTML-escaped automatically. `|safe` is never used on user input.

### 3.1 `base.html` (modified)

Add:
- `<link rel="stylesheet" href="/static/style.css">` in `<head>`.
- Viewport meta is already present (line 5) — keep.
- A `{% block topbar %}{% endblock %}` placeholder before `<main>`. `login.html` leaves it empty; the three authed templates fill it with the shared top bar markup (`Method | {{ user.email }} | 历史 | 登出`). The logout link POSTs via JS (fetch) and redirects client-side — no `<form method=post>` in the topbar to keep markup uniform.

### 3.2 `index.html` (new — workspace, spec §7.2 B)

```html
{% extends "base.html" %}
{% block topbar %}{% include "_topbar.html" %}{% endblock %}
{% block content %}
<section class="workspace">
  <h1>帮你设计研究计划</h1>
  <form id="research-form" enctype="multipart/form-data">
    <textarea id="question" name="question" data-autofocus required
              placeholder="把研究问题写在这里..."></textarea>
    <div id="drop-zone" class="drop-zone">
      <span class="dz-desktop">📎 拖拽上传资料，或 </span>
      <button type="button" id="pick-files">选择文件</button>
      <input id="file-input" type="file" name="files"
             accept=".md,.txt,.pdf,.docx" multiple hidden>
      <p class="hint">支持 .md / .txt / .pdf / .docx · 最多 20 个 · 单文件 ≤ 30 MB</p>
    </div>
    <ul id="file-chips" class="chips"></ul>
    <button type="submit" class="primary" id="submit-btn">生成研究方案</button>
  </form>
</section>
{% endblock %}
```

The `data-autofocus` attribute is read by `app.js`; JS conditionally calls `.focus()` only when `window.innerWidth >= 768` (spec §7.3 — desktop only; avoid iOS keyboard pop on load).

### 3.3 `history.html` (new — list, spec §7.2 D)

`{% for item in items %}` renders a `<a class="card" href="/history/{{ item.request_id }}">` per request. Card body:

```
<span class="status-dot status-{{ item.status }}">●</span>
<span class="question">{{ item.question[:80] }}{% if item.question|length > 80 %}…{% endif %}</span>
<div class="meta">{{ item.created_at_display }} · {{ item.n_files }} files · {{ item.cost_display }}</div>
```

`cost_display` is `"$0.00"` when `cost_usd` is null (M4) — the card still renders cleanly. `status-failed` gets a red dot (CSS class → `color: #B91C1C`); `status-done` green; `status-pending`/`running` amber. Empty list → `<p class="empty">还没有研究记录</p>`.

Dates are rendered server-side as Beijing time string (spec §13 Q7 resolved in favour of UTC-stored + Beijing-display). A `format_beijing(dt) -> str` helper lives in `app/routers/history.py` (single call site; no premature shared util).

### 3.4 `history_detail.html` (new — detail + SSE client, spec §7.2 C)

```html
{% extends "base.html" %}
{% block topbar %}{% include "_topbar.html" %}{% endblock %}
{% block content %}
<section class="detail" data-request-id="{{ request_id }}"
                       data-initial-status="{{ status }}">
  <a class="back" href="/history">← 返回历史</a>
  <h2>研究问题</h2>
  <blockquote class="question">{{ question }}</blockquote>
  {% if files %}
  <p class="uploaded-files">上传资料：
    {% for f in files %}{{ f.name }}{% if not loop.last %}, {% endif %}{% endfor %}
  </p>
  {% endif %}
  <div class="status-row">
    <span class="status-indicator status-{{ status }}">
      {% if status in ("pending","running") %}● 生成中{% elif status == "done" %}● 已完成{% else %}✗ 失败{% endif %}
    </span>
    <button id="copy-btn" {% if status != "done" %}disabled{% endif %}>复制</button>
    <a id="download-btn" class="button" href="/api/research/{{ request_id }}/download"
       {% if status != "done" %}aria-disabled="true" tabindex="-1"{% endif %}>下载 .md</a>
  </div>
  {% if status == "failed" %}
  <div class="error-banner">{{ error_message }}</div>
  {% endif %}
  <article id="markdown" class="markdown"></article>
</section>
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/app.js"></script>
{% endblock %}
```

The anchor "download" styled as a button is disabled via `aria-disabled` + CSS `pointer-events: none`; the true guard is the server: `GET /api/research/<id>/download` already returns 404 unless `status == "done"` (research.py §334).

### 3.5 `_topbar.html` (new — shared partial)

```html
<header class="topbar">
  <a class="brand" href="/">Method</a>
  <span class="email" title="{{ user.email }}">{{ user.email }}</span>
  <a href="/history" class="nav-history">历史</a>
  <button id="logout-btn" class="nav-logout">登出</button>
</header>
```

---

## 4. Static assets

### 4.1 `app/static/style.css` (new)

One file; no preprocessor. Sections in order: reset, tokens (spec §7.1 palette as CSS custom properties), layout primitives (.topbar, .workspace, .card, .chips, .markdown), state classes (.status-*, .error-banner), media query `@media (max-width: 767px)` that implements the table in spec §7.3.

Concrete rules (non-exhaustive, the ones that are load-bearing for tests):

- `body { background: #FAFAF7; color: #1F2937; font: 1rem/-apple-system,"Segoe UI","PingFang SC",sans-serif; }`
- `.topbar { position: sticky; top: 0; display: flex; gap: 16px; padding: 12px 20px; }`
- `textarea, input { font-size: 16px; min-height: 44px; }` (spec §7.3 rules 3 + 4)
- `button { padding: 12px 20px; min-width: 44px; min-height: 44px; }`
- `@media (max-width: 767px) { .email { max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .workspace textarea { width: calc(100% - 32px); } #submit-btn { position: sticky; bottom: 0; width: 100%; } .dz-desktop { display: none; } .markdown pre { overflow-x: auto; } .markdown table { display: block; overflow-x: auto; } }`

No dark mode. No animations beyond a 150ms button hover.

### 4.2 `app/static/app.js` (new, single file)

Pure ES2020. No modules, no bundler. Uses DOM-level feature detection (`if (document.getElementById("research-form"))`) to scope behaviour per page.

Four behaviours:

1. **Login page state machine** — the existing `login.html` already has three `<section data-state>` blocks; `app.js` wires form submits to `/api/auth/request_code` → `/api/auth/verify_code` and toggles `hidden` on the sections based on response `status`. Enhances today's hand-written inline behaviour (the template currently has no script); after M4 `login.html` gets `<script src="/static/app.js"></script>` appended.
2. **Index (`#research-form`)** — bind click on `#pick-files` to `#file-input.click()`; drag-drop handlers on `#drop-zone` (`dragover`/`drop` preventDefault, push files into `FileList` via `DataTransfer`); render `#file-chips` from current `input.files`; chip × button clears one; submit → `fetch("/api/research", {method:"POST", body: FormData(form)})` → `201 {request_id}` → `location.assign("/history/" + request_id)`. On non-2xx, append red message under the form.
3. **History detail (`.detail`)** — reads `data-request-id` + `data-initial-status`; if initial status is `pending`/`running`, call `connectSSE(id)`; if `done`, `fetch("/api/research/" + id)` and render `data.markdown`; if `failed`, no fetch (template already shows `error_message` banner). Copy button writes `#markdown.innerText` to clipboard via `navigator.clipboard.writeText`.
4. **Topbar logout** — `#logout-btn` → `fetch("/api/auth/logout", {method:"POST"})` → `location.assign("/login")`.

Markdown rendering uses global `marked.parse(src, {gfm: true, breaks: false, mangle: false, headerIds: false})`. XSS defence: pass `{sanitize: true}` **not** available in marked v12 (it was removed). Instead, configure `marked` with a minimal `hooks.postprocess` that passes output through `DOMPurify`? No — adds 20 KB. Acceptable alternative given threat model: user is rendering the assistant's own output, not arbitrary third-party content; session cookie is `HttpOnly`; CSRF is covered by `SameSite=Lax` + origin check. Documented risk in §11.

### 4.3 `app/static/vendor/marked.min.js` (new)

- Source: `https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js`. Downloaded once, committed to the repo. Vendoring rationale: no CDN dependency at runtime (offline-safe, no extra DNS/TLS handshake, no supply-chain surprise between releases).
- Size: ~40–60 KB (source-of-truth: jsdelivr file at time of vendor). Unit test asserts `20 KB ≤ size ≤ 120 KB` to catch "forgot to download / downloaded an HTML error page" failures.
- Integrity: we do **not** add SRI hash in the template since the file is served by our own `/static/` mount; tampering would require filesystem access, at which point SRI is moot.

---

## 5. Mobile responsive (spec §7.3)

Implemented entirely in `style.css` + one JS branch. Hard rules checklist:

1. Viewport meta — already in `base.html` line 5. Test asserts presence.
2. Single breakpoint at 768px — `@media (max-width: 767px)` only.
3. Touch targets ≥ 44×44 — enforced via `button { min-width:44px; min-height:44px; }`.
4. Inputs ≥ 16px — enforced via `textarea, input { font-size: 16px; }`.
5. Email truncation — `.email { max-width: 120px; text-overflow: ellipsis; }` in the mobile query; full email remains in the `title=""` attribute for long-press.
6. Submit button sticky-bottom on mobile — `position: sticky; bottom: 0; width: 100%;`.
7. Markdown code blocks + tables `overflow-x: auto`.
8. `data-autofocus` conditional: JS `if (window.innerWidth >= 768) el.focus();`.

Not implemented (spec §7.3 "已知限制"): Android Chrome large-file white-screen (accepted degradation); iOS SSE background loss (handled by polling fallback in §6).

---

## 6. SSE client-side behaviour

```javascript
function connectSSE(requestId) {
  const es = new EventSource(`/api/research/${requestId}/stream`);
  es.addEventListener('delta', (ev) => {
    const {text} = JSON.parse(ev.data);
    appendMarkdown(text);              // append to a buffer, re-render
  });
  es.addEventListener('done', (ev) => {
    const {markdown} = JSON.parse(ev.data);
    renderFinalMarkdown(markdown);
    es.close();
    updateStatus('done');
  });
  es.addEventListener('error', (ev) => {
    // Server-sent error event (ev.data is JSON per research.py _sse_frame)
    let msg = 'unknown error';
    try { msg = JSON.parse(ev.data).message; } catch { /* no payload */ }
    updateStatus('failed', msg);
    es.close();
  });
  es.onerror = () => {
    // EventSource transport error (no ev.data). iOS Safari background tab
    // often lands here. Fall back to polling.
    if (es.readyState === EventSource.CLOSED) {
      setTimeout(() => pollForResult(requestId), 2000);
    }
  };
}

function pollForResult(requestId) {
  fetch(`/api/research/${requestId}`).then(r => r.json()).then(data => {
    if (data.status === 'done') {
      renderFinalMarkdown(data.markdown || '');
      updateStatus('done');
    } else if (data.status === 'failed') {
      updateStatus('failed', data.error_message || 'unknown error');
    } else {
      setTimeout(() => pollForResult(requestId), 3000);
    }
  });
}
```

Distinguishing server-sent `event: error` from transport error: the browser dispatches the typed `error` listener (with `ev.data`) for server frames and the generic `onerror` (no payload, `readyState === CLOSED`) for transport failures. We parse `ev.data` in the listener but guard with try/catch — onerror handler never dereferences `ev.data`.

Polling stops naturally when `data.status` reaches `done` or `failed`. No hard cap; `CLAUDE_TIMEOUT_SEC=600` on the backend guarantees termination.

---

## 7. Field mapping

| Field | Input (HTTP) | Server rendering | Client rendering | Source |
|---|---|---|---|---|
| `question` | textarea → multipart form | stored as-is (strip); autoescape on render | `<blockquote>` in detail, `:80` + ellipsis in list | user |
| `files[]` | multipart | stored via `file_processor`; only `original_name` surfaced | chip list on index, comma list on detail | user |
| `markdown` | SSE `delta`/`done` or `/api/research/<id>.markdown` | read from `plan_path` | `marked.parse()` into `<article id="markdown">` | server/claude |
| `status` | `data-initial-status` + SSE events + polling | template `{% if %}` branches | `.status-*` class swap + label text | server |
| `error_message` | `/api/research/<id>.error_message` + SSE `error` event | red banner in template | `.error-banner` textContent | server (HARNESS §1 non-empty) |
| `cost_usd` | `/api/history[].cost_usd` | always null in M4 | rendered `$0.00` / hidden | deferred |
| `n_files` | `/api/history[].n_files` | SQL count | list card meta | server |
| `created_at` | `/api/history[].created_at` | Beijing time string | list card meta | server |

---

## 8. Files created/modified

| Path | Action | Purpose |
|---|---|---|
| `app/routers/history.py` | create | `/`, `/history`, `/history/<id>`, `/api/history` |
| `app/templates/index.html` | create | workspace page (spec §7.2 B) |
| `app/templates/history.html` | create | list page (spec §7.2 D) |
| `app/templates/history_detail.html` | create | detail page + SSE client (spec §7.2 C) |
| `app/templates/_topbar.html` | create | shared topbar partial |
| `app/templates/base.html` | modify | add `<link>` to `/static/style.css`, `{% block topbar %}` |
| `app/templates/landing.html` | delete | replaced by `index.html` |
| `app/static/style.css` | create | spec §7.1 + §7.3 |
| `app/static/app.js` | create | login FSM, index upload, detail SSE, logout, copy |
| `app/static/vendor/marked.min.js` | create | markdown renderer (vendored from jsdelivr@12.0.0) |
| `app/main.py` | modify | `include_router(history.router)`; order: history BEFORE auth so `/` resolves to history's handler |
| `app/routers/auth.py` | modify | delete `root()` handler (lines 256–268); keep `login_page` |
| `app/templates/login.html` | modify | append `<script src="/static/app.js"></script>` |
| `tests/integration/test_history_endpoints.py` | create | routes + templates |
| `tests/integration/test_index_page.py` | create | index template structure + static reachability |
| `tests/unit/test_static_assets.py` | create | marked.min.js size + signature |

`main.py` router order matters: FastAPI resolves by first-match. `history.router` must be included before `auth.router` because `auth.router` still registers `GET /login` (kept) — no collision — but if the `/` handler in auth were left in place it would shadow. Remove `auth.router.root` explicitly; don't rely on ordering to mask it.

---

## 9. Test plan (coverage hint for /tester)

Integration tests (no JS execution — HTML structure + endpoint contracts):

1. `test_get_root_authed_renders_index_with_textarea`
2. `test_get_root_unauthed_redirects_to_login`
3. `test_get_history_lists_user_research` — 2 requests seeded, both cards rendered in order
4. `test_get_history_empty_state` — new user, renders "还没有研究记录"
5. `test_get_history_cross_user_isolation` — alice seeds, bob GETs `/history`, sees empty
6. `test_get_api_history_returns_json_list` — shape includes `request_id, question, status, created_at, completed_at, n_files, cost_usd`
7. `test_get_api_history_ordered_newest_first`
8. `test_get_api_history_cost_usd_always_null_in_m4`
9. `test_get_history_detail_shows_question_and_files`
10. `test_get_history_detail_includes_sse_url_for_pending` — HTML contains `data-initial-status="pending"`
11. `test_get_history_detail_includes_download_button_when_done` — no `aria-disabled`
12. `test_get_history_detail_hides_download_button_when_pending` — `aria-disabled="true"`
13. `test_get_history_detail_shows_error_banner_when_failed` — banner contains `error_message` text
14. `test_get_history_detail_cross_user_returns_404`
15. `test_get_history_detail_404_for_unknown_id`
16. `test_base_html_includes_viewport_meta` — GET `/login` HTML contains the meta tag
17. `test_static_style_css_reachable` — GET `/static/style.css` → 200, `text/css`
18. `test_static_app_js_reachable` — GET `/static/app.js` → 200, `application/javascript` (or `text/javascript`)
19. `test_static_marked_min_js_reachable` — GET `/static/vendor/marked.min.js` → 200
20. `test_index_html_has_file_drop_zone` — `id="drop-zone"`, `id="file-input"`, `accept` attr
21. `test_index_html_has_question_textarea` — `id="question"`, `required`, `data-autofocus`
22. `test_index_html_form_targets_api_research` — form has no `action` (JS intercepts); presence of `id="research-form"` plus `<script src="/static/app.js">` (contract: JS POSTs to `/api/research`)
23. `test_topbar_logout_button_present_when_authed`
24. `test_history_detail_escapes_question_html` — seeded question `<script>alert(1)</script>` renders as `&lt;script&gt;` in response body

Unit tests:

25. `test_marked_min_js_exists_and_reasonable_size` — file on disk, 20 KB ≤ size ≤ 120 KB, first 200 bytes contain `marked` and `function` or `=>`
26. `test_format_beijing_returns_expected_string` — `format_beijing(datetime(2026,4,19,7,2,0))` == `"2026-04-19 15:02"`

---

## 10. Infra dependencies

| Dep | Failure mode | Degradation |
|---|---|---|
| `marked.min.js` file on disk | 404 | Markdown displays as raw text in `<pre>` fallback (client catches missing `window.marked` → `<pre>` wrap). Test #19 + #25 catch at build. |
| `StaticFiles` mount | missing dir | startup fails — already covered by M1 smoke test |
| Jinja2 template missing | 500 at request time | caught by tests #1, #3, #9 |
| `navigator.clipboard` | some browsers | Copy button falls back to selecting `#markdown` text; user Ctrl+C |
| `EventSource` | unsupported (no browser targeted lacks it — Safari/Chrome/Firefox all OK) | not covered |

---

## 11. Security

- Jinja2 autoescape is enabled by default; every `{{ … }}` of user data (email, question, filename, error_message) emits HTML-escaped. No `|safe` on user input. Test #24 verifies question-escape.
- Markdown is rendered by `marked.parse()` **client-side**. Marked v12 no longer ships `sanitize`; the rendered string can contain arbitrary HTML. Threat model: the only path for user-controlled HTML to reach `marked.parse` is the assistant's own stream — constrained by our skill prompt, `--allowed-tools Read,Glob,Grep` (HARNESS §3), and a 600s timeout. Session cookie is `HttpOnly`, so even an `<img onerror>` cannot steal it; `SameSite=Lax` + `verify_origin` cover CSRF. We accept this risk in M4 and revisit if the threat model changes (e.g. shared plans — spec §14 YAGNI).
- POST `/api/research` inherits `verify_origin` from `auth.py` — no change.
- Logout uses POST (not GET) to avoid accidental logout via prefetched links.
- Logs never include question text or filenames — `logger.info` lines use `user_id` + `request_id` only (consistent with `research.py`).

---

## 12. Not in scope

- `cost_usd` display: backend does not persist it yet (spec §12 deferred; `/api/history` returns null).
- History pagination / search: out of scope for MVP (spec §14).
- Live refresh of the list when a new request finishes in another tab: user refreshes.
- Settings page, password reset, account deletion.
- PWA manifest / service worker / dark mode.
- Retry button on failed requests.
- Inline preview of uploaded files before submit.
- HTML `<noscript>` degradation: the flow fundamentally requires JS (SSE + upload chips); a `<noscript>` block on `index.html` says "请启用 JavaScript".
