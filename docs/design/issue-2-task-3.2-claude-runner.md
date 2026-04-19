# Task 3.2 — Claude Runner (Issue #2 / M3)

Date: 2026-04-19
Status: Draft (pre-review)
Scope: Milestone 3 Task 3.2 — async subprocess wrapper around the `claude` CLI that streams `stream-json` output as typed events.

---

## 1. Purpose

Subprocess wrapper for the Claude Code CLI. Consumes a prompt + working directory, invokes:

```
claude -p <prompt> \
    --output-format stream-json \
    --model <claude_model> \
    --allowed-tools Read,Glob,Grep \
    --permission-mode acceptEdits \
    --add-dir <cwd>          # grants Read/Glob/Grep access to this dir
```

The subprocess working directory is pinned via the ``cwd=`` kwarg of
``asyncio.create_subprocess_exec`` (NOT via an argv flag — the real
``claude`` CLI rejects ``--cwd`` as an unknown option).

Parses stdout line-by-line, yielding typed events as they arrive. Enforces per-request timeout (`settings.claude_timeout_sec`) and a global concurrency cap (`settings.claude_concurrency`). Kills the child process cleanly on timeout, non-zero exit, or caller cancellation.

Non-goals (deferred):

- HTTP endpoint wrapping (Task 3.3)
- SSE streaming to browser (Task 3.3)
- DB persistence of final markdown (Task 3.3)

---

## 2. Public API

```python
# app/services/claude_runner.py

from pathlib import Path
from typing import AsyncIterator, Literal

# Event tuple variants
DeltaEvent = tuple[Literal["delta"], str]                   # ("delta", text_chunk)
DoneEvent  = tuple[Literal["done"], str, float, int]        # ("done", final_markdown, cost_usd, elapsed_ms)
ErrorEvent = tuple[Literal["error"], str]                   # ("error", error_message)

Event = DeltaEvent | DoneEvent | ErrorEvent


async def stream(prompt: str, cwd: Path) -> AsyncIterator[Event]:
    """Launch the claude CLI and yield events as they arrive.

    - On subprocess exit code 0: yields zero-or-more ``delta`` events, then
      exactly one ``done`` event (constructed from the final ``result`` line).
    - On non-zero exit: yields exactly one ``error`` event with a truncated
      stderr tail (≤1000 chars).
    - On wall-time exceeding ``settings.claude_timeout_sec``: SIGTERM the
      subprocess; if it has not exited within 5s, SIGKILL. Yields one ``error``
      event (``"timeout after Ns"``). No zombies.
    - On caller cancellation (async generator ``aclose()`` / ``GeneratorExit``):
      SIGTERM + 5s wait + SIGKILL, then re-raise. No events yielded after cancel.

    Concurrency is bounded by a module-level
    ``asyncio.Semaphore(settings.claude_concurrency)``; ``stream`` acquires
    before launch and releases after the subprocess has fully exited.
    """
```

---

## 3. Command construction

Exact argv list (never `shell=True`):

```python
argv = [
    settings.claude_bin,
    "-p", prompt,
    "--output-format", "stream-json",
    "--model", settings.claude_model,
    "--allowed-tools", "Read,Glob,Grep",
    "--permission-mode", "acceptEdits",
    "--add-dir", str(cwd),
]
proc = await asyncio.create_subprocess_exec(
    *argv,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=str(cwd),          # actual subprocess working directory
)
```

Rationale:

- List-form (`create_subprocess_exec`) makes shell injection impossible regardless of prompt content.
- Prompt passed via `-p` CLI arg (not stdin) per spec §5.1.
- `--allowed-tools Read,Glob,Grep` is mandated by HARNESS §3. No `Write`, `Edit`, or `Bash`. This MUST be covered by a test.
- `--add-dir <cwd>` grants the allow-listed tools (Read/Glob/Grep) access to the sandboxed per-request uploads dir. The real `claude` CLI does NOT expose a `--cwd` flag — passing one would crash with "unknown option".
- The `cwd=str(cwd)` kwarg on `create_subprocess_exec` is where real sandboxing happens: the child process's working directory is pinned to the ULID-validated uploads dir (paired with Task 3.1). This prevents `Read`/`Glob` from escaping via relative paths even when the user-provided prompt embeds them.

