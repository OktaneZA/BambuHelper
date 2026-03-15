# BambuHelper — Installation Guide

## Requirements

- Raspberry Pi Zero 2 W (64-bit; requires Raspberry Pi OS Bookworm or later)
- Waveshare 1.54" LCD Module (240×240, ST7789 SPI)
- MicroSD card (8 GB+)
- Internet access on the Pi for initial install

---

## 1. Prepare the Pi

1. Flash **Raspberry Pi OS Lite (64-bit)** using [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. In the Imager's advanced settings (gear icon), set:
   - Hostname (e.g. `bambuhelper`)
   - SSH enabled
   - WiFi SSID and password
   - Username and password
3. Boot the Pi and SSH in:
   ```bash
   ssh pi@bambuhelper.local
   ```

---

## 2. Wire the Display

Connect the Waveshare 1.54" LCD to the Pi GPIO header (see [`docs/wiring-diagram.svg`](docs/wiring-diagram.svg) for a visual diagram):

```
Waveshare 1.54" LCD          Raspberry Pi Zero 2 W
─────────────────────        ─────────────────────
VCC  (3.3V/5V)    ────────►  Pin 2   (5V)
GND               ────────►  Pin 6   (GND)
DIN (MOSI)        ────────►  Pin 19  (GPIO 10, SPI0 MOSI)
CLK (SCLK)        ────────►  Pin 23  (GPIO 11, SPI0 CLK)
CS                ────────►  Pin 24  (GPIO 8,  CE0)
DC                ────────►  Pin 22  (GPIO 25)
RST               ────────►  Pin 13  (GPIO 27)
BL (Backlight)    ────────►  Pin 18  (GPIO 24, PWM)
```

> **Tip:** Use female-to-female jumper wires. The display runs fine at 3.3V logic even when powered from 5V.

### GPIO Pinout Reference

```
                    3V3  [ 1][ 2] 5V  ◄── VCC
                  GPIO2  [ 3][ 4] 5V
                  GPIO3  [ 5][ 6] GND ◄── GND
                  GPIO4  [ 7][ 8] GPIO14
                    GND  [ 9][10] GPIO15
                 GPIO17  [11][12] GPIO18 ◄── BL
  RST ──► GPIO27 [13][14] GND
  DC  ──► GPIO22 [15][16] GPIO23 ◄── CLK
                    3V3  [17][18] GPIO24 ◄── CS
                 GPIO10  [19][20] GND
                  GPIO9  [21][22] GPIO25
                 GPIO11  [23][24] GPIO8
                    GND  [25][26] GPIO7
                  GPIO0  [27][28] GPIO1
                  GPIO5  [29][30] GND
                  GPIO6  [31][32] GPIO12
                 GPIO13  [33][34] GND
DIN ──► GPIO19   [35][36] GPIO16
                 GPIO26  [37][38] GPIO20
                    GND  [39][40] GPIO21
```

---

## 3. Run the Installer

```bash
# As root on the Pi:
sudo bash install.sh
```

The installer will:
1. Verify you are on a Raspberry Pi
2. Install system packages (`python3-venv`, `python3-pip`, `git`, `python3-spidev`, `python3-rpi.gpio`)
3. Enable the SPI interface
4. Clone the repo to `/opt/bambu-helper`
5. Create a Python virtual environment and install dependencies
6. Create a system user `bambu-helper`
7. Prompt you for your printer connection details
8. Write `/etc/bambu-helper/config.json` with secure permissions
9. Install and start the systemd service
10. Print the web portal URL

---

## 4. Configure via Web Portal

Open in your browser:
```
http://bambuhelper.local:8080
```
Default credentials: `admin` / `admin`

Enter your printer details (see [README.md](README.md) for LAN vs Cloud setup) and click **Save & Reconnect**.

---

## 5. Validate the Installation

```bash
sudo /opt/bambu-helper/.venv/bin/python /opt/bambu-helper/validate.py
```

All 5 checks should show `[ PASS ]`.

---

## 6. View Logs

```bash
journalctl -u bambu-helper -f
```

---

## Updating

```bash
sudo bash /opt/bambu-helper/update.sh
```

---

## Uninstalling

```bash
sudo systemctl stop bambu-helper
sudo systemctl disable bambu-helper
sudo rm -f /etc/systemd/system/bambu-helper.service
sudo systemctl daemon-reload
sudo userdel bambu-helper
sudo rm -rf /opt/bambu-helper /etc/bambu-helper
```
