#!/bin/sh
# Run ON the Pi, from the project directory: ./deploy/install.sh
# Checks prerequisites, runs offline tests, then installs and starts the service.
set -eu
cd "$(dirname "$0")/.."
DIR=$(pwd)

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "Python 3.11+ required (Raspberry Pi OS Bookworm or newer)"; exit 1; }
[ -f config.toml ] || { echo "Missing config.toml (copy config.example.toml and edit)"; exit 1; }
[ -f .env ] || { echo "Missing .env containing WMATA_API_KEY=..."; exit 1; }
chmod 600 .env
grep -q '^enabled = true' config.toml || echo "Note: enabled = false in config.toml; the service will exit until it is true."

python3 -m unittest discover -s tests -q
python3 bus_display.py probe

sed -e "s|@USER@|$(id -un)|g" -e "s|@DIR@|$DIR|g" deploy/kitchen-display.service \
  | sudo tee /etc/systemd/system/kitchen-display.service >/dev/null
sudo systemctl enable --now systemd-time-wait-sync.service
sudo systemctl daemon-reload
sudo systemctl enable kitchen-display.service
sudo systemctl restart kitchen-display.service
sleep 3
systemctl --no-pager --lines=10 status kitchen-display.service || true
echo "Logs: journalctl -u kitchen-display -f"