---

## 4. stream-json output parsing

`claude --output-format stream-json` emits one JSON object per line. Line types we care about:

| Incoming line shape | Action |
|---|---|
| `{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}` | yield `("delta", text)` for each `text` chunk in `content` |
| `{"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}` | IGNORE (do not surface tool internals to user) |
| `{"type":"result","subtype":"success","result":"<final>","total_cost_usd":N}` | yield `("done", result, cost_usd, elapsed_ms)` where `elapsed_ms` is measured by the runner (wall time from `create_subprocess_exec` call), not read from the payload |
| other `type` values | IGNORE (forward-compatible) |
| malformed JSON line | log WARNING with line prefix, skip |
| partial line without `\n` | buffer until newline or EOF |

Implementation notes:

- Read with `proc.stdout.readline()` in an `async for`-style loop; `readline()` already handles partial-line buffering at the stream reader level, but we additionally tolerate missing trailing newline at EOF.
- An `assistant` message may carry a `content` array with mixed `text` + `tool_use` items — iterate the array and yield a `delta` per `text` item in order.
- `cost_usd` defaults to `0.0` if `total_cost_usd` is missing.
- `result` string defaults to `""` if missing.

---

## 5. Error handling

| Condition | Behavior |
|---|---|
| `settings.claude_bin` not found (FileNotFoundError / ENOENT on launch) | yield `("error", "claude not found: <bin>")`, return normally (no raise — caller gets a clean single event) |
| PermissionError on launch | yield `("error", "claude not executable: <bin>")`, return |
| Subprocess exits non-zero | drain stderr, yield `("error", stderr.decode('utf-8', errors='replace')[-1000:])` |
| Wall-time > `claude_timeout_sec` | SIGTERM → `asyncio.wait_for(proc.wait(), 5)` → SIGKILL if still alive → yield `("error", f"timeout after {claude_timeout_sec}s")`. Never leave a zombie. |
| Caller cancels (`aclose()` / `GeneratorExit` / task cancellation) | SIGTERM + 5s grace + SIGKILL; propagate `GeneratorExit`; emit no further events |
| JSON decode error | log WARNING; skip line; continue |

Finally block must `await proc.wait()` (after kill if needed) to reap the child; the semaphore release happens in the same finally so we never leak a permit.

> Stderr is drained concurrently into an in-memory `bytearray` via a sidecar `asyncio.Task` spawned when the subprocess launches; this prevents a stderr-pipe-full deadlock while the runner reads stdout. The sidecar task is awaited in the finally block.

---

## 6. Concurrency

```python
_CLAUDE_SEM: asyncio.Semaphore | None = None

def _get_sem() -> asyncio.Semaphore:
    global _CLAUDE_SEM
    if _CLAUDE_SEM is None:
        _CLAUDE_SEM = asyncio.Semaphore(settings.claude_concurrency)
    return _CLAUDE_SEM
```

- Lazy-init so tests can monkeypatch `settings.claude_concurrency` before first use.

> Lazy-init is race-free under CPython asyncio because `_get_sem` contains no `await` between the None-check and the assignment.

- Acquire before `create_subprocess_exec`; release in the `finally` block after `proc.wait()` returns.
- When saturated, additional callers await — upstream `router/research` treats this as back-pressure (request stays `pending` until a slot frees).

---

## 7. Logging

`logging.getLogger("method.claude_runner")`:

