#!/usr/bin/env python3
"""Method web E2E runner — headless chromium screenshots against live tunnel.

Usage: RUN_E2E=1 python tests/e2e/web/runner.py

Verifies unauthenticated scenarios (see scenarios.md). Authenticated flows are
covered by tests/e2e/test_real_email_flow.py (real SMTP) +
tests/e2e/test_real_claude_call.py (real claude CLI) — this runner intentionally
does not attempt cookie injection, since doing it robustly with chrome-CLI
requires either playwright or a pre-seeded user-data-dir, both of which are
out of scope for M5.

Exits 0 on all-pass, 1 otherwise. Prints one line per scenario.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE_URL = os.environ.get(
    "BASE_URL", "https://que-career-favour-mode.trycloudflare.com"
)
CHROME = os.environ.get("CHROME_BIN", "google-chrome")
OUT = Path(__file__).parent / "screenshots"
MIN_SIZE_BYTES = 3000  # Blank/error pages are < 1KB; mobile viewport screenshots run ~4-5KB


def shot(name: str, url: str, width: int = 1280, height: int = 800) -> tuple[str, bool, int]:
    """Run headless chrome, take screenshot, verify size. Returns (name, ok, size)."""
    out = OUT / name
    if out.exists():
        out.unlink()
    cmd = [
        CHROME,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        f"--screenshot={out}",
        url,
    ]
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        return (name, False, 0)
    if not out.exists():
        return (name, False, 0)
    size = out.stat().st_size
    return (name, size >= MIN_SIZE_BYTES, size)


def main() -> int:
    if os.environ.get("RUN_E2E") != "1":
        print("skip: set RUN_E2E=1 to run web e2e scenarios")
        return 0

    OUT.mkdir(exist_ok=True)
    print(f"Method web e2e — BASE_URL={BASE_URL}")
    print(f"Screenshots: {OUT}")
    print()

    scenarios = [
        ("场景 1 登录页 desktop", lambda: shot("01-login-page.png", f"{BASE_URL}/login", 1280, 800)),
        ("场景 2 登录页 mobile",  lambda: shot("02-login-mobile.png", f"{BASE_URL}/login", 375, 667)),
        ("场景 3 健康检查",       lambda: shot("03-health.png", f"{BASE_URL}/api/health", 1280, 800)),
        ("场景 5 /history 未登录跳 login", lambda: shot("05-history-unauth.png", f"{BASE_URL}/history", 1280, 800)),
    ]

    results: list[tuple[str, bool, int]] = []
    for label, fn in scenarios:
        name, ok, size = fn()
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label} — {name} ({size} bytes)")
        results.append((label, ok, size))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    verdict = "PASS" if passed == total else "FAIL"
    print(f"{verdict}: {passed}/{total} scenarios")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
