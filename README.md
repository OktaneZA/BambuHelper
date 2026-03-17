# BambuHelper

> **Python port of [Keralots/BambuHelper](https://github.com/Keralots/BambuHelper) — all credit for the original design, icons, and display logic goes to [@Keralots](https://github.com/Keralots).**

A Python port of [BambuHelper](https://github.com/Keralots/BambuHelper) for the **Raspberry Pi Zero 2 W** with a **Waveshare 1.54" LCD** (240×240 ST7789). Displays live Bambu Lab 3D printer status — print progress, temperatures, fan speeds, layer count, and ETA — with the same faithful visual design as the original ESP32 version.

![Screen states: connecting → printing dashboard → finished](.github/screenshot.png)

---

## Hardware

| Component | Details |
|-----------|---------|
| Raspberry Pi | Zero 2 W (64-bit, Raspberry Pi OS Bookworm Lite) |
| Display | Waveshare 1.54" LCD Module — 240×240 ST7789 SPI |
| Printer | Any Bambu Lab printer (X1, X1C, X1E, P1P, P1S, A1, A1 Mini) |

---

## Display Wiring (GPIO)

| Display Pin | Pi GPIO | Physical Pin |
|-------------|---------|-------------|
| VCC | 5V | Pin 2 |
| GND | GND | Pin 6 |
| DIN (MOSI) | GPIO 10 | Pin 19 (SPI0 MOSI) |
| CLK (SCLK) | GPIO 11 | Pin 23 (SPI0 CLK) |
| CS | GPIO 8 | Pin 24 (CE0) |
| DC | GPIO 25 | Pin 22 |
| RST | GPIO 27 | Pin 13 |
| BL | GPIO 24 | Pin 18 |

> **Enable SPI** on the Pi: `sudo raspi-config` → Interface Options → SPI → Enable.
> The installer (`install.sh`) does this automatically.

See [INSTALL.md](INSTALL.md) for the full wiring diagram.

---

## Quick Install

```bash
# On your Pi Zero 2 W, as root:
curl -fsSL https://raw.githubusercontent.com/OktaneZA/bambuhelper/master/install.sh | sudo bash
```

Or after cloning:

```bash
git clone https://github.com/OktaneZA/bambuhelper.git
sudo bash bambuhelper/install.sh
```

---

## Configuration

After installation, open the web portal in your browser. The port is randomly
assigned during install from the range 4001–65000 — the exact URL is shown at
the end of the install output:

```
http://<pi-ip>:<port>
```

The installer prompts for a portal password:

- **Leave blank** — portal is accessible from **localhost only** (`127.0.0.1` / `::1`).
  Useful when you access the Pi via SSH tunnel (`ssh -L 8080:localhost:<port> pi@<pi-ip>`).
  All remote access is blocked with HTTP 403.
- **Set a password** — HTTP Basic Auth is required from all clients (username: `admin`).
  The password is stored as a PBKDF2-HMAC-SHA256 hash in the config file; it is never
  stored in plaintext.

To change the password after install, use the web portal form — the new value is
hashed automatically on save.

---

## LAN Mode Setup

LAN mode connects directly to your printer over your local network. No internet required after initial setup.

**What you need:**
1. **Printer IP address** — found in the printer's touchscreen under *Network* settings, or in your router's DHCP list. Tip: assign a static IP to avoid changes.
2. **Access code** — shown on the printer touchscreen under *Network → Access Code* (8 characters, e.g. `12345678`).
3. **Serial number** — shown on the touchscreen under *About* or on the label underneath the printer (format: `01P00A123456789`).

**In the web portal:**
1. Set *Connection Mode* → **LAN**
2. Enter *Printer IP*, *Access Code*, *Serial Number*
3. Click **Save & Reconnect**

**Troubleshooting LAN:**
- Can you ping the printer? `ping <printer-ip>`
- Is the access code correct? It changes if you press "Reset" in the printer's network settings.
- Check logs: `journalctl -u bambu-helper -f`

---

## Cloud Mode Setup

Cloud mode connects via Bambu Lab's MQTT cloud service. Useful if the Pi is on a different network from the printer, or if LAN access is blocked.

**Requirements:**
- A Bambu Lab account
- Your printer registered in Bambu Studio / Bambu Handy
- A **cloud token** (valid for ~3 months, then requires renewal)

### Getting Your Cloud Token

**Option A — Python helper script (recommended):**

```bash
# Install the helper dependency
pip install curl_cffi

# Run the token extractor
python scripts/get_cloud_token.py
```

Follow the prompts — it will open a browser session, log in, handle 2FA, and print your token.

**Option B — Browser DevTools:**

1. Open [Bambu Lab Studio](https://bambulab.com) in Chrome
2. Press **F12** → *Network* tab
3. Log in to your account
4. Filter requests for `api.bambulab.com`
5. Look for any request with an `Authorization: Bearer eyJ...` header
6. Copy the token (the part after `Bearer `)

### Configuring Cloud Mode in the Portal

1. Set *Connection Mode* → **Cloud**
2. Enter your **Token** (the `eyJ...` string)
3. Select your **Region**: `us` (Americas/Europe) or `cn` (China)
4. Enter your printer's **Serial Number**
5. Click **Save & Reconnect**

The portal extracts your user ID from the token automatically (JWT decode, no Bambu API call needed).

**Troubleshooting Cloud:**
- Token expired? Re-run `scripts/get_cloud_token.py` and update in the portal.
- Wrong region? EU accounts use the `us` region.
- Check logs: `journalctl -u bambu-helper -f`

---

## Screen States

| State | Shown When |
|-------|-----------|
| Splash | Boot (2 s) |
| Connecting | MQTT not yet connected (spinner + attempt counter) |
| Idle | Connected, printer not printing (nozzle + bed gauges) |
| Printing | Print in progress (full 6-gauge dashboard) |
| Paused | Print paused ("PAUSED" alert on info line) |
| Finished | Print complete (completion animation + filename) |
| Clock | After print finishes and timeout elapses |
| Off | Display timeout |

---

## Updating

```bash
sudo bash /opt/bambu-helper/update.sh
```

---

## Post-Install Validation

```bash
sudo /opt/bambu-helper/.venv/bin/python /opt/bambu-helper/validate.py
```

Expected: 5 checks, all `[ PASS ]`.

---

## Logs

```bash
journalctl -u bambu-helper -f
```

---

## Running Tests (dev)

```bash
pip install -r requirements-dev.txt
pytest --tb=short -q
```

Tests mock all Pi hardware — safe on Windows/macOS/Linux.

---

## Credits & Attribution

This project is a **Python port** of the original **[BambuHelper](https://github.com/Keralots/BambuHelper)** by [@Keralots](https://github.com/Keralots).

The original is an ESP32/Arduino C++ project that inspired this port in its entirety. The following were ported faithfully:

| Original file | Ported to | What it contains |
|---|---|---|
| `display_ui.cpp` | `src/display.py` | All screen states, layout, progress bar |
| `display_gauges.cpp` | `src/display.py` | Arc gauge drawing (240° sweep) |
| `display_anim.cpp` | `src/display.py` | Spinner, dots, completion ring animation |
| `icons.h` | `src/display.py` | All 16×16 and 32×32 bitmap icons |
| `bambu_mqtt.cpp` | `src/bambu.py` | MQTT topics, pushall, delta-merge, backoff |
| `bambu_cloud.cpp` | `src/cloud.py` | Cloud region, JWT decode, user ID |
| `bambu_state.h` | `src/bambu.py` | All state fields and JSON key mappings |

**Additions in this port:** Flask web config portal, LAN/Cloud mode switching, cloud token extractor, Raspberry Pi installer, systemd service, and a full pytest test suite.

See [NOTICE](NOTICE) for full attribution and third-party library licences.
