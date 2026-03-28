"""ST7789 display rendering for BambuHelper.

Faithful Python/PIL port of display_ui.cpp, display_gauges.cpp,
display_anim.cpp, and icons.h from Keralots/BambuHelper.

Supported display models (DISP-01, CFG-06):
  waveshare_1in54 — 240×240 ST7789 (default)
  waveshare_1in3  — 240×240 ST7789 (alternate VRH register)
  waveshare_2in0  — 320×240 ST7789 (landscape, MADCTL 0x70)

All three models share the same SPI wiring and init sequence; only the
MADCTL orientation byte, column/row address window, and VRH register differ.
The model is selected via config.json "display_model" and passed to
ST7789(model=...) on startup.

Screen states (DISP-02):
  SCREEN_SPLASH         — boot splash
  SCREEN_CONNECTING     — MQTT connecting (spinner + dots)
  SCREEN_IDLE           — connected, not printing (nozzle + bed gauges)
  SCREEN_PRINTING       — printing (2 gauges top, progress + ETA bottom)
  SCREEN_FINISHED       — print complete (completion animation)
  SCREEN_CLOCK          — digital clock
  SCREEN_OFF            — blank screen

PIL angle convention differs from TFT_eSPI: PIL 0° = 3 o'clock, goes CW.
The original C++ uses start=60°, end=300° for a 240° CCW arc.
We map: PIL start=150, end=150+240=390 (→30 mod 360) CW sweep.
"""

import logging
import os
import time
from typing import Any, NamedTuple, Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Screen state constants                                               #
# ------------------------------------------------------------------ #

SCREEN_SPLASH = "SPLASH"
SCREEN_CONNECTING = "CONNECTING"
SCREEN_IDLE = "IDLE"
SCREEN_PRINTING = "PRINTING"
SCREEN_FINISHED = "FINISHED"
SCREEN_CLOCK = "CLOCK"
SCREEN_OFF = "OFF"

# ------------------------------------------------------------------ #
# Colour palette (RGB, from RGB565 originals in config.h)             #
# DISP-16                                                             #
# ------------------------------------------------------------------ #

BG_COLOR = (8, 12, 24)          # 0x0861 — very dark navy
TEXT_COLOR = (255, 255, 255)    # white
TRACK_COLOR = (12, 28, 28)      # 0x18E3 — dark teal arc track
ACCENT_COLOR = (0, 200, 255)    # cyan accent (spinner, header line)
DIM_COLOR = (60, 60, 80)        # dimmed text / inactive gauges
BADGE_COLORS = {
    "RUNNING": (0, 200, 50),
    "PREPARE": (0, 200, 50),
    "PAUSE":   (255, 160, 0),
    "FINISH":  (0, 200, 50),
    "FAILED":  (220, 30, 30),
    "IDLE":    (80, 80, 100),
}

# Speed-level colours (DISP-06)
SPEED_COLORS = {
    1: (0, 120, 255),    # silent: blue
    2: (0, 255, 64),     # standard: green
    3: (255, 160, 0),    # sport: orange
    4: (255, 0, 0),      # ludicrous: red
}

# ------------------------------------------------------------------ #
# Display dimensions — module-level defaults for the 240×240 profile. #
# Kept for backward compatibility with tests that import WIDTH/HEIGHT. #
# Renderer instances derive actual dimensions from self._w / self._h. #
# ------------------------------------------------------------------ #

WIDTH = 240
HEIGHT = 240
PROGRESS_BAR_HEIGHT = 5

# ------------------------------------------------------------------ #
# Arc gauge parameters (DISP-04)                                       #
# PIL arc: angles in degrees, 0° = 3 o'clock, CW.                    #
# Original C++: 60°–300° CCW sweep = bottom 240° of circle.          #
# PIL equivalent: start=150°, end=30° (150 CW to 30 = 240°)          #
# ------------------------------------------------------------------ #

ARC_PIL_START = 150
ARC_PIL_END = 30
ARC_FULL_DEGREES = 240

# ------------------------------------------------------------------ #
# Display profiles (CFG-06)                                            #
#                                                                      #
# Hardware-specific SPI init parameters for each supported Waveshare  #
# ST7789 module. Selected via config.json "display_model".            #
# ------------------------------------------------------------------ #


class DisplayProfile(NamedTuple):
    """Hardware parameters for a Waveshare ST7789 display module."""

    width: int        # Horizontal resolution in pixels
    height: int       # Vertical resolution in pixels
    madctl: int       # MADCTL register value (0x36) — controls orientation
    col_end_hi: int   # CASET column-end high byte  (= width  - 1)
    col_end_lo: int   # CASET column-end low byte
    row_end_hi: int   # RASET row-end high byte     (= row_start + height - 1)
    row_end_lo: int   # RASET row-end low byte
    vrh_set: int = 0x12   # VRH register (0xC3) — output voltage range
    row_start_lo: int = 0x00  # RASET row-start low byte; 0x50 (80) for 240×240 modules
                              # whose ST7789 chip has a 320-row framebuffer offset


