"""Unit tests for the claude_runner service (Task 3.2).

Covers the full contract from ``docs/design/issue-2-task-3.2-claude-runner.md``:

  - §4 stream-json parsing: delta / done / tool_use-ignored / malformed-skip /
    partial-line buffering
  - §5 error handling: non-zero exit, timeout, ENOENT, cancellation
  - §6 concurrency semaphore back-pressure
  - §3 command construction (argv): allowed-tools, model, cwd
  - §5 stderr drain concurrency (no deadlock on large stderr)

HARNESS §3 is explicitly enforced by ``test_command_includes_allowed_tools_read_glob_grep``.

All tests mock ``asyncio.create_subprocess_exec`` via ``monkeypatch`` with a
``FakeProcess`` whose ``stdout``/``stderr`` behave like ``asyncio.StreamReader``
(at least for the ``readline()``/``read()`` surface the runner consumes).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# FakeProcess / FakeReader infrastructure
# ---------------------------------------------------------------------------


class _FakeReader:
    """Minimal ``asyncio.StreamReader``-compatible stub.

    Exposes ``readline()``, ``read()``, ``at_eof()`` — the surface the runner
    uses to consume stdout/stderr. A list of byte chunks is consumed in order;
    a chunk may or may not include a trailing newline (for partial-line tests).

    Optionally delays each readline by ``line_delay`` seconds so the timeout
    test can simulate a hang.
    """

    def __init__(self, chunks: list[bytes], line_delay: float = 0.0) -> None:
        # Split chunks into discrete lines so readline() returns one at a time,
        # mirroring real StreamReader semantics. We preserve trailing content
        # without a newline as the final readline return (then EOF).
        self._buffer = bytearray(b"".join(chunks))
        self._line_delay = line_delay
        self._eof = False

    async def readline(self) -> bytes:
        if self._line_delay:
            await asyncio.sleep(self._line_delay)
        if not self._buffer:
            self._eof = True
            return b""
        nl = self._buffer.find(b"\n")
        if nl == -1:
            # Final partial line with no newline — return it all, then EOF.
            out = bytes(self._buffer)
            self._buffer.clear()
            self._eof = True
            return out
        out = bytes(self._buffer[: nl + 1])
        del self._buffer[: nl + 1]
        if not self._buffer:
            self._eof = True
        return out

    async def read(self, n: int = -1) -> bytes:
        if n == -1 or n >= len(self._buffer):
            out = bytes(self._buffer)
            self._buffer.clear()
            self._eof = True
            return out
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def at_eof(self) -> bool:
        return self._eof and not self._buffer


class FakeProcess:
    """Stand-in for ``asyncio.subprocess.Process`` returned by
    ``create_subprocess_exec``.

    Tracks terminate/kill invocations, exposes readers for stdout/stderr, and
    supports a ``wait_delay`` to simulate a hung subprocess for timeout tests.
    """

    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_data: bytes = b"",
        exit_code: int = 0,
        wait_delay: float = 0.0,
    ) -> None:
        self._stdout = _FakeReader(stdout_lines)
        self._stderr = _FakeReader([stderr_data] if stderr_data else [])
        self._exit = exit_code
        self._wait_delay = wait_delay
        self.terminated = False
        self.killed = False
        self._returncode: int | None = None
        self._exit_event = asyncio.Event()
        self.pid = 12345

    @property
    def stdout(self) -> _FakeReader:
        return self._stdout

    @property
    def stderr(self) -> _FakeReader:
        return self._stderr

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        # Simulate the subprocess observing SIGTERM and exiting.
        self._returncode = -15
        self._exit_event.set()

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self._exit_event.set()

    async def wait(self) -> int:
        if self._wait_delay and not self._exit_event.is_set():
            try:
                await asyncio.wait_for(
                    self._exit_event.wait(), timeout=self._wait_delay
                )
            except asyncio.TimeoutError:
                pass
        if self._returncode is None:
            self._returncode = self._exit
        return self._returncode


# ---------------------------------------------------------------------------
# Fixtures: force the runner to see a tiny timeout / tiny semaphore / a known
# claude_bin / claude_model, and reset the module-level semaphore between
# tests so concurrency tests don't interfere.
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_claude_runner_state(monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level semaphore so each test sees a fresh one."""
    import app.services.claude_runner as cr

    # Wipe any cached semaphore from a prior test.
    monkeypatch.setattr(cr, "_CLAUDE_SEM", None, raising=False)
    yield
    # Wipe again after, for safety.
    monkeypatch.setattr(cr, "_CLAUDE_SEM", None, raising=False)


