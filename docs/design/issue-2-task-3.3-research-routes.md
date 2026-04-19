# Task 3.3 — Research routes + SSE (Issue #2 / M3)

Date: 2026-04-19
Status: Draft (pre-review)
Scope: Milestone 3 Task 3.3 — HTTP layer for the research pipeline: `POST /api/research` (multipart upload), `GET /api/research/<id>/stream` (SSE), `GET /api/research/<id>` (JSON), `GET /api/research/<id>/download` (markdown attachment). Wires `file_processor` (Task 3.1) and `claude_runner` (Task 3.2) into FastAPI and manages the `research_requests` state machine.

---

## 1. Purpose

HTTP boundary for research creation, live streaming, and retrieval. Owns the `pending → running → done | failed` state machine on `research_requests`. Satisfies HARNESS §1 (failed rows MUST have non-empty `error_message`) and HARNESS §2 (absolute `plan_path`). Router validates + translates; the background worker in `app/services/research_runner.py` drives claude and persists outcomes.

Non-goals: `/api/history` + `/history/<id>` (Task 4), retry UI, pagination, multi-user sharing, iOS polling fallback (Task 4.2 frontend).

---

## 2. Endpoints

All endpoints require a valid session (`get_current_user` from `app/routers/auth.py`); missing/expired cookie → 401. Ownership mismatch → 404 (spec §8: no enumeration). The `<id>` endpoints use a single ownership-scoped query `SELECT ... FROM research_requests WHERE id = :rid AND user_id = :uid LIMIT 1`, so missing and cross-user rows share the same query shape (no timing oracle).

### 2.1 `POST /api/research`

- `multipart/form-data`: `question: str`, `files: list[UploadFile]` (0–20).
- Dependencies: `require_user`, `verify_origin` (reused from `auth.py`).

Steps:

1. `question = question.strip()`; empty → 400 `{"error": "empty_question"}`.
2. `len(question) > 4000` → 400 `{"error": "question_too_long"}`.
3. `await file_processor.validate_upload_limits(files)` — `LimitExceededError` is already `HTTPException(400)`, bubbles with `{"code", "message"}`.
4. `request_id = ulid_new()` (26-char Crockford base32; helper in `research_runner`).
5. `async with session.begin():`
   - Insert `ResearchRequest(id=request_id, user_id=user.id, question, status='pending', model=settings.claude_model, created_at=utcnow())`.
   - For each `UploadFile`: `content = await file.read()`; `saved = await file_processor.save_and_extract(request_id, file.filename, content)`; insert `UploadedFile(request_id, original_name=file.filename, stored_path=str(saved.stored_path), extracted_path=str(saved.extracted_path) if saved.extracted_path else None, size_bytes=saved.size_bytes, mime_type=saved.mime_type, created_at=utcnow())`.
6. After commit: `asyncio.create_task(research_runner.run_research(request_id))`; task refs held in `_TASKS: set[asyncio.Task]` (prevent GC).
7. Return 201 `{"request_id", "status": "pending"}`.

Step-5 failure (disk full, DB) → txn rollback, no task, 500 `{"error": "internal"}`. Orphan upload bytes under `{upload_dir}/{request_id}/` are out of scope for M3 (§12).

### 2.2 `GET /api/research/<id>/stream`

- `StreamingResponse(..., media_type="text/event-stream")`, headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

Steps:

1. Ownership-scoped load (§2 preamble); missing row → 404.
2. `status == 'done'`: emit one `event: done` with `{"request_id", "markdown": <plan_path text>, "cost_usd": null}`; close.
3. `status == 'failed'`: emit `event: error` with `{"message": error_message}`; close.
4. Else (`pending`/`running`): `q = research_runner.subscribe(request_id)`; loop on `q.get()`:
   - `("delta", text)` → `event: delta\ndata: {"text": text}\n\n`.
   - `("done", final_md, cost, ms)` → `event: done\ndata: {"request_id", "markdown": final_md, "cost_usd": cost, "elapsed_ms": ms}\n\n`; break.
   - `("error", msg)` → `event: error\ndata: {"message": msg}\n\n`; break.
   - `None` sentinel → break (channel closed without terminal — safety net; client refreshes).
5. `finally: research_runner.unsubscribe(request_id, q)`. Client disconnect (CancelledError) unwinds finally; background task continues.

SSE framing: `event: <name>\ndata: <json>\n\n`; `json.dumps(..., ensure_ascii=False)`. Newlines inside `text` are JSON-escaped (`\n`), so the blank-line delimiter remains unambiguous.

