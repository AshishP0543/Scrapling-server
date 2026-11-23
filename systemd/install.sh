#!/usr/bin/env bash
# Installs and enables the scrapling + cv-node cloudflared services so they
# start automatically on boot. Run with: sudo bash systemd/install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS=(scrapling-api.service scrapling-dashboard.service cloudflared-cvnode.service)

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash $0" >&2
  exit 1
fi

for u in "${UNITS[@]}"; do
  echo "Installing $u"
  install -m 0644 "$SRC_DIR/$u" "/etc/systemd/system/$u"
done

systemctl daemon-reload

for u in "${UNITS[@]}"; do
  systemctl enable "$u"
done

echo
echo "Installed and enabled. To (re)start now under systemd, first stop any"
echo "manually-launched copies, then:"
echo "  sudo systemctl restart ${UNITS[*]}"
echo
echo "Status:"
systemctl --no-pager status "${UNITS[@]}" || true
