#!/bin/bash
set -euo pipefail
# Wait for tunnel URL to appear in journal (max 60s)
for i in {1..60}; do
  URL=$(journalctl -u cloudflared --since "5 minutes ago" --no-pager 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
  if [ -n "$URL" ]; then
    echo "Tunnel URL: $URL"
    # Update .env BASE_URL
    sed -i.bak "s|^BASE_URL=.*|BASE_URL=$URL|" /home/ubuntu/method/.env
    echo "BASE_URL updated in .env"
    exit 0
  fi
  sleep 1
done
echo "tunnel URL not found in journal"
exit 1
