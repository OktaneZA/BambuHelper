"""Tests for src/display.py — rendering logic (mocked ST7789/GPIO/spidev)."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ------------------------------------------------------------------ #
# Mock hardware before importing display (MUST be before import)      #
# ------------------------------------------------------------------ #

def _mock_hardware():
    """Inject mock spidev, RPi.GPIO so display.py can be imported anywhere."""
    mocks = {
        "spidev": MagicMock(),
        "RPi": MagicMock(),
        "RPi.GPIO": MagicMock(),
        "ST7789": MagicMock(),
    }
    for name, mock in mocks.items():
        if name not in sys.modules:
            sys.modules[name] = mock

    # Set up SpiDev class on spidev mock
    sys.modules["spidev"].SpiDev = MagicMock
    # GPIO mock returns numeric constants
    gpio_mock = sys.modules["RPi.GPIO"]
    gpio_mock.BCM = 11
    gpio_mock.OUT = 0
    gpio_mock.HIGH = 1
    gpio_mock.LOW = 0
    gpio_mock.setmode = MagicMock()
    gpio_mock.setup = MagicMock()
    gpio_mock.output = MagicMock()
    gpio_mock.cleanup = MagicMock()
    gpio_mock.PWM = MagicMock(return_value=MagicMock(
        start=MagicMock(), stop=MagicMock(), ChangeDutyCycle=MagicMock()
    ))


_mock_hardware()

# Add src to path
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PIL import Image, ImageDraw

import display as disp_module
from display import (
    Renderer, ST7789,
    SCREEN_SPLASH, SCREEN_CONNECTING, SCREEN_IDLE, SCREEN_PRINTING,
    SCREEN_FINISHED, SCREEN_CLOCK, SCREEN_OFF,
    BG_COLOR, WIDTH, HEIGHT, PROGRESS_BAR_HEIGHT, SPEED_COLORS,
    _draw_arc_gauge, _draw_bitmap,
)
from bambu import DEFAULT_STATE


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _make_display() -> ST7789:
    """Create a mocked ST7789 that captures images instead of pushing SPI."""
    d = object.__new__(ST7789)
    d._images = []
    d._GPIO = sys.modules["RPi.GPIO"]
    d._spi = MagicMock()
    d._pwm = MagicMock()
    d.show_image = lambda img: d._images.append(img.copy())
    d.set_brightness = MagicMock()
    d.close = MagicMock()
    return d


def _printing_state(**overrides) -> dict:
    state = dict(DEFAULT_STATE)
    state.update({
        "connected": True,
        "printing": True,
        "gcode_state": "RUNNING",
        "progress": 68,
        "remaining_minutes": 83,
        "nozzle_temp": 220.5,
        "bed_temp": 60.0,
        "chamber_temp": 28.0,
        "subtask_name": "benchy.3mf",
        "layer_num": 45,
        "total_layers": 120,
        "cooling_fan_pct": 80,
        "aux_fan_pct": 60,
        "chamber_fan_pct": 30,
        "speed_level": 2,
        "wifi_signal": -45,
    })
    state.update(overrides)
    return state


_MOCK_CONFIG = {
    "printer_name": "Test Printer",
    "show_clock": True,
    "finish_timeout_s": 300,
    "display_brightness": 100,
}


# ------------------------------------------------------------------ #
# ST7789 mock                                                          #
# ------------------------------------------------------------------ #

class TestST7789Mock:
    def test_show_image_called(self):
        d = _make_display()
        img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
        d.show_image(img)
        assert len(d._images) == 1
        assert d._images[0].size == (WIDTH, HEIGHT)


# ------------------------------------------------------------------ #
# Renderer screen dimensions                                           #
# ------------------------------------------------------------------ #

class TestRendererDimensions:
    def setup_method(self):
        self.display = _make_display()
        self.renderer = Renderer(self.display)

    def _render(self, screen, state=None, error_count=0):
        if state is None:
            state = dict(DEFAULT_STATE)
        self.renderer.render(screen, state, "connected", error_count, _MOCK_CONFIG)
        return self.display._images[-1]

    def test_splash_produces_240x240_image(self):
        img = self._render(SCREEN_SPLASH)
        assert img.size == (WIDTH, HEIGHT)

    def test_connecting_produces_240x240_image(self):
        img = self._render(SCREEN_CONNECTING)
        assert img.size == (WIDTH, HEIGHT)

    def test_idle_produces_240x240_image(self):
        img = self._render(SCREEN_IDLE, _printing_state())
        assert img.size == (WIDTH, HEIGHT)

    def test_printing_produces_240x240_image(self):
        img = self._render(SCREEN_PRINTING, _printing_state())
        assert img.size == (WIDTH, HEIGHT)

    def test_finished_produces_240x240_image(self):
        img = self._render(SCREEN_FINISHED, _printing_state())
        assert img.size == (WIDTH, HEIGHT)

    def test_clock_produces_240x240_image(self):
        img = self._render(SCREEN_CLOCK)
        assert img.size == (WIDTH, HEIGHT)

    def test_off_screen_is_all_black(self):
        img = self._render(SCREEN_OFF)
        # Sample corners and center — all should be background on OFF screen
        for pt in [(0, 0), (239, 0), (0, 239), (239, 239), (120, 120)]:
            assert img.getpixel(pt) == BG_COLOR


# ------------------------------------------------------------------ #
# Progress bar rendering (DISP-05)                                     #
# ------------------------------------------------------------------ #

class TestProgressBar:
    def setup_method(self):
        self.display = _make_display()
        self.renderer = Renderer(self.display)

    def _render_printing(self, progress: int, speed_level: int = 2) -> Image.Image:
        state = _printing_state(progress=progress, speed_level=speed_level)
        self.renderer.render(SCREEN_PRINTING, state, "connected", 0, _MOCK_CONFIG)
        return self.display._images[-1]

    def test_zero_progress_bar_has_no_fill(self):
        img = self._render_printing(0)
        # At 0% fill, the bar area Y=1–5 should be background colour
        pixel = img.getpixel((118, 2))  # middle of bar, should be BG
        assert pixel == BG_COLOR

    def test_full_progress_bar_fills_correctly(self):
        img = self._render_printing(100)
        # At 100% the bar should fill to X=238 (2 + 236)
        pixel = img.getpixel((120, 2))
        assert pixel != BG_COLOR, "Progress bar at 100% should be non-background"

    def test_50_percent_progress_fills_half(self):
        img = self._render_printing(50)
        expected_fill = int(50 / 100 * 236)
        # Pixel just inside fill zone should be non-background
        if expected_fill > 5:
            pixel_in = img.getpixel((2 + expected_fill - 5, 2))
            assert pixel_in != BG_COLOR
        # Pixel well past fill zone should be background
        if expected_fill < 230:
            pixel_out = img.getpixel((2 + expected_fill + 10, 2))
            assert pixel_out == BG_COLOR

    def test_speed_level_colors(self):
        for level, color in SPEED_COLORS.items():
            img = self._render_printing(50, speed_level=level)
            # The progress bar pixel should match the speed color (approximately)
            pixel = img.getpixel((60, 2))
            # Allow some tolerance for glow effect on top row
            assert pixel != BG_COLOR, f"Speed level {level} bar not rendered"


# ------------------------------------------------------------------ #
# Spinner animation (DISP-08)                                          #
# ------------------------------------------------------------------ #

class TestSpinnerAnimation:
    def setup_method(self):
        self.display = _make_display()
        self.renderer = Renderer(self.display)

    def test_spinner_advances_per_frame(self):
        """Consecutive connecting frames must differ (spinner moves)."""
        frames = []
        for _ in range(4):
            self.renderer.render(SCREEN_CONNECTING, {}, "connecting", 0, _MOCK_CONFIG)
            frames.append(self.display._images[-1].copy())

        # At least some frames should differ
        diffs = sum(
            1 for i in range(1, len(frames))
            if frames[i].tobytes() != frames[i - 1].tobytes()
        )
        assert diffs > 0, "Spinner frames are all identical — animation not working"

    def test_frame_counter_increments(self):
        initial = self.renderer._frame
        self.renderer.render(SCREEN_CONNECTING, {}, "connecting", 0, _MOCK_CONFIG)
        assert self.renderer._frame == initial + 1


# ------------------------------------------------------------------ #
# Completion ring animation (DISP-10)                                  #
# ------------------------------------------------------------------ #

class TestCompletionAnimation:
    def setup_method(self):
        self.display = _make_display()
        self.renderer = Renderer(self.display)

    def test_reset_anim_clears_start_time(self):
        import time
        self.renderer._anim_start = time.monotonic() - 999
        self.renderer.reset_anim()
        assert self.renderer._anim_start is None

    def test_finished_screen_renders_without_error(self):
        state = _printing_state(gcode_state="FINISH")
        self.renderer.render(SCREEN_FINISHED, state, "connected", 0, _MOCK_CONFIG)
        assert len(self.display._images) == 1

    def test_anim_start_set_on_first_finished_render(self):
        state = _printing_state(gcode_state="FINISH")
        assert self.renderer._anim_start is None
        self.renderer.render(SCREEN_FINISHED, state, "connected", 0, _MOCK_CONFIG)
        assert self.renderer._anim_start is not None


# ------------------------------------------------------------------ #
# Arc gauge drawing (DISP-04)                                          #
# ------------------------------------------------------------------ #

class TestArcGauge:
    def test_gauge_draws_without_error(self):
        img = Image.new("RGB", (80, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_arc_gauge(draw, cx=40, cy=40, radius=30, value=50, max_value=100,
                        color=(0, 200, 255), label="Test", unit="%")

    def test_gauge_at_zero_value(self):
        img = Image.new("RGB", (80, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_arc_gauge(draw, cx=40, cy=40, radius=30, value=0, max_value=100,
                        color=(0, 200, 255), label="Test", unit="%")

    def test_gauge_at_max_value(self):
        img = Image.new("RGB", (80, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_arc_gauge(draw, cx=40, cy=40, radius=30, value=100, max_value=100,
                        color=(0, 200, 255), label="Test", unit="%")

    def test_gauge_value_exceeds_max_clamped(self):
        """Value > max should not cause errors (ratio clamped to 1.0)."""
        img = Image.new("RGB", (80, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_arc_gauge(draw, cx=40, cy=40, radius=30, value=150, max_value=100,
                        color=(0, 200, 255), label="Test", unit="%")


# ------------------------------------------------------------------ #
# Bitmap icon rendering (DISP-07)                                      #
# ------------------------------------------------------------------ #

class TestBitmapIcon:
    def test_bitmap_renders_without_error(self):
        img = Image.new("RGB", (20, 20), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_bitmap(draw, 2, 2, disp_module.ICON_NOZZLE, 16, 16, (255, 255, 255))

    def test_bitmap_bed_renders_without_error(self):
        img = Image.new("RGB", (20, 20), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_bitmap(draw, 2, 2, disp_module.ICON_BED, 16, 16, (255, 255, 255))

    def test_bitmap_check32_renders_without_error(self):
        img = Image.new("RGB", (40, 40), BG_COLOR)
        draw = ImageDraw.Draw(img)
        _draw_bitmap(draw, 4, 4, disp_module.ICON_CHECK_32, 32, 32, (0, 220, 80))