Delivery guarantee: `delta` / `done` / `error` events are best-effort — if a subscriber queue is full (>256 pending, `_publish` drops silently per §5) the subscriber sees a gap. Clients SHOULD reload `GET /api/research/<id>` after `done` to fetch canonical markdown from disk; the DB is ground truth, SSE is a convenience. The `__close__` sentinel tells subscribers to disconnect and re-fetch.

### 2.3 `GET /api/research/<id>`

JSON. Ownership-scoped load (§2 preamble); missing row → 404. Then load `uploaded_files` by `request_id`. Body:

```json
{
  "request_id": "...",
  "status": "pending|running|done|failed",
  "question": "...",
  "markdown": "<plan_path contents>|null",
  "error_message": "...|null",
  "cost_usd": null,
  "created_at": "<iso8601>",
  "completed_at": "<iso8601>|null",
  "files": [{"name": "original", "size": 12345}]
}
```

`markdown` is populated from disk only when `status=='done'`; `error_message` only when `status=='failed'`. `cost_usd` is always `null` in M3 (not persisted — §9). Used by Task 4.2 frontend polling after iOS SSE drop.

### 2.4 `GET /api/research/<id>/download`

Ownership-scoped load (§2 preamble); missing row, `status != 'done'`, or `plan_path` absent → 404. On success: `FileResponse(plan_path, media_type="text/markdown", filename=f"research-{id}.md")` (spec §8).

---

## 3. Background task: `_run_research(request_id)`

Module: `app/services/research_runner.py`. `run_research` is the router-facing dispatcher; `_run_research` is the coroutine.

```python
def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("research task failed for request", exc_info=exc)

async def run_research(request_id: str) -> None:
    task = asyncio.create_task(_run_research(request_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_log_task_exception)

async def _run_research(request_id: str) -> None:
    # Block A: mark running, snapshot inputs. Short-lived session; no network I/O.
    async with get_sessionmaker()() as s1:
        async with s1.begin():
            req = await _load_for_update(s1, request_id)
            if req is None or req.status != 'pending':
                return  # idempotent
            req.status = "running"
            # Snapshot to locals so we can drop the session before the claude stream.
            question = req.question
            file_rows = list(await _load_files(s1, request_id))
        # s1 closed here — no DB connection held during claude run.

    # Build prompt outside any session.
    prompt = _render_prompt(question, file_rows)
    cwd = (Path(settings.upload_dir) / request_id).resolve()
    cwd.mkdir(parents=True, exist_ok=True)  # 0-file case

    # Run claude (no DB session held — claude runs can be 60s–10min).
    final_md = ""
    cost_usd = 0.0
    elapsed_ms = 0
    error_msg: str | None = None
    try:
        async for ev in claude_runner.stream(prompt, cwd):
            await _publish(request_id, ev)
            tag = ev[0]
            if tag == "delta":
                final_md += ev[1]
            elif tag == "done":
                final_md, cost_usd, elapsed_ms = ev[1], ev[2], ev[3]
            elif tag == "error":
                error_msg = ev[1] or "unknown claude error"
    except Exception as e:
        error_msg = error_msg or f"internal: {e!r}"

    # Block B: terminal write (separate session).
    try:
        async with get_sessionmaker()() as s2:
            async with s2.begin():
                req = await _load_for_update(s2, request_id)
                if req is None:
                    return
                if error_msg:
                    req.status = "failed"
                    req.error_message = error_msg
                    req.completed_at = _utcnow()
                else:
                    try:
                        plan_path = _write_plan(request_id, final_md)
                    except OSError as e:
                        req.status = "failed"
                        req.error_message = f"plan_write_failed: {e!r}"
                        req.completed_at = _utcnow()
                    else:
                        req.status = "done"
                        req.plan_path = str(plan_path)
                        req.completed_at = _utcnow()
    except Exception as e:
        # Last-resort: open a fresh session to mark failed.
        logger.error("terminal-write failure for %s: %r", request_id, e)
        try:
            async with get_sessionmaker()() as s3:
                async with s3.begin():
                    req = await _load_for_update(s3, request_id)
                    if req and req.status == "running":
                        req.status = "failed"
                        req.error_message = f"internal: {e!r}"
                        req.completed_at = _utcnow()
        except Exception as e2:
            logger.error("even rescue-write failed for %s: %r", request_id, e2)
    finally:
        await _publish(request_id, ("__close__",))  # signal subscribers to disconnect
```

