#!/usr/bin/env bash
# Install RC-1 operational dashboard on an Ubuntu VPS. Run as root from a release checkout.
set -euo pipefail
if [ "${EUID}" -ne 0 ]; then echo "Run as root: sudo deploy/install.sh" >&2; exit 1; fi
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
APP_DIR=/opt/quant-india
id -u quantindia >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin quantindia
install -d -o quantindia -g quantindia -m 0750 "$APP_DIR" "$APP_DIR/var" "$APP_DIR/logs" /etc/quant-india
# Exclude credentials, VCS data, and operational data from a source deployment.
tar --exclude=.git --exclude=.env --exclude=.venv --exclude=venv --exclude=data/raw \
  --exclude=data/clean --exclude=data/features --exclude=data/snapshots \
  -C "$ROOT_DIR" -cf - . | tar -C "$APP_DIR" -xf -
chown -R quantindia:quantindia "$APP_DIR"
runuser -u quantindia -- python3.12 -m venv "$APP_DIR/.venv"
runuser -u quantindia -- "$APP_DIR/.venv/bin/pip" install --upgrade pip
runuser -u quantindia -- "$APP_DIR/.venv/bin/pip" install "$APP_DIR"
if [ ! -f /etc/quant-india/env ]; then
  install -m 0640 -o root -g quantindia /dev/null /etc/quant-india/env
  echo "Created /etc/quant-india/env; populate it before starting the service."
fi
install -m 0644 "$APP_DIR/deploy/systemd/quant-india.service" /etc/systemd/system/quant-india.service
install -m 0644 "$APP_DIR/deploy/logrotate/quant-india" /etc/logrotate.d/quant-india
systemctl daemon-reload
systemctl enable quant-india.service
echo "Installed. Review /etc/quant-india/env, then run: sudo systemctl start quant-india"
