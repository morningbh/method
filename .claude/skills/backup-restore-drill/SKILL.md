---
name: backup-restore-drill
description: Periodic restore drill that verifies the latest off-site backup can be restored and booted. Use weekly via systemd timer, or ad-hoc when backup integrity is in question. Shells out to the deterministic `scripts/restore_drill.py`; no LLM-improvised steps.
---

# /backup-restore-drill — Restore Drill

Invocation: `/backup-restore-drill [--source gdrive|local] [--keep]`

## Why this skill exists

A backup that has never been restored is belief, not recovery. Method's deploy pipeline writes backups to `gdrive:backups/method/<ts>/` (see `/deploy-prod`), but nothing exercises those backups until a real incident — and incidents are the worst time to discover that the backup is unusable.

This skill runs a scripted drill: pull the latest backup, stand up a throw-away uvicorn against it, hit one read path, verify it answered, tear down. Exit code + report are the source of truth.

Symmetric with `/deploy-prod`: deterministic script, fixed phases, fixed report shape, mandatory Feishu notification on FAIL.

## Phases (strict order; abort on first failure)

1. **SELECT** — pick the latest backup.
   - `--source gdrive` (default): `rclone lsd gdrive:backups/method/` → most recent directory by name
   - `--source local`: latest under `/home/ubuntu/backups/`
2. **DOWNLOAD** — `rclone copy` into `/tmp/restore-drill-<ts>/source/` (skipped when `--source local`; symlinks instead).
3. **EXTRACT** — place the backup's DB at `/tmp/restore-drill-<ts>/sandbox/method.sqlite`; copy `.env` to `/tmp/.../sandbox/.env`; override `DB_PATH` / `UPLOAD_DIR` / `PLAN_DIR` / `BASE_URL` to point into the sandbox.
4. **BOOT** — spawn `uvicorn app.main:app --port <random-free>` with the sandbox env. Poll `/api/health` every 0.5 s up to 30 s; PASS when 200.
5. **EXERCISE** — hit one unauthenticated read endpoint (expects 401/403 — proves auth middleware + DB session lookup both run). Also re-check `/api/health` for schema-derived endpoints.
6. **CLEANUP** — SIGTERM uvicorn → wait 5 s → SIGKILL if still alive. `rm -rf /tmp/restore-drill-<ts>/` unless `--keep`.
7. **REPORT** — write markdown to `/home/ubuntu/method-dev/docs/runs/<ts>-restore-drill.md`. On FAIL: send Feishu DM to operator via `lark-cli im +messages-send` (best-effort; non-fatal if Feishu is down).

## Inputs

- `--source gdrive|local` — where to pull the backup from (default: `gdrive`).
- `--keep` — skip CLEANUP; leave `/tmp/restore-drill-<ts>/` for manual inspection. (For debugging only — cron invocations should never pass this.)
- `--yes` — skip any interactive prompt (required for cron / agent invocation).

## How to invoke

**Ad-hoc by agent (this skill):**
```
cd /home/ubuntu/method-dev
.venv/bin/python scripts/restore_drill.py --yes
```

**Weekly by systemd timer (see F3 in TODO):**
```
/etc/systemd/system/method-restore-drill.service  (ExecStart=…/scripts/restore_drill.py --yes)
/etc/systemd/system/method-restore-drill.timer    (OnCalendar=Mon 03:00 Asia/Shanghai)
```
systemd timer invokes the script directly — **not** through an LLM. LLM-in-loop for periodic jobs is overkill (see `deploy-discipline.md` §"Cron vs skill").

## Sub-agent prompt (use verbatim when dispatched)

> Run `/home/ubuntu/method-dev/scripts/restore_drill.py {args} --yes` from `cwd=/home/ubuntu/method-dev`. Stream its stdout. When it finishes, read the report path (`REPORT FILE: <path>`) and `VERDICT:` line.
>
> The script is deterministic — do not interpret, edit, or substitute its steps. If the script exits non-zero, the failing phase is reported in its own log; surface the exact error text.
>
> Your return to the caller must be EXACTLY this shape (cap 200 words):
>
> ```
> VERDICT: PASS | FAIL
> PHASE: SELECT | DOWNLOAD | EXTRACT | BOOT | EXERCISE | CLEANUP | REPORT
> BACKUP: <gdrive: URI or local path of the backup drilled>
> REPORT FILE: <absolute path>
> NEXT STEP: <one sentence>
> ```
>
> If FAIL, include the failing step's error in ≤ 2 lines (cite exact stderr). Do NOT attempt to fix or re-run.

## Report file conventions

Path: `/home/ubuntu/method-dev/docs/runs/<YYYYMMDD-HHMMSS>-restore-drill.md`

Required headings (the script writes these):
- `# Summary` — VERDICT, duration, backup drilled, sandbox path
- `# SELECT` — which backup + selection criterion
- `# DOWNLOAD` — rclone transfer stats (or "skipped: local source")
- `# EXTRACT` — sandbox layout + env overrides
- `# BOOT` — port, time-to-healthy, last uvicorn startup log lines
- `# EXERCISE` — endpoints exercised + response codes
- `# CLEANUP` — sandbox removed? notes
- `# Appendix: uvicorn stderr tail` — last 50 lines

## Main-agent summary shape (to user)

- 1 line: VERDICT + duration + backup drilled
- 1 line: REPORT FILE path
- 1 line: NEXT STEP (usually "none" on PASS; "investigate <failing phase>" on FAIL)

No paraphrasing of the report body.

## Hard invariants

- **MUST NOT touch production DB / uploads / plans.** All writes land under `/tmp/restore-drill-<ts>/`.
- **MUST use a random free port.** Never reuses 8001 (prod) or 8002 (dev) — would collide with live services.
- **MUST send Feishu on FAIL** (best-effort). Silent failure defeats the purpose of the drill.
- **MUST cleanup** /tmp on success *and* on failure, unless `--keep`.

## What this skill does NOT do

- Does NOT replace prod — it only verifies backup restorability.
- Does NOT restore uploads/plans to real paths; only DB. (Rationale: DB is the hard part; uploads/plans are plain files — `ls` + `file` is sufficient verification, covered by deploy.py's file-count check.)
- Does NOT validate data semantics (e.g., "is this comment from the right user?"). Schema-readable is the bar.