- **INFO** on subprocess start: `cmd=<argv[0]> cwd=<cwd> model=<claude_model> prompt_sha256=<hash[:12]>` — never log prompt body (may contain user PII).
- **INFO** on successful completion: `elapsed_ms=<N> cost_usd=<f> result_len=<N>`.
- **WARNING** on timeout: `timeout after <N>s, sent SIGTERM; killed=<bool>`.
- **WARNING** on malformed JSON line: `bad stream-json line (skip): <line[:120]>`.
- **ERROR** on non-zero exit: `exit=<N> stderr_tail=<stderr[-400:]>`.
- **ERROR** on ENOENT: `claude_bin not found: <bin>`.

---

## 8. Field mapping table

| Field | Input | Processing | Output event |
|---|---|---|---|
| `prompt` | caller arg (str) | passed as `-p <value>` argv entry; logged as sha256 prefix only | — |
| `cwd` | caller arg (Path) | `str(cwd)` passed as (a) `--add-dir` argv entry granting tool access and (b) `cwd=` kwarg to `create_subprocess_exec` pinning the subprocess working directory | — |
| stdout JSON lines | child subprocess pipe | `json.loads` per line; dispatched on `type` | `delta` / `done` |
| stderr | child subprocess pipe | captured in-memory, truncated to 1000 chars on non-zero exit | `error` |
| `cost_usd` | `result` line `total_cost_usd` | float coerce, default 0.0 | `done` tuple[2] |
| `elapsed_ms` | wall clock | `int((time.monotonic() - start) * 1000)` | `done` tuple[3] |
| exit code | `proc.returncode` | 0 → emit `done` from buffered result; non-zero → emit `error` | determines final event |
| tool_use lines | stdout | filtered out | — |
| malformed line | stdout | logged WARNING, skipped | — |

---

## 9. Files created / modified

| Path | Action | Purpose |
|---|---|---|
| `app/services/claude_runner.py` | **create** | Async subprocess wrapper + stream-json parser + event generator |
| `tests/unit/test_claude_runner.py` | **create** | Unit tests; subprocess is mocked via `monkeypatch` of `asyncio.create_subprocess_exec` |

No changes to models, config, or routers in this task. `app/config.py` already exposes `claude_bin`, `claude_model`, `claude_timeout_sec`, `claude_concurrency` (verified).

---

## 10. Test plan (hint for `/tester`)

Mechanical mapping from sections above — every bullet below must have at least one test. Sub-agent should add coverage but never drop items from this list.

1. `test_stream_yields_deltas_from_assistant_text` — mock stdout emits an assistant line with one `text` item → one `delta` event with matching text (§4).
2. `test_stream_yields_done_on_result_line` — mock emits a `result` line → one `done` event; `result` string preserved; `cost_usd` extracted; `elapsed_ms >= 0` (§4).
3. `test_stream_yields_error_on_nonzero_exit` — `returncode=1`, stderr = `"boom"` → one `error` event containing `"boom"` (§5).
4. `test_stream_ignores_tool_use_events` — assistant line whose `content` is `[{"type":"tool_use",...}]` → no event yielded (§4).
5. `test_stream_skips_malformed_json_lines` — one bad line, one valid `result` line → only `done` event, no exception, bad line is WARNING-logged (§4).
6. `test_stream_handles_partial_line_buffering` — stdout delivers a valid JSON split across two read chunks → single event yielded correctly (§4).
7. `test_stream_timeout_kills_subprocess` — simulate hang; `settings.claude_timeout_sec` shortened to e.g. 0.5; after timeout, `proc.terminate()` was called, then `proc.kill()` after grace; `error` event emitted; no zombie (§5).
8. `test_stream_cancellation_kills_subprocess_cleanly` — caller calls `agen.aclose()` mid-stream → subprocess terminated; generator completes without further events; semaphore permit released (§5, §6).
9. `test_stream_respects_concurrency_semaphore` — `claude_concurrency=2`; launch 3 streams; 3rd blocks on `acquire` until one finishes (§6).
10. `test_command_includes_allowed_tools_read_glob_grep` — assert exact `--allowed-tools Read,Glob,Grep` argv slice present (HARNESS §3 enforcement).
11. `test_command_uses_configured_model` — assert `--model` argv = `settings.claude_model` (§3).
12. `test_command_uses_configured_cwd` — assert `--add-dir` argv = `str(cwd)` AND that the `cwd=` kwarg on `create_subprocess_exec` equals `str(cwd)`. Also assert `--cwd` does NOT appear in argv (the real claude CLI rejects it). (§3)
13. `test_subprocess_enoent_yields_error` — `create_subprocess_exec` raises `FileNotFoundError` → one `error` event with `"claude not found"` substring; generator returns normally, no raise (§5).
14. `test_stream_does_not_deadlock_on_large_stderr` — sidecar stderr drain proven by feeding >64KB to mock stderr while a normal `done` flows on stdout.

