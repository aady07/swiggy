#!/bin/bash
# Pull latest code and restart the Swiggy app on EC2.
#
# Usage:
#   ./deploy/deploy.sh              # deploy main branch
#   ./deploy/deploy.sh main         # explicit branch
#
# First-time EC2 setup (once):
#   git clone git@github.com:aady07/swiggy.git /home/ec2-user/swiggy
#   cd /home/ec2-user/swiggy
#   cp deploy/env.production.example .env   # edit secrets
#   ./deploy/setup-ec2.sh                   # Python 3.11 venv + nginx HTTP + systemd
#   ./deploy/setup-ec2.sh --ssl             # same + certbot when DNS is ready

set -euo pipefail

APP_DIR="/home/ec2-user/swiggy"
BRANCH="${1:-main}"
PORT=8083
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

cd "$APP_DIR" || { echo "Missing $APP_DIR — clone the repo first."; exit 1; }

mkdir -p "$LOG_DIR" "$APP_DIR/data"

echo "→ Fetching $BRANCH …"
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

if [ ! -d "$VENV" ]; then
  echo "→ Creating virtualenv …"
  PY=""
  for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
      if "$cmd" -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)'; then
        PY="$cmd"
        break
      fi
    fi
  done
  if [ -z "$PY" ]; then
    echo "ERROR: Python 3.10+ required. Run: sudo dnf install -y python3.11"
    exit 1
  fi
  "$PY" -m venv "$VENV"
fi

echo "→ Bundling frontend CSS …"
python3 frontend/embed_css.py 2>/dev/null || true

echo "→ Installing dependencies …"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip
pip install -q -r requirements.txt

restart_systemd() {
  if systemctl list-unit-files swiggy.service &>/dev/null; then
    sudo systemctl restart swiggy
    sudo systemctl --no-pager status swiggy
    echo "✅ Deployed via systemd on port $PORT"
    echo "   https://swiggy.thetarunainitiative.in/"
    return 0
  fi
  return 1
}

restart_nohup() {
  echo "→ Restarting uvicorn (nohup) …"
  pkill -f "uvicorn backend.main:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  sleep 2

  LOG_FILE="$LOG_DIR/app-$(date '+%Y-%m-%d_%H-%M-%S').log"
  nohup "$VENV/bin/uvicorn" backend.main:app \
    --host 127.0.0.1 \
    --port "$PORT" \
    --workers 1 \
    >> "$LOG_FILE" 2>&1 &

  echo "✅ Deployed via nohup on port $PORT"
  echo "   Logs: $LOG_FILE"
  echo "   https://swiggy.thetarunainitiative.in/"
}

if restart_systemd; then
  exit 0
fi

restart_nohup
