"""Static-asset grep tests for Issue #5 (error copy refresh).

Contract: ``docs/design/issue-5-error-copy.md`` §5, §6, §7.

Two responsibilities (all RED until ``app/static/app.js`` is refactored):

1. **No raw machine code leaked into alert() strings.** The reverse-scan
   pattern from integration-path-discipline L4: any `alert(... + <obj>.error)`
   concatenation is the anti-pattern that Issue #5 eliminates. The design
   registers `tests/test_static_assets.py::test_no_raw_error_alert` as the
   CI-level tripwire (§5 row for `app.js`).

2. **Central helper `showError(body, status, fallback?)` is defined.** The
   design §6.1 mandates a single renderer. Absence of the helper is the
   same shape of "implementation missed a file" bug we guard against.

This file reads ``app/static/app.js`` as text only. It performs NO JS
execution; the assertions are regex-based per design §5's "review-by-grep"
strategy. We avoid touching the implementation logic — only checking for
the presence / absence of textual patterns the design pins down.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


APP_JS = (
    Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"
)


def _read_app_js() -> str:
    assert APP_JS.exists(), f"app/static/app.js missing at {APP_JS}"
    return APP_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. No raw machine code leaked via `alert(... + <something>.error)`
# ---------------------------------------------------------------------------


# Matches the forbidden pattern family:
#   alert("发送失败：" + body.error)
#   alert("xxx：" + (body.error || r.status))
#   alert("foo" + data.error)
#   alert("..." + resBody.error)
# Any concatenation of a string literal with <word>.error inside alert() is
# an anti-pattern (design §6.1, §7).
_ALERT_RAW_ERROR = re.compile(
    r"alert\s*\([^)]*\+\s*(?:\(\s*)?[A-Za-z_][A-Za-z0-9_]*\.error\b"
)


def test_no_raw_error_alert_body_error() -> None:
    """No ``alert(... + body.error)`` — this is the exact bug Issue #5 fixes."""
    src = _read_app_js()
    matches = _ALERT_RAW_ERROR.findall(src)
    # Surface line-level context on failure for easier triage.
    lines = [
        (i + 1, line)
        for i, line in enumerate(src.splitlines())
        if _ALERT_RAW_ERROR.search(line)
    ]
    assert matches == [], (
        "app/static/app.js still contains `alert(... + <x>.error)` pattern "
        f"at lines {[ln for ln, _ in lines]}: {lines!r}"
    )


@pytest.mark.parametrize(
    "variable_name",
    ["body", "data", "resBody"],
    ids=["body.error", "data.error", "resBody.error"],
)
def test_no_raw_alert_for_common_variable_names(variable_name: str) -> None:
    """Explicit reverse-scan for the three known spellings in the current
    codebase (design §5 names all three)."""
    src = _read_app_js()
    pattern = re.compile(
        r"alert\s*\([^)]*\+\s*(?:\(\s*)?" + re.escape(variable_name) + r"\.error\b"
    )
    matches = pattern.findall(src)
    assert matches == [], (
        f"`alert(... + {variable_name}.error)` must be replaced by showError() "
        f"(design §6.1); found {len(matches)} occurrence(s)"
    )


# ---------------------------------------------------------------------------
# 2. Central helper defined
# ---------------------------------------------------------------------------


def test_show_error_helper_defined() -> None:
    """Design §6.1 mandates ``function showError(body, status, fallback)``."""
    src = _read_app_js()
    # Allow whitespace variation; accept both `function showError(` and
    # `const showError = function(...)`/arrow form.
    assert re.search(
        r"\bfunction\s+showError\s*\(", src
    ) or re.search(
        r"\bshowError\s*=\s*(?:function|\()", src
    ), "app.js must define the showError(body, status, fallback?) helper (design §6.1)"


def test_show_network_error_helper_defined() -> None:
    """Design §6.1 mandates ``function showNetworkError()`` for fetch throws."""
    src = _read_app_js()
    assert re.search(
        r"\bfunction\s+showNetworkError\s*\(", src
    ) or re.search(
        r"\bshowNetworkError\s*=\s*(?:function|\()", src
    ), "app.js must define showNetworkError() helper (design §6.1)"


def test_network_error_copy_string_present() -> None:
    """The fallback network-error string (design §4.2) must appear verbatim."""
    src = _read_app_js()
    assert "网络异常，请检查连接后重试" in src, (
        "expected network-error copy '网络异常，请检查连接后重试' in app.js (design §4.2)"
    )


def test_server_error_fallback_copy_present() -> None:
    """5xx generic fallback string (design §4.2 + §6.1) must appear verbatim."""
    src = _read_app_js()
    assert "服务器开小差了，请稍后重试" in src, (
        "expected 5xx fallback copy '服务器开小差了，请稍后重试' in app.js "
        "(design §4.2 / §6.1)"
    )


# ---------------------------------------------------------------------------
# 3. Call-site migration completeness (design §7 — the 17 alert lines)
# ---------------------------------------------------------------------------


def test_show_error_helper_is_actually_invoked() -> None:
    """A helper defined but never called = dead code. Design §7 migrates
    several alert call sites to use ``showError(...)``; at least some of
    them must invoke the helper."""
    src = _read_app_js()
    # Exclude the definition line(s) to be sure we count real invocations.
    invocation_count = len(
        re.findall(r"\bshowError\s*\(", src)
    ) - len(re.findall(r"\bfunction\s+showError\s*\(", src))
    assert invocation_count >= 3, (
        f"expected ≥3 showError() invocations per design §7 migration table, "
        f"got {invocation_count}"
    )
