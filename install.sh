#!/usr/bin/env bash
# BambuHelper installer — idempotent, must run as root on a Raspberry Pi.
# INST-01, INST-02, INST-03, INST-04, INST-05

set -euo pipefail

# ------------------------------------------------------------------ #
# Colours and helpers                                                  #
# ------------------------------------------------------------------ #

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${CYAN}▶ $*${NC}"; }
confirm() { read -r -p "$1 [y/N] " _ans; [[ "${_ans,,}" == "y" ]]; }

# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

INSTALL_DIR="/opt/bambu-helper"
CONFIG_DIR="/etc/bambu-helper"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SERVICE_FILE="/etc/systemd/system/bambu-helper.service"
TIMER_FILE="/etc/systemd/system/bambu-helper-reboot.timer"
SERVICE_USER="bambu-helper"
REPO_URL="https://github.com/YOUR_USER/bambuhelper.git"  # UPDATE THIS

# ------------------------------------------------------------------ #
# INST-02: Verify running on Raspberry Pi                              #
# ------------------------------------------------------------------ #

step "Checking environment"

[[ "$EUID" -eq 0 ]] || error "This installer must run as root (sudo bash install.sh)"

if [[ ! -f /proc/device-tree/model ]] || ! grep -qi "raspberry" /proc/device-tree/model; then
    error "This installer must run on a Raspberry Pi."
fi
info "Raspberry Pi detected: $(cat /proc/device-tree/model)"

# ------------------------------------------------------------------ #
# Install prerequisites                                                #
# ------------------------------------------------------------------ #

step "Installing system packages"

apt-get update -qq
for pkg in python3 python3-venv python3-pip python3-spidev python3-rpi.gpio git; do
    if dpkg -s "$pkg" &>/dev/null; then
        info "  $pkg already installed"
    else
        info "  Installing $pkg …"
        apt-get install -y -qq "$pkg"
    fi
done

# ------------------------------------------------------------------ #
# INST-03: Enable SPI                                                  #
# ------------------------------------------------------------------ #

step "Enabling SPI interface"

if raspi-config nonint get_spi | grep -q "0"; then
    info "SPI already enabled"
else
    raspi-config nonint do_spi 0
    info "SPI enabled"
fi

# ------------------------------------------------------------------ #
# Clone / update repo                                                  #
# ------------------------------------------------------------------ #

step "Installing BambuHelper to ${INSTALL_DIR}"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Updating existing installation …"
    git -C "${INSTALL_DIR}" fetch --quiet
    git -C "${INSTALL_DIR}" reset --hard origin/main --quiet
    info "Updated to $(git -C "${INSTALL_DIR}" rev-parse --short HEAD)"
else
    info "Cloning repository …"
    git clone --quiet "${REPO_URL}" "${INSTALL_DIR}"
    info "Cloned to ${INSTALL_DIR}"
fi

# ------------------------------------------------------------------ #
# Create venv and install dependencies                                 #
# ------------------------------------------------------------------ #

step "Setting up Python virtual environment"

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
info "Dependencies installed"

# ------------------------------------------------------------------ #
# INST-04: Create system user                                          #
# ------------------------------------------------------------------ #

step "Creating system user '${SERVICE_USER}'"

if id -u "${SERVICE_USER}" &>/dev/null; then
    info "User '${SERVICE_USER}' already exists"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
    info "User '${SERVICE_USER}' created"
fi

# Add to spi and gpio groups
for grp in spi gpio; do
    if getent group "$grp" &>/dev/null; then
        usermod -aG "$grp" "${SERVICE_USER}"
        info "  Added to group: $grp"
    else
        warn "  Group $grp not found — skipping"
    fi
done

# ------------------------------------------------------------------ #
# Interactive configuration                                            #
# ------------------------------------------------------------------ #

step "Configuring printer connection"

echo ""
echo "Choose connection mode:"
echo "  1) LAN  — direct local network connection (recommended)"
echo "  2) Cloud — Bambu Lab cloud MQTT"
read -r -p "Enter 1 or 2 [1]: " MODE_CHOICE
MODE_CHOICE="${MODE_CHOICE:-1}"

