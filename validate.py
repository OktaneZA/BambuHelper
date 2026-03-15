"""BambuHelper post-install validator.

Performs 5 checks and prints [ PASS ] or [ FAIL ] for each. (INST-06)
Returns exit code 0 on full pass, 1 on any failure.

Security: Never prints printer_access_code or bambu_token. (SEC-01)
"""

import json
import os
import socket
import sys
import threading
import time

CONFIG_PATH = os.environ.get("BAMBU_CONFIG", "/etc/bambu-helper/config.json")

# Add src to path
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _check(label: str, ok: bool, reason: str = "") -> bool:
    """Print pass/fail line and return ok."""
    if ok:
        print(f"[ PASS ] {label}")
    else:
        print(f"[ FAIL ] {label}{': ' + reason if reason else ''}")
    return ok


def main() -> int:
    all_ok = True
    print("\nBambuHelper Post-Install Validation")
    print("=" * 44)

    # ---------------------------------------------------------------- #
    # Check 1: Config file exists and is readable                       #
    # ---------------------------------------------------------------- #

    config_readable = (
        os.path.isfile(CONFIG_PATH)
        and os.access(CONFIG_PATH, os.R_OK)
    )
    all_ok &= _check(
        f"Config file exists and is readable ({CONFIG_PATH})",
        config_readable,
        f"Not found: {CONFIG_PATH}",
    )

    if not config_readable:
        print("\n  Cannot continue without config. Run install.sh first.")
        return 1

    # ---------------------------------------------------------------- #
    # Check 2: Config fields are valid                                  #
    # ---------------------------------------------------------------- #

    try:
        import config as cfg_module  # noqa: PLC0415

        cfg = cfg_module.load_config(CONFIG_PATH)
        config_valid = True
        config_error = ""
    except ValueError as exc:
        config_valid = False
        config_error = str(exc)
        cfg = {}
    except FileNotFoundError:
        config_valid = False
        config_error = "File not found"
        cfg = {}

    all_ok &= _check("Config is valid", config_valid, config_error)

    if not config_valid:
        print("\n  Fix config errors, then re-run this validator.")
        return 1

    # ---------------------------------------------------------------- #
    # Check 3: Printer network reachable                                #
    # ---------------------------------------------------------------- #

    mode = cfg.get("connection_mode", "lan")
    if mode == "lan":
        host = cfg.get("printer_ip", "")
        check_label = f"Printer reachable via TCP ({host}:8883)"
        try:
            sock = socket.create_connection((host, 8883), timeout=5)
            sock.close()
            network_ok = True
            network_reason = ""
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            network_ok = False
            network_reason = str(exc)
    else:
        from cloud import get_broker  # noqa: PLC0415

        broker = get_broker(cfg.get("bambu_region", "us"))
        check_label = f"Cloud broker DNS resolves ({broker})"
        try:
            socket.getaddrinfo(broker, 8883)
            network_ok = True
            network_reason = ""
        except socket.gaierror as exc:
            network_ok = False
            network_reason = str(exc)

    all_ok &= _check(check_label, network_ok, network_reason)

    # ---------------------------------------------------------------- #
    # Check 4: MQTT connects within 10 s                               #
    # ---------------------------------------------------------------- #

    mqtt_ok = False
    mqtt_reason = "Timed out (10s)"

    try:
        import ssl as _ssl

        import paho.mqtt.client as mqtt

        connected_event = threading.Event()

        def _on_connect(client, userdata, flags, rc):
            nonlocal mqtt_ok
            if rc == 0:
                mqtt_ok = True
                connected_event.set()
            else:
                import paho.mqtt.client as _m
                mqtt_reason_local = f"rc={rc} ({_m.connack_string(rc)})"
                connected_event.set()

        client = mqtt.Client(client_id="bambu_validate", clean_session=True)
        client.on_connect = _on_connect

        if mode == "cloud":
            from cloud import resolve_user_id  # noqa: PLC0415

            user_id = resolve_user_id(cfg["bambu_token"], cfg.get("bambu_region", "us"))
            client.tls_set()
            client.username_pw_set(user_id, cfg["bambu_token"])
            broker = get_broker(cfg.get("bambu_region", "us"))
        else:
            client.tls_set(cert_reqs=_ssl.CERT_NONE)
            client.tls_insecure_set(True)
            client.username_pw_set("bblp", cfg["printer_access_code"])
            broker = cfg["printer_ip"]

        client.connect_async(broker, 8883, keepalive=10)
        client.loop_start()
        connected_event.wait(timeout=10)
        client.loop_stop()
        client.disconnect()

    except ImportError:
        mqtt_ok = False
        mqtt_reason = "paho-mqtt not installed"
    except Exception as exc:  # noqa: BLE001
        mqtt_ok = False
        mqtt_reason = str(exc)

    all_ok &= _check("MQTT connection established within 10s", mqtt_ok, mqtt_reason)

    # ---------------------------------------------------------------- #
    # Check 5: Serial number is set and valid format                   #
    # ---------------------------------------------------------------- #

    import re

    serial = cfg.get("printer_serial", "")
    serial_ok = bool(serial) and bool(re.match(r"^[0-9A-Z]{15,20}$", serial))
    all_ok &= _check(
        f"Printer serial number is set ({serial!r})",
        serial_ok,
        "Empty or invalid format" if not serial_ok else "",
    )

    # ---------------------------------------------------------------- #
    # Summary                                                           #
    # ---------------------------------------------------------------- #

    print("=" * 44)
    if all_ok:
        print("\n✓ All checks passed. BambuHelper is ready!\n")
        print(f"  Web portal: http://{socket.gethostname()}.local:{cfg.get('portal_port', 8080)}")
        print("  Logs:       journalctl -u bambu-helper -f\n")
    else:
        print("\n✗ Some checks failed. Review errors above.\n")
        print("  Troubleshooting:")
        print("    - LAN: ensure printer IP is correct and on same network")
        print("    - Cloud: run scripts/get_cloud_token.py to refresh token")
        print("    - Logs: journalctl -u bambu-helper -f\n")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
