"""Tests for src/config.py — validation, defaults, and atomic write."""

import json
import os
import sys
import tempfile

import pytest

# Add src to path
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import config as cfg_module


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _write_config(data: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(data, f)


def _valid_lan_config() -> dict:
    return {
        "connection_mode": "lan",
        "printer_ip": "192.168.1.50",
        "printer_access_code": "abcd1234",
        "printer_serial": "01P00A123456789",
        "printer_name": "Test Printer",
    }


def _valid_cloud_config() -> dict:
    return {
        "connection_mode": "cloud",
        "bambu_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOiIxMjM0NTYifQ.sig",
        "bambu_region": "us",
        "printer_serial": "01P00A123456789",
    }


# ------------------------------------------------------------------ #
# validate_config                                                      #
# ------------------------------------------------------------------ #

class TestValidateConfig:
    def test_valid_lan_has_no_errors(self):
        errors = cfg_module.validate_config(_valid_lan_config())
        assert errors == []

    def test_valid_cloud_has_no_errors(self):
        errors = cfg_module.validate_config(_valid_cloud_config())
        assert errors == []

    def test_missing_serial_is_error(self):
        data = _valid_lan_config()
        data["printer_serial"] = ""
        errors = cfg_module.validate_config(data)
        assert any("serial" in e.lower() for e in errors)

    def test_invalid_serial_format_is_error(self):
        data = _valid_lan_config()
        data["printer_serial"] = "abc"  # too short
        errors = cfg_module.validate_config(data)
        assert any("serial" in e.lower() for e in errors)

    def test_missing_ip_in_lan_mode_is_error(self):
        data = _valid_lan_config()
        data["printer_ip"] = ""
        errors = cfg_module.validate_config(data)
        assert any("ip" in e.lower() for e in errors)

    def test_invalid_ip_format_is_error(self):
        data = _valid_lan_config()
        data["printer_ip"] = "not-an-ip"
        errors = cfg_module.validate_config(data)
        assert any("ip" in e.lower() for e in errors)

    def test_missing_access_code_in_lan_is_error(self):
        data = _valid_lan_config()
        data["printer_access_code"] = ""
        errors = cfg_module.validate_config(data)
        assert any("access_code" in e.lower() for e in errors)

    def test_cloud_mode_empty_token_is_error(self):
        data = _valid_cloud_config()
        data["bambu_token"] = ""
        errors = cfg_module.validate_config(data)
        assert any("token" in e.lower() for e in errors)

    def test_invalid_region_is_error(self):
        data = _valid_cloud_config()
        data["bambu_region"] = "xx"
        errors = cfg_module.validate_config(data)
        assert any("region" in e.lower() for e in errors)

    def test_invalid_rotation_is_error(self):
        data = _valid_lan_config()
        data["display_rotation"] = 45
        errors = cfg_module.validate_config(data)
        assert any("rotation" in e.lower() for e in errors)

    def test_valid_rotations_accepted(self):
        for r in (0, 90, 180, 270):
            data = _valid_lan_config()
            data["display_rotation"] = r
            assert cfg_module.validate_config(data) == []

    def test_brightness_out_of_range_is_error(self):
        data = _valid_lan_config()
        data["display_brightness"] = 300
        errors = cfg_module.validate_config(data)
        assert any("brightness" in e.lower() for e in errors)

    def test_invalid_connection_mode_is_error(self):
        data = _valid_lan_config()
        data["connection_mode"] = "wifi"
        errors = cfg_module.validate_config(data)
        assert any("connection_mode" in e.lower() for e in errors)


# ------------------------------------------------------------------ #
# load_config                                                          #
# ------------------------------------------------------------------ #

class TestLoadConfig:
    def test_valid_lan_config_loads(self, tmp_path):
        path = str(tmp_path / "config.json")
        _write_config(_valid_lan_config(), path)
        result = cfg_module.load_config(path)
        assert result["printer_serial"] == "01P00A123456789"
        assert result["connection_mode"] == "lan"

    def test_defaults_applied_for_missing_keys(self, tmp_path):
        path = str(tmp_path / "config.json")
        # Only required fields
        _write_config(_valid_lan_config(), path)
        result = cfg_module.load_config(path)
        assert result["display_brightness"] == cfg_module.DEFAULTS["display_brightness"]
        assert result["portal_port"] == cfg_module.DEFAULTS["portal_port"]

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            cfg_module.load_config(str(tmp_path / "nonexistent.json"))

    def test_invalid_json_raises_value_error(self, tmp_path):
        path = str(tmp_path / "config.json")
        with open(path, "w") as f:
            f.write("{not valid json")
        with pytest.raises(ValueError, match="valid JSON"):
            cfg_module.load_config(path)

    def test_invalid_config_raises_value_error(self, tmp_path):
        path = str(tmp_path / "config.json")
        bad = _valid_lan_config()
        bad["printer_serial"] = ""
        _write_config(bad, path)
        with pytest.raises(ValueError):
            cfg_module.load_config(path)

    def test_access_code_not_in_error_message(self, tmp_path):
        path = str(tmp_path / "config.json")
        bad = _valid_lan_config()
        bad["printer_serial"] = ""
        _write_config(bad, path)
        try:
            cfg_module.load_config(path)
        except ValueError as exc:
            # SEC-01: access code must not appear in error messages
            assert bad["printer_access_code"] not in str(exc)


# ------------------------------------------------------------------ #
# save_config                                                          #
# ------------------------------------------------------------------ #

class TestSaveConfig:
    def test_save_writes_valid_json(self, tmp_path):
        path = str(tmp_path / "config.json")
        _write_config(_valid_lan_config(), path)  # create first
        cfg_module.save_config(_valid_lan_config(), path)
        with open(path) as f:
            data = json.load(f)
        assert data["connection_mode"] == "lan"

    def test_save_is_atomic_no_tmp_left(self, tmp_path):
        path = str(tmp_path / "config.json")
        _write_config(_valid_lan_config(), path)
        cfg_module.save_config(_valid_lan_config(), path)
        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert tmp_files == [], "Temporary file was not cleaned up"

    def test_save_invalid_config_raises(self, tmp_path):
        path = str(tmp_path / "config.json")
        bad = _valid_lan_config()
        bad["printer_serial"] = ""
        with pytest.raises(ValueError):
            cfg_module.save_config(bad, path)

    def test_save_then_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "config.json")
        original = _valid_lan_config()
        original["printer_name"] = "Roundtrip Test"
        _write_config(original, path)
        cfg_module.save_config(original, path)
        loaded = cfg_module.load_config(path)
        assert loaded["printer_name"] == "Roundtrip Test"


