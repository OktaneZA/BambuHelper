# BambuHelper — Requirements

## Architecture

| ID | Requirement |
|----|-------------|
| ARCH-01 | Three-phase exponential back-off for MQTT reconnection: attempts 1–5 → 10 s, attempts 6–15 → 60 s, attempts >15 → 120 s |
| ARCH-02 | Three-thread model: render thread (main), MQTT background thread, web portal thread |
| ARCH-03 | All shared printer state protected by `threading.Lock`; render thread never writes; MQTT thread never calls display functions |
| ARCH-04 | Delta-merge state updates: P1/A1 printers send partial MQTT payloads; only present keys overwrite existing state |
| ARCH-05 | Stale detection: LAN 60 s without MQTT message → mark printer disconnected; Cloud 300 s |
| ARCH-06 | `_display_epoch` counter incremented on every state change; render loop only redraws when epoch changes |
| ARCH-07 | SIGTERM and SIGINT set `_shutdown_event`; all threads check it and exit cleanly within one loop iteration |
| ARCH-08 | Config reload without process restart: `_restart_event` set by portal triggers MQTT reconnect with new credentials |

## Display

| ID | Requirement |
|----|-------------|
| DISP-01 | 240×240 ST7789 SPI display at 250 ms refresh rate (~4 Hz) |
| DISP-02 | Ten screen states matching original BambuHelper: SPLASH, CONNECTING_MQTT, IDLE, PRINTING, FINISHED, CLOCK, OFF (AP/WiFi states skipped — Pi handles WiFi natively) |
| DISP-03 | SCREEN_PRINTING: 2-row layout. Top row: Nozzle Temp arc gauge (left, cx=60) + Bed Temp arc gauge (right, cx=180), radius=50, arc\_width=6. Bottom row split by vertical divider: left panel = large progress percentage (34 px bold); right panel = ETA countdown or PAUSED/FAILED status (22 px bold). |
| DISP-04 | Arc gauges: 60°–300° sweep (240° span); track arc in `(12, 28, 28)`; fill arc proportional to value/max |
| DISP-05 | LED progress bar: Y=0–5, full-width 236 px; fill = `progress/100 * 236`; colour set by speed level |
| DISP-06 | Speed-level colour coding: 1=silent blue `(0,120,255)`, 2=standard green `(0,255,64)`, 3=sport orange `(255,160,0)`, 4=ludicrous red `(255,0,0)` |
| DISP-07 | 16×16 monochrome bitmap icons: nozzle, bed, fan, clock, layers, WiFi, check; 32×32 checkmark for completion animation |
| DISP-08 | Spinner animation: 12° advance per 250 ms frame, 60° arc width, wraps at 360° |
| DISP-09 | Animated dots: 3 dots, 4 states, 400 ms period |
| DISP-10 | Completion ring animation: radius expands 10→45 px over 400 ms, checkmark appears at 400 ms, static after 600 ms |
| DISP-11 | SCREEN_IDLE: nozzle + bed arc gauges only |
| DISP-12 | SCREEN_FINISHED: completion animation + filename + "Print Complete!" text |
| DISP-13 | SCREEN_CLOCK: digital clock + date |
| DISP-14 | Bottom bar: WiFi RSSI (dBm) | Layer N/M | speed level label |
| DISP-15 | ETA displayed in printing screen bottom-right panel; "PAUSED" or "FAILED" replaces ETA text when gcode\_state matches |
| DISP-16 | Background colour `(8, 12, 24)` (RGB565 `0x0861`); text white `(255,255,255)` |
| DISP-17 | `GET /preview` portal endpoint returns the last rendered frame as a 3× scaled PNG (720×720); requires auth; returns HTTP 503 if no frame yet rendered |

## Networking

| ID | Requirement |
|----|-------------|
| NET-01 | LAN mode: MQTT broker = `printer_ip:8883`, TLS with self-signed cert accepted (`tls_insecure_set(True)`), username `bblp`, password = access code |
| NET-02 | Cloud mode: broker `us.mqtt.bambulab.com` (US/EU) or `cn.mqtt.bambulab.com` (CN), TLS with system CAs, username = `u_{uid}`, password = token |
| NET-03 | Subscribe topic: `device/{serial}/report` |
| NET-04 | Pushall request published to `device/{serial}/request` every 30 s |
| NET-05 | paho-mqtt `MQTT_ERR_SUCCESS` on connect triggers immediate pushall and subscription |
| NET-06 | Cloud user ID extracted from JWT payload first; fallback to Bambu API `/v1/user-service/my/profile` |
| NET-07 | Region-aware cloud brokers: US and EU both use `us.mqtt.bambulab.com`; CN uses `cn.mqtt.bambulab.com` |
| NET-08 | MQTT buffer size: 16 384 bytes (to handle full pushall responses) |

## Security

| ID | Requirement |
|----|-------------|
| SEC-01 | `printer_access_code` and `bambu_token` must **never** appear in log output, tracebacks, or error messages |
| SEC-02 | Config file permissions `640` (root:bambu-helper); never world-readable |
| SEC-03 | Service runs as non-root system user `bambu-helper` |
| SEC-04 | Web portal auth depends on whether `portal_password` is set. Empty password: requests from `127.0.0.1` or `::1` are allowed without credentials; all other origins receive HTTP 403. Non-empty password: HTTP Basic Auth is required from all origins; incorrect credentials return HTTP 401 with `WWW-Authenticate`. |
| SEC-05 | Cloud MQTT uses system CA verification (`tls_set()` default); LAN uses `tls_insecure_set(True)` only because printer uses self-signed cert |
| SEC-06 | No `eval()`, `exec()`, `os.system()`, or `subprocess` with config-derived strings |
| SEC-07 | All outbound HTTPS calls use `verify=True` |
| SEC-08 | Portal password stored as `pbkdf2:sha256:260000:<salt_hex>:<base64_hash>`; `hash_password()` uses `hashlib.pbkdf2_hmac` + `secrets.token_hex` (stdlib only, no new dependencies). `verify_password()` detects legacy plaintext (no `pbkdf2:` prefix) and accepts it during migration. |

## Configuration

| ID | Requirement |
|----|-------------|
| CFG-01 | Config stored as JSON at `/etc/bambu-helper/config.json` |
| CFG-02 | Web portal port is randomly selected from the range 4001–65000 at install time (no collision guaranteed); the chosen port is written to config.json and shown in the install summary. `DEFAULTS` retains 8080 as a fallback only when the key is absent from the file. |
| CFG-03 | All config fields have documented defaults; missing fields filled from defaults on load |
| CFG-04 | Config validated at load time; invalid config raises `ValueError` with actionable message |
| CFG-05 | Config writes are atomic: write to `.tmp`, then `os.replace()` |

## Installation

| ID | Requirement |
|----|-------------|
| INST-01 | `install.sh` is idempotent: re-running on an already-installed system updates code without data loss |
| INST-02 | Installer verifies it is running on a Raspberry Pi before proceeding |
| INST-03 | Installer enables SPI interface via `raspi-config nonint do_spi 0` |
| INST-04 | Installer creates system user `bambu-helper` and adds to `spi` and `gpio` groups |
| INST-05 | Service managed by systemd; starts on boot after `network-online.target` |
| INST-06 | `validate.py` performs 5 post-install checks and prints `[ PASS ]` / `[ FAIL ]` per check |
