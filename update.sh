#!/usr/bin/env bash
# BambuHelper update script — re-download latest release + reinstall deps + restart.

set -euo pipefail

INSTALL_DIR="/opt/bambu-helper"
TARBALL_URL="https://github.com/OktaneZA/bambuhelper/archive/refs/heads/master.tar.gz"

[[ "$EUID" -eq 0 ]] || { echo "Run as root: sudo bash update.sh" >&2; exit 1; }

echo "[INFO] Downloading latest release …"
TMP_DIR="$(mktemp -d)"
curl -fsSL "${TARBALL_URL}" | tar -xz -C "${TMP_DIR}"
EXTRACTED_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -1)"
rm -rf "${INSTALL_DIR}"
mv "${EXTRACTED_DIR}" "${INSTALL_DIR}"
rm -rf "${TMP_DIR}"

echo "[INFO] Updating dependencies …"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade -r "${INSTALL_DIR}/requirements.txt"

echo "[INFO] Restarting service …"
systemctl restart bambu-helper.service

echo "[INFO] Update complete."
