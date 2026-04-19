#!/bin/bash
set -euo pipefail
sudo cp deploy/method.service /etc/systemd/system/
sudo cp deploy/cloudflared.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable method cloudflared
sudo systemctl start method cloudflared
sleep 3
sudo systemctl status method --no-pager
sudo systemctl status cloudflared --no-pager