if [[ "$MODE_CHOICE" == "2" ]]; then
    CONNECTION_MODE="cloud"
    echo ""
    read -r -p "Bambu Cloud region (us/eu/cn) [us]: " REGION
    REGION="${REGION:-us}"
    echo ""
    warn "You need a Bambu Cloud token. Run: python scripts/get_cloud_token.py"
    read -r -s -p "Paste your cloud token: " BAMBU_TOKEN
    echo ""
    PRINTER_IP=""
    PRINTER_ACCESS_CODE=""
else
    CONNECTION_MODE="lan"
    REGION="us"
    BAMBU_TOKEN=""
    echo ""
    read -r -p "Printer IP address: " PRINTER_IP
    echo ""
    read -r -s -p "Access code (from printer touchscreen → Network): " PRINTER_ACCESS_CODE
    echo ""
fi

echo ""
read -r -p "Printer serial number: " PRINTER_SERIAL
read -r -p "Printer name (display label) [My Printer]: " PRINTER_NAME
PRINTER_NAME="${PRINTER_NAME:-My Printer}"

echo ""
read -r -s -p "Web portal password [admin]: " PORTAL_PASSWORD
echo ""
PORTAL_PASSWORD="${PORTAL_PASSWORD:-admin}"

# ------------------------------------------------------------------ #
# Write config file (SEC-02: 640 permissions)                          #
# ------------------------------------------------------------------ #

step "Writing config to ${CONFIG_FILE}"

mkdir -p "${CONFIG_DIR}"

cat > "${CONFIG_FILE}" <<JSONEOF
{
  "connection_mode": "${CONNECTION_MODE}",
  "printer_ip": "${PRINTER_IP}",
  "printer_access_code": "${PRINTER_ACCESS_CODE}",
  "printer_serial": "${PRINTER_SERIAL}",
  "printer_name": "${PRINTER_NAME}",
  "bambu_token": "${BAMBU_TOKEN}",
  "bambu_region": "${REGION}",
  "display_brightness": 100,
  "display_rotation": 0,
  "finish_timeout_s": 300,
  "show_clock": true,
  "portal_password": "${PORTAL_PASSWORD}",
  "portal_port": 8080
}
JSONEOF

chown root:"${SERVICE_USER}" "${CONFIG_FILE}"
chmod 640 "${CONFIG_FILE}"
info "Config written with permissions 640 (root:${SERVICE_USER})"

# ------------------------------------------------------------------ #
# Install systemd service (INST-05)                                    #
# ------------------------------------------------------------------ #

step "Installing systemd service"

cp "${INSTALL_DIR}/systemd/bambu-helper.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable bambu-helper.service
info "Service enabled"

# Optional weekly reboot timer
if confirm "Enable optional weekly reboot timer (recommended for stability)?"; then
    cp "${INSTALL_DIR}/systemd/bambu-helper-reboot.timer" "${TIMER_FILE}"
    cp "${INSTALL_DIR}/systemd/bambu-helper-reboot.service" \
       "/etc/systemd/system/bambu-helper-reboot.service"
    systemctl daemon-reload
    systemctl enable bambu-helper-reboot.timer
    systemctl start bambu-helper-reboot.timer
    info "Weekly reboot timer enabled (Sunday 03:00)"
fi

# ------------------------------------------------------------------ #
# Start service                                                        #
# ------------------------------------------------------------------ #

step "Starting BambuHelper service"

systemctl restart bambu-helper.service
sleep 2

if systemctl is-active --quiet bambu-helper.service; then
    info "Service started successfully"
else
    warn "Service may not have started yet — check: journalctl -u bambu-helper -n 50"
fi

# ------------------------------------------------------------------ #
# Summary                                                              #
# ------------------------------------------------------------------ #

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  BambuHelper installed successfully!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  Web portal:  ${CYAN}http://$(hostname).local:8080${NC}"
echo -e "  Credentials: ${CYAN}admin / ${PORTAL_PASSWORD}${NC}"
echo ""
echo -e "  Logs:        ${CYAN}journalctl -u bambu-helper -f${NC}"
echo -e "  Update:      ${CYAN}sudo bash ${INSTALL_DIR}/update.sh${NC}"
echo ""

# ------------------------------------------------------------------ #
# Offer to run validator (INST-06)                                     #
# ------------------------------------------------------------------ #

if confirm "Run post-install validator now?"; then
    "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/validate.py" || true
fi
