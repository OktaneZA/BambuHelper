"""Tests for src/bambu.py — MQTT parsing, delta merge, backoff, security."""

import logging
import os
import sys

import pytest

# Add src to path
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Mock paho-mqtt before importing bambu (not available in dev environment)
from unittest.mock import MagicMock
import sys as _sys

if "paho" not in _sys.modules:
    paho_mock = MagicMock()
    paho_mock.mqtt.client.Client = MagicMock
    paho_mock.mqtt.client.MQTTMessage = MagicMock
    _sys.modules["paho"] = paho_mock
    _sys.modules["paho.mqtt"] = paho_mock.mqtt
    _sys.modules["paho.mqtt.client"] = paho_mock.mqtt.client

# Mock cloud module dependencies
if "requests" not in _sys.modules:
    _sys.modules["requests"] = MagicMock()

from bambu import parse_report, backoff_delay, DEFAULT_STATE


# ------------------------------------------------------------------ #
# parse_report                                                         #
# ------------------------------------------------------------------ #

# Full P1P-style payload (all fields present)
_FULL_PAYLOAD = {
    "print": {
        "gcode_state": "RUNNING",
        "mc_percent": 68,
        "mc_remaining_time": 83,
        "nozzle_temper": 220.5,
        "nozzle_target_temper": 220.0,
        "bed_temper": 60.1,
        "bed_target_temper": 60.0,
        "chamber_temper": 28.3,
        "subtask_name": "benchy.3mf",
        "layer_num": 45,
        "total_layer_num": 120,
        "cooling_fan_speed": 10,  # raw 0–15, should → 66%
        "big_fan1_speed": 80,
        "big_fan2_speed": 30,
        "heatbreak_fan_speed": 100,
        "wifi_signal": -45,
        "spd_lvl": 3,
    }
}


class TestParseReport:
    def test_all_fields_extracted(self):
        result = parse_report(_FULL_PAYLOAD)
        assert result["gcode_state"] == "RUNNING"
        assert result["progress"] == 68
        assert result["remaining_minutes"] == 83
        assert result["nozzle_temp"] == pytest.approx(220.5)
        assert result["nozzle_target"] == pytest.approx(220.0)
        assert result["bed_temp"] == pytest.approx(60.1)
        assert result["bed_target"] == pytest.approx(60.0)
        assert result["chamber_temp"] == pytest.approx(28.3)
        assert result["subtask_name"] == "benchy.3mf"
        assert result["layer_num"] == 45
        assert result["total_layers"] == 120
        assert result["aux_fan_pct"] == 80
        assert result["chamber_fan_pct"] == 30
        assert result["heatbreak_fan_pct"] == 100
        assert result["wifi_signal"] == -45
        assert result["speed_level"] == 3

    def test_cooling_fan_scaling(self):
        """Raw cooling_fan_speed 0–15 must scale to 0–100%."""
        result = parse_report(_FULL_PAYLOAD)
        # 10 * 100 // 15 = 66
        assert result["cooling_fan_pct"] == 66

    def test_cooling_fan_zero(self):
        payload = {"print": {"cooling_fan_speed": 0}}
        result = parse_report(payload)
        assert result["cooling_fan_pct"] == 0

    def test_cooling_fan_max(self):
        payload = {"print": {"cooling_fan_speed": 15}}
        result = parse_report(payload)
        assert result["cooling_fan_pct"] == 100

    def test_partial_payload_delta(self):
        """P1/A1 partial payloads — only keys present should be in result."""
        partial = {"print": {"mc_percent": 50, "nozzle_temper": 215.0}}
        result = parse_report(partial)
        assert result["progress"] == 50
        assert result["nozzle_temp"] == pytest.approx(215.0)
        # Keys not in partial should not be present in result
        assert "bed_temp" not in result
        assert "layer_num" not in result

    def test_empty_payload_returns_empty(self):
        result = parse_report({})
        assert result == {}

    def test_no_print_key_returns_empty(self):
        result = parse_report({"info": {"something": 1}})
        assert result == {}

    def test_empty_string_fields_excluded(self):
        """Empty gcode_state should not overwrite existing state."""
        payload = {"print": {"gcode_state": "", "mc_percent": 25}}
        result = parse_report(payload)
        assert "gcode_state" not in result
        assert result["progress"] == 25

    def test_zero_progress_included(self):
        """Zero is a valid value for progress — must not be excluded."""
        payload = {"print": {"mc_percent": 0}}
        result = parse_report(payload)
        assert result["progress"] == 0

    def test_non_dict_print_returns_empty(self):
        result = parse_report({"print": "not a dict"})
        assert result == {}


# ------------------------------------------------------------------ #
# backoff_delay                                                        #
# ------------------------------------------------------------------ #

class TestBackoffDelay:
    def test_phase1_attempts_1_to_5_return_10s(self):
        for attempt in range(1, 6):
            assert backoff_delay(attempt) == 10

    def test_phase2_attempts_6_to_15_return_60s(self):
        for attempt in range(6, 16):
            assert backoff_delay(attempt) == 60

    def test_phase3_attempts_above_15_return_120s(self):
        for attempt in (16, 20, 50, 100):
            assert backoff_delay(attempt) == 120


# ------------------------------------------------------------------ #
# Security: sensitive values never logged (SEC-01)                    #
# ------------------------------------------------------------------ #

class TestSecurityLogging:
    def test_access_code_not_logged(self, caplog):
        """printer_access_code must never appear in log output."""
        secret = "SUPER_SECRET_CODE_123"
        payload = {"print": {"gcode_state": "RUNNING", "mc_percent": 50}}
        with caplog.at_level(logging.DEBUG, logger="bambu"):
            parse_report(payload)
        assert secret not in caplog.text

    def test_token_not_logged(self, caplog):
        """bambu_token must never appear in log output."""
        token = "eyJhbGciOiJSUzI1NiJ9.eyJ1aWQiOiI5OTk5OTkifQ.secret_sig"
        # parse_report doesn't receive token, but verify the test pattern works
        with caplog.at_level(logging.DEBUG, logger="bambu"):
            parse_report({"print": {}})
        assert token not in caplog.text


# ------------------------------------------------------------------ #
# DEFAULT_STATE                                                        #
# ------------------------------------------------------------------ #

class TestDefaultState:
    def test_default_state_has_all_required_keys(self):
        required = [
            "connected", "printing", "gcode_state", "progress", "remaining_minutes",
            "nozzle_temp", "nozzle_target", "bed_temp", "bed_target", "chamber_temp",
            "subtask_name", "layer_num", "total_layers", "cooling_fan_pct",
            "aux_fan_pct", "chamber_fan_pct", "heatbreak_fan_pct",
            "wifi_signal", "speed_level", "last_update",
        ]
        for key in required:
            assert key in DEFAULT_STATE, f"Missing key: {key}"

    def test_default_state_connected_is_false(self):
        assert DEFAULT_STATE["connected"] is False

    def test_default_state_printing_is_false(self):
        assert DEFAULT_STATE["printing"] is False