DISPLAY_PROFILES: dict[str, DisplayProfile] = {
    # 1.54" — 240×240 portrait. ST7789 chip has 320 rows internally; LCD is wired
    # starting at chip row 80 (0x50). RASET must be 0x0050–0x013F (80–319).
    "waveshare_1in54": DisplayProfile(
        width=240, height=240,
        madctl=0x00,
        col_end_hi=0x00, col_end_lo=0xEF,   # columns 0–239
        row_end_hi=0x01, row_end_lo=0x3F,   # rows 80–319 (end = 0x013F)
        row_start_lo=0x50,                   # start at chip row 80
    ),
    # 1.3" — same chip wiring as 1.54"; VRH differs per Waveshare datasheet
    "waveshare_1in3": DisplayProfile(
        width=240, height=240,
        madctl=0x00,
        col_end_hi=0x00, col_end_lo=0xEF,
        row_end_hi=0x01, row_end_lo=0x3F,   # rows 80–319
        vrh_set=0x0B,
        row_start_lo=0x50,
    ),
    # 2.0" — 320×240 landscape; MADCTL 0x70 = MX|MV|MH swaps axes; no row offset
    "waveshare_2in0": DisplayProfile(
        width=320, height=240,
        madctl=0x70,
        col_end_hi=0x01, col_end_lo=0x3F,   # columns 0–319
        row_end_hi=0x00, row_end_lo=0xEF,   # rows    0–239
    ),
}

# ------------------------------------------------------------------ #
# Icons (from icons.h) — 16×16 monochrome bitmaps (DISP-07)          #
# Each byte is a row of 8 pixels, MSB = leftmost pixel.              #
# ------------------------------------------------------------------ #

# Nozzle icon 16×16
ICON_NOZZLE = bytes([
    0b00000000, 0b00000000,
    0b00011110, 0b00000000,
    0b00111111, 0b00000000,
    0b00111111, 0b00000000,
    0b00011110, 0b00000000,
    0b00001100, 0b00000000,
    0b00001100, 0b00000000,
    0b00001100, 0b00000000,
    0b00001100, 0b00000000,
    0b00001100, 0b00000000,
    0b00001100, 0b00000000,
    0b00000100, 0b00000000,
    0b00000110, 0b00000000,
    0b00000011, 0b00000000,
    0b00000001, 0b00000000,
    0b00000000, 0b00000000,
])

# Bed/heated bed icon 16×16
ICON_BED = bytes([
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b01111110, 0b00000000,
    0b01111110, 0b00000000,
    0b01111110, 0b00000000,
    0b00000000, 0b00000000,
    0b11111111, 0b10000000,
    0b11111111, 0b10000000,
    0b00000000, 0b00000000,
    0b00100010, 0b00000000,
    0b00100010, 0b00000000,
    0b00100010, 0b00000000,
    0b01110111, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
])

# Fan icon 16×16
ICON_FAN = bytes([
    0b00000000, 0b00000000,
    0b00011000, 0b00000000,
    0b00111100, 0b00000000,
    0b01111110, 0b00000000,
    0b11111111, 0b00000000,
    0b11011011, 0b00000000,
    0b10011001, 0b00000000,
    0b11011011, 0b00000000,
    0b11111111, 0b00000000,
    0b01111110, 0b00000000,
    0b00111100, 0b00000000,
    0b00011000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
])

# Clock icon 16×16
ICON_CLOCK = bytes([
    0b00000000, 0b00000000,
    0b00111100, 0b00000000,
    0b01000010, 0b00000000,
    0b10011001, 0b00000000,
    0b10100101, 0b00000000,
    0b10100001, 0b00000000,
    0b10000001, 0b00000000,
    0b01000010, 0b00000000,
    0b00111100, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
])

# Layer/stack icon 16×16
ICON_LAYERS = bytes([
    0b00000000, 0b00000000,
    0b00011000, 0b00000000,
    0b00111100, 0b00000000,
    0b01111110, 0b00000000,
    0b11111111, 0b00000000,
    0b01111110, 0b00000000,
    0b00111100, 0b00000000,
    0b11111111, 0b00000000,
    0b01111110, 0b00000000,
    0b00111100, 0b00000000,
    0b11111111, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
])

# WiFi icon 16×16
ICON_WIFI = bytes([
    0b00000000, 0b00000000,
    0b01111110, 0b00000000,
    0b11000011, 0b00000000,
    0b00111100, 0b00000000,
    0b01000010, 0b00000000,
    0b00011000, 0b00000000,
    0b00100100, 0b00000000,
    0b00011000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
])

# Checkmark icon 16×16
ICON_CHECK_16 = bytes([
    0b00000000, 0b00000000,
    0b00000000, 0b00000001,
    0b00000000, 0b00000011,
    0b00000000, 0b00000110,
    0b00000000, 0b00001100,
    0b00000000, 0b00011000,
    0b10000000, 0b00110000,
    0b11000000, 0b01100000,
    0b01100000, 0b11000000,
    0b00110001, 0b10000000,
    0b00011111, 0b00000000,
    0b00001110, 0b00000000,
    0b00000100, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000,
])

