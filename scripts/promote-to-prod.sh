#!/bin/bash
# Promote /home/ubuntu/method-dev/ → /home/ubuntu/method/ and restart prod.
#
# Usage:
#   ./scripts/promote-to-prod.sh            # dry-run (prints what would change)
#   ./scripts/promote-to-prod.sh --apply    # actually copy + restart
#
# Excludes:
#   .env / .env.resend  — per-env secrets stay per-env
#   .venv/              — prod has its own venv
#   data/               — prod's DB / uploads / plans stay intact
#   __pycache__, etc.   — build artifacts
#
# Safety:
#   1. Refuses to run if prod has pending/running research in flight.
#   2. Creates a timestamped backup of the whole tree (code only) at /tmp
#      before overwriting.
#   3. After restart, waits up to 10s for /api/health to return 200.

set -euo pipefail

DEV=/home/ubuntu/method-dev
PROD=/home/ubuntu/method
APPLY=false

if [[ "${1:-}" == "--apply" ]]; then
  APPLY=true
fi

# 1. In-flight check (prod DB).
INFLIGHT=$(python3 -c "
import sqlite3
con = sqlite3.connect('$PROD/data/method.sqlite')
print(con.execute(\"SELECT COUNT(*) FROM research_requests WHERE status IN ('pending','running')\").fetchone()[0])
")
if [[ "$INFLIGHT" != "0" ]]; then
  echo "REFUSING: $INFLIGHT research request(s) in-flight on prod. Wait for them to finish."
  exit 1
fi

# 2. Diff what would change.
echo "=== files that will change (excluding .env, .venv, data) ==="
rsync -avn \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='.env' \
  --exclude='.env.resend' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='method.egg-info' \
  "$DEV/" "$PROD/" | grep -vE '^sending|^sent|^total|^$'

if ! $APPLY; then
  echo
  echo "(dry-run; rerun with --apply to actually promote)"
  exit 0
fi

# 3. Backup prod code.
BACKUP=/tmp/method-prod-backup-$(date +%s)
rsync -a \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='.env.resend' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  "$PROD/" "$BACKUP/"
echo "prod code backup: $BACKUP"

# 4. Copy dev → prod.
rsync -a --delete-after \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='.env' \
  --exclude='.env.resend' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='method.egg-info' \
  "$DEV/" "$PROD/"

# 5. Restart prod.
sudo systemctl restart method.service

# 6. Health check.
for i in $(seq 1 10); do
  sleep 1
  if curl -sSf http://127.0.0.1:8001/api/health > /dev/null 2>&1; then
    echo "prod restarted + healthy after ${i}s"
    exit 0
  fi
done
echo "WARNING: prod /api/health not 200 after 10s; investigate"
exit 1
