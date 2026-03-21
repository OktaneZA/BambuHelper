"""Bambu Lab MQTT client and state parser for BambuHelper.

Faithful Python port of bambu_mqtt.cpp and parts of bambu_cloud.cpp
from Keralots/BambuHelper.

Architecture:
- BambuClient wraps a paho-mqtt Client
- Runs inside the MQTT background thread (called from main.py)
- Writes to shared state dict under a threading.Lock
- Never calls display functions directly

Security: printer_access_code and bambu_token are NEVER logged. (SEC-01)
"""

import json
import logging
import ssl
import threading
import time
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

from cloud import get_broker, resolve_user_id

logger = logging.getLogger(__name__)

# MQTT constants (from config.h)
_BAMBU_PORT = 8883
_BAMBU_USERNAME_LAN = "bblp"
_BAMBU_BUFFER_SIZE = 16384
_PUSHALL_INTERVAL_S = 30  # NET-03: request full status every 30 s

# Back-off phases (ARCH-01) matching original bambu_mqtt.cpp exactly
_BACKOFF_PHASE1_MAX = 5    # attempts 1–5
_BACKOFF_PHASE2_MAX = 15   # attempts 6–15
_BACKOFF_PHASE1_S = 10
_BACKOFF_PHASE2_S = 60
_BACKOFF_PHASE3_S = 120

# Stale timeout: no MQTT message → mark disconnected (ARCH-05)
_STALE_LAN_S = 60
_STALE_CLOUD_S = 300

# Default BambuState matching bambu_state.h
DEFAULT_STATE: dict[str, Any] = {
    "connected": False,
    "printing": False,
    "gcode_state": "",
    "progress": 0,
    "remaining_minutes": 0,
    "nozzle_temp": 0.0,
    "nozzle_target": 0.0,
    "bed_temp": 0.0,
    "bed_target": 0.0,
    "chamber_temp": 0.0,
    "subtask_name": "",
    "layer_num": 0,
    "total_layers": 0,
    "cooling_fan_pct": 0,     # raw 0–15 → 0–100% (NET-01 fan scaling)
    "aux_fan_pct": 0,
    "chamber_fan_pct": 0,
    "heatbreak_fan_pct": 0,
    "wifi_signal": 0,         # RSSI dBm
    "speed_level": 2,         # 1=silent,2=std,3=sport,4=ludicrous
    "last_update": 0.0,       # time.monotonic()
}


def backoff_delay(attempt: int) -> int:
    """Return reconnect delay in seconds for *attempt* number (ARCH-01).

    Matches the three-phase back-off from the original C++:
    - Phase 1 (1–5):  10 s
    - Phase 2 (6–15): 60 s
    - Phase 3 (>15): 120 s
    """
    if attempt <= _BACKOFF_PHASE1_MAX:
        return _BACKOFF_PHASE1_S
    if attempt <= _BACKOFF_PHASE2_MAX:
        return _BACKOFF_PHASE2_S
    return _BACKOFF_PHASE3_S


