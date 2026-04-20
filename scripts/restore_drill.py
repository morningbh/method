#!/usr/bin/env python3
"""Restore drill — download latest backup, boot uvicorn against it, verify.

See .claude/skills/backup-restore-drill/SKILL.md for the contract.
Phases: SELECT → DOWNLOAD → EXTRACT → BOOT → EXERCISE → CLEANUP → REPORT.
Any phase failure aborts; report + Feishu notify; cleanup still runs.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEV = Path("/home/ubuntu/method-dev")
LOCAL_BACKUPS = Path("/home/ubuntu/backups")
GDRIVE_BASE = "gdrive:backups/method"
SANDBOX_ROOT = Path("/tmp")
REPORT_DIR = DEV / "docs" / "runs"
VENV_PY = DEV / ".venv" / "bin" / "python"
FEISHU_CHAT_ID = "ou_d9e4f77e8e63ddf2e32677fb72b1435b"

BOOT_TIMEOUT_SEC = 30
BOOT_POLL_INTERVAL = 0.5
SIGTERM_WAIT_SEC = 5


# --------- log / report plumbing ---------

log = logging.getLogger("drill")


class StepFailed(Exception):
    pass


@dataclasses.dataclass
class StepResult:
    phase: str
    name: str
    status: str  # PASS / FAIL / WARN / SKIP
    duration_s: float
    detail: str = ""


@dataclasses.dataclass
class Report:
    started_at: dt.datetime
    steps: list[StepResult] = dataclasses.field(default_factory=list)
    backup: str = "(not selected)"
    sandbox: str = "(not created)"
    port: int = 0
    uvicorn_tail: str = ""
    verdict: str = "RUNNING"
    last_phase: str = "SELECT"


REPORT = Report(started_at=dt.datetime.now())


class Step:
    def __init__(self, phase: str, name: str) -> None:
        self.phase = phase
        self.name = name
        self.started = 0.0
        self.detail = ""

    def __enter__(self) -> "Step":
        self.started = time.time()
        log.info("[%s] %s ...", self.phase, self.name)
        # CLEANUP/REPORT always run in finally; don't let them mask the real failing phase.
        if self.phase not in ("CLEANUP", "REPORT"):
            REPORT.last_phase = self.phase
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        dur = time.time() - self.started
        if exc is None:
            status = "PASS"
        elif isinstance(exc, StepFailed):
            status = "FAIL"
        else:
            status = "FAIL"
            self.detail = f"{exc.__class__.__name__}: {exc}"
        REPORT.steps.append(StepResult(self.phase, self.name, status, dur, self.detail))
        msg = f"[{self.phase}] {self.name} {status} ({dur:.2f}s)"
        if self.detail:
            msg += f" — {self.detail}"
        (log.error if status == "FAIL" else log.info)(msg)
        return False  # re-raise


def step(phase: str, name: str) -> Step:
    return Step(phase, name)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Sync shell-out with captured output; no check by default."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --------- phases ---------

def phase_select(source: str) -> tuple[str, str]:
    """Return (backup_id, backup_spec) where spec is a gdrive URI or local path."""
    if source == "gdrive":
        with step("SELECT", "rclone lsd gdrive:backups/method/") as s:
            r = run(["rclone", "lsd", GDRIVE_BASE])
            if r.returncode != 0:
                raise StepFailed(f"rclone lsd failed: {r.stderr.strip()}")
            # rclone lsd output: "   <size> <date> <time> <count> <name>"
            names = []
            for line in r.stdout.splitlines():
                m = re.match(r"\s*\S+\s+\S+\s+\S+\s+\S+\s+(\S+)", line)
                if m:
                    names.append(m.group(1))
            if not names:
                raise StepFailed(f"no backups found in {GDRIVE_BASE}")
            # Names start with YYYYMMDD-HHMMSS; lexical sort = chronological.
            latest = sorted(names)[-1]
            s.detail = f"{len(names)} backups; latest={latest}"
            REPORT.backup = f"{GDRIVE_BASE}/{latest}"
            return latest, f"{GDRIVE_BASE}/{latest}"
    else:
        with step("SELECT", "scan local /home/ubuntu/backups/") as s:
            if not LOCAL_BACKUPS.exists():
                raise StepFailed(f"{LOCAL_BACKUPS} does not exist")
            dirs = sorted(p.name for p in LOCAL_BACKUPS.iterdir() if p.is_dir())
            if not dirs:
                raise StepFailed(f"no backups in {LOCAL_BACKUPS}")
            latest = dirs[-1]
            s.detail = f"{len(dirs)} backups; latest={latest}"
            REPORT.backup = str(LOCAL_BACKUPS / latest)
            return latest, str(LOCAL_BACKUPS / latest)


def phase_download(spec: str, sandbox: Path, source: str) -> Path:
    src_dir = sandbox / "source"
    if source == "gdrive":
        with step("DOWNLOAD", f"rclone copy {spec}") as s:
            src_dir.mkdir(parents=True, exist_ok=True)
            r = run(["rclone", "copy", spec, str(src_dir), "--transfers", "8"])
            if r.returncode != 0:
                raise StepFailed(f"rclone copy failed: {r.stderr.strip()[:200]}")
            s.detail = f"downloaded to {src_dir}"
    else:
        with step("DOWNLOAD", "local source → symlink") as s:
            # Local backups are already on disk; a readonly symlink is enough.
            src_dir.symlink_to(spec)
            s.detail = f"symlink {src_dir} → {spec}"
    return src_dir


def phase_extract(src_dir: Path, sandbox: Path) -> tuple[Path, dict]:
    """Place DB + env in sandbox; return (db_path, env_dict)."""
    sandbox_srv = sandbox / "sandbox"
    sandbox_srv.mkdir(parents=True, exist_ok=True)
    db_backup = src_dir / "db" / "method.sqlite"
    env_backup = src_dir / "env" / ".env"
    if not db_backup.exists():
        raise StepFailed(f"backup missing DB at {db_backup}")
    if not env_backup.exists():
        raise StepFailed(f"backup missing .env at {env_backup}")

    with step("EXTRACT", "copy DB + env to sandbox") as s:
        db_sandbox = sandbox_srv / "method.sqlite"
        shutil.copy2(db_backup, db_sandbox)
        env_sandbox = sandbox_srv / ".env"
        shutil.copy2(env_backup, env_sandbox)

        # Parse env + override
        env: dict[str, str] = {}
        for line in env_backup.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            # Strip surrounding quotes if present
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = v

        uploads_dir = sandbox_srv / "uploads"
        plans_dir = sandbox_srv / "plans"
        log_dir = sandbox_srv / "logs"
        uploads_dir.mkdir()
        plans_dir.mkdir()
        log_dir.mkdir()

        env["DB_PATH"] = str(db_sandbox)
        env["UPLOAD_DIR"] = str(uploads_dir)
        env["PLAN_DIR"] = str(plans_dir)
        env["LOG_DIR"] = str(log_dir)
        # BASE_URL filled in after port pick (caller handles it)
        s.detail = f"sandbox={sandbox_srv}"
        return db_sandbox, env


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def phase_boot(env: dict, sandbox: Path) -> tuple[subprocess.Popen, int, Path]:
    with step("BOOT", "launch uvicorn on random free port") as s:
        port = _pick_free_port()
        REPORT.port = port
        env["BASE_URL"] = f"http://127.0.0.1:{port}"

        stderr_path = sandbox / "uvicorn.err"
        stdout_path = sandbox / "uvicorn.out"
        # Merged env: inherit PATH / HOME etc., overlay backup env
        full_env = {**os.environ, **env}
        cmd = [
            str(VENV_PY), "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(DEV),
            env=full_env,
            stdout=stdout_path.open("w"),
            stderr=stderr_path.open("w"),
        )
        s.detail = f"pid={proc.pid}, port={port}"

    with step("BOOT", "poll /api/health until 200") as s:
        deadline = time.time() + BOOT_TIMEOUT_SEC
        last_err = ""
        attempts = 0
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = stderr_path.read_text().splitlines()[-10:]
                raise StepFailed(f"uvicorn exited rc={proc.returncode} tail={tail!r}")
            attempts += 1
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                    body = resp.read().decode("utf-8", errors="replace")[:200]
                    if resp.status == 200:
                        s.detail = f"{attempts} attempts, body={body!r}"
                        return proc, port, stderr_path
                    last_err = f"status={resp.status} body={body!r}"
            except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
                last_err = f"{e.__class__.__name__}: {e}"
            time.sleep(BOOT_POLL_INTERVAL)
        raise StepFailed(f"/api/health never returned 200 within {BOOT_TIMEOUT_SEC}s; last_err={last_err}")


def phase_exercise(port: int) -> None:
    """Exercise a read path that needs DB access (not just a hardcoded OK)."""
    with step("EXERCISE", "GET /api/history (expect 401/403 unauthenticated)") as s:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/history", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                # 200 would be surprising (no cookie) — 401/403 is the happy path
                raise StepFailed(f"unexpected 2xx without auth: status={resp.status}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                s.detail = f"status={e.code} (auth middleware OK)"
                return
            raise StepFailed(f"unexpected status {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")


def phase_cleanup(proc: subprocess.Popen | None, sandbox: Path, keep: bool,
                  stderr_path: Path | None) -> None:
    with step("CLEANUP", "stop uvicorn (SIGTERM → wait → SIGKILL)") as s:
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=SIGTERM_WAIT_SEC)
                s.detail = f"SIGTERM; rc={proc.returncode}"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                s.detail = f"SIGKILL after {SIGTERM_WAIT_SEC}s; rc={proc.returncode}"
        else:
            s.detail = "already exited" if proc else "never started"

    if stderr_path and stderr_path.exists():
        lines = stderr_path.read_text().splitlines()
        REPORT.uvicorn_tail = "\n".join(lines[-50:])

    with step("CLEANUP", f"remove sandbox {sandbox}") as s:
        if keep:
            s.detail = "skipped (--keep)"
            return
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=False)
            s.detail = "removed"
        else:
            s.detail = "already gone"


def phase_report(report_path: Path) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ended = dt.datetime.now()
    duration = (ended - REPORT.started_at).total_seconds()
    lines = [
        f"# Summary",
        f"",
        f"- VERDICT: {REPORT.verdict}",
        f"- Started: {REPORT.started_at.isoformat(timespec='seconds')}",
        f"- Ended:   {ended.isoformat(timespec='seconds')}",
        f"- Duration: {duration:.1f}s",
        f"- Backup drilled: `{REPORT.backup}`",
        f"- Sandbox: `{REPORT.sandbox}`",
        f"- Uvicorn port: {REPORT.port or '(not bound)'}",
        f"- Last phase: {REPORT.last_phase}",
        f"",
    ]
    by_phase: dict[str, list[StepResult]] = {}
    for r in REPORT.steps:
        by_phase.setdefault(r.phase, []).append(r)
    for phase_name in ("SELECT", "DOWNLOAD", "EXTRACT", "BOOT", "EXERCISE", "CLEANUP"):
        results = by_phase.get(phase_name)
        lines.append(f"# {phase_name}")
        lines.append("")
        if not results:
            lines.append("_(not reached)_")
        else:
            for r in results:
                mark = "✅" if r.status == "PASS" else ("⚠️" if r.status in ("WARN", "SKIP") else "❌")
                lines.append(f"- {mark} **{r.name}** ({r.duration_s:.2f}s) — {r.status}"
                             + (f": {r.detail}" if r.detail else ""))
        lines.append("")
    lines.append("# Appendix: uvicorn stderr tail (last 50 lines)")
    lines.append("")
    lines.append("```")
    lines.append(REPORT.uvicorn_tail or "(none captured)")
    lines.append("```")
    report_path.write_text("\n".join(lines), encoding="utf-8")


def notify_feishu_on_fail(report_path: Path) -> None:
    if REPORT.verdict != "FAIL":
        return
    msg = (
        f"⚠️ Method restore drill FAILED\n"
        f"Phase: {REPORT.last_phase}\n"
        f"Backup: {REPORT.backup}\n"
        f"Report: {report_path}"
    )
    # FEISHU_CHAT_ID is an ou_* user open_id → use --user-id, not --chat-id.
    try:
        r = run([
            "lark-cli", "im", "+messages-send",
            "--user-id", FEISHU_CHAT_ID,
            "--text", msg,
        ], timeout=10)
        if r.returncode != 0:
            log.warning("lark-cli notify failed rc=%s stderr=%s", r.returncode, r.stderr[:200])
    except Exception as e:  # best-effort — never block the drill on Feishu failure
        log.warning("lark-cli notify exception: %s", e)


# --------- main ---------

def main() -> int:
    ap = argparse.ArgumentParser(description="Method restore drill")
    ap.add_argument("--source", choices=["gdrive", "local"], default="gdrive")
    ap.add_argument("--keep", action="store_true", help="skip cleanup for debugging")
    ap.add_argument("--yes", action="store_true", help="non-interactive")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)-6s %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = REPORT.started_at.strftime("%Y%m%d-%H%M%S")
    sandbox = SANDBOX_ROOT / f"restore-drill-{ts}"
    REPORT.sandbox = str(sandbox)
    sandbox.mkdir(parents=True, exist_ok=False)
    report_path = REPORT_DIR / f"{ts}-restore-drill.md"

    proc: subprocess.Popen | None = None
    stderr_path: Path | None = None
    try:
        _, spec = phase_select(args.source)
        src_dir = phase_download(spec, sandbox, args.source)
        _, env = phase_extract(src_dir, sandbox)
        proc, port, stderr_path = phase_boot(env, sandbox)
        phase_exercise(port)
        REPORT.verdict = "PASS"
    except StepFailed as e:
        REPORT.verdict = "FAIL"
        log.error("FAIL: %s", e)
    except Exception as e:
        REPORT.verdict = "FAIL"
        log.exception("unexpected exception: %s", e)
    finally:
        with contextlib.suppress(Exception):
            phase_cleanup(proc, sandbox, args.keep, stderr_path)
        phase_report(report_path)
        notify_feishu_on_fail(report_path)

    log.info("=" * 70)
    log.info("VERDICT: %s", REPORT.verdict)
    log.info("PHASE: %s", REPORT.last_phase)
    log.info("BACKUP: %s", REPORT.backup)
    log.info("REPORT FILE: %s", report_path)
    log.info("=" * 70)
    return 0 if REPORT.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
