# BambuHelper — Claude Instructions

## What This Project Is

A Raspberry Pi departure board that displays live Bambu Lab 3D printer status on a 240×240 ST7789 SPI LCD (Waveshare 1.54" module). Runs natively on Raspberry Pi OS on a Pi Zero 2 W — no Docker, no cloud dependency beyond optional Bambu Cloud MQTT.

A faithful Python port of [Keralots/BambuHelper](https://github.com/Keralots/BambuHelper) (ESP32/C++) with native systemd deployment and a Flask web config portal replacing the ESP32's built-in AP web server.

## Repository Structure

```
/
├── CLAUDE.md                   ← You are here
├── REQUIREMENTS.md             ← Canonical requirements (read before coding)
├── INSTALL.md                  ← Wiring diagram, GPIO table, step-by-step guide
├── README.md                   ← Project overview, LAN/Cloud setup, troubleshooting
│
├── src/
│   ├── main.py                 ← Entry point: threads, render loop, SIGTERM handler
│   ├── bambu.py                ← MQTT client (LAN + Cloud), state parser, delta merge
│   ├── cloud.py                ← Cloud token helpers: JWT decode, user ID, region
│   ├── config.py               ← Config load/save/validate (JSON-based)
│   ├── display.py              ← ST7789 rendering via PIL (faithful port of C++ original)
│   ├── portal.py               ← Flask web config portal
│   ├── fonts/                  ← TTF fonts for display rendering
│   └── templates/
│       └── index.html          ← Web portal HTML (dark theme, no CDN)
│
├── tests/
│   ├── test_bambu.py           ← MQTT JSON parsing, delta merge, state logic
│   ├── test_config.py          ← Config validation, defaults, atomic write
│   └── test_display.py         ← Rendering (mocked spidev/RPi.GPIO)
│
├── systemd/
│   ├── bambu-helper.service    ← systemd service unit
│   └── bambu-helper-reboot.timer ← Optional weekly reboot timer
│
├── scripts/
│   └── get_cloud_token.py      ← Extract Bambu Cloud token via browser TLS
│
├── validate.py                 ← Post-install connectivity checker
├── install.sh                  ← Idempotent installer (run as root on Pi)
├── update.sh                   ← git pull + restart
├── requirements.txt            ← Pinned runtime dependencies
└── requirements-dev.txt        ← Dev-only: pytest, pytest-mock
```

## Requirements First

**Read `REQUIREMENTS.md` before making any changes.** All functional behaviour and requirement IDs (e.g. `ARCH-01`, `NET-03`, `DISP-05`) are defined there. Reference requirement IDs in code comments where non-obvious.

## Python Conventions

- Python 3.9+ compatible — no walrus operator in 3.8-incompatible ways, no 3.10+ match statements
- Type hints on all public functions and class methods
- Docstrings on all public functions (one-line for simple, multi-line for complex)
- `logging` module for all output — never `print()` in production code
- Log levels: `DEBUG` for render detail, `INFO` for connections and state changes, `WARNING` for recoverable errors, `ERROR` for failures
- Line length: 100 characters max

## Error Handling Conventions

- MQTT: `try/except Exception` in `_on_message` — malformed JSON must never crash the thread
- Config load: validate all fields, raise `ValueError` with clear message on invalid input
- Display init: if SPI raises on init, log and `sys.exit(1)` — do not mask hardware failures
- Never catch bare `except Exception` without re-logging with full context
- Back-off: ARCH-01 three-phase back-off (10s/60s/120s) — check `_shutdown_event` between retries

## Threading Model

- **Three threads**: main render thread + MQTT background thread + Flask portal thread
- **Shared state**: all `_printer_state`, `_connection_status`, `_fetch_error_count`, `_display_epoch`,
  `_screen_state` protected by `threading.Lock`
- Render thread **never** writes to shared state
- MQTT thread **never** calls display functions
- SIGTERM/SIGINT sets `_shutdown_event` — all threads check and exit cleanly (ARCH-07)
- `_restart_event` set by portal triggers config reload + MQTT reconnect (ARCH-08)

## Security — Do Not

- **NEVER** log or print `printer_access_code` or `bambu_token` — not in errors, not in debug
- **NEVER** use `tls_insecure_set(True)` on Cloud connections (only LAN — printer uses self-signed cert)
- **NEVER** run the service as root (SEC-03)
- **NEVER** hardcode printer IP, serial, access code, or token in source (SEC-06)
- **NEVER** use `eval()`, `exec()`, `os.system()`, or `subprocess` with config-derived strings (SEC-06)
- **NEVER** use `verify=False` on any `requests` call (SEC-07)

## Display Port Notes

The display module is a direct Python/PIL port of the original C++ `display_ui.cpp`, `display_gauges.cpp`,
`display_anim.cpp`, and `icons.h`. When changing rendering logic, cross-reference the original source.
Key mappings:
- `TFT_eSPI::drawSmoothArc()` → `PIL.ImageDraw.arc()` with PIL angle convention (0°=3 o'clock, CCW)
- RGB565 colours → RGB tuples (helper: `rgb565_to_rgb()` in `display.py`)
- PROGMEM bitmap arrays → Python `bytes` literals in `display.py`
- `millis()` → `time.monotonic() * 1000`

## How to Run Tests

```bash
# From project root on any machine (no Pi hardware needed)
pip install -r requirements-dev.txt
pytest --tb=short -q
```

All tests mock spidev, RPi.GPIO, and paho-mqtt — safe to run on Windows/macOS/Linux.

## How to Run the Validator (on Pi after install)

```bash
sudo /opt/bambu-helper/.venv/bin/python /opt/bambu-helper/validate.py
```

Expected output: 5 checks, all `[ PASS ]`.

## How to Install

```bash
# On the Pi, as root:
sudo bash install.sh
```

## How to Update

```bash
sudo bash /opt/bambu-helper/update.sh
```

## How to View Logs

```bash
journalctl -u bambu-helper -f
```

## Web Config Portal

```
http://<pi-ip>:8080
# Default credentials: admin / admin
# Change portal_password in config after first login
```

## Current Status

| Component | Status |
|---|---|
| REQUIREMENTS.md | Complete |
| src/config.py | Complete |
| src/cloud.py | Complete |
| src/bambu.py | Complete |
| src/display.py | Complete |
| src/portal.py | Complete |
| src/main.py | Complete |
| tests/ | Complete |
| validate.py | Complete |
| install.sh | Complete |
| systemd units | Complete |