HARNESS §1: `error_msg or "unknown claude error"` fallback covers the "neither done nor error" case; rescue session (s3) covers Block-B crashes. HARNESS §2: `_write_plan` does `Path(settings.plan_dir).resolve() / f"{request_id}.md"`, writes UTF-8, returns absolute `Path`. Two-session split (A/B) prevents holding a DB connection across the (potentially minutes-long) claude stream.

---

## 4. Prompt template

File: `app/templates/prompts/research.j2`. Rendered via a dedicated `Environment(autoescape=False, keep_trailing_newline=True, loader=FileSystemLoader(...))` in `research_runner` — NOT via `app.state.templates` (that one auto-escapes HTML, which mangles the prompt).

```jinja
/research-method-designer

用户的研究问题：
{{ question }}

{% if uploaded_files %}
用户上传了以下资料（相对于当前工作目录）：
{% for f in uploaded_files %}
- {{ f.original_name }} → {{ f.local_path }}{% if not f.extraction_ok %} (解析失败，已忽略){% endif %}
{% endfor %}

请根据需要用 Read 工具阅读这些文件，把相关内容纳入研究方案的"已有材料"部分。
{% endif %}

请直接产出完整的研究方案 markdown（按 skill 要求的 10 节结构）。
```

`_render_prompt` builds `uploaded_files` as a list of `_PromptFile(original_name, local_path, extraction_ok)` where `local_path = str(extracted_path) if extracted_path else str(stored_path)` (always absolute), and `extraction_ok = True` for `.md/.txt` always, `True` for pdf/docx iff `extracted_path is not None`.

---

## 5. Pub/sub infrastructure

Module-level in `research_runner`:

```python
_channels: dict[str, list[asyncio.Queue[Event | None]]] = {}

def subscribe(rid: str) -> asyncio.Queue[Event | None]:
    q = asyncio.Queue(maxsize=256)
    _channels.setdefault(rid, []).append(q); return q

def unsubscribe(rid: str, q) -> None:
    lst = _channels.get(rid)
    if not lst: return
    try: lst.remove(q)
    except ValueError: pass
    if not lst: _channels.pop(rid, None)

def _publish(rid: str, ev: Event) -> None:
    for q in list(_channels.get(rid, [])):
        try: q.put_nowait(ev)
        except asyncio.QueueFull: pass  # slow subscriber: drop (ground truth on disk)

def _close_channel(rid: str) -> None:
    for q in list(_channels.get(rid, [])):
        try: q.put_nowait(None)
        except asyncio.QueueFull: pass
    _channels.pop(rid, None)
```

Thread-safety: asyncio single-threaded guarantee — no locks. Single-worker uvicorn per spec §1; no cross-process sharing. Late subscribers (client connects after `_close_channel`) take the DB-replay path in §2.2 steps 1–3; no race.

Publisher-thread invariant: all `_publish` / `_close_channel` calls MUST be on the event loop thread. Executor-dispatched code that needs to publish MUST use `loop.call_soon_threadsafe(_publish, rid, ev)`.

---

## 6. State machine

`POST → INSERT pending → create_task → _run_research flips running → one of: {done + plan_path, failed + error_message ("timeout after Ns" | "internal: <repr>" | "plan_write_failed: <repr>" | claude stderr tail)}; completed_at set in all terminal cases.`

Only `_run_research` writes `status`; routers never mutate it. No retry in M3. HARNESS §1 is covered by `test_failed_request_has_non_empty_error_message` at the router layer.

---

## 7. Concurrency

`claude_runner` already owns `asyncio.Semaphore(settings.claude_concurrency=3)`. `research_runner` adds NO second layer; background tasks queue naturally inside `claude_runner.stream`. UX note: we flip `running` before entering the semaphore, so queued requests display `running` with no deltas (spec §5.4 allows this blurring). SSE subscribers are uncapped — one per tab.

---

## 8. Error handling

