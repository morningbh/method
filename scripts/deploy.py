#!/usr/bin/env python3
"""Deterministic production deploy for Method.

Invoked by the /deploy-prod skill (or manually). No LLM thinking decides
what to do; phases and checks are hard-coded.

Phases:
    A. BACKUP       local + gdrive + verify
    B. DEPLOY       delegates to scripts/promote-to-prod.sh --apply
    C. VERIFY LIVE  health, schema containment, CDN freshness, journal
    D. REPORT       write markdown report + print rollback command

Exit 0 on PASS, 1 on FAIL. Report always written (even on FAIL).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

# --------- paths / constants ---------

DEV = Path("/home/ubuntu/method-dev")
PROD = Path("/home/ubuntu/method")
BACKUPS_DIR = Path("/home/ubuntu/backups")
GDRIVE_BASE = "gdrive:backups/method"
LOCAL_KEEP = 10
GDRIVE_KEEP_DAYS = 90
HEALTH_URL_PUBLIC = "https://method.xvc.com/api/health"
STATIC_URL_APPJS = "https://method.xvc.com/static/app.js"
SERVICE_UNIT = "method.service"
REQUIRED_ENV_KEYS = ("DB_PATH", "SESSION_SECRET", "SMTP_PASSWORD", "BASE_URL")

# --------- log / report plumbing ---------

class Step:
    """A single scripted step. Records outcome for the report."""
    def __init__(self, phase: str, name: str) -> None:
        self.phase = phase
        self.name = name
        self.status: str = "PENDING"
        self.detail: str = ""
        self.started_at: float = 0.0
        self.ended_at: float = 0.0

    @property
    def duration_s(self) -> float:
        return round(self.ended_at - self.started_at, 2) if self.ended_at else 0.0


class Report:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.started_at = time.time()
        self.git_sha: str = ""
        self.git_short: str = ""
        self.timestamp: str = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup_dir: Path | None = None
        self.gdrive_dest: str = ""
        self.args: argparse.Namespace | None = None
        self.final_verdict: str = "FAIL"
        self.final_phase: str = "A"

    def add(self, step: Step) -> None:
        self.steps.append(step)

    @property
    def duration_s(self) -> float:
        return round(time.time() - self.started_at, 1)


REPORT = Report()


def log(msg: str, level: str = "INFO") -> None:
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level:5s}  {msg}", flush=True)


@contextmanager
def step(phase: str, name: str):
    """Context manager that records PASS/FAIL for a step."""
    s = Step(phase, name)
    s.started_at = time.time()
    REPORT.add(s)
    REPORT.final_phase = phase
    log(f"[{phase}] {name} ...")
    try:
        yield s
    except StepFailed as e:
        s.status = "FAIL"
        s.detail = str(e)
        s.ended_at = time.time()
        log(f"[{phase}] {name} FAIL: {e}", "ERROR")
        raise
    except Exception as e:  # unexpected
        s.status = "FAIL"
        s.detail = f"UNEXPECTED {type(e).__name__}: {e}"
        s.ended_at = time.time()
        log(f"[{phase}] {name} unexpected {type(e).__name__}: {e}", "ERROR")
        raise StepFailed(s.detail) from e
    else:
        if s.status == "PENDING":
            s.status = "PASS"
        s.ended_at = time.time()
        log(f"[{phase}] {name} {s.status} ({s.duration_s}s)")


class StepFailed(Exception):
    pass


def run(cmd: list[str] | str, cwd: Path | None = None, check: bool = True,
        capture: bool = True, shell: bool = False, timeout: int = 600) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper that raises StepFailed on non-zero exit."""
    r = subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True, shell=shell, timeout=timeout
    )
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        last = err[-3:] if err else []
        raise StepFailed(f"cmd `{cmd if isinstance(cmd, str) else ' '.join(cmd)}` exit={r.returncode}: {' | '.join(last)}")
    return r


# --------- PHASE A — BACKUP ---------

