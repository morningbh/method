# /deploy-prod — Deterministic Production Deploy

Invocation: `/deploy-prod [--dry-run] [--skip-tests] [--skip-human-smoke] [--yes]`

## What this skill does

Spawns a sub-agent that runs `scripts/deploy.py` — a single deterministic Python script that handles backup, deploy, verify, and notify. **No LLM thinking decides what to do**; the script's exit code + report file are the source of truth.

## Why this skill exists

Ad-hoc deploy (dispatcher improvises steps) produced real failures in Method (2026-04-20):
- User-test gate was skipped because "deploy" appeared as an A/B/C option
- DB schema migration risk (`CREATE TABLE IF NOT EXISTS` gotcha) wasn't checked post-deploy
- Cloudflare / browser cache freshness not verified
- Backup only covered code (no DB, no .env, in `/tmp` which vanishes on reboot)
- No rollback command surfaced to user

Hence: fixed phases, fixed script, fixed report shape. The skill's only job is to fire the script and relay the summary.

## Phases (run strictly in order; any failure aborts)

1. **Phase A — BACKUP**
   1. Pre-flight: branch = `main`, working tree clean, `pytest` green, `docs/runs/*human-smoke*.md` ≤ 24 h old (unless `--skip-human-smoke`)
   2. Code backup to `/home/ubuntu/backups/<ts>-deploy/code/` (rsync)
   3. DB backup via `sqlite3 .backup` (atomic, WAL-safe) to `db/method.sqlite`
   4. `.env`, `.env.resend` to `env/` (chmod 0600)
   5. `data/uploads/` + `data/plans/` to `uploads/` + `plans/`
   6. **Verify**: integrity_check, row counts match live (±5 for WAL), code size ≥ 95%, env keys present, file counts match
   7. `rclone copy` backup to `gdrive:backups/method/<ts>/`
   8. Prune local `/home/ubuntu/backups/` to latest 10; prune gdrive > 90 d
2. **Phase B — DEPLOY**
   1. Call `scripts/promote-to-prod.sh --apply` (which does its own in-flight check + rsync + restart + local /api/health)
3. **Phase C — VERIFY LIVE**
   1. Public `https://method.xvc.com/api/health` = 200
   2. Schema containment: every table + column in dev DB also exists in prod DB (treats `CREATE TABLE IF NOT EXISTS` risk)
   3. CDN freshness: `/static/app.js` `last-modified` ≥ deploy start
   4. `journalctl -u method.service --since "-30s"` free of `ERROR` / `Traceback`
4. **Phase D — REPORT & NOTIFY**
   1. Write markdown report to `/home/ubuntu/method-dev/docs/runs/<ts>-deploy-<git-sha>.md`
   2. Print the rollback one-liner prominently to stdout

## Inputs

- `--dry-run` — run Phase A only (including gdrive upload + verify); **do not** call promote-to-prod.sh, do not restart, do not touch prod. Useful for exercising backup path.
- `--skip-tests` — bypass pytest gate (for emergency hotfix; logged to report).
- `--skip-human-smoke` — bypass the ≤24h human-smoke evidence gate (emergency only; logged).
- `--yes` — skip final "press Enter to continue after backup" prompt; required for agent invocation.

## Sub-agent prompt (use this verbatim)

> Run `/home/ubuntu/method-dev/scripts/deploy.py {args} --yes` from `cwd=/home/ubuntu/method-dev`. Stream its stdout. When it finishes, read the final report path (printed as `REPORT FILE: <path>` on its last lines) and the `VERDICT:` / `ROLLBACK CMD:` lines.
>
> The script is deterministic — **do not interpret, edit, or substitute its steps**. If the script exits non-zero, capture the failing phase/step from its own log and surface it exactly.
>
> Your return to the caller must be EXACTLY this shape (cap 200 words):
>
> ```
> VERDICT: PASS | FAIL
> PHASE: A | B | C | D  (last phase attempted)
> REPORT FILE: <absolute path>
> GDRIVE BACKUP: <gdrive:... URL or path>
> ROLLBACK CMD: <the one-liner printed by the script>
> NEXT STEP: <one sentence>
> ```
>
> If FAIL, list the failing step's error in ≤ 2 lines. Do not paraphrase — cite the exact stderr line.
>
> Do NOT run any other commands. Do NOT attempt to fix or re-run. The main agent decides next steps.

## Report file conventions

Path: `/home/ubuntu/method-dev/docs/runs/<YYYYMMDD-HHMMSS>-deploy-<git-short-sha>.md`

Required headings (the script writes these):
- `# Summary` (VERDICT, phase reached, duration, git sha)
- `# Phase A — Backup` (all 8 sub-steps with PASS/FAIL + details)
- `# Phase B — Deploy` (promote-to-prod.sh output + exit code)
- `# Phase C — Verify Live` (4 sub-checks)
- `# Rollback instructions` (exact commands + backup paths)
- `# Appendix: git diff file list`

## Main-agent summary shape (to user)

After receiving the sub-agent's return, the main agent forwards to the user:
- 1 line: VERDICT + duration + git sha
- 1 line: REPORT FILE path (clickable)
- 1 line: ROLLBACK CMD (always print, even on PASS)
- 1 line: NEXT STEP suggestion

That's it. No paraphrasing of the report; if user wants details they open the report file.

## What this skill does NOT do

- Does NOT merge branches. Merge to `main` first, then invoke this skill.
- Does NOT push to GitHub. Separate concern.
- Does NOT post to Feishu. Caller can use `/feishu` after PASS.
- Does NOT run the restore drill. See `/backup-restore-drill` for that.