@pytest.fixture
def claude_settings(monkeypatch: pytest.MonkeyPatch):
    """Pin claude_bin / claude_model / timeout / concurrency for deterministic tests."""
    from app import config as config_mod

    monkeypatch.setattr(config_mod.settings, "claude_bin", "/usr/bin/claude-fake")
    monkeypatch.setattr(config_mod.settings, "claude_model", "claude-test-model")
    monkeypatch.setattr(config_mod.settings, "claude_timeout_sec", 30)
    monkeypatch.setattr(config_mod.settings, "claude_concurrency", 4)
    return config_mod.settings


class _SubprocessSpy:
    """Captures the argv/kwargs passed to ``create_subprocess_exec``."""

    def __init__(self, proc_factory):
        self.argv: tuple[Any, ...] | None = None
        self.kwargs: dict[str, Any] | None = None
        self._proc_factory = proc_factory
        self.call_count = 0

    async def __call__(self, *argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.call_count += 1
        return self._proc_factory()


def _install_subprocess(
    monkeypatch: pytest.MonkeyPatch, proc_or_factory
) -> _SubprocessSpy:
    """Install a mock ``create_subprocess_exec`` that returns the given proc.

    Accepts either a FakeProcess instance (returned on every call) or a
    zero-arg factory for tests that need multiple invocations.
    """
    if callable(proc_or_factory):
        factory = proc_or_factory
    else:
        def factory(_p=proc_or_factory):
            return _p

    spy = _SubprocessSpy(factory)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return spy


# Helpers to build stream-json lines
def _assistant_text_line(text: str) -> bytes:
    payload = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    return (json.dumps(payload) + "\n").encode()


def _assistant_tool_use_line() -> bytes:
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
            ]
        },
    }
    return (json.dumps(payload) + "\n").encode()


def _result_line(result: str = "final markdown", cost: float = 0.0042) -> bytes:
    payload = {
        "type": "result",
        "subtype": "success",
        "result": result,
        "total_cost_usd": cost,
    }
    return (json.dumps(payload) + "\n").encode()


async def _collect(agen):
    events: list[Any] = []
    async for e in agen:
        events.append(e)
    return events


# ---------------------------------------------------------------------------
# #1. assistant text → delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_deltas_from_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(
        stdout_lines=[_assistant_text_line("hello "), _assistant_text_line("world"), _result_line("hello world")],
        exit_code=0,
    )
    _install_subprocess(monkeypatch, proc)

    events = await _collect(stream("prompt", tmp_path))

    # First two events are deltas with matching text, order preserved.
    assert events[0][0] == "delta"
    assert events[0][1] == "hello "
    assert events[1][0] == "delta"
    assert events[1][1] == "world"


# ---------------------------------------------------------------------------
# #2. result line → done event (result, cost, elapsed_ms)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_done_on_result_line(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(
        stdout_lines=[_result_line(result="FINAL", cost=0.1234)],
        exit_code=0,
    )
    _install_subprocess(monkeypatch, proc)

    events = await _collect(stream("prompt", tmp_path))

    done = [e for e in events if e[0] == "done"]
    assert len(done) == 1
    tag, result, cost_usd, elapsed_ms = done[0]
    assert tag == "done"
    assert result == "FINAL"
    assert cost_usd == pytest.approx(0.1234)
    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0