All tests mock `asyncio.create_subprocess_exec` (via `monkeypatch`) with a fake `Process` whose `stdout`/`stderr` are `asyncio.StreamReader`-compatible objects we feed canned bytes into, and whose `wait()` / `terminate()` / `kill()` are tracked.

---

## 11. Infra dependency table

| Dep | Failure mode | Degradation |
|---|---|---|
| `claude` CLI at `settings.claude_bin` | ENOENT / not executable | yield single `error` event with descriptive message; caller surfaces as request `failed` (HARNESS §1 — must have non-empty `error_message`) |
| `claude` CLI non-zero exit | API auth error, rate limit, internal crash | parse stderr tail, yield `error` |
| model availability | API-side error → stderr | yield `error` (non-zero exit path) |
| subprocess timeout | hung/slow model call | SIGTERM → 5s grace → SIGKILL; yield `error`; reap child |
| concurrency saturation | many concurrent requests | back-pressure only — `semaphore.acquire()` awaits; no error event |
| stdout pipe buffer | enormous single line | `StreamReader.readline` reads until `\n`; we do not cap line length (stream-json lines are small per claude CLI behaviour) |

---

## 12. Security

- HARNESS §3 enforced: `--allowed-tools Read,Glob,Grep`; no `Write`, `Edit`, or `Bash`. Test #10 is load-bearing.
- Shell injection impossible: we use `create_subprocess_exec(argv-list)`, never `shell=True`. User prompt content is a single argv item.
- `cwd` is a trusted caller-supplied path; upstream Task 3.1 guarantees it is an absolute, ULID-validated directory under `settings.upload_dir`. This module does not re-validate; boundary is documented. The sandbox is enforced on two axes: (a) the subprocess's actual working directory is set via the `cwd=` kwarg on `create_subprocess_exec` (so relative paths cannot escape via shell semantics), and (b) `--add-dir <cwd>` is the only directory the allow-listed Read/Glob/Grep tools are permitted to access. We deliberately do NOT use a `--cwd` argv flag — the real `claude` CLI has no such option and would error out immediately.
- Prompt is NOT logged verbatim — only a sha256 prefix — to avoid PII leakage in log files.
- stderr is truncated to the last 1000 chars before being placed in the `error` event payload (defence-in-depth against pathological error streams).

---

## 13. NOT in scope (deferred)

| Item | Target task |
|---|---|
| `POST /api/research` endpoint + multipart intake | Task 3.3 |
| SSE bridging `stream()` → `data: {...}\n\n` to browser | Task 3.3 |
| DB persistence of `final_markdown` to `research_requests.plan_path` | Task 3.3 |
| Prompt template rendering (`/research-method-designer` + uploaded_files block) | Task 3.3 |
| E2E test that actually spawns `claude` | Task 3.4 (opt-in, `RUN_E2E=1`) |

---

## 14. Open questions

- None blocking. The `result` string's encoding (UTF-8 by CLI convention) is assumed; if future versions add a `"encoding"` hint we can respect it — for now `str` from JSON decode is sufficient.
