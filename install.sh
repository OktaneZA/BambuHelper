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
TARBALL_URL="https://github.com/OktaneZA/bambuhelper/archive/refs/heads/master.tar.gz"

# ------------------------------------------------------------------ #
# INST-02: Verify running on Raspberry Pi                              #
# ------------------------------------------------------------------ #

step "Checking environment"

[[ "$EUID" -eq 0 ]] || error "This installer must run as root (sudo bash install.sh)"

if [[ ! -f /proc/device-tree/model ]] || ! grep -qi "raspberry" /proc/device-tree/model; then
    error "This installer must run on a Raspberry Pi."
fi
info "Raspberry Pi detected: $(tr -d '\0' < /proc/device-tree/model)"

# ------------------------------------------------------------------ #
# Install prerequisites                                                #
# ------------------------------------------------------------------ #

step "Installing system packages"

apt-get update -qq
for pkg in python3 python3-venv python3-pip python3-spidev python3-rpi.gpio git \
           libjpeg-dev zlib1g-dev libfreetype6-dev libopenjp2-7; do
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

info "Downloading latest release …"
TMP_DIR="$(mktemp -d)"
curl -fsSL "${TARBALL_URL}" | tar -xz -C "${TMP_DIR}"
# GitHub tarballs extract to <RepoName>-<branch>/ — use glob to handle any casing
EXTRACTED_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -1)"
rm -rf "${INSTALL_DIR}"
mv "${EXTRACTED_DIR}" "${INSTALL_DIR}"
rm -rf "${TMP_DIR}"
info "Installed to ${INSTALL_DIR}"

# ------------------------------------------------------------------ #
# Create venv and install dependencies                                 #
# ------------------------------------------------------------------ #

step "Setting up Python virtual environment"

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --prefer-binary -r "${INSTALL_DIR}/requirements.txt"
info "Dependencies installed"

step "Copying fonts"
mkdir -p "${INSTALL_DIR}/src/fonts"
for FONT_SRC in \
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf:RobotoMono-Regular.ttf" \
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf:RobotoMono-Bold.ttf"; do
    SRC="${FONT_SRC%%:*}"
    DST="${FONT_SRC##*:}"
    if [[ -f "$SRC" ]]; then
        cp "$SRC" "${INSTALL_DIR}/src/fonts/${DST}"
        info "  Copied $(basename "$SRC") → $DST"
    else
        warn "  Font not found: $SRC (display will use fallback)"
    fi
done

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

if [[ -f "${CONFIG_FILE}" ]]; then
    info "Existing config found at ${CONFIG_FILE} — skipping configuration prompts."
    info "To reconfigure, delete the file and re-run the installer:"
    info "  sudo rm ${CONFIG_FILE} && sudo bash install.sh"
    SKIP_CONFIG=true
else
    SKIP_CONFIG=false
fi

if [[ "${SKIP_CONFIG}" == "false" ]]; then

step "Configuring printer connection"

# Pre-initialise all interactive variables so set -u never fires when
# the script is piped via curl | bash (stdin is the pipe, not a tty).
# All reads redirect from /dev/tty so prompts reach the user's terminal.
MODE_CHOICE=""
CONNECTION_MODE="lan"
REGION="us"
BAMBU_TOKEN=""
PRINTER_IP=""
PRINTER_ACCESS_CODE=""
PRINTER_SERIAL=""
PRINTER_NAME="My Printer"
PORTAL_PASSWORD=""

echo ""
echo "Choose connection mode:"
echo "  1) LAN  — direct local network connection (recommended)"
echo "  2) Cloud — Bambu Lab cloud MQTT"
read -r -p "Enter 1 or 2 [1]: " MODE_CHOICE </dev/tty || true
MODE_CHOICE="${MODE_CHOICE:-1}"

if [[ "$MODE_CHOICE" == "2" ]]; then
    CONNECTION_MODE="cloud"
    echo ""
    read -r -p "Bambu Cloud region (us/eu/cn) [us]: " REGION </dev/tty || true
    REGION="${REGION:-us}"

    # ── Fetch token via Bambu API (same as Bambu Studio / OrcaSlicer) ───────
    echo ""
    info "Logging in to Bambu Lab API to fetch your JWT access token …"
    info "Enter your Bambu Lab account credentials at the prompts below."
    info "If Bambu sends a verification email, you will be prompted for the code."
    echo ""

    TOKEN_FILE="$(mktemp)"
    set +e
    "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/scripts/get_cloud_token.py" \
        --output-file "${TOKEN_FILE}" </dev/tty
    TOKEN_EXIT=$?
    set -e
    BAMBU_TOKEN="$(cat "${TOKEN_FILE}" 2>/dev/null || echo "")"
    rm -f "${TOKEN_FILE}"

    if [[ -z "${BAMBU_TOKEN}" ]] || [[ "${TOKEN_EXIT}" -ne 0 ]]; then
        warn "Token extraction failed — you can paste it manually or set it later via the portal."
        BAMBU_TOKEN=""
    else
        info "Token extracted successfully."
    fi

    if [[ -z "${BAMBU_TOKEN}" ]]; then
        echo ""
        echo "  To get your token manually, run on any PC with Python:"
        echo "    pip install requests"
        echo "    python /opt/bambu-helper/scripts/get_cloud_token.py"
        echo ""
        read -r -s -p "Paste your cloud token (or press Enter to configure later via the portal): " \
            BAMBU_TOKEN </dev/tty || true
        echo ""
    fi

    PRINTER_IP=""
    PRINTER_ACCESS_CODE=""