| Scenario | Handling |
|---|---|
| POST without session | 401 `{"error": "unauthenticated"}` (via `require_user`) |
| POST empty question | 400 `{"error": "empty_question"}` |
| POST question > 4000 chars | 400 `{"error": "question_too_long"}` |
| POST file-limit violation | 400 `{"code": "<LimitExceededError code>", "message": ...}` — bubbled from file_processor |
| POST disk full during save | 500 `{"error": "internal"}`; row never committed; task not spawned |
| claude subprocess error | `status='failed'`, `error_message=<stderr tail or reason>` |
| claude timeout | `status='failed'`, `error_message="timeout after Ns"` |
| claude binary missing | same — claude_runner yields error event with message |
| Background task crashes unexpectedly | caught in outer `try` in `_run_research`, `status='failed'`, `error_message=f"internal: {exc!r}"` |
| SSE client disconnects mid-stream | finally block unsubscribes; background task continues; other subscribers + DB row unaffected |
| GET stream/JSON/download for cross-user or non-existent id | 404 (ownership-scoped query, §2 preamble) |
| GET download while pending or failed | 404 |
| `plan_path` file missing on disk at download time | 500 `{"error": "plan_missing"}` — inconsistent state, log ERROR |

---

## 9. Field mapping

| Field | Source | DB column | SSE event | JSON API (`GET /api/research/<id>`) |
|---|---|---|---|---|
| `question` | POST form | `research_requests.question` | — | echoed |
| file bytes | POST `files[]` | `uploaded_files.stored_path` (absolute) | — | — |
| extracted text | file_processor | `uploaded_files.extracted_path` (absolute, nullable) | — | — |
| file metadata | file_processor | `uploaded_files.{size_bytes,mime_type,original_name}` | — | `files: [{name, size}]` |
| `request_id` | `ulid_new()` | `research_requests.id` (PK) | `done` payload | `request_id` |
| `user_id` | `get_current_user` | `research_requests.user_id` | — | (enforces ownership; not returned) |
| `status` | state machine | `research_requests.status` | — | `status` |
| `model` | `settings.claude_model` at POST time | `research_requests.model` | — | — |
| `created_at` | utcnow at POST | `research_requests.created_at` | — | `created_at` |
| `completed_at` | utcnow at terminal | `research_requests.completed_at` | — | `completed_at` |
| final markdown | claude `done` event | written to `plan_path` on disk | `done.markdown` | `markdown` (read from disk) |
| `plan_path` | `_write_plan` | `research_requests.plan_path` (absolute) | — | — (internal) |
| `cost_usd` | claude `done` event | NOT PERSISTED (M3) | `done.cost_usd` | `cost_usd: null` (always, M3) |
| `elapsed_ms` | claude `done` event | — | `done.elapsed_ms` | — |
| `error_message` | claude `error` event or internal | `research_requests.error_message` (non-empty iff `status='failed'`) | `error.message` | `error_message` |

`cost_usd` persistence is deferred to Task 4 (history UI needs it). M3 shows it live only.

Note: the `model` column is persisted for Task 4 history display (so an M4 user can see which model was used for each research). Intentionally absent from the M3 `GET /api/research/<id>` JSON response; it will be added to the JSON body in Task 4.

---

## 10. Files created / modified

| Path | Action | Purpose |
|---|---|---|
| `app/routers/research.py` | create | HTTP endpoints (POST, SSE, JSON, download) |
| `app/services/research_runner.py` | create | background task, pub/sub bus, prompt renderer, plan writer, ULID helper |
| `app/templates/prompts/research.j2` | create | Jinja2 prompt template |
| `app/main.py` | modify | `app.include_router(research.router)` |
| `tests/integration/test_research_endpoints.py` | create | HTTP-level integration tests (mocks `claude_runner.stream`) |
| `tests/unit/test_research_runner.py` | create | unit tests for background orchestration, prompt render, pub/sub |

No changes to `app/models.py`, `app/config.py`, `app/db.py`, or auth code.

---

## 11. Test plan

### 11.1 Unit tests — `tests/unit/test_research_runner.py`

All mock `claude_runner.stream` as an async generator yielding canned events.

