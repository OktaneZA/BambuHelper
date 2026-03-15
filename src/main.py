"""BambuHelper — main entry point.

Starts three threads (ARCH-02):
  1. Main / render thread  — 250 ms display loop (this thread)
  2. MQTT background thread — printer state polling
  3. Flask portal thread    — web config interface

Shared state is protected by threading.Lock (ARCH-03).
SIGTERM / SIGINT sets _shutdown_event; all threads exit cleanly (ARCH-07).
Config reload without restart: _restart_event triggers MQTT reconnect (ARCH-08).
"""

import logging
import os
import signal
import sys
import threading
import time

# Add src directory to path when running directly
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import config as cfg_module
from bambu import BambuClient, DEFAULT_STATE
from display import (
    Renderer, ST7789,
    SCREEN_SPLASH, SCREEN_CONNECTING, SCREEN_IDLE, SCREEN_PRINTING,
    SCREEN_FINISHED, SCREEN_CLOCK, SCREEN_OFF,
)
from portal import create_app

# ------------------------------------------------------------------ #
# Logging setup                                                        #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "").upper() == "TRUE" else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Shared state (ARCH-03)                                               #
# ------------------------------------------------------------------ #

_lock = threading.Lock()
_printer_state: dict = dict(DEFAULT_STATE)
_connection_status: str = "disconnected"
_fetch_error_count: int = 0
_display_epoch: list[int] = [0]  # mutable ref for epoch counter
_screen_state: str = SCREEN_SPLASH

_shutdown_event = threading.Event()
_restart_event = threading.Event()

# ------------------------------------------------------------------ #
# Signal handler (ARCH-07)                                             #
# ------------------------------------------------------------------ #

def _handle_signal(signum: int, frame) -> None:  # type: ignore[type-arg]
    """Set shutdown event on SIGTERM or SIGINT."""
    logger.info("Signal %d received — shutting down", signum)
    _shutdown_event.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ------------------------------------------------------------------ #
# MQTT background thread                                               #
# ------------------------------------------------------------------ #

def _mqtt_thread_func(config_path: str) -> None:
    """MQTT background thread: connect, parse, update shared state."""
    while not _shutdown_event.is_set():
        # Reload config on restart signal
        if _restart_event.is_set():
            _restart_event.clear()
            logger.info("Restart event received — reloading config")

        try:
            config = cfg_module.load_config(config_path)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Cannot load config: %s — retry in 10s", exc)
            _shutdown_event.wait(timeout=10)
            continue

        logger.info("Starting MQTT client (mode=%s)", config["connection_mode"])
        client = BambuClient(
            config=config,
            state=_printer_state,
            lock=_lock,
            epoch_counter=_display_epoch,
        )

        def _on_change() -> None:
            # Called by BambuClient after epoch increment — wake render thread
            pass  # render thread polls epoch; no explicit wakeup needed

        client._on_change = _on_change  # type: ignore[attr-defined]

        try:
            # BambuClient.run_forever blocks until shutdown or restart
            # We wrap to detect restart_event mid-run
            _run_with_restart_check(client)
        except Exception as exc:  # noqa: BLE001 — must not crash this thread
            logger.error("Unexpected MQTT thread error: %s", exc, exc_info=True)
            _shutdown_event.wait(timeout=5)

    logger.info("MQTT thread exiting")


def _run_with_restart_check(client: BambuClient) -> None:
    """Run client until shutdown or restart event."""
    # Run in a sub-thread so we can interrupt it on restart
    inner_shutdown = threading.Event()

    def _runner() -> None:
        client.run_forever(inner_shutdown)

    t = threading.Thread(target=_runner, daemon=True, name="mqtt-inner")
    t.start()

    while not _shutdown_event.is_set() and not _restart_event.is_set():
        t.join(timeout=1.0)
        if not t.is_alive():
            break

    inner_shutdown.set()
    t.join(timeout=5)


# ------------------------------------------------------------------ #
# Portal thread                                                        #
# ------------------------------------------------------------------ #

def _portal_thread_func(config_path: str) -> None:
    """Run Flask portal in a daemon thread."""
    try:
        config = cfg_module.load_config(config_path)
        port = config.get("portal_port", 8080)
    except Exception:  # noqa: BLE001
        port = 8080

    app = create_app(
        config_path=config_path,
        shared_state=_printer_state,
        lock=_lock,
        restart_event=_restart_event,
    )

    logger.info("Web portal starting on port %d", port)
    try:
        # use_reloader=False — we're running in a thread, not the main process
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("Portal crashed: %s", exc, exc_info=True)


