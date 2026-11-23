#!/bin/sh
# Run on a computer with this checkout (including config.toml and .env):
#   ./deploy/push-to-pi.sh [user@]kitchen-pi.local
# Copies the project over SSH (no GitHub access needed on the Pi) and installs it.
set -eu
HOST=${1:-kitchen-pi.local}
cd "$(dirname "$0")/.."
[ -f config.toml ] && [ -f .env ] || { echo "Create config.toml and .env here first"; exit 1; }

COPYFILE_DISABLE=1 tar -czf - --exclude .git --exclude __pycache__ --exclude .venv --exclude .DS_Store . \
  | ssh "$HOST" 'mkdir -p kitchen-display && tar -xzf - -C kitchen-display'
ssh -t "$HOST" 'cd kitchen-display && sh deploy/install.sh'
