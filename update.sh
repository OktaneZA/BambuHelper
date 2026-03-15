#!/usr/bin/env bash
# BambuHelper update script — git pull + reinstall deps + restart.

set -euo pipefail

INSTALL_DIR="/opt/bambu-helper"

[[ "$EUID" -eq 0 ]] || { echo "Run as root: sudo bash update.sh" >&2; exit 1; }

echo "[INFO] Pulling latest code …"
git -C "${INSTALL_DIR}" pull --ff-only

echo "[INFO] Updating dependencies …"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade -r "${INSTALL_DIR}/requirements.txt"

echo "[INFO] Restarting service …"
systemctl restart bambu-helper.service

echo "[INFO] Done. New version: $(git -C "${INSTALL_DIR}" rev-parse --short HEAD)"
