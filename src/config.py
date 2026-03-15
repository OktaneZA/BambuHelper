"""Config loading, validation, and atomic saving for BambuHelper.

Config is stored as JSON at /etc/bambu-helper/config.json (or path from
BAMBU_CONFIG env var). All fields have defaults; missing keys are filled
automatically on load. (CFG-01, CFG-03, CFG-04, CFG-05)
"""

import json
import logging
import os
import re
import stat
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

# Default config path (override via BAMBU_CONFIG env var)
DEFAULT_CONFIG_PATH = "/etc/bambu-helper/config.json"

# Defaults applied when a key is absent from the file
DEFAULTS: dict[str, Any] = {
    "connection_mode": "lan",           # "lan" | "cloud"
    "printer_ip": "",
    "printer_access_code": "",          # SEC-01: never log this value
    "printer_serial": "",
    "printer_name": "My Printer",
    "bambu_token": "",                  # SEC-01: never log this value
    "bambu_region": "us",               # "us" | "eu" | "cn"
    "display_brightness": 100,          # 0–255
    "display_rotation": 0,              # 0 | 90 | 180 | 270
    "finish_timeout_s": 300,            # seconds → SCREEN_CLOCK
    "show_clock": True,
    "portal_password": "admin",         # SEC-04: basic auth password
    "portal_port": 8080,
}

_IP_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$"
)
_SERIAL_RE = re.compile(r"^[0-9A-Z]{15,20}$")
_ACCESS_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,}$")


def _check_file_permissions(path: str) -> None:
    """Warn if config file is world-readable (SEC-02)."""
    try:
        mode = os.stat(path).st_mode
        if mode & stat.S_IROTH:
            logger.warning(
                "Config file %s is world-readable — consider `chmod 640 %s`",
                path, path,
            )
    except OSError:
        pass


def validate_config(data: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings; empty list means valid.

    Does not raise — callers decide whether to raise on errors.
    """
    errors: list[str] = []
    mode = data.get("connection_mode", "")

    if mode not in ("lan", "cloud"):
        errors.append(f"connection_mode must be 'lan' or 'cloud', got {mode!r}")

    serial = data.get("printer_serial", "")
    if not serial:
        errors.append("printer_serial is required")
    elif not _SERIAL_RE.match(serial):
        errors.append(f"printer_serial {serial!r} must be 15–20 uppercase alphanumeric characters")

    if mode == "lan":
        ip = data.get("printer_ip", "")
        if not ip:
            errors.append("printer_ip is required in LAN mode")
        elif not _IP_RE.match(ip):
            errors.append(f"printer_ip {ip!r} is not a valid IPv4 address")

        code = data.get("printer_access_code", "")
        if not code:
            errors.append("printer_access_code is required in LAN mode")
        elif not _ACCESS_CODE_RE.match(code):
            errors.append("printer_access_code must be at least 8 alphanumeric characters")

    if mode == "cloud":
        token = data.get("bambu_token", "")
        if not token:
            errors.append("bambu_token is required in Cloud mode")

        region = data.get("bambu_region", "")
        if region not in ("us", "eu", "cn"):
            errors.append(f"bambu_region must be 'us', 'eu', or 'cn', got {region!r}")

    rotation = data.get("display_rotation", 0)
    if rotation not in (0, 90, 180, 270):
        errors.append(f"display_rotation must be 0, 90, 180, or 270; got {rotation!r}")

    brightness = data.get("display_brightness", 100)
    if not isinstance(brightness, int) or not (0 <= brightness <= 255):
        errors.append(f"display_brightness must be an integer 0–255, got {brightness!r}")

    port = data.get("portal_port", 8080)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        errors.append(f"portal_port must be 1–65535, got {port!r}")

    return errors


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load and validate config from *path*.

    Missing keys are filled from DEFAULTS. Raises ValueError if the
    file contains validation errors. Never logs sensitive values. (CFG-01, CFG-04)
    """
    path = path or os.environ.get("BAMBU_CONFIG", DEFAULT_CONFIG_PATH)

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    _check_file_permissions(path)

    try:
        with open(path, encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Config file {path} is not valid JSON: {exc}") from exc

    # Apply defaults for missing keys
    data = {**DEFAULTS, **raw}

    errors = validate_config(data)
    if errors:
        # Do NOT include sensitive values in error messages
        raise ValueError(
            f"Config validation failed ({len(errors)} error(s)):\n  "
            + "\n  ".join(errors)
        )

    logger.info(
        "Config loaded: mode=%s serial=%s ip=%s region=%s",
        data["connection_mode"],
        data["printer_serial"],
        data["printer_ip"] if data["connection_mode"] == "lan" else "(cloud)",
        data.get("bambu_region", "n/a"),
    )
    return data


def save_config(data: dict[str, Any], path: str | None = None) -> None:
    """Validate and atomically write config to *path*.

    Writes to a .tmp file then uses os.replace() for atomicity (CFG-05).
    Raises ValueError if validation fails.
    """
    path = path or os.environ.get("BAMBU_CONFIG", DEFAULT_CONFIG_PATH)

    errors = validate_config(data)
    if errors:
        raise ValueError(
            f"Config validation failed ({len(errors)} error(s)):\n  "
            + "\n  ".join(errors)
        )

    dir_path = os.path.dirname(path) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except PermissionError as exc:
        raise PermissionError(f"Cannot write config to {path}: {exc}") from exc

    logger.info("Config saved to %s", path)