# Checkmark icon 32×32 (for completion animation, DISP-10)
ICON_CHECK_32 = bytes([
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x03,
    0x00, 0x00, 0x00, 0x07,
    0x00, 0x00, 0x00, 0x0E,
    0x00, 0x00, 0x00, 0x1C,
    0x00, 0x00, 0x00, 0x38,
    0x00, 0x00, 0x00, 0x70,
    0x00, 0x00, 0x00, 0xE0,
    0x40, 0x00, 0x01, 0xC0,
    0x60, 0x00, 0x03, 0x80,
    0x70, 0x00, 0x07, 0x00,
    0x38, 0x00, 0x0E, 0x00,
    0x1C, 0x00, 0x1C, 0x00,
    0x0E, 0x00, 0x38, 0x00,
    0x07, 0x00, 0x70, 0x00,
    0x03, 0x80, 0xE0, 0x00,
    0x01, 0xC1, 0xC0, 0x00,
    0x00, 0xFF, 0x80, 0x00,
    0x00, 0x7F, 0x00, 0x00,
    0x00, 0x3E, 0x00, 0x00,
    0x00, 0x1C, 0x00, 0x00,
    0x00, 0x08, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])


def _draw_bitmap(draw: ImageDraw.ImageDraw, x: int, y: int,
                 data: bytes, w: int, h: int, color: tuple) -> None:
    """Render a monochrome bitmap at (x, y) with transparent background.

    Ported from icons.h rendering in display_ui.cpp.
    Each row is ceil(w/8) bytes; MSB = leftmost pixel.
    """
    bytes_per_row = (w + 7) // 8
    for row in range(h):
        for col in range(w):
            byte_idx = row * bytes_per_row + col // 8
            bit = 7 - (col % 8)
            if byte_idx < len(data) and (data[byte_idx] >> bit) & 1:
                draw.point((x + col, y + row), fill=color)


# ------------------------------------------------------------------ #
# Font helpers                                                         #
# ------------------------------------------------------------------ #