1. `test_run_research_marks_status_running_then_done` — observe status via a pre-injected spy or two reads.
2. `test_run_research_writes_plan_path_on_done` — assert `plan_path` is absolute, file exists, content matches.
3. `test_run_research_marks_failed_with_error_on_claude_error` — claude yields `("error", "boom")`; DB row ends with `status='failed'`, `error_message == "boom"` (HARNESS §1).
4. `test_run_research_timeout_marks_failed` — claude yields `("error", "timeout after 600s")`; DB row ends failed with that exact message.
5. `test_run_research_fallback_error_when_no_terminal_event` — claude yields deltas then closes without done/error; row ends failed with `error_message == "claude produced no output"` (HARNESS §1 safety net).
6. `test_pubsub_publishes_to_subscribers` — two subscribers, one publish, both receive.
7. `test_pubsub_unsubscribe_removes_queue` — after unsubscribe, publish does not raise; internal dict is empty.
8. `test_pubsub_slow_subscriber_drop` — fill queue to 256, next publish does not raise, does not block publisher.
9. `test_prompt_template_includes_uploaded_files` — renders with 2 files, asserts both names and local_paths appear.
10. `test_prompt_template_omits_files_section_when_empty` — no `uploaded_files` → no `用户上传了以下资料` block.
11. `test_prompt_template_notes_extraction_failed_files` — file with `extraction_ok=False` shows `(解析失败，已忽略)`.
12. `test_plan_path_is_absolute` — direct unit on `_write_plan`.
13. `test_ulid_new_format` — 26 chars, Crockford base32.
14. `test_run_research_plan_write_failure_marks_failed_with_message` — monkeypatch `_write_plan` to raise `OSError("disk full")`; assert row ends `status='failed'`, `error_message` starts with `plan_write_failed:`.
15. `test_run_research_terminal_session_failure_marks_failed_via_rescue` — patch the sessionmaker so the second `get_sessionmaker()()` call raises; assert the rescue session (s3) still flips `running` → `failed` with `error_message` starting `internal:`.
16. `test_prompt_template_preserves_malicious_user_content_literally` — render with `question="{{ '{{7*7}}' }} <script>alert(1)</script>"` and a filename containing `../../etc/passwd`; assert output contains the literal strings (no autoescape mangling, no eval) — confirms the dedicated `Environment(autoescape=False)` does not evaluate nested jinja expressions and does not HTML-escape.

### 11.2 Integration tests — `tests/integration/test_research_endpoints.py`

Uses `httpx.AsyncClient(app=app)` + authenticated session cookie. Mocks `app.services.claude_runner.stream` at module path.

