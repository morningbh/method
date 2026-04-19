import asyncio
import os
import pytest
from pathlib import Path
import tempfile

from app.services.claude_runner import stream


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1",
    reason="e2e not requested (set RUN_E2E=1)",
)


async def test_real_claude_stream_yields_done():
    """Actually spawn claude CLI with a trivial prompt, verify we receive a done event.

    This is the LP L1 antidote: proves our runner actually works with the real CLI,
    catching any argv drift, model-name issues, or stream-json format changes that
    unit tests with mocked subprocesses would miss.

    The prompt is intentionally minimal to reduce cost ($0.01-$0.10).
    """
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        events = []
        async for ev in stream("Answer with exactly: hello", cwd):
            events.append(ev)
            if ev[0] == "done":
                break
            if ev[0] == "error":
                pytest.fail(f"claude errored: {ev[1]}")

        # Must have at least one delta + one done
        assert any(ev[0] == "delta" for ev in events), "no delta events received"
        done_events = [ev for ev in events if ev[0] == "done"]
        assert len(done_events) == 1, f"expected 1 done event, got {len(done_events)}"
        _, markdown, cost_usd, elapsed_ms = done_events[0]
        assert len(markdown) > 0, "done event has empty markdown"
        assert cost_usd > 0, f"cost_usd should be positive, got {cost_usd}"
        assert elapsed_ms > 0, f"elapsed_ms should be positive, got {elapsed_ms}"

        # Log for debugging (captured by pytest -s)
        print(f"\nE2E_MARKER_MODEL={os.environ.get('CLAUDE_MODEL', 'default')}")
        print(f"E2E_MARKER_COST_USD={cost_usd}")
        print(f"E2E_MARKER_ELAPSED_MS={elapsed_ms}")
        print(f"E2E_MARKER_MARKDOWN_LEN={len(markdown)}")