_FONT_CACHE: dict[tuple, ImageFont.FreeTypeFont] = {}
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a TTF font at *size*, caching the result."""
    key = (size, bold)
    if key not in _FONT_CACHE:
        name = "RobotoMono-Bold.ttf" if bold else "RobotoMono-Regular.ttf"
        path = os.path.join(_FONTS_DIR, name)
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except (IOError, OSError):
            logger.warning("Font %s not found; using PIL default", path)
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


# ------------------------------------------------------------------ #
# Arc gauge drawing (DISP-03, DISP-04)                                #
# Ported from display_gauges.cpp                                      #
# ------------------------------------------------------------------ #

# Gauge max values for arc fill ratio
_GAUGE_MAX = {
    "nozzle": 300,
    "bed": 120,
}

# Default colours per gauge
_GAUGE_COLORS = {
    "nozzle":   (255, 120, 0),
    "bed":      (255, 60, 60),
}


def _draw_arc_gauge(
    draw: ImageDraw.ImageDraw,
    cx: int, cy: int, radius: int,
    value: float, max_value: float,
    color: tuple,
    label: str,
    unit: str = "",
    icon: Optional[bytes] = None,
    font_small: Optional[ImageFont.FreeTypeFont] = None,
    font_value: Optional[ImageFont.FreeTypeFont] = None,
    arc_width: int = 4,
) -> None:
    """Draw a single arc gauge at centre (cx, cy).

    Arc spans 240° (from 150° to 390° in PIL CW convention).
    Ported from display_gauges.cpp drawGauge().
    """
    if font_small is None:
        font_small = _font(9)
    if font_value is None:
        font_value = _font(13, bold=True)

    r = radius
    bbox = [cx - r, cy - r, cx + r, cy + r]

    # Track arc (background) — 150° CW to 30° = 240° sweep
    draw.arc(bbox, start=150, end=30, fill=TRACK_COLOR, width=arc_width)

    # Fill arc proportional to value
    value = float(value) if value is not None else 0.0
    ratio = min(1.0, max(0.0, value / max_value)) if max_value > 0 else 0.0
    fill_degrees = ratio * ARC_FULL_DEGREES
    if fill_degrees > 1:
        fill_end_angle = (150 + fill_degrees) % 360
        draw.arc(bbox, start=150, end=fill_end_angle, fill=color, width=arc_width)

    # Center value text
    display_val = f"{value:.0f}"
    try:
        bbox_t = draw.textbbox((0, 0), display_val + unit, font=font_value)
        tw = bbox_t[2] - bbox_t[0]
        th = bbox_t[3] - bbox_t[1]
    except AttributeError:
        tw, th = draw.textsize(display_val + unit, font=font_value)  # type: ignore[attr-defined]
    draw.text((cx - tw // 2, cy - th // 2 - 4), display_val + unit, font=font_value, fill=TEXT_COLOR)

    # Sub-label below center
    try:
        lb = draw.textbbox((0, 0), label, font=font_small)
        lw = lb[2] - lb[0]
    except AttributeError:
        lw, _ = draw.textsize(label, font=font_small)  # type: ignore[attr-defined]
    draw.text((cx - lw // 2, cy + r - 14), label, font=font_small, fill=DIM_COLOR)

    # Icon above center (if provided)
    if icon is not None:
        _draw_bitmap(draw, cx - 8, cy - r + 2, icon, 16, 16, DIM_COLOR)


# ------------------------------------------------------------------ #
# ST7789 SPI driver                                                    #
# ------------------------------------------------------------------ #

class ST7789:
    """Raw SPI driver for Waveshare ST7789 LCD modules (240×240 and 320×240).

    Drives the display directly via spidev + RPi.GPIO — no Pimoroni library required.
    The hardware profile (resolution, MADCTL orientation, CASET/RASET window) is
    selected via the *model* parameter using the DISPLAY_PROFILES registry (CFG-06).

    GPIO pin mapping (BCM numbering) — identical for all supported models:
      DC  = GPIO 25  (physical pin 22)
      RST = GPIO 27  (physical pin 13)
      BL  = GPIO 18  (physical pin 12) — PWM backlight
      CS  = GPIO 8   (physical pin 24, CE0) — managed by spidev CE0
      MOSI= GPIO 10  (physical pin 19)
      SCLK= GPIO 11  (physical pin 23)
    """

    DC_PIN    = 25
    RST_PIN   = 27
    BL_PIN    = 18   # GPIO 18, physical pin 12 (Waveshare standard wiring)
    SPI_PORT  = 0
    SPI_CS    = 0    # spidev chip-select index: 0 = CE0 → /dev/spidev0.0
    SPI_SPEED = 16_000_000

    _CHUNK = 4096

    def __init__(self, brightness: int = 100, model: str = "waveshare_1in54") -> None:
        """Initialise display via direct spidev + RPi.GPIO.

        Args:
            brightness: Backlight level 0–255.
            model: Display hardware profile key from DISPLAY_PROFILES (CFG-06).
        """
        import spidev
        import RPi.GPIO as GPIO

        self._profile = DISPLAY_PROFILES.get(model)
        if self._profile is None:
            raise ValueError(
                f"Unknown display model {model!r}. Valid models: {sorted(DISPLAY_PROFILES)}"
            )
        self.width: int = self._profile.width
        self.height: int = self._profile.height

        self._GPIO = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.DC_PIN, GPIO.OUT)
        GPIO.setup(self.RST_PIN, GPIO.OUT)
        GPIO.setup(self.BL_PIN, GPIO.OUT)

        self._spi = spidev.SpiDev()
        self._spi.open(self.SPI_PORT, self.SPI_CS)
        self._spi.max_speed_hz = self.SPI_SPEED
        self._spi.mode = 0

        self._pwm = GPIO.PWM(self.BL_PIN, 1000)
        self._pwm.start(max(0, min(255, brightness)) / 255 * 100)

        self._reset()
        self._init_display()
        logger.info("ST7789 display initialised (model=%s, %dx%d, brightness=%d)",
                    model, self.width, self.height, brightness)

    def _cmd(self, cmd: int) -> None:
        self._GPIO.output(self.DC_PIN, self._GPIO.LOW)
        self._spi.xfer2([cmd])

    def _data(self, data: bytes) -> None:
        self._GPIO.output(self.DC_PIN, self._GPIO.HIGH)
        mv = memoryview(data)
        for offset in range(0, len(mv), self._CHUNK):
            self._spi.writebytes2(mv[offset: offset + self._CHUNK])

    def _reset(self) -> None:
        import time
        self._GPIO.output(self.RST_PIN, self._GPIO.HIGH)
        time.sleep(0.05)
        self._GPIO.output(self.RST_PIN, self._GPIO.LOW)
        time.sleep(0.05)
        self._GPIO.output(self.RST_PIN, self._GPIO.HIGH)
        time.sleep(0.15)

    def _init_display(self) -> None:
        """Send full Waveshare init sequence using profile values for model-specific registers."""
        import time
        p = self._profile
        self._cmd(0x01); time.sleep(0.15)   # SW reset
        self._cmd(0x11); time.sleep(0.12)   # Sleep out
        self._cmd(0xB2); self._data(bytes([0x0C, 0x0C, 0x00, 0x33, 0x33]))  # Porch control
        self._cmd(0xB7); self._data(bytes([0x35]))                           # Gate control
        self._cmd(0xBB); self._data(bytes([0x19]))                           # VCOMS
        self._cmd(0xC0); self._data(bytes([0x2C]))                           # LCM control
        self._cmd(0xC2); self._data(bytes([0x01]))                           # VDV/VRH enable
        self._cmd(0xC3); self._data(bytes([p.vrh_set]))                      # VRH (model-specific)
        self._cmd(0xC4); self._data(bytes([0x20]))                           # VDV set
        self._cmd(0xC6); self._data(bytes([0x0F]))                           # Frame rate 60 Hz
        self._cmd(0xD0); self._data(bytes([0xA4, 0xA1]))                     # Power control 1
        self._cmd(0xE0); self._data(bytes([0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B,  # Positive gamma
                                           0x3F, 0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23]))
        self._cmd(0xE1); self._data(bytes([0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C,  # Negative gamma
                                           0x3F, 0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23]))
        self._cmd(0x21)                                                      # Inversion on
        self._cmd(0x3A); self._data(bytes([0x05]))                           # 16-bit RGB565
        self._cmd(0x36); self._data(bytes([p.madctl]))                       # MADCTL (model-specific)
        self._cmd(0x2A); self._data(bytes([0x00, 0x00,                       # CASET
                                           p.col_end_hi, p.col_end_lo]))
        self._cmd(0x2B); self._data(bytes([0x00, p.row_start_lo,             # RASET
                                           p.row_end_hi, p.row_end_lo]))
        self._cmd(0x29); time.sleep(0.05)                                    # Display on

    def set_brightness(self, brightness: int) -> None:
        """Set backlight brightness 0–255."""
        self._pwm.ChangeDutyCycle(max(0, min(255, brightness)) / 255 * 100)

    def show_image(self, image: Image.Image) -> None:
        """Push a PIL Image to the display using the profile's pixel window."""
        p = self._profile
        self._cmd(0x2A)
        self._data(bytes([0x00, 0x00, p.col_end_hi, p.col_end_lo]))
        self._cmd(0x2B)
        self._data(bytes([0x00, p.row_start_lo, p.row_end_hi, p.row_end_lo]))
        self._cmd(0x2C)
        import numpy as np
        arr = np.frombuffer(
            image.convert("RGB").tobytes(), dtype=np.uint8
        ).reshape(self.height, self.width, 3)
        r = arr[:, :, 0].astype(np.uint16)
        g = arr[:, :, 1].astype(np.uint16)
        b = arr[:, :, 2].astype(np.uint16)
        px565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        self._data(px565.astype(np.dtype(">u2")).tobytes())

    def close(self) -> None:
        """Release SPI and GPIO resources."""
        try:
            self._pwm.stop()
            self._spi.close()
            self._GPIO.cleanup()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ #
