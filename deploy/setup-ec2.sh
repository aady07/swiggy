#!/bin/bash
# One-time EC2 bootstrap (works when system python3 is 3.9).
#
#   cd /home/ec2-user/swiggy
#   ./deploy/setup-ec2.sh          # HTTP + app
#   ./deploy/setup-ec2.sh --ssl    # + Let's Encrypt when DNS is ready

set -euo pipefail

APP_DIR="/home/ec2-user/swiggy"
PORT=8083
DO_SSL=false

for arg in "$@"; do
  case "$arg" in
    --ssl) DO_SSL=true ;;
  esac
done

cd "$APP_DIR" || { echo "Clone repo to $APP_DIR first."; exit 1; }

pick_python() {
  for cmd in python3.12 python3.11 python3.10; do
    if command -v "$cmd" >/dev/null 2>&1; then
      if "$cmd" -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

ensure_python311() {
  if PY=$(pick_python); then
    echo "$PY"
    return 0
  fi
  echo "→ System python3 is $(python3 --version 2>/dev/null || echo 'missing') — need 3.10+ for this app."
  echo "→ Installing Python 3.11 (keeps your system Python 3.9 as-is) …"
  bash "$APP_DIR/deploy/install-python311.sh"
  pick_python
}

echo "→ Checking Python …"
PY=$(ensure_python311) || {
  echo "ERROR: Python 3.11 install failed."
  exit 1
}
echo "   App will use $PY ($($PY --version))"
echo "   (system python3 stays $(python3 --version 2>/dev/null || echo 'unchanged'))"

if [ ! -f .env ]; then
  cp deploy/env.production.example .env
  echo "→ Created .env — edit GEMINI_API_KEY before going live: nano .env"
fi

mkdir -p data logs

# Recreate venv if it was built with Python 3.9
if [ -d .venv ]; then
  if ! .venv/bin/python -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "→ Removing old .venv (was Python 3.9) …"
    rm -rf .venv
  fi
fi

if [ ! -d .venv ]; then
  echo "→ Creating virtualenv with $PY …"
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
echo "   venv python: $(python --version)"
echo "→ Upgrading pip …"
python -m pip install --upgrade pip setuptools wheel
echo "→ Installing dependencies …"
pip install -r requirements.txt

if [ "$DO_SSL" = true ]; then
  if [ ! -f "/etc/letsencrypt/live/swiggy.thetarunainitiative.in/fullchain.pem" ]; then
    echo "→ Requesting SSL certificate (standalone, nginx paused briefly) …"
    sudo systemctl stop nginx
    sudo certbot certonly --standalone \
      -d swiggy.thetarunainitiative.in \
      --non-interactive --agree-tos -m admin@thetarunainitiative.in || {
        sudo systemctl start nginx
        echo "Certbot failed. Check DNS: swiggy.thetarunainitiative.in → this EC2 IP."
        exit 1
      }
    sudo systemctl start nginx
  fi
fi

if [ -f "/etc/letsencrypt/live/swiggy.thetarunainitiative.in/fullchain.pem" ]; then
  echo "→ Installing nginx config …"
  sudo cp deploy/nginx.conf.example /etc/nginx/conf.d/swiggy.conf
  sudo nginx -t
  sudo systemctl reload nginx
  echo "✅ nginx + HTTPS ready."
else
  echo ""
  echo "ℹ️  SSL cert not found — nginx config skipped (would fail nginx -t)."
  echo "   Get cert first:"
  echo "   sudo systemctl stop nginx"
  echo "   sudo certbot certonly --standalone -d swiggy.thetarunainitiative.in"
  echo "   sudo systemctl start nginx"
  echo "   sudo cp deploy/nginx.conf.example /etc/nginx/conf.d/swiggy.conf"
  echo "   sudo nginx -t && sudo systemctl reload nginx"
fi

if [ ! -f /etc/systemd/system/swiggy.service ]; then
  echo "→ Installing systemd service …"
  sudo cp deploy/swiggy.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable swiggy
fi

sudo systemctl restart swiggy
sudo systemctl --no-pager status swiggy || true

echo ""
echo "✅ Done."
echo "   Local app:  http://127.0.0.1:$PORT"
echo "   Public URL: http://swiggy.thetarunainitiative.in/  (HTTPS after cert step)"