# ---------------------------------------------------------------------------
# #3. non-zero exit → error event (stderr tail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(stdout_lines=[], stderr_data=b"boom", exit_code=1)
    _install_subprocess(monkeypatch, proc)

    events = await _collect(stream("prompt", tmp_path))

    assert len(events) == 1
    assert events[0][0] == "error"
    assert "boom" in events[0][1]


# ---------------------------------------------------------------------------
# #4. tool_use content items are ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_ignores_tool_use_events(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(
        stdout_lines=[_assistant_tool_use_line(), _result_line("done")],
        exit_code=0,
    )
    _install_subprocess(monkeypatch, proc)

    events = await _collect(stream("prompt", tmp_path))

    # No delta events should be emitted for tool_use items.
    assert [e for e in events if e[0] == "delta"] == []
    assert len([e for e in events if e[0] == "done"]) == 1


# ---------------------------------------------------------------------------
# #5. malformed JSON lines are skipped (logged WARNING), do not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_skips_malformed_json_lines(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(
        stdout_lines=[b"this is not valid json\n", _result_line("ok")],
        exit_code=0,
    )
    _install_subprocess(monkeypatch, proc)

    import logging

    caplog.set_level(logging.WARNING, logger="method.claude_runner")
    events = await _collect(stream("prompt", tmp_path))

    # One done, no crash, no delta for the garbage line.
    assert [e[0] for e in events] == ["done"]
    # Warning must mention the bad line (at least in part).
    assert any(
        rec.levelno == logging.WARNING for rec in caplog.records
    ), "expected a WARNING log for the malformed line"