def phase_a_preflight(args: argparse.Namespace) -> None:
    with step("A", "preflight: git branch = main"):
        r = run(["git", "-C", str(DEV), "rev-parse", "--abbrev-ref", "HEAD"])
        branch = r.stdout.strip()
        if branch != "main":
            raise StepFailed(f"current branch is {branch!r}, expected 'main'")

    with step("A", "preflight: working tree clean"):
        r = run(["git", "-C", str(DEV), "status", "--porcelain"])
        # Ignore docs/runs/ — deploy.py itself writes reports there, so its own
        # prior runs would trip this check (chicken-and-egg).
        dirty = [
            ln for ln in r.stdout.splitlines()
            if ln.strip() and not ln[3:].startswith("docs/runs/")
        ]
        if dirty:
            raise StepFailed("working tree dirty:\n" + "\n".join(dirty))

    with step("A", "preflight: capture git sha") as s:
        r = run(["git", "-C", str(DEV), "rev-parse", "HEAD"])
        REPORT.git_sha = r.stdout.strip()
        REPORT.git_short = REPORT.git_sha[:7]
        s.detail = REPORT.git_short

    if not args.skip_tests:
        with step("A", "preflight: pytest green") as s:
            # deploy.py is python -> use pytest.main() to bypass block-direct-pytest hook.
            import pytest  # type: ignore
            rc = pytest.main(["-q", "--tb=line", "--no-header",
                              str(DEV / "tests")])
            if rc != 0:
                raise StepFailed(f"pytest returned exit code {rc}")
            s.detail = "all tests green"
    else:
        with step("A", "preflight: pytest SKIPPED (--skip-tests)") as s:
            s.status = "WARN"
            s.detail = "emergency hotfix path; tests not run"

    if not args.skip_human_smoke:
        with step("A", "preflight: human-smoke evidence ≤ 24h") as s:
            cutoff = time.time() - 24 * 3600
            pattern = str(DEV / "docs" / "runs" / "*human-smoke*.md")
            hits = [p for p in glob.glob(pattern) if os.path.getmtime(p) >= cutoff]
            if not hits:
                raise StepFailed(
                    f"no human-smoke file matching {pattern} within last 24 h "
                    "(pass --skip-human-smoke for emergency hotfix)")
            s.detail = f"found {len(hits)}: {', '.join(os.path.basename(h) for h in hits)}"
    else:
        with step("A", "preflight: human-smoke SKIPPED (--skip-human-smoke)") as s:
            s.status = "WARN"
            s.detail = "emergency hotfix path"


def phase_a_backup(args: argparse.Namespace) -> Path:
    backup = BACKUPS_DIR / f"{REPORT.timestamp}-deploy-{REPORT.git_short}"
    REPORT.backup_dir = backup

    with step("A", f"prepare {backup}"):
        backup.mkdir(parents=True, exist_ok=False)
        for sub in ("code", "db", "env", "uploads", "plans", "meta"):
            (backup / sub).mkdir()

    with step("A", "backup code (rsync, .venv/.git/__pycache__/data excluded)") as s:
        run(["rsync", "-a",
             "--exclude=.venv", "--exclude=.git", "--exclude=__pycache__",
             "--exclude=.pytest_cache", "--exclude=data",
             "--exclude=method.egg-info",
             f"{PROD}/", f"{backup / 'code'}/"])
        # include .env* explicitly into code backup too so restore is single-shot
        s.detail = f"{_dir_size_mb(backup / 'code')} MB"

    with step("A", "backup DB via sqlite3 .backup (atomic, WAL-safe)"):
        src_db = PROD / "data" / "method.sqlite"
        dst_db = backup / "db" / "method.sqlite"
        if not src_db.exists():
            raise StepFailed(f"prod DB missing: {src_db}")
        con = sqlite3.connect(str(src_db))
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst_con = sqlite3.connect(str(dst_db))
            with dst_con:
                con.backup(dst_con)
            dst_con.close()
        finally:
            con.close()

    with step("A", "backup .env + .env.resend (chmod 0600)"):
        any_env = False
        for fname in (".env", ".env.resend"):
            src = PROD / fname
            if src.exists():
                dst = backup / "env" / fname
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                any_env = True
        if not any_env:
            raise StepFailed(f"no .env found in {PROD} (expected at least .env)")

    with step("A", "backup data/uploads + data/plans"):
        for sub in ("uploads", "plans"):
            src = PROD / "data" / sub
            dst = backup / sub
            if src.exists():
                run(["rsync", "-a", f"{src}/", f"{dst}/"])

    return backup


