#!/bin/bash
# Install Python 3.11 alongside system Python 3.9 on Amazon Linux EC2.
# The app needs 3.10+ (mcp SDK). System python3 (3.9) stays untouched.
#
#   ./deploy/install-python311.sh

set -euo pipefail

install_amzn() {
  if command -v dnf >/dev/null 2>&1; then
    echo "→ Amazon Linux 2023 — installing python3.11 …"
    sudo dnf install -y python3.11 python3.11-pip
    return 0
  fi
  if command -v amazon-linux-extras >/dev/null 2>&1; then
    echo "→ Amazon Linux 2 — installing python3.11 …"
    sudo amazon-linux-extras install python3.11 -y
    return 0
  fi
  return 1
}

if command -v python3.11 >/dev/null 2>&1; then
  echo "✅ python3.11 already installed: $(python3.11 --version)"
  exit 0
fi

if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" == "amzn" ]] || [[ "${ID:-}" == "amazon" ]]; then
    install_amzn
    echo "✅ $(python3.11 --version)"
    exit 0
  fi
fi

echo "ERROR: Could not auto-install Python 3.11 on this OS."
echo "Install Python 3.10+ manually, then re-run ./deploy/setup-ec2.sh"
exit 1
