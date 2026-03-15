"""Flask web config portal for BambuHelper.

Serves a dark-themed configuration page on port 8080 (configurable).
Basic auth is always enabled — no opt-out (SEC-04).

Sensitive fields (access_code, token) are masked in the UI and only
replaced if the user enters a new value. (SEC-01)
"""

import functools
import logging
import threading
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

import config as cfg_module

logger = logging.getLogger(__name__)

_MASK = "••••••••"
_SENSITIVE_KEYS = ("printer_access_code", "bambu_token", "portal_password")


def _mask_config(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with sensitive fields replaced by mask."""
    masked = dict(data)
    for key in _SENSITIVE_KEYS:
        if masked.get(key):
            masked[key] = _MASK
    return masked


def create_app(
    config_path: str,
    shared_state: dict[str, Any],
    lock: threading.Lock,
    restart_event: threading.Event,
) -> Flask:
    """Create and return the Flask app.

    Args:
        config_path: Path to the JSON config file.
        shared_state: Shared printer state dict (read-only in portal).
        lock: Lock protecting *shared_state*.
        restart_event: Set this event to trigger MQTT reconnect after save.
    """
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "bambu-helper-portal"

    # -------------------------------------------------------------- #
    # Auth decorator                                                   #
    # -------------------------------------------------------------- #

    def require_auth(f):  # type: ignore[no-untyped-def]
        @functools.wraps(f)
        def decorated(*args, **kwargs):  # type: ignore[no-untyped-def]
            auth = request.authorization
            try:
                current_cfg = cfg_module.load_config(config_path)
                portal_password = current_cfg.get("portal_password", "admin")
            except Exception:  # noqa: BLE001
                portal_password = "admin"

            if not auth or auth.username != "admin" or auth.password != portal_password:
                return Response(
                    "Authentication required",
                    401,
                    {"WWW-Authenticate": 'Basic realm="BambuHelper"'},
                )
            return f(*args, **kwargs)
        return decorated

    # -------------------------------------------------------------- #
    # Routes                                                           #
    # -------------------------------------------------------------- #

    @app.route("/", methods=["GET"])
    @require_auth
    def index() -> str:
        """Show configuration form."""
        try:
            current_cfg = cfg_module.load_config(config_path)
        except (FileNotFoundError, ValueError):
            current_cfg = dict(cfg_module.DEFAULTS)

        masked = _mask_config(current_cfg)
        errors = request.args.get("errors", "")
        saved = request.args.get("saved", "")
        return render_template(
            "index.html",
            cfg=masked,
            errors=errors,
            saved=saved,
        )

    @app.route("/save", methods=["POST"])
    @require_auth
    def save() -> Response:
        """Validate and write config, then trigger MQTT reconnect."""
        try:
            current_cfg = cfg_module.load_config(config_path)
        except Exception:  # noqa: BLE001
            current_cfg = dict(cfg_module.DEFAULTS)

        form = request.form

        # Build new config from form, preserving sensitive fields if masked
        new_cfg: dict[str, Any] = dict(current_cfg)
        new_cfg["connection_mode"] = form.get("connection_mode", "lan")
        new_cfg["printer_ip"] = form.get("printer_ip", "").strip()
        new_cfg["printer_serial"] = form.get("printer_serial", "").strip()
        new_cfg["printer_name"] = form.get("printer_name", "My Printer").strip()
        new_cfg["bambu_region"] = form.get("bambu_region", "us")
        new_cfg["display_brightness"] = int(form.get("display_brightness", 100))
        new_cfg["display_rotation"] = int(form.get("display_rotation", 0))
        new_cfg["finish_timeout_s"] = int(form.get("finish_timeout_s", 300))
        new_cfg["show_clock"] = form.get("show_clock") == "on"
        new_cfg["portal_port"] = int(form.get("portal_port", 8080))

        # Only update sensitive fields if user entered a new value (not the mask)
        for key in ("printer_access_code", "bambu_token", "portal_password"):
            val = form.get(key, "").strip()
            if val and val != _MASK:
                new_cfg[key] = val  # never log these values (SEC-01)

        errors = cfg_module.validate_config(new_cfg)
        if errors:
            error_str = " | ".join(errors)
            return redirect(url_for("index", errors=error_str))

        try:
            cfg_module.save_config(new_cfg, config_path)
        except (PermissionError, OSError) as exc:
            return redirect(url_for("index", errors=str(exc)))

        logger.info("Config saved via portal; triggering restart")
        restart_event.set()
        return redirect(url_for("index", saved="1"))

    @app.route("/status", methods=["GET"])
    @require_auth
    def status() -> Response:
        """Return live printer state as JSON."""
        with lock:
            state_copy = dict(shared_state)
        # Sanitise: remove last_update float (not JSON-serializable if monotonic)
        state_copy.pop("last_update", None)
        return jsonify(state_copy)

    @app.route("/health", methods=["GET"])
    def health() -> Response:
        """Liveness check — no auth required."""
        return jsonify({"ok": True})

    return app