else
    CONNECTION_MODE="lan"
    REGION="us"
    BAMBU_TOKEN=""
    echo ""
    read -r -p "Printer IP address: " PRINTER_IP </dev/tty || true
    echo ""
    echo "  Access code: on the printer touchscreen go to Settings → LAN Only Mode,"
    echo "  toggle it ON, and the 8-character access code will be shown."
    echo "  You can turn LAN Only Mode back off after noting the code."
    read -r -s -p "Access code: " PRINTER_ACCESS_CODE </dev/tty || true
    echo ""
fi

echo ""
echo "Serial number: open Bambu Studio on your PC → go to your Device tab → click"
echo "the update/firmware button. The serial number (e.g. 01P00C...) is shown there."
read -r -p "Printer serial number: " PRINTER_SERIAL </dev/tty || true
read -r -p "Printer name (display label) [My Printer]: " PRINTER_NAME </dev/tty || true
PRINTER_NAME="${PRINTER_NAME:-My Printer}"

echo ""
read -r -s -p "Web portal password (leave blank for localhost-only access): " PORTAL_PASSWORD </dev/tty || true
echo ""
# No default — empty string means local-only mode (SEC-04)

# ------------------------------------------------------------------ #
# Select display screen model (CFG-06)                                 #
# ------------------------------------------------------------------ #

echo ""
echo "Select display screen model:"
echo "  1) Waveshare 1.54\"  240×240 ST7789  [default, recommended]"
echo "  2) Waveshare 2.0\"   320×240 ST7789"
echo "  3) Waveshare 1.3\"   240×240 ST7789"
read -r -p "Enter 1, 2, or 3 [1]: " SCREEN_CHOICE </dev/tty || true
SCREEN_CHOICE="${SCREEN_CHOICE:-1}"

case "$SCREEN_CHOICE" in
    2) DISPLAY_MODEL="waveshare_2in0" ;;
    3) DISPLAY_MODEL="waveshare_1in3" ;;
    *)
        DISPLAY_MODEL="waveshare_1in54"
        if [[ "$SCREEN_CHOICE" != "1" ]]; then
            warn "Unrecognised choice '${SCREEN_CHOICE}' — defaulting to 1.54\" (waveshare_1in54)"
        fi
        ;;
esac
info "Display model: ${DISPLAY_MODEL}"

# ------------------------------------------------------------------ #
# Find a free port above 4000 (CFG-02)                                #
# ------------------------------------------------------------------ #

step "Finding free port for web portal"

PORTAL_PORT=$(python3 - <<'PYEOF'
import socket, random, sys
for _ in range(100):
    port = random.randint(4001, 65000)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", port))
            print(port)
            sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PYEOF
)
info "Selected portal port: ${PORTAL_PORT}"

# ------------------------------------------------------------------ #
# Hash the portal password (SEC-08)                                   #
# ------------------------------------------------------------------ #

if [[ -n "${PORTAL_PASSWORD}" ]]; then
    # Pass password via stdin to avoid exposure in /proc/cmdline (HIGH-1)
    PORTAL_PASSWORD_HASH=$(printf '%s' "${PORTAL_PASSWORD}" | python3 -c "
import hashlib, secrets, base64, sys
pw = sys.stdin.read()
salt = secrets.token_hex(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 260000)
print(f'pbkdf2:sha256:260000:{salt}:{base64.b64encode(dk).decode()}')
")
else
    PORTAL_PASSWORD_HASH=""
    info "No password set — portal will be accessible from localhost only (SEC-04)"
fi

# ------------------------------------------------------------------ #
# Write config file (SEC-02: 640 permissions)                          #
# ------------------------------------------------------------------ #

step "Writing config to ${CONFIG_FILE}"

mkdir -p "${CONFIG_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG_DIR}"
chmod 750 "${CONFIG_DIR}"

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
  "display_model": "${DISPLAY_MODEL}",
  "finish_timeout_s": 300,
  "show_clock": true,
  "portal_password": "${PORTAL_PASSWORD_HASH}",
  "portal_port": ${PORTAL_PORT}
}
JSONEOF

chown "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG_FILE}"
chmod 640 "${CONFIG_FILE}"
info "Config written with permissions 640 (${SERVICE_USER}:${SERVICE_USER})"

fi # end SKIP_CONFIG

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
PORTAL_IP=$(hostname -I | awk '{print $1}')
# Read values from config in case we skipped the prompts
DISPLAY_PORT="${PORTAL_PORT:-$(python3 -c "import json; print(json.load(open('${CONFIG_FILE}'))['portal_port'])" 2>/dev/null || echo '???')}"
DISPLAY_PASS="${PORTAL_PASSWORD:-$(python3 -c "import json; print(json.load(open('${CONFIG_FILE}'))['portal_password'])" 2>/dev/null || echo '')}"
DISPLAY_MODEL_SHOWN="${DISPLAY_MODEL:-$(python3 -c "import json; print(json.load(open('${CONFIG_FILE}')).get('display_model','waveshare_1in54'))" 2>/dev/null || echo '???')}"
echo -e "  Display:     ${CYAN}${DISPLAY_MODEL_SHOWN}${NC}"
echo -e "  Web portal:  ${CYAN}http://${PORTAL_IP}:${DISPLAY_PORT}${NC}"
if [[ -n "${DISPLAY_PASS}" ]]; then
    echo -e "  Credentials: ${CYAN}admin / <password set during install>${NC}  (stored as PBKDF2 hash)"
else
    echo -e "  Access:      ${CYAN}Localhost only (no password set — use SSH tunnel for remote access)${NC}"
fi
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