def parse_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract printer state from a Bambu MQTT report payload.

    Pure function — no side effects, safe to call from tests.
    Implements field mapping from bambu_mqtt.cpp with delta-merge support (ARCH-04).
    P1/A1 printers send partial payloads; only non-None/non-empty values are returned.
    """
    p = payload.get("print", {})
    if not isinstance(p, dict):
        return {}

    result: dict[str, Any] = {}

    def _set(key: str, value: Any, transform: Optional[Callable] = None) -> None:
        """Only include key if value is meaningfully present."""
        if value is None:
            return
        if isinstance(value, str) and value == "":
            return
        result[key] = transform(value) if transform else value

    _set("gcode_state", p.get("gcode_state"))
    _set("progress", p.get("mc_percent"))
    _set("remaining_minutes", p.get("mc_remaining_time"))
    _set("nozzle_temp", p.get("nozzle_temper"))
    _set("nozzle_target", p.get("nozzle_target_temper"))
    _set("bed_temp", p.get("bed_temper"))
    _set("bed_target", p.get("bed_target_temper"))
    _set("chamber_temp", p.get("chamber_temper"))
    _set("subtask_name", p.get("subtask_name"))
    _set("layer_num", p.get("layer_num"))
    _set("total_layers", p.get("total_layer_num"))
    _set("wifi_signal", p.get("wifi_signal"))
    _set("speed_level", p.get("spd_lvl"))

    # Fan scaling: cooling_fan_speed is 0–15 raw → 0–100% (from bambu_mqtt.cpp)
    raw_cooling = p.get("cooling_fan_speed")
    if raw_cooling is not None:
        result["cooling_fan_pct"] = int(raw_cooling) * 100 // 15

    # Aux and chamber fans are already 0–100 in the payload
    _set("aux_fan_pct", p.get("big_fan1_speed"))
    _set("chamber_fan_pct", p.get("big_fan2_speed"))
    _set("heatbreak_fan_pct", p.get("heatbreak_fan_speed"))

    return result


class BambuClient:
    """MQTT client for Bambu Lab printers (LAN and Cloud modes).

    Designed to run inside a background thread. Call `run_forever()` which
    blocks, handling connection, subscription, message parsing, and
    reconnection. Signal shutdown by setting the passed `shutdown_event`.

    Ported from bambu_mqtt.cpp + bambu_cloud.cpp (Keralots/BambuHelper).
    """

    def __init__(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        lock: threading.Lock,
        epoch_counter: list[int],   # mutable single-element list for shared epoch
        on_state_change: Optional[Callable] = None,
    ) -> None:
        """Initialise with shared state references.

        Args:
            config: Loaded config dict from config.py.
            state: Shared printer state dict (written under *lock*).
            lock: threading.Lock protecting *state* and *epoch_counter*.
            epoch_counter: Single-element list [int] — incremented on state change.
            on_state_change: Optional callback invoked (without lock) after epoch increment.
        """
        self._config = config
        self._state = state
        self._lock = lock
        self._epoch = epoch_counter
        self._on_change = on_state_change
        self._client: Optional[mqtt.Client] = None
        self._attempt = 0
        self._last_pushall = 0.0
        self._pushall_seq = 0
        self._is_cloud = config.get("connection_mode") == "cloud"

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def run_forever(self, shutdown_event: threading.Event) -> None:
        """Connect, subscribe, and loop until *shutdown_event* is set.

        Reconnects automatically with back-off on failure.
        """
        while not shutdown_event.is_set():
            self._attempt += 1
            logger.info("MQTT connect attempt %d (mode=%s)", self._attempt, self._config["connection_mode"])

            try:
                self._client = self._build_client()
                self._do_connect()
                self._client.loop_start()

                # Wait for either shutdown or stale-timeout, sending pushall periodically
                self._run_loop(shutdown_event)

            except Exception as exc:  # noqa: BLE001 — must not crash background thread
                logger.warning(
                    "MQTT connection failed (attempt %d): %s — retry in %ds",
                    self._attempt, exc, backoff_delay(self._attempt),
                )
                self._mark_disconnected()
            finally:
                if self._client:
                    try:
                        self._client.loop_stop()
                        self._client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self._client = None

            if not shutdown_event.is_set():
                delay = backoff_delay(self._attempt)
                logger.debug("Waiting %ds before next MQTT connect attempt", delay)
                shutdown_event.wait(timeout=delay)

    # ------------------------------------------------------------------ #
    # Connection setup                                                     #
    # ------------------------------------------------------------------ #

    def _build_client(self) -> mqtt.Client:
        """Create and configure a paho MQTT client."""
        client_id = f"bambu_helper_{self._attempt}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
            clean_session=True,
        )
        client.max_inflight_messages_set(1)

        # Callbacks
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_log = self._on_log

        # TLS setup
        if self._is_cloud:
            # Cloud: verify against system CAs (SEC-05)
            client.tls_set()
        else:
            # LAN: printer uses self-signed cert — must accept (NET-01)
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)

        # Credentials — access code / token never logged (SEC-01)
        if self._is_cloud:
            user_id = resolve_user_id(self._config["bambu_token"], self._config["bambu_region"])
            client.username_pw_set(user_id, self._config["bambu_token"])
        else:
            client.username_pw_set(_BAMBU_USERNAME_LAN, self._config["printer_access_code"])

        # Buffer size for full pushall responses (NET-08)
        # paho v1.x does not have a direct buffer size API; handled by socket buffers.
        # Max packet size can be set on paho v2; skip for compat.

        return client

    def _do_connect(self) -> None:
        """Resolve broker and initiate TCP connect."""
        serial = self._config["printer_serial"]
        if self._is_cloud:
            broker = get_broker(self._config["bambu_region"])
        else:
            broker = self._config["printer_ip"]

        logger.info("Connecting to MQTT broker %s:%d (serial=%s)", broker, _BAMBU_PORT, serial)
        self._client.connect(broker, _BAMBU_PORT, keepalive=60)

    # ------------------------------------------------------------------ #
    # paho callbacks                                                       #
    # ------------------------------------------------------------------ #

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict, rc: int) -> None:
        """Called by paho on successful or failed connect."""
        if rc != 0:
            logger.warning("MQTT connect refused: rc=%d (%s)", rc, mqtt.connack_string(rc))
            return

        serial = self._config["printer_serial"]
        topic = f"device/{serial}/report"
        client.subscribe(topic)
        logger.info("MQTT connected; subscribed to %s", topic)

        self._attempt = 0  # reset back-off on success
        self._last_pushall = 0.0  # trigger immediate pushall

        with self._lock:
            self._state["connected"] = True
            self._epoch[0] += 1
        if self._on_change:
            self._on_change()

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Called by paho on disconnect."""
        logger.warning("MQTT disconnected: rc=%d", rc)
        self._mark_disconnected()

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        """Parse incoming printer report and update shared state (ARCH-04)."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Received malformed MQTT message: %s", exc)
            return

        parsed = parse_report(payload)
        if not parsed:
            logger.debug("MQTT message had no print fields (topic=%s)", msg.topic)
            return

        # Derive printing flag from gcode_state
        gcode_state = parsed.get("gcode_state", self._state.get("gcode_state", ""))
        printing = gcode_state in ("RUNNING", "PREPARE", "PAUSE")

        with self._lock:
            # Delta merge — only overwrite keys present in this payload (ARCH-04)
            for k, v in parsed.items():
                self._state[k] = v
            self._state["printing"] = printing
            self._state["last_update"] = time.monotonic()
            self._epoch[0] += 1

        logger.debug(
            "State updated: gcode=%s progress=%s%% nozzle=%.1f layer=%s/%s",
            gcode_state,
            parsed.get("progress", "-"),
            parsed.get("nozzle_temp", self._state.get("nozzle_temp", 0)),
            parsed.get("layer_num", "-"),
            parsed.get("total_layers", "-"),
        )

        if self._on_change:
            self._on_change()

    # ------------------------------------------------------------------ #
    # Run loop                                                             #
    # ------------------------------------------------------------------ #

    def _run_loop(self, shutdown_event: threading.Event) -> None:
        """Block while connected, sending periodic pushall requests."""
        stale_timeout = _STALE_CLOUD_S if self._is_cloud else _STALE_LAN_S
        serial = self._config["printer_serial"]

        while not shutdown_event.is_set() and self._client and self._client.is_connected():
            now = time.monotonic()

            # Periodic pushall (NET-03, NET-04)
            if now - self._last_pushall >= _PUSHALL_INTERVAL_S:
                self._send_pushall(serial)
                self._last_pushall = now

            # Stale detection (ARCH-05)
            last_update = self._state.get("last_update", 0.0)
            if last_update > 0 and now - last_update > stale_timeout:
                logger.warning(
                    "No MQTT message for %.0fs (timeout=%ds) — marking stale",
                    now - last_update, stale_timeout,
                )
                with self._lock:
                    self._state["printing"] = False
                    self._epoch[0] += 1
                if self._on_change:
                    self._on_change()

            shutdown_event.wait(timeout=1.0)

    def _send_pushall(self, serial: str) -> None:
        """Publish a pushall command to request full printer status."""
        self._pushall_seq += 1
        payload = json.dumps({
            "pushing": {
                "sequence_id": str(self._pushall_seq),
                "command": "pushall",
            }
        })
        topic = f"device/{serial}/request"
        try:
            self._client.publish(topic, payload)
            logger.debug("Published pushall #%d to %s", self._pushall_seq, topic)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to publish pushall: %s", exc)

    def _on_log(self, client: mqtt.Client, userdata: Any, level: int, buf: str) -> None:
        """Log all paho internal messages at DEBUG level."""
        logger.debug("paho [%d]: %s", level, buf)

    def _mark_disconnected(self) -> None:
        """Update shared state to reflect MQTT disconnection."""
        with self._lock:
            self._state["connected"] = False
            self._epoch[0] += 1
        if self._on_change:
            self._on_change()