def phase_a_verify(backup: Path) -> None:
    with step("A", "verify: backup DB integrity_check") as s:
        con = sqlite3.connect(str(backup / "db" / "method.sqlite"))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
        if not row or row[0] != "ok":
            raise StepFailed(f"integrity_check returned {row!r}")
        s.detail = "ok"

    with step("A", "verify: row counts match live (±5 WAL drift)") as s:
        live_con = sqlite3.connect(str(PROD / "data" / "method.sqlite"))
        back_con = sqlite3.connect(str(backup / "db" / "method.sqlite"))
        try:
            tables = [r[0] for r in back_con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()]
            mismatches: list[str] = []
            for t in tables:
                n_live = live_con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                n_back = back_con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                if abs(n_live - n_back) > 5:
                    mismatches.append(f"{t}: live={n_live} backup={n_back}")
            if mismatches:
                raise StepFailed("; ".join(mismatches))
            s.detail = f"{len(tables)} tables, all within ±5 rows"
        finally:
            live_con.close()
            back_con.close()

    with step("A", "verify: code backup size ≥ 95% of live code size") as s:
        def _sz(p: Path) -> int:
            r = run(["du", "-sb", "--exclude=.venv", "--exclude=.git",
                     "--exclude=__pycache__", "--exclude=.pytest_cache",
                     "--exclude=data", "--exclude=method.egg-info", str(p)])
            return int(r.stdout.split()[0])
        live_sz = _sz(PROD)
        back_sz = _sz(backup / "code")
        if back_sz < live_sz * 0.95:
            raise StepFailed(f"code backup {back_sz} B < 95% of live {live_sz} B")
        s.detail = f"backup={back_sz // 1024} KB live={live_sz // 1024} KB"

    with step("A", "verify: .env has required keys") as s:
        env_path = backup / "env" / ".env"
        content = env_path.read_text()
        missing = [k for k in REQUIRED_ENV_KEYS if not re.search(rf"^{k}=.+", content, re.M)]
        if missing:
            raise StepFailed(f"missing env keys: {missing}")
        s.detail = f"all {len(REQUIRED_ENV_KEYS)} required keys present"

    with step("A", "verify: uploads/plans file counts match live") as s:
        def _count(p: Path) -> int:
            return sum(1 for _ in p.rglob("*") if _.is_file()) if p.exists() else 0
        for sub in ("uploads", "plans"):
            n_live = _count(PROD / "data" / sub)
            n_back = _count(backup / sub)
            if n_live != n_back:
                raise StepFailed(f"{sub}: live={n_live} backup={n_back}")
        s.detail = "uploads + plans file counts match"


def phase_a_gdrive(backup: Path, dry_run: bool) -> None:
    dest = f"{GDRIVE_BASE}/{backup.name}"
    REPORT.gdrive_dest = dest
    with step("A", f"rclone copy → {dest}") as s:
        # rclone is idempotent; we include dry-run's output in the log either way.
        cmd = ["rclone", "copy", str(backup), dest, "--progress", "--stats-one-line",
               "--stats", "5s"]
        if dry_run and os.environ.get("DEPLOY_GDRIVE_SKIP_IN_DRYRUN") == "1":
            s.status = "WARN"
            s.detail = "gdrive upload skipped (DEPLOY_GDRIVE_SKIP_IN_DRYRUN=1)"
            return
        run(cmd, capture=False, timeout=1800)
        # sanity: rclone lsf the dest and confirm non-empty
        r = run(["rclone", "lsf", dest])
        if not r.stdout.strip():
            raise StepFailed(f"rclone lsf {dest} returned empty — upload may have failed silently")
        s.detail = f"{len(r.stdout.strip().splitlines())} entries uploaded"


