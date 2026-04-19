"""Async subprocess wrapper for the Claude CLI (Task 3.2).

Streams ``stream-json`` output from the ``claude`` CLI as typed events.

See ``docs/design/issue-2-task-3.2-claude-runner.md`` for the full contract.

Security
--------
- HARNESS §3: ``--allowed-tools Read,Glob,Grep`` — no ``Write``, ``Edit``, or
  ``Bash``. This is load-bearing and covered by a dedicated test.
- Shell injection impossible: ``create_subprocess_exec`` with argv list,
  never ``shell=True``.
- Prompt bodies are NEVER logged verbatim — only a sha256 prefix.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from app import config as _config

logger = logging.getLogger("method.claude_runner")

# Event tuple variants (§2)
DeltaEvent = tuple[Literal["delta"], str]
DoneEvent = tuple[Literal["done"], str, float, int]
ErrorEvent = tuple[Literal["error"], str]
Event = DeltaEvent | DoneEvent | ErrorEvent

# Module-level semaphore — lazy-init so tests can monkeypatch
# ``settings.claude_concurrency`` before first use (design §6).
_CLAUDE_SEM: asyncio.Semaphore | None = None

# Grace period between SIGTERM and SIGKILL on timeout/cancellation (design §5).
_GRACE_SEC = 5.0

# Max bytes of stderr reported back to the caller inside an error event.
_STDERR_TAIL = 1000


def _get_sem() -> asyncio.Semaphore:
    """Lazy-init the module-level concurrency semaphore.

    Race-free under CPython asyncio: no ``await`` between the ``None`` check
    and the assignment (design §6).
    """
    global _CLAUDE_SEM
    if _CLAUDE_SEM is None:
        _CLAUDE_SEM = asyncio.Semaphore(_config.settings.claude_concurrency)
    return _CLAUDE_SEM


async def stream(prompt: str, cwd: Path) -> AsyncIterator[Event]:
    """Launch the claude CLI and yield events as they arrive.

    See ``docs/design/issue-2-task-3.2-claude-runner.md`` §2 for the full
    public-API contract.
    """
    prompt_hash = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]
    sem = _get_sem()

    async with sem:
        argv = [
            _config.settings.claude_bin,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",  # required by claude CLI when pairing --print with stream-json
            "--model", _config.settings.claude_model,
            "--allowed-tools", "Read,Glob,Grep",
            "--permission-mode", "acceptEdits",
            "--add-dir", str(cwd),
        ]
        logger.info(
            "claude_start cmd=%s cwd=%s model=%s prompt_sha256=%s",
            _config.settings.claude_bin,
            cwd,
            _config.settings.claude_model,
            prompt_hash,
        )

        # 1. Launch. ENOENT / PermissionError → single error event, no raise.
        # Subprocess working directory is pinned via the ``cwd=`` kwarg (not an
        # argv flag — the real claude CLI has no ``--cwd`` option).
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
        except FileNotFoundError:
            logger.error("claude_bin not found: %s", _config.settings.claude_bin)
            yield ("error", f"claude not found: {_config.settings.claude_bin}")
            return
        except PermissionError:
            logger.error("claude_bin not executable: %s", _config.settings.claude_bin)
            yield ("error", f"claude not executable: {_config.settings.claude_bin}")
            return

        start = time.monotonic()
        stderr_bytes = bytearray()

        # 2. Sidecar stderr drain — prevents pipe-full deadlock (design §5).
        async def _drain_stderr() -> None:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_bytes.extend(chunk)

        stderr_task: asyncio.Task[None] = asyncio.create_task(_drain_stderr())

        final_result: str = ""
        cost_usd: float = 0.0
        saw_result = False
        timed_out = False
        timeout = _config.settings.claude_timeout_sec
        deadline = start + timeout

        try:
            # 3. Read stdout line-by-line, dispatch stream-json events.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining
                    )
                except TimeoutError:
                    timed_out = True
                    break

                if not line:
                    break  # EOF

                # Parse one stream-json line.
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace").rstrip())
                except json.JSONDecodeError:
                    logger.warning(
                        "bad stream-json line (skip): %s",
                        line[:120].decode("utf-8", errors="replace"),
                    )
                    continue

                msg_type = obj.get("type")
                if msg_type == "assistant":
                    content = obj.get("message", {}).get("content", []) or []
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            if text:
                                yield ("delta", text)
                        # tool_use and other item types are intentionally ignored.
                elif msg_type == "result":
                    final_result = str(obj.get("result") or "")
                    try:
                        cost_usd = float(obj.get("total_cost_usd") or 0.0)
                    except (TypeError, ValueError):
                        cost_usd = 0.0
                    saw_result = True
                # All other message types are ignored (forward-compatible).

            # 4. Timeout path: SIGTERM → 5s grace → SIGKILL. Yield error, return.
            if timed_out:
                logger.warning(
                    "timeout after %ss, sent SIGTERM; killed=%s",
                    timeout,
                    False,
                )
                killed = await _terminate_and_reap(proc)
                if killed:
                    logger.warning(
                        "timeout after %ss, SIGKILL after grace", timeout
                    )
                yield ("error", f"timeout after {timeout}s")
                return

            # 5. Normal EOF. If we saw a ``result`` line, that is
            # authoritative success — emit ``done`` immediately without
            # blocking on ``proc.wait()``. The ``finally`` block reaps the
            # child. Otherwise we need the exit code to distinguish success
            # from error.
            exit_code: int | None = proc.returncode  # may already be set

            if saw_result and (exit_code is None or exit_code == 0):
                await _await_stderr(stderr_task)
                elapsed_ms = int((time.monotonic() - start) * 1000)
                logger.info(
                    "claude_done elapsed_ms=%d cost_usd=%.4f result_len=%d",
                    elapsed_ms,
                    cost_usd,
                    len(final_result),
                )
                yield ("done", final_result, cost_usd, elapsed_ms)
                return

            # No result line — need the exit code. Wait up to deadline.
            if exit_code is None:
                wait_remaining = max(deadline - time.monotonic(), 0.0)
                try:
                    exit_code = await asyncio.wait_for(
                        proc.wait(),
                        timeout=wait_remaining if wait_remaining > 0 else None,
                    )
                except TimeoutError:
                    logger.warning(
                        "timeout after %ss (post-EOF wait)", timeout
                    )
                    await _terminate_and_reap(proc)
                    await _await_stderr(stderr_task)
                    yield ("error", f"timeout after {timeout}s")
                    return

            await _await_stderr(stderr_task)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if exit_code == 0:
                # Exit 0 but no result line — surface as an error so the
                # caller can distinguish "claude succeeded with no output"
                # from a legitimate run. (I3 from code review.)
                logger.error(
                    "claude exit=0 but no result line (elapsed_ms=%d)",
                    elapsed_ms,
                )
                yield (
                    "error",
                    "claude exited 0 but emitted no result line",
                )
            else:
                tail = bytes(stderr_bytes)[-_STDERR_TAIL:].decode(
                    "utf-8", errors="replace"
                )
                logger.error(
                    "claude exit=%d stderr_tail=%r",
                    exit_code,
                    tail[-400:],
                )
                yield ("error", tail or f"exit code {exit_code}")

        finally:
            # Reap child and stop stderr drain on every exit path, including
            # GeneratorExit and asyncio.CancelledError (which propagate after
            # this block). Semaphore releases via ``async with sem`` when this
            # frame unwinds.
            await _terminate_and_reap(proc)

            if not stderr_task.done():
                stderr_task.cancel()
            try:
                await asyncio.wait_for(stderr_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass


async def _await_stderr(stderr_task: asyncio.Task[None]) -> None:
    """Wait (bounded) for the sidecar stderr drain to finish.

    Swallows timeouts/cancellations so stderr quirks cannot prevent the
    runner from emitting its final event.
    """
    if stderr_task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2.0)
    except TimeoutError:
        stderr_task.cancel()
        try:
            await asyncio.wait_for(stderr_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
    except asyncio.CancelledError:
        pass


async def _terminate_and_reap(proc) -> bool:
    """SIGTERM → wait ``_GRACE_SEC`` → SIGKILL. Returns True if SIGKILL fired.

    Safe to call repeatedly; swallows ``ProcessLookupError`` so double-kill
    during cancellation races does not raise.
    """
    if proc.returncode is not None:
        return False

    try:
        proc.terminate()
    except (ProcessLookupError, OSError) as exc:
        logger.debug("terminate failed: %s", exc)

    try:
        await asyncio.wait_for(proc.wait(), timeout=_GRACE_SEC)
        return False
    except TimeoutError:
        pass

    # Grace elapsed — SIGKILL.
    try:
        proc.kill()
    except (ProcessLookupError, OSError) as exc:
        logger.debug("kill failed: %s", exc)

    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError as exc:
        logger.debug("wait after kill failed: %s", exc)
    return True
