#!/bin/bash
set -euo pipefail
cd /home/ubuntu/method
git pull
.venv/bin/pip install -e ".[dev]" --quiet
sudo systemctl restart method
sleep 2
curl -fsS http://127.0.0.1:8001/api/health || { echo "health check failed"; exit 1; }
echo "deploy OK"