# ------------------------------------------------------------------ #
# hash_password / verify_password (SEC-08)                            #
# ------------------------------------------------------------------ #

class TestPasswordHashing:
    def test_hash_returns_pbkdf2_prefix(self):
        h = cfg_module.hash_password("mypassword")
        assert h.startswith("pbkdf2:sha256:260000:")

    def test_hash_has_five_colon_separated_parts(self):
        h = cfg_module.hash_password("mypassword")
        parts = h.split(":")
        assert len(parts) == 5

    def test_hash_round_trip_correct_password(self):
        h = cfg_module.hash_password("correct-horse")
        assert cfg_module.verify_password("correct-horse", h) is True

    def test_hash_round_trip_wrong_password(self):
        h = cfg_module.hash_password("correct-horse")
        assert cfg_module.verify_password("wrong-horse", h) is False

    def test_two_hashes_of_same_password_differ(self):
        """Different salts must produce different hashes."""
        h1 = cfg_module.hash_password("same")
        h2 = cfg_module.hash_password("same")
        assert h1 != h2
        assert cfg_module.verify_password("same", h1) is True
        assert cfg_module.verify_password("same", h2) is True

    def test_verify_legacy_plaintext_migration(self):
        """Old installs stored 'admin' as plaintext — must still verify (SEC-08)."""
        assert cfg_module.verify_password("admin", "admin") is True

    def test_verify_legacy_wrong_password(self):
        assert cfg_module.verify_password("wrong", "admin") is False

    def test_verify_empty_stored_returns_false(self):
        """Empty stored string means 'no password set' — never authenticates."""
        assert cfg_module.verify_password("anything", "") is False

    def test_verify_malformed_hash_returns_false(self):
        assert cfg_module.verify_password("pw", "pbkdf2:sha256:NOTANINT:abc:def") is False

    def test_hash_empty_password(self):
        """Empty string can be hashed and verified without error."""
        h = cfg_module.hash_password("")
        assert cfg_module.verify_password("", h) is True
        assert cfg_module.verify_password("notempty", h) is False
