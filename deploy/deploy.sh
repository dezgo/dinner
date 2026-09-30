#!/bin/bash
# Run on do-personal by GitHub Actions (the deploy key is locked to this script).
set -euo pipefail
APP_DIR=/var/www/dinner

sudo chown -R derek:derek "$APP_DIR"
cd "$APP_DIR"
git fetch --prune origin
git reset --hard origin/main   # .env is untracked and survives this
git --no-pager log --oneline -1
.venv/bin/pip install -q -r requirements.txt
sudo chown -R www-data:www-data "$APP_DIR"
sudo systemctl restart dinner

for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8030/healthz >/dev/null 2>&1; then
    echo "Healthy after ${i}s: $(curl -fsS http://127.0.0.1:8030/healthz)"
    exit 0
  fi
  sleep 1
done
echo "Dinner Tab did not become healthy"
sudo journalctl -u dinner -n 40 --no-pager
exit 1
