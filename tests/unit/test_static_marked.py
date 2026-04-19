"""Unit tests for vendored marked.min.js (Task 4.2).

Contract source: ``docs/design/issue-3-m4-frontend-ui.md`` §4.3, §9 #25.

Ensures the file is on disk with a plausible size and the right shape —
catches "forgot to download / downloaded an HTML error page" regressions.
"""
from __future__ import annotations

from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[2] / "app"
_MARKED_PATH = _APP_DIR / "static" / "vendor" / "marked.min.js"


def test_marked_min_js_exists_and_reasonable_size():
    assert _MARKED_PATH.exists(), f"missing vendored file: {_MARKED_PATH}"
    size = _MARKED_PATH.stat().st_size
    # Expected ~40–60 KB; allow 20 KB lower bound to catch truncation and
    # 120 KB upper bound to catch "HTML error page was saved as JS".
    assert 20 * 1024 <= size <= 120 * 1024, (
        f"marked.min.js size {size} bytes outside reasonable 20KB..120KB "
        "range — possibly truncated or an HTML error page."
    )


def test_marked_min_js_first_bytes_look_like_marked():
    head = _MARKED_PATH.read_bytes()[:400].decode("utf-8", errors="replace")
    # Must reference "marked" somewhere in the header comment or code.
    assert "marked" in head.lower()
    # JS minified output will contain function syntax somewhere in the
    # first 400 bytes.
    assert ("function" in head) or ("=>" in head)