# ------------------------------------------------------------------ #
# Screen state machine                                                 #
# ------------------------------------------------------------------ #

def _determine_screen_state(
    state: dict,
    connected: bool,
    config: dict,
    prev_screen: str,
    finish_time: list[float],
) -> str:
    """Derive the correct screen state from printer state.

    Implements the same state machine as the original display_ui.cpp.
    """
    if not connected:
        return SCREEN_CONNECTING

    gcode = state.get("gcode_state", "")
    printing = state.get("printing", False)

    if gcode == "FINISH":
        # Record finish time on first transition
        if prev_screen != SCREEN_FINISHED and prev_screen != SCREEN_CLOCK:
            if not finish_time[0]:
                finish_time[0] = time.monotonic()
        elapsed = time.monotonic() - finish_time[0] if finish_time[0] else 0
        timeout = config.get("finish_timeout_s", 300)
        if elapsed > timeout and config.get("show_clock", True):
            return SCREEN_CLOCK
        return SCREEN_FINISHED

    # Not finished — reset finish timer
    finish_time[0] = 0.0

    if printing or gcode in ("RUNNING", "PREPARE", "PAUSE"):
        return SCREEN_PRINTING

    return SCREEN_IDLE


# ------------------------------------------------------------------ #
# Render loop                                                          #
# ------------------------------------------------------------------ #

_RENDER_INTERVAL_S = 0.250  # 250 ms = ~4 Hz (DISP-01)


def _render_loop(config_path: str, display: ST7789) -> None:
    """Main render loop — reads shared state, drives screen. (ARCH-02)"""
    renderer = Renderer(display)
    prev_epoch = -1
    prev_screen = SCREEN_SPLASH
    finish_time: list[float] = [0.0]
    splash_until = time.monotonic() + 2.0  # 2s splash (DISP-02)
    config: dict = {}

    try:
        config = cfg_module.load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Config not available at render start: %s", exc)

    while not _shutdown_event.is_set():
        loop_start = time.monotonic()

        # Show splash for 2 seconds on boot
        if loop_start < splash_until:
            if prev_screen != SCREEN_SPLASH:
                renderer.render(SCREEN_SPLASH, {}, "splash", 0, config)
                prev_screen = SCREEN_SPLASH
            _shutdown_event.wait(timeout=max(0, splash_until - loop_start))
            continue

        with _lock:
            epoch = _display_epoch[0]
            state_snap = dict(_printer_state)
            error_count = _fetch_error_count
            connected = state_snap.get("connected", False)

        # Reload config if restart was triggered
        if _restart_event.is_set():
            try:
                config = cfg_module.load_config(config_path)
            except Exception:  # noqa: BLE001
                pass

        screen = _determine_screen_state(
            state_snap, connected, config, prev_screen, finish_time
        )

        # Reset completion animation on state transition to FINISHED
        if screen == SCREEN_FINISHED and prev_screen != SCREEN_FINISHED:
            renderer.reset_anim()

        # Render if epoch changed (ARCH-06) or screen state changed
        if epoch != prev_epoch or screen != prev_screen:
            renderer.render(screen, state_snap, "connected" if connected else "disconnected",
                            error_count, config)
            prev_epoch = epoch
            prev_screen = screen

        # Sleep for remainder of 250 ms frame
        elapsed = time.monotonic() - loop_start
        sleep_time = max(0.0, _RENDER_INTERVAL_S - elapsed)
        _shutdown_event.wait(timeout=sleep_time)

    logger.info("Render loop exiting")


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

def main() -> None:
    """Initialise hardware and start threads."""
    config_path = os.environ.get("BAMBU_CONFIG", cfg_module.DEFAULT_CONFIG_PATH)

    logger.info("BambuHelper starting (config=%s)", config_path)

    # Load config (fail fast if invalid)
    try:
        config = cfg_module.load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    # Initialise display (fail fast on hardware error)
    try:
        display = ST7789(brightness=config.get("display_brightness", 100))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise display: %s", exc)
        sys.exit(1)

    # Start MQTT background thread
    mqtt_thread = threading.Thread(
        target=_mqtt_thread_func,
        args=(config_path,),
        daemon=True,
        name="mqtt",
    )
    mqtt_thread.start()

    # Start portal thread
    portal_thread = threading.Thread(
        target=_portal_thread_func,
        args=(config_path,),
        daemon=True,
        name="portal",
    )
    portal_thread.start()

    # Run render loop on main thread (blocks until shutdown)
    try:
        _render_loop(config_path, display)
    finally:
        display.close()
        logger.info("BambuHelper stopped")


if __name__ == "__main__":
    main()