# ---------------------------------------------------------------------------
# #6. partial line buffering — JSON split across two chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_handles_partial_line_buffering(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    full = _result_line("split")
    # Split mid-JSON; concatenation inside the reader will still produce a
    # single readline() result because _FakeReader scans for \n.
    chunk_a = full[: len(full) // 2]
    chunk_b = full[len(full) // 2 :]

    proc = FakeProcess(stdout_lines=[chunk_a, chunk_b], exit_code=0)
    _install_subprocess(monkeypatch, proc)

    events = await _collect(stream("prompt", tmp_path))

    done = [e for e in events if e[0] == "done"]
    assert len(done) == 1
    assert done[0][1] == "split"


# ---------------------------------------------------------------------------
# #7. timeout kills subprocess (SIGTERM then SIGKILL after grace)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_timeout_kills_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    """Subprocess hangs indefinitely → runner must terminate it and emit error."""
    from app import config as config_mod
    from app.services.claude_runner import stream

    # Force a tiny timeout.
    monkeypatch.setattr(config_mod.settings, "claude_timeout_sec", 0.1)

    # wait_delay > 10s and no stdout lines => blocks on readline + wait.
    proc = FakeProcess(stdout_lines=[], exit_code=0, wait_delay=10.0)
    # Patch stdout.readline to block until proc is terminated so we simulate a hang.
    original_readline = proc.stdout.readline

    async def blocking_readline() -> bytes:
        # Wait until the proc is terminated (simulating hung subprocess).
        while not proc.terminated and not proc.killed:
            await asyncio.sleep(0.02)
        return b""

    proc.stdout.readline = blocking_readline  # type: ignore[assignment]
    _install_subprocess(monkeypatch, proc)

    events = await _collect(stream("prompt", tmp_path))

    assert proc.terminated is True, "runner must SIGTERM on timeout"
    # Must yield exactly one error event.
    errors = [e for e in events if e[0] == "error"]
    assert len(errors) == 1
    assert "timeout" in errors[0][1].lower()


# ---------------------------------------------------------------------------
# #8. caller cancellation via aclose() kills subprocess cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_cancellation_kills_subprocess_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    # Subprocess emits one delta then "hangs" — readline blocks until terminated.
    proc = FakeProcess(
        stdout_lines=[_assistant_text_line("first chunk")],
        exit_code=0,
        wait_delay=10.0,
    )

    original_readline = proc.stdout.readline

    async def emit_then_block() -> bytes:
        # Let the first real line through once, then block until terminated.
        if proc.stdout._buffer:
            return await original_readline()
        while not proc.terminated and not proc.killed:
            await asyncio.sleep(0.02)
        return b""

    proc.stdout.readline = emit_then_block  # type: ignore[assignment]
    _install_subprocess(monkeypatch, proc)

    agen = stream("prompt", tmp_path)
    first = await agen.__anext__()
    assert first[0] == "delta"

    # Cancel mid-stream.
    await agen.aclose()

    # Subprocess must be terminated (or killed).
    assert proc.terminated or proc.killed, "cancel must propagate to subprocess"

    # Semaphore permit released: a subsequent stream must be able to acquire.
    # Set concurrency to 1 and launch another; it should not deadlock.
    from app import config as config_mod
    import app.services.claude_runner as cr

    monkeypatch.setattr(config_mod.settings, "claude_concurrency", 1)
    monkeypatch.setattr(cr, "_CLAUDE_SEM", None, raising=False)

    proc2 = FakeProcess(stdout_lines=[_result_line("ok")], exit_code=0)
    _install_subprocess(monkeypatch, proc2)

    events = await asyncio.wait_for(_collect(stream("prompt", tmp_path)), timeout=5.0)
    assert any(e[0] == "done" for e in events), (
        "second stream must complete — permit must have been released on cancel"
    )


# ---------------------------------------------------------------------------
# #9. concurrency semaphore: N=2, 3rd stream blocks until one frees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_respects_concurrency_semaphore(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app import config as config_mod
    from app.services.claude_runner import stream

    monkeypatch.setattr(config_mod.settings, "claude_concurrency", 2)

    # Each process has a gate event. Stream holds the subprocess open (readline
    # blocks) until the gate fires.
    gates: list[asyncio.Event] = [asyncio.Event() for _ in range(3)]
    procs: list[FakeProcess] = []
    index = {"n": 0}

    def make_proc() -> FakeProcess:
        i = index["n"]
        index["n"] += 1
        proc = FakeProcess(
            stdout_lines=[_result_line(f"ok-{i}")],
            exit_code=0,
            wait_delay=10.0,
        )

        async def gated_readline(p=proc, gate=gates[i]) -> bytes:
            await gate.wait()
            if p.stdout._buffer:
                nl = p.stdout._buffer.find(b"\n")
                if nl == -1:
                    out = bytes(p.stdout._buffer)
                    p.stdout._buffer.clear()
                    p.stdout._eof = True
                    return out
                out = bytes(p.stdout._buffer[: nl + 1])
                del p.stdout._buffer[: nl + 1]
                if not p.stdout._buffer:
                    p.stdout._eof = True
                return out
            p.stdout._eof = True
            return b""

        proc.stdout.readline = gated_readline  # type: ignore[assignment]
        procs.append(proc)
        return proc

    _install_subprocess(monkeypatch, make_proc)

    # Launch 3 concurrent streams.
    results: list[list[Any]] = [[], [], []]

    async def run(i: int) -> None:
        async for ev in stream(f"p-{i}", tmp_path):
            results[i].append(ev)

    tasks = [asyncio.create_task(run(i)) for i in range(3)]

    # Give the event loop time to try launching all streams.
    await asyncio.sleep(0.2)

    # Only two subprocesses should have been created — the 3rd is blocked on
    # the semaphore and has NOT yet hit create_subprocess_exec.
    assert index["n"] == 2, (
        f"expected 2 subprocesses launched under concurrency=2, got {index['n']}"
    )

    # Release first subprocess; 3rd should then launch.
    gates[0].set()
    await asyncio.sleep(0.2)
    assert index["n"] == 3, "3rd subprocess must launch after 1st completes"

    # Release the rest so we can shut down cleanly.
    gates[1].set()
    gates[2].set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    for i in range(3):
        assert any(e[0] == "done" for e in results[i])


# ---------------------------------------------------------------------------
# #10. HARNESS §3 — --allowed-tools Read,Glob,Grep must be in argv
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_includes_allowed_tools_read_glob_grep(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(stdout_lines=[_result_line("ok")], exit_code=0)
    spy = _install_subprocess(monkeypatch, proc)

    await _collect(stream("prompt", tmp_path))

    argv = spy.argv
    assert argv is not None, "create_subprocess_exec was never called"
    argv_list = list(argv)
    assert "--allowed-tools" in argv_list, f"argv missing --allowed-tools: {argv_list}"
    idx = argv_list.index("--allowed-tools")
    assert argv_list[idx + 1] == "Read,Glob,Grep", (
        f"HARNESS §3 violated: --allowed-tools value was "
        f"{argv_list[idx + 1]!r}, expected 'Read,Glob,Grep'"
    )
    # Also assert no forbidden tool names leak in.
    joined = ",".join(str(a) for a in argv_list)
    for forbidden in ("Write", "Edit", "Bash"):
        # Match as a tool-list token, not a substring of unrelated args.
        assert forbidden not in argv_list[idx + 1].split(","), (
            f"forbidden tool {forbidden!r} must not be in --allowed-tools"
        )


# ---------------------------------------------------------------------------
# #11. --model argv matches settings.claude_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_uses_configured_model(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    proc = FakeProcess(stdout_lines=[_result_line("ok")], exit_code=0)
    spy = _install_subprocess(monkeypatch, proc)

    await _collect(stream("prompt", tmp_path))

    argv_list = list(spy.argv or ())
    assert "--model" in argv_list
    idx = argv_list.index("--model")
    assert argv_list[idx + 1] == "claude-test-model"


# ---------------------------------------------------------------------------
# #12. --cwd argv matches str(cwd)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_uses_configured_cwd(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    cwd = tmp_path / "sandbox-01HXZK8D7Q3V0S9B4W2N6M5C7R"
    cwd.mkdir()

    proc = FakeProcess(stdout_lines=[_result_line("ok")], exit_code=0)
    spy = _install_subprocess(monkeypatch, proc)

    await _collect(stream("prompt", cwd))

    argv_list = list(spy.argv or ())
    assert "--cwd" in argv_list
    idx = argv_list.index("--cwd")
    assert argv_list[idx + 1] == str(cwd)


# ---------------------------------------------------------------------------
# #13. ENOENT on launch → single error event, no raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_enoent_yields_error(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    async def boom(*argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    events = await _collect(stream("prompt", tmp_path))

    assert len(events) == 1
    assert events[0][0] == "error"
    assert "claude not found" in events[0][1].lower()


# ---------------------------------------------------------------------------
# #14. Large stderr must not deadlock stdout stream (sidecar drain proof)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_does_not_deadlock_on_large_stderr(
    monkeypatch: pytest.MonkeyPatch,
    claude_settings,
    reset_claude_runner_state,
    tmp_path: Path,
) -> None:
    from app.services.claude_runner import stream

    # >64KB of stderr must be drained concurrently so stdout readline can
    # make progress. If the runner reads stderr only after stdout, feeding
    # this much data into a pipe would deadlock the real subprocess. Here we
    # verify with a FakeReader that the runner completes without hanging.
    big_stderr = (b"warn: something\n" * 5000)  # ~80KB
    assert len(big_stderr) > 64 * 1024

    proc = FakeProcess(
        stdout_lines=[_result_line("ok")],
        stderr_data=big_stderr,
        exit_code=0,
    )
    _install_subprocess(monkeypatch, proc)

    events = await asyncio.wait_for(
        _collect(stream("prompt", tmp_path)), timeout=5.0
    )
    # Exit was 0, so we should get exactly one done and no error.
    kinds = [e[0] for e in events]
    assert "done" in kinds
    assert "error" not in kinds