# Renderer                                                             #
# ------------------------------------------------------------------ #

class Renderer:
    """Builds PIL images for each screen state and pushes to ST7789.

    All geometry is derived from self._w / self._h (read from the attached
    display at construction time) so layouts adapt to both 240×240 and
    320×240 screens without code changes. Module-level WIDTH/HEIGHT are
    used only as a fallback for mock displays in tests.
    """

    # Completion animation timing (DISP-10)
    _ANIM_RING_MS = 400   # ring expansion phase
    _ANIM_CHECK_MS = 600  # checkmark appears at this ms mark

    def __init__(self, display: ST7789) -> None:
        self._display = display
        # Read dimensions from display; fall back to 240×240 for mocks (DISP-01)
        self._w: int = getattr(display, "width", WIDTH)
        self._h: int = getattr(display, "height", HEIGHT)
        self._frame = 0
        self._anim_start: Optional[float] = None
        self._last_frame: Optional[Image.Image] = None

    def render(
        self,
        screen_state: str,
        printer_state: dict[str, Any],
        connection_status: str,
        error_count: int,
        config: dict[str, Any],
    ) -> None:
        """Render the appropriate screen and push to display.

        Called from the main render thread every 250 ms (DISP-01).
        Image dimensions match self._w × self._h.
        """
        image = Image.new("RGB", (self._w, self._h), BG_COLOR)
        draw = ImageDraw.Draw(image)

        if screen_state == SCREEN_SPLASH:
            self._render_splash(draw, config)
        elif screen_state == SCREEN_CONNECTING:
            self._render_connecting(draw, error_count)
        elif screen_state == SCREEN_IDLE:
            self._render_idle(draw, printer_state)
        elif screen_state == SCREEN_PRINTING:
            self._render_printing(draw, printer_state)
        elif screen_state == SCREEN_FINISHED:
            self._render_finished(draw, printer_state)
        elif screen_state == SCREEN_CLOCK:
            self._render_clock(draw)
        elif screen_state == SCREEN_OFF:
            pass  # Black image already
        else:
            self._render_connecting(draw, error_count)

        self._last_frame = image
        self._display.show_image(image)
        self._frame += 1

    def get_preview_png(self) -> Optional[bytes]:
        """Return the last rendered frame as 3× scaled PNG bytes (DISP-17).

        Output is self._w*3 × self._h*3 (e.g. 720×720 for 240×240, 960×720 for 320×240).
        Returns None if no frame has been rendered yet.
        """
        if self._last_frame is None:
            return None
        import io
        buf = io.BytesIO()
        img = self._last_frame.resize((self._w * 3, self._h * 3), Image.NEAREST)
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _render_splash(self, draw: ImageDraw.ImageDraw, config: dict[str, Any]) -> None:
        """Boot splash screen with version and printer name."""
        f_large = _font(28, bold=True)
        f_small = _font(12)
        title = "BambuHelper"
        try:
            tw = draw.textbbox((0, 0), title, font=f_large)[2]
        except AttributeError:
            tw, _ = draw.textsize(title, font=f_large)  # type: ignore[attr-defined]
        draw.text((self._w // 2 - tw // 2, 80), title, font=f_large, fill=ACCENT_COLOR)
        version = "v1.0.0"
        try:
            vw = draw.textbbox((0, 0), version, font=f_small)[2]
        except AttributeError:
            vw, _ = draw.textsize(version, font=f_small)  # type: ignore[attr-defined]
        draw.text((self._w // 2 - vw // 2, 115), version, font=f_small, fill=DIM_COLOR)
        name = config.get("printer_name", "My Printer")
        try:
            nw = draw.textbbox((0, 0), name, font=f_small)[2]
        except AttributeError:
            nw, _ = draw.textsize(name, font=f_small)  # type: ignore[attr-defined]
        draw.text((self._w // 2 - nw // 2, 150), name, font=f_small, fill=TEXT_COLOR)

    def _render_connecting(self, draw: ImageDraw.ImageDraw, error_count: int) -> None:
        """Connecting screen: spinner + animated dots + attempt counter. (DISP-08, DISP-09)"""
        f_small = _font(11)
        f_tiny = _font(9)
        f_title = _font(16, bold=True)

        # Title — centred
        title = "BambuHelper"
        try:
            ttw = draw.textbbox((0, 0), title, font=f_title)[2]
        except AttributeError:
            ttw, _ = draw.textsize(title, font=f_title)  # type: ignore[attr-defined]
        draw.text((self._w // 2 - ttw // 2, 10), title, font=f_title, fill=ACCENT_COLOR)

        # Spinner (DISP-08): 12° advance, 60° arc width, CW
        angle = (self._frame * 12) % 360
        end_angle = (angle + 60) % 360
        cx, cy, r = self._w // 2, self._h // 2 - 10, 28
        bbox = [cx - r, cy - r, cx + r, cy + r]
        draw.arc(bbox, start=angle, end=end_angle, fill=ACCENT_COLOR, width=4)

        # "Connecting" label
        draw.text((cx - 35, cy + r + 8), "Connecting", font=f_small, fill=TEXT_COLOR)

        # Animated dots (DISP-09): 4 states, 400ms period at 250ms frame
        dot_state = (self._frame // (400 // 250)) % 4
        dots = "." * dot_state
        draw.text((cx + 37, cy + r + 8), dots, font=f_small, fill=ACCENT_COLOR)

        # Attempt counter
        if error_count > 0:
            draw.text((10, self._h - 30), f"Attempt {error_count}", font=f_tiny, fill=DIM_COLOR)

        draw.text((10, self._h - 18), "MQTT", font=f_tiny, fill=DIM_COLOR)

    def _render_idle(self, draw: ImageDraw.ImageDraw, state: dict[str, Any]) -> None:
        """Idle screen: nozzle + bed arc gauges only. (DISP-11)"""
        f_small = _font(10)
        f_value = _font(16, bold=True)
        f_header = _font(12, bold=True)

        draw.text((8, 8), "IDLE", font=f_header, fill=BADGE_COLORS.get("IDLE", DIM_COLOR))

        gauge_cy = self._h // 2
        gauge_r = 50

        _draw_arc_gauge(
            draw, cx=self._w // 4, cy=gauge_cy, radius=gauge_r,
            value=state.get("nozzle_temp", 0),
            max_value=_GAUGE_MAX["nozzle"],
            color=_GAUGE_COLORS["nozzle"],
            label="Nozzle", unit="°",
            icon=ICON_NOZZLE,
            font_small=f_small, font_value=f_value,
            arc_width=6,
        )
        _draw_arc_gauge(
            draw, cx=(self._w * 3) // 4, cy=gauge_cy, radius=gauge_r,
            value=state.get("bed_temp", 0),
            max_value=_GAUGE_MAX["bed"],
            color=_GAUGE_COLORS["bed"],
            label="Bed", unit="°",
            icon=ICON_BED,
            font_small=f_small, font_value=f_value,
            arc_width=6,
        )

        self._draw_bottom_bar(draw, state)

    def _render_printing(self, draw: ImageDraw.ImageDraw, state: dict[str, Any]) -> None:
        """Printing dashboard: 2 large arc gauges top + progress/ETA panels bottom. (DISP-03)"""
        speed = state.get("speed_level", 2)
        bar_color = SPEED_COLORS.get(speed, SPEED_COLORS[2])

        # LED progress bar at top (DISP-05) — width scales with display
        progress = state.get("progress", 0)
        bar_width = self._w - 4
        fill_w = int(progress / 100 * bar_width)
        if fill_w > 0:
            draw.rectangle([2, 1, 2 + fill_w, PROGRESS_BAR_HEIGHT], fill=bar_color)
            glow = tuple(min(255, c + 60) for c in bar_color)
            draw.line([(2, 1), (2 + fill_w, 1)], fill=glow, width=1)

        # Header: job name (left) + gcode state badge (right)
        self._draw_header(draw, state)

        # Top row: 2 large arc gauges — Nozzle (left) + Bed (right)
        f_small = _font(11)
        f_value = _font(20, bold=True)

        _draw_arc_gauge(
            draw, cx=self._w // 4, cy=83, radius=50,
            value=state.get("nozzle_temp", 0),
            max_value=_GAUGE_MAX["nozzle"],
            color=_GAUGE_COLORS["nozzle"],
            label="Nozzle", unit="°",
            icon=ICON_NOZZLE,
            font_small=f_small, font_value=f_value,
            arc_width=6,
        )
        _draw_arc_gauge(
            draw, cx=(self._w * 3) // 4, cy=83, radius=50,
            value=state.get("bed_temp", 0),
            max_value=_GAUGE_MAX["bed"],
            color=_GAUGE_COLORS["bed"],
            label="Bed", unit="°",
            icon=ICON_BED,
            font_small=f_small, font_value=f_value,
            arc_width=6,
        )

        # Horizontal divider
        draw.line([(4, 140), (self._w - 4, 140)], fill=TRACK_COLOR, width=1)

        # Vertical divider splitting bottom panels
        panel_mid = self._w // 2
        draw.line([(panel_mid, 142), (panel_mid, self._h - 20)], fill=TRACK_COLOR, width=1)

        # Bottom-left panel: large progress %
        self._draw_progress_panel(draw, state, cx=self._w // 4)

        # Bottom-right panel: ETA or PAUSED/FAILED (DISP-15)
        self._draw_eta_panel(draw, state, cx=(self._w * 3) // 4)

        # Bottom bar (DISP-14)
        self._draw_bottom_bar(draw, state)

    def _draw_progress_panel(
        self, draw: ImageDraw.ImageDraw, state: dict[str, Any], cx: int
    ) -> None:
        """Large progress percentage centred at cx in the bottom-left panel."""
        f_large = _font(34, bold=True)
        f_label = _font(11)
        progress = state.get("progress", 0)
        val_str = f"{int(progress)}%"
        try:
            tb = draw.textbbox((0, 0), val_str, font=f_large)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except AttributeError:
            tw, th = draw.textsize(val_str, font=f_large)  # type: ignore[attr-defined]
        y_val = 155
        draw.text((cx - tw // 2, y_val), val_str, font=f_large, fill=TEXT_COLOR)
        label = "Progress"
        try:
            lb = draw.textbbox((0, 0), label, font=f_label)
            lw = lb[2] - lb[0]
        except AttributeError:
            lw, _ = draw.textsize(label, font=f_label)  # type: ignore[attr-defined]
        draw.text((cx - lw // 2, y_val + th + 4), label, font=f_label, fill=DIM_COLOR)

    def _draw_eta_panel(
        self, draw: ImageDraw.ImageDraw, state: dict[str, Any], cx: int
    ) -> None:
        """ETA, PAUSED, or FAILED text centred at cx in the bottom-right panel. (DISP-15)"""
        gcode = state.get("gcode_state", "")
        f_status = _font(22, bold=True)
        f_eta = _font(22, bold=True)
        f_label = _font(11)
        y_val = 155

        if gcode == "PAUSE":
            text = "PAUSED"
            color = SPEED_COLORS[3]
            try:
                tw = draw.textbbox((0, 0), text, font=f_status)[2]
            except AttributeError:
                tw, _ = draw.textsize(text, font=f_status)  # type: ignore[attr-defined]
            draw.text((cx - tw // 2, y_val + 6), text, font=f_status, fill=color)

        elif gcode == "FAILED":
            text = "FAILED"
            color = SPEED_COLORS[4]
            try:
                tw = draw.textbbox((0, 0), text, font=f_status)[2]
            except AttributeError:
                tw, _ = draw.textsize(text, font=f_status)  # type: ignore[attr-defined]
            draw.text((cx - tw // 2, y_val + 6), text, font=f_status, fill=color)

        else:
            mins = state.get("remaining_minutes", 0) or 0
            if mins > 0:
                h, m = divmod(int(mins), 60)
                eta_str = f"{h}h {m:02d}m" if h else f"{m}m"
            else:
                eta_str = "--"
            try:
                tb = draw.textbbox((0, 0), eta_str, font=f_eta)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
            except AttributeError:
                tw, th = draw.textsize(eta_str, font=f_eta)  # type: ignore[attr-defined]
            draw.text((cx - tw // 2, y_val), eta_str, font=f_eta, fill=TEXT_COLOR)
            label = "remaining"
            try:
                lb = draw.textbbox((0, 0), label, font=f_label)
                lw = lb[2] - lb[0]
            except AttributeError:
                lw, _ = draw.textsize(label, font=f_label)  # type: ignore[attr-defined]
            draw.text((cx - lw // 2, y_val + th + 4), label, font=f_label, fill=DIM_COLOR)

    def _draw_header(self, draw: ImageDraw.ImageDraw, state: dict[str, Any]) -> None:
        """Draw printer name (left) and gcode_state badge (right) at Y=7."""
        f = _font(11, bold=True)
        gcode = state.get("gcode_state", "")
        badge_color = BADGE_COLORS.get(gcode, DIM_COLOR)

        name = state.get("subtask_name", "") or "Printing"
        if len(name) > 16:
            name = name[:15] + "…"
        draw.text((4, 8), name, font=f, fill=TEXT_COLOR)

        badge = gcode if gcode else "???"
        try:
            bw = draw.textbbox((0, 0), badge, font=f)[2]
        except AttributeError:
            bw, _ = draw.textsize(badge, font=f)  # type: ignore[attr-defined]
        draw.text((self._w - bw - 4, 8), badge, font=f, fill=badge_color)

    def _draw_bottom_bar(self, draw: ImageDraw.ImageDraw, state: dict[str, Any]) -> None:
        """WiFi RSSI | Layer N/M | Speed level at bottom of screen. (DISP-14)"""
        f = _font(9)
        y = self._h - 18

        rssi = state.get("wifi_signal", 0)
        _draw_bitmap(draw, 2, y, ICON_WIFI, 16, 16, DIM_COLOR)
        draw.text((20, y + 1), f"{rssi}dBm", font=f, fill=DIM_COLOR)

        layer = state.get("layer_num", 0)
        total = state.get("total_layers", 0)
        if total > 0:
            _draw_bitmap(draw, self._w // 2 - 30, y, ICON_LAYERS, 16, 16, DIM_COLOR)
            draw.text((self._w // 2 - 12, y + 1), f"{layer}/{total}", font=f, fill=DIM_COLOR)

        speed = state.get("speed_level", 2)
        speed_labels = {1: "SILENT", 2: "STANDARD", 3: "SPORT", 4: "LUDICROUS"}
        label = speed_labels.get(speed, "")
        color = SPEED_COLORS.get(speed, DIM_COLOR)
        try:
            sw = draw.textbbox((0, 0), label, font=f)[2]
        except AttributeError:
            sw, _ = draw.textsize(label, font=f)  # type: ignore[attr-defined]
        draw.text((self._w - sw - 4, y + 1), label, font=f, fill=color)

    def _render_finished(self, draw: ImageDraw.ImageDraw, state: dict[str, Any]) -> None:
        """Completion animation: expanding ring + checkmark. (DISP-10, DISP-12)"""
        if self._anim_start is None:
            self._anim_start = time.monotonic()

        elapsed_ms = (time.monotonic() - self._anim_start) * 1000
        cx, cy = self._w // 2, self._h // 2 - 10
        f = _font(12)
        f_small = _font(10)

        if elapsed_ms < self._ANIM_RING_MS:
            radius = int(10 + (45 - 10) * (elapsed_ms / self._ANIM_RING_MS))
        else:
            radius = 45

        ring_color = (0, 220, 80)
        draw.arc(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=0, end=359, fill=ring_color, width=3,
        )

        if elapsed_ms >= self._ANIM_CHECK_MS:
            _draw_bitmap(draw, cx - 16, cy - 16, ICON_CHECK_32, 32, 32, ring_color)

        complete_text = "Print Complete!"
        try:
            ctw = draw.textbbox((0, 0), complete_text, font=f)[2]
        except AttributeError:
            ctw, _ = draw.textsize(complete_text, font=f)  # type: ignore[attr-defined]
        draw.text((cx - ctw // 2, cy + radius + 8), complete_text, font=f, fill=ring_color)

        name = state.get("subtask_name", "")
        if name:
            if len(name) > 22:
                name = name[:21] + "…"
            try:
                nw = draw.textbbox((0, 0), name, font=f_small)[2]
            except AttributeError:
                nw, _ = draw.textsize(name, font=f_small)  # type: ignore[attr-defined]
            draw.text((cx - nw // 2, cy + radius + 26), name, font=f_small, fill=DIM_COLOR)

    def _render_clock(self, draw: ImageDraw.ImageDraw) -> None:
        """Digital clock + date. (DISP-13)"""
        import datetime
        f_time = _font(40, bold=True)
        f_date = _font(14)

        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")
        date_str = now.strftime("%a %d %b")

        try:
            tw = draw.textbbox((0, 0), time_str, font=f_time)[2]
            dw = draw.textbbox((0, 0), date_str, font=f_date)[2]
        except AttributeError:
            tw, _ = draw.textsize(time_str, font=f_time)  # type: ignore[attr-defined]
            dw, _ = draw.textsize(date_str, font=f_date)  # type: ignore[attr-defined]

        draw.text((self._w // 2 - tw // 2, 80), time_str, font=f_time, fill=TEXT_COLOR)
        draw.text((self._w // 2 - dw // 2, 140), date_str, font=f_date, fill=DIM_COLOR)

    def reset_anim(self) -> None:
        """Reset completion animation state (call when entering SCREEN_FINISHED)."""
        self._anim_start = None
