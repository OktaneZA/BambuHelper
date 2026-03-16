"""Tests for src/portal.py — auth, local-only mode, password hashing on save."""

import base64
import json
import os
import sys
import threading

import pytest

# Add src to path
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import config as cfg_module
from portal import create_app


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


def _make_app(config_path: str):
    """Create a test Flask app with minimal shared state."""
    shared_state: dict = {}
    lock = threading.Lock()
    restart_event = threading.Event()
    return create_app(config_path, shared_state, lock, restart_event)


def _b64creds(username: str, password: str) -> str:
    """Return base64-encoded 'username:password' for an Authorization header."""
    raw = f"{username}:{password}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


# ------------------------------------------------------------------ #
# No-password (local-only) mode                                        #
# ------------------------------------------------------------------ #

class TestNoPasswordMode:
    """portal_password is empty — local access allowed, remote blocked (SEC-04)."""

    def _cfg(self, tmp_path) -> str:
        cfg = {**_valid_lan_config(), "portal_password": ""}
        path = str(tmp_path / "config.json")
        _write_config(cfg, path)
        return path

    def test_localhost_gets_200(self, tmp_path):
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 200

    def test_ipv6_localhost_gets_200(self, tmp_path):
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "::1"})
        assert resp.status_code == 200

    def test_remote_ip_gets_403(self, tmp_path):
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 403

    def test_remote_403_body_mentions_password(self, tmp_path):
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert b"password" in resp.data.lower()

    def test_health_endpoint_always_200_no_auth(self, tmp_path):
        """Health check has no auth — accessible from any origin (SEC-04 exemption)."""
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.get("/health", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 200

    def test_status_endpoint_remote_gets_403(self, tmp_path):
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.get("/status", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 403

    def test_save_endpoint_remote_gets_403(self, tmp_path):
        """POST /save from remote must be blocked (most sensitive endpoint)."""
        app = _make_app(self._cfg(tmp_path))
        with app.test_client() as client:
            resp = client.post("/save", data={}, environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 403


# ------------------------------------------------------------------ #
# Password set — Basic Auth required from all origins                 #
# ------------------------------------------------------------------ #

class TestPasswordMode:
    """portal_password is a PBKDF2 hash — HTTP Basic Auth required everywhere (SEC-04)."""

    def _cfg_with_hash(self, tmp_path, plaintext: str = "secret") -> tuple:
        pw_hash = cfg_module.hash_password(plaintext)
        cfg = {**_valid_lan_config(), "portal_password": pw_hash}
        path = str(tmp_path / "config.json")
        _write_config(cfg, path)
        return path, plaintext

    def test_remote_correct_password_gets_200(self, tmp_path):
        path, pw = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get(
                "/",
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', pw)}"},
            )
        assert resp.status_code == 200

    def test_remote_wrong_password_gets_401(self, tmp_path):
        path, _ = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get(
                "/",
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', 'wrongpassword')}"},
            )
        assert resp.status_code == 401

    def test_remote_no_credentials_gets_401(self, tmp_path):
        path, _ = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 401

    def test_401_includes_www_authenticate_header(self, tmp_path):
        path, _ = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert "WWW-Authenticate" in resp.headers

    def test_localhost_with_password_set_requires_auth(self, tmp_path):
        """Local access does NOT bypass auth when a password is configured."""
        path, _ = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 401

    def test_localhost_correct_password_gets_200(self, tmp_path):
        path, pw = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get(
                "/",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', pw)}"},
            )
        assert resp.status_code == 200

    def test_wrong_username_gets_401(self, tmp_path):
        path, pw = self._cfg_with_hash(tmp_path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get(
                "/",
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('notadmin', pw)}"},
            )
        assert resp.status_code == 401


# ------------------------------------------------------------------ #
# Legacy plaintext migration                                           #
# ------------------------------------------------------------------ #

class TestLegacyPlaintextMigration:
    """Old installs with plaintext portal_password must still authenticate (SEC-08)."""

    def test_legacy_correct_password_accepted(self, tmp_path):
        cfg = {**_valid_lan_config(), "portal_password": "admin"}
        path = str(tmp_path / "config.json")
        _write_config(cfg, path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get(
                "/",
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', 'admin')}"},
            )
        assert resp.status_code == 200

    def test_legacy_wrong_password_rejected(self, tmp_path):
        cfg = {**_valid_lan_config(), "portal_password": "admin"}
        path = str(tmp_path / "config.json")
        _write_config(cfg, path)
        app = _make_app(path)
        with app.test_client() as client:
            resp = client.get(
                "/",
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', 'wrong')}"},
            )
        assert resp.status_code == 401


# ------------------------------------------------------------------ #
# Save route hashes new password                                       #
# ------------------------------------------------------------------ #

class TestSaveRouteHashesPassword:
    """Submitting a new password via /save must store it hashed (SEC-08)."""

    def _form_data(self, **overrides) -> dict:
        base = {
            "connection_mode": "lan",
            "printer_ip": "192.168.1.50",
            "printer_access_code": "abcd1234",
            "printer_serial": "01P00A123456789",
            "printer_name": "Test Printer",
            "bambu_region": "us",
            "display_brightness": "100",
            "display_rotation": "0",
            "finish_timeout_s": "300",
            "show_clock": "on",
            "portal_port": "5432",
            "portal_password": "",
            "bambu_token": "",
        }
        base.update(overrides)
        return base

    def test_save_new_password_is_stored_hashed(self, tmp_path):
        pw_hash = cfg_module.hash_password("currentpass")
        cfg = {**_valid_lan_config(), "portal_password": pw_hash}
        path = str(tmp_path / "config.json")
        _write_config(cfg, path)
        app = _make_app(path)

        with app.test_client() as client:
            client.post(
                "/save",
                data=self._form_data(portal_password="newpassword"),
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', 'currentpass')}"},
            )

        with open(path) as f:
            saved = json.load(f)

        stored = saved["portal_password"]
        assert stored.startswith("pbkdf2:sha256:"), f"Expected hash, got: {stored!r}"
        assert cfg_module.verify_password("newpassword", stored)

    def test_save_masked_password_not_overwritten(self, tmp_path):
        """Submitting the mask placeholder must not overwrite the existing hash."""
        pw_hash = cfg_module.hash_password("keepme")
        cfg = {**_valid_lan_config(), "portal_password": pw_hash}
        path = str(tmp_path / "config.json")
        _write_config(cfg, path)
        app = _make_app(path)

        with app.test_client() as client:
            client.post(
                "/save",
                data=self._form_data(portal_password="••••••••"),  # mask value
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"Authorization": f"Basic {_b64creds('admin', 'keepme')}"},
            )

        with open(path) as f:
            saved = json.load(f)

        assert cfg_module.verify_password("keepme", saved["portal_password"])