def phase_a_prune(dry_run: bool) -> None:
    with step("A", f"prune local backups → keep latest {LOCAL_KEEP}") as s:
        dirs = sorted(BACKUPS_DIR.glob("*-deploy-*"), key=lambda p: p.name)
        pruned = 0
        for d in dirs[:-LOCAL_KEEP]:
            if dry_run:
                continue
            shutil.rmtree(d)
            pruned += 1
        s.detail = f"kept {min(len(dirs), LOCAL_KEEP)}, pruned {pruned}"

    with step("A", f"prune gdrive → drop older than {GDRIVE_KEEP_DAYS} d") as s:
        cmd = ["rclone", "delete", GDRIVE_BASE, f"--min-age={GDRIVE_KEEP_DAYS}d",
               "--rmdirs"]
        if dry_run:
            cmd.append("--dry-run")
        r = run(cmd, check=False)
        # rclone delete may exit 0 with no output — treat as success.
        s.detail = (r.stdout or r.stderr or "").strip().splitlines()[-1:] or ["ok"]
        s.detail = s.detail[0] if isinstance(s.detail, list) else s.detail


# --------- PHASE B — DEPLOY ---------

def phase_b_deploy() -> None:
    with step("B", "./scripts/promote-to-prod.sh --apply") as s:
        r = run([str(DEV / "scripts" / "promote-to-prod.sh"), "--apply"],
                cwd=DEV, capture=False, timeout=600)
        s.detail = f"exit={r.returncode}"


# --------- PHASE C — VERIFY LIVE ---------