17. `test_post_research_creates_request_and_files` — 2 files uploaded, 201 returned, DB row exists, 2 uploaded_files rows.
18. `test_post_research_without_auth_returns_401`.
19. `test_post_research_empty_question_returns_400` — body `{"error": "empty_question"}`.
20. `test_post_research_whitespace_only_question_returns_400` — strip-then-check.
21. `test_post_research_too_long_question_returns_400` — 4001 chars.
22. `test_post_research_too_many_files_returns_400` — 21 files.
23. `test_post_research_allows_zero_files` — 201, cwd created.
24. `test_get_research_stream_sse_events` — live stream: two deltas then done.
25. `test_get_research_stream_replays_done_if_already_finished` — pre-set `status='done'`, `plan_path` on disk; connect stream, get one `done` event.
26. `test_get_research_stream_replays_error_if_failed` — pre-set `status='failed'`, `error_message`; connect, get `error`.
27. `test_get_research_stream_returns_404_for_others_request`.
28. `test_get_research_json_returns_full_state` — pending/running/done/failed all four.
29. `test_get_research_json_includes_files_metadata`.
30. `test_get_research_download_returns_md_when_done` — Content-Disposition asserted.
31. `test_get_research_download_returns_404_when_pending`.
32. `test_get_research_download_returns_404_when_failed`.
33. `test_cross_user_isolation_post_returns_404` — alice's session, bob's `GET /api/research/<bob_id>` returns 404. (Per /design-check SUGGEST S2: per-endpoint isolation.)
34. `test_cross_user_isolation_stream_returns_404` — alice's session, bob's `GET /api/research/<bob_id>/stream` returns 404.
35. `test_cross_user_isolation_json_returns_404` — alice's session, bob's `GET /api/research/<bob_id>` JSON returns 404.
36. `test_cross_user_isolation_download_returns_404` — alice's session, bob's `GET /api/research/<bob_id>/download` returns 404.
37. `test_failed_request_has_non_empty_error_message` — HARNESS §1 invariant at router level.
38. `test_plan_path_stored_absolute` — HARNESS §2 — after a successful end-to-end run, DB value satisfies `Path(plan_path).is_absolute()`.
39. `test_claude_runner_allowed_tools_unchanged` — imports claude_runner and asserts the argv still includes `["--allowed-tools", "Read,Glob,Grep"]` (cross-task guard; belongs to Task 3.2's suite too, duplicated here as a tripwire).
40. `test_post_research_accepts_malicious_filename_without_crash` — POST with a file named `../../etc/passwd\x00.md` and a question containing shell metachars; assert 201 (or 400 from file_processor, if the filename is rejected at that layer), row/file state consistent, no path-traversal write under `settings.upload_dir`.

Tests 17–40 all require the tester to mock `claude_runner.stream` explicitly; none may exercise the real subprocess. The `RUN_E2E=1` test in Task 3.4 covers the real path.

Total tests: 16 unit + 24 integration = 40 (was 33; added 3 unit + 1 integration new tests, and SUGGEST S2 split cross-user isolation into 4 per-endpoint tests).

---

## 12. Infrastructure dependencies

| Dependency | Failure mode | Degradation |
|---|---|---|
| `file_processor.validate_upload_limits` | `LimitExceededError` (HTTP 400) | bubbles to client |
| `file_processor.save_and_extract` | `LimitExceededError`, `OSError` | 400 / 500; DB txn rolls back |
| `claude_runner.stream` | yields `error` event | mapped to `status='failed'` |
| Disk write of `plan_path` | `OSError` | caught → `status='failed'`, `error_message=f"plan_write_failed: {exc!r}"` |
| `settings.plan_dir` missing/unwritable | `OSError` on write | `_write_plan` runs `Path(settings.plan_dir).mkdir(parents=True, exist_ok=True)` first; residual `OSError` caught in Block B → `status='failed'`, `error_message=f"plan_write_failed: {exc!r}"`. |
| Jinja2 template missing | fails at module import of `research_runner` | fail-fast at app startup |
| DB | IntegrityError etc. | surfaces as 500; no partial state |
| ULID collision | `IntegrityError` on INSERT (duplicate PK) | astronomically rare (≈ 2^-80); mapped to 500 `{"error": "internal"}`; user retries. No explicit retry loop in M3. |

Orphan cleanup (uploads dir for requests where DB insert failed after file save) is NOT handled in M3. Tracked for M5 (cron).

---

## 13. Security

- `Depends(require_user)` guards all four endpoints. Missing cookie → 401 JSON (or redirect for HTML — inapplicable here; all research endpoints are JSON/SSE/download).
- `Depends(verify_origin)` on `POST /api/research` for CSRF. GET endpoints skip Origin: ownership-404 substitutes on `<id>` GET/SSE/download — they are GET-only with no state mutation, so the residual stolen-cookie risk is defeated by ownership enforcement.
- Ownership check (`row.user_id == user.id`) on all three `<id>` endpoints — 404 on mismatch per spec §8.
- `request_id` is ULID (validated by `file_processor` before it touches disk). Never user-supplied at POST.
- Background task runs in the FastAPI worker process with worker permissions; `cwd` for claude is `settings.upload_dir / request_id` — the subprocess allowlist (Read, Glob, Grep) and sandbox dir restrict what it can touch. HARNESS §3 enforced in `claude_runner`; this task relies on that contract.
- `plan_path` is always absolute (HARNESS §2) — `_write_plan` resolves `settings.plan_dir` via `Path(...).resolve()` before joining.
- `stored_path` / `extracted_path` are absolute (HARNESS §2) — `file_processor.save_and_extract` already guarantees this.
- Prompt contains untrusted user text (question + filenames). The claude skill (`/research-method-designer`) is the answer-boundary defence, not this router. We do not scan or censor the prompt.
- SSE endpoint does not accept a `Last-Event-ID` header; resumption is deliberately out of scope (spec §8). Clients refresh to re-fetch from DB.
- Reverse-scan (L4): `claude_runner.stream` yields only `("delta", str)`, `("done", str, float, int)`, `("error", str)` — verified by reading `app/services/claude_runner.py` on 2026-04-19. Any new event type added there requires updating `_run_research`'s tag dispatch; test #39 guards the argv surface, not the event surface.

---

## 14. Not in scope

- Task 4.1: `/api/history`, `/history/<id>`. Task 4.2/4.3: frontend (marked.js, drag-drop), iOS SSE→polling fallback (GET JSON already supports polling). Task 3.4: `RUN_E2E=1` real-claude SSE test. Deferred: retry UI, `cost_usd` persistence (Task 4), orphan upload cleanup (M5 cron), disk-full preflight via `shutil.disk_usage` (M5).

---

## 15. Open questions for human review

- **Q1**: `_run_research` restart idempotency — current code treats `status != 'pending'` as "skip". Restart mid-request leaves a row stuck `running`. M3 accepts; M5 adds startup sweep flipping `running` → `failed` with `error_message="server restarted mid-request"`. Flagged for DEV_LOG.
- **Q2**: Queue maxsize=256 — silent drop on subscriber stall (ground truth on disk, user refreshes to see final). Alternative: block publisher. Chosen: drop. Confirm.
- **Q3**: `cost_usd` transient in M3; Task 4 adds DB column. Accept deferral?