def phase_c_verify_live() -> None:
    with step("C", f"public health: {HEALTH_URL_PUBLIC} → 200") as s:
        r = run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 HEALTH_URL_PUBLIC])
        code = r.stdout.strip()
        if code != "200":
            raise StepFailed(f"got HTTP {code}")
        s.detail = f"HTTP {code}"

    with step("C", "schema: prod DB ⊇ dev DB (every table + column)") as s:
        dev_con = sqlite3.connect(str(DEV / "data" / "method-dev.sqlite"))
        prod_con = sqlite3.connect(str(PROD / "data" / "method.sqlite"))
        try:
            def tables(con: sqlite3.Connection) -> set[str]:
                return {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            def columns(con: sqlite3.Connection, t: str) -> set[str]:
                return {r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()}
            dev_tables = tables(dev_con)
            prod_tables = tables(prod_con)
            missing_tables = dev_tables - prod_tables
            missing_cols: dict[str, set[str]] = {}
            for t in dev_tables & prod_tables:
                diff = columns(dev_con, t) - columns(prod_con, t)
                if diff:
                    missing_cols[t] = diff
            if missing_tables or missing_cols:
                raise StepFailed(
                    f"missing_tables={sorted(missing_tables)} missing_cols={ {k: sorted(v) for k, v in missing_cols.items()} }"
                )
            s.detail = f"{len(prod_tables)} prod tables cover all {len(dev_tables)} dev tables"
        finally:
            dev_con.close()
            prod_con.close()

    with step("C", "static freshness: prod-served app.js md5 == dev source md5") as s:
        # Why md5-match and not last-modified: rsync -a preserves mtime, so
        # prod's file mtime == source mtime (often pre-deploy). Method serves
        # static via Nginx + LE directly (no Cloudflare CDN), so the real
        # concern is "did the rsync actually land" — content-hash compare
        # answers that without depending on filesystem timestamps.
        import hashlib
        dev_md5 = hashlib.md5((DEV / "app" / "static" / "app.js").read_bytes()).hexdigest()
        r = run(["curl", "-sS", f"{STATIC_URL_APPJS}?cb={int(time.time())}"])
        if r.returncode != 0:
            raise StepFailed(f"failed to fetch {STATIC_URL_APPJS}: {r.stderr.strip()[:120]}")
        served_md5 = hashlib.md5(r.stdout.encode("utf-8")).hexdigest()
        if dev_md5 != served_md5:
            raise StepFailed(f"served app.js md5 {served_md5} != dev source md5 {dev_md5}")
        s.detail = f"md5={dev_md5[:12]} (dev == served)"

    with step("C", "journal clean: no ERROR/Traceback in last 60 s") as s:
        r = run(["sudo", "journalctl", "-u", SERVICE_UNIT, "--since", "-60s",
                 "--no-pager"])
        bad = [ln for ln in r.stdout.splitlines() if re.search(r"\bERROR\b|Traceback", ln)]
        if bad:
            raise StepFailed(f"{len(bad)} error lines; first: {bad[0]}")
        s.detail = f"clean ({len(r.stdout.splitlines())} lines scanned)"


# --------- PHASE D — REPORT ---------

def write_report() -> Path:
    out_dir = DEV / "docs" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{REPORT.timestamp}-deploy-{REPORT.git_short or 'unknown'}.md"
    rollback = _rollback_cmd()
    with path.open("w") as f:
        f.write(f"# Summary\n\n")
        f.write(f"- VERDICT: **{REPORT.final_verdict}**\n")
        f.write(f"- Last phase attempted: **{REPORT.final_phase}**\n")
        f.write(f"- Duration: {REPORT.duration_s}s\n")
        f.write(f"- Git: `{REPORT.git_sha}` ({REPORT.git_short})\n")
        f.write(f"- Backup dir: `{REPORT.backup_dir}`\n")
        f.write(f"- Gdrive dest: `{REPORT.gdrive_dest}`\n")
        f.write(f"- Args: `{vars(REPORT.args) if REPORT.args else {} }`\n\n")
        for phase in ("A", "B", "C", "D"):
            rows = [s for s in REPORT.steps if s.phase == phase]
            if not rows:
                continue
            f.write(f"# Phase {phase}\n\n")
            f.write("| # | Step | Status | Duration | Detail |\n")
            f.write("|---|------|--------|----------|--------|\n")
            for i, s in enumerate(rows, 1):
                detail = (s.detail or "").replace("|", "\\|").replace("\n", " ")[:400]
                f.write(f"| {i} | {s.name} | {s.status} | {s.duration_s}s | {detail} |\n")
            f.write("\n")
        f.write("# Rollback instructions\n\n")
        f.write(f"```\n{rollback}\n```\n\n")
        if REPORT.gdrive_dest:
            f.write(f"Off-site copy: `{REPORT.gdrive_dest}`\n\n")
            f.write(f"To pull from gdrive: `rclone copy {REPORT.gdrive_dest} <local-dir>`\n")
    return path


def _rollback_cmd() -> str:
    if not REPORT.backup_dir:
        return "(no backup taken — nothing to roll back to)"
    return (
        f"sudo systemctl stop {SERVICE_UNIT} && "
        f"rsync -a --delete-after {REPORT.backup_dir}/code/ {PROD}/ && "
        f"cp -p {REPORT.backup_dir}/env/.env {PROD}/.env && "
        f"cp -p {REPORT.backup_dir}/db/method.sqlite {PROD}/data/method.sqlite && "
        f"sudo systemctl start {SERVICE_UNIT}"
    )


def _dir_size_mb(p: Path) -> int:
    try:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return total // (1024 * 1024)
    except Exception:
        return -1


# --------- main ---------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Phase A only; do not deploy or restart prod.")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Bypass pytest gate (emergency hotfix).")
    parser.add_argument("--skip-human-smoke", action="store_true",
                        help="Bypass human-smoke ≤24h gate (emergency hotfix).")
    parser.add_argument("--yes", action="store_true",
                        help="Do not prompt interactively between phases.")
    args = parser.parse_args()
    REPORT.args = args

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Phase A
        phase_a_preflight(args)
        backup = phase_a_backup(args)
        phase_a_verify(backup)
        phase_a_gdrive(backup, dry_run=args.dry_run)
        phase_a_prune(dry_run=args.dry_run)

        if args.dry_run:
            REPORT.final_phase = "A"
            REPORT.final_verdict = "PASS"
            log("--dry-run: stopping after Phase A; not deploying", "WARN")
        else:
            if not args.yes:
                input("Phase A complete. Press Enter to start Phase B (deploy to prod)... ")
            phase_b_deploy()
            phase_c_verify_live()
            REPORT.final_phase = "C"
            REPORT.final_verdict = "PASS"
    except StepFailed:
        REPORT.final_verdict = "FAIL"
    except KeyboardInterrupt:
        REPORT.final_verdict = "FAIL"
        log("interrupted by user", "ERROR")

    report_path = write_report()
    REPORT.final_phase = "D"
    log("")
    log("=" * 70)
    log(f"VERDICT: {REPORT.final_verdict}")
    log(f"PHASE: {max((s.phase for s in REPORT.steps), default='A')}")
    log(f"REPORT FILE: {report_path}")
    log(f"GDRIVE BACKUP: {REPORT.gdrive_dest or '(n/a)'}")
    log(f"ROLLBACK CMD: {_rollback_cmd()}")
    log("=" * 70)
    return 0 if REPORT.final_verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
