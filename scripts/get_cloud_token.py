"""Bambu Lab cloud token extractor.

Logs into the Bambu Lab API (same endpoint used by Bambu Studio / OrcaSlicer)
and returns the JWT accessToken required for MQTT.

The Bambu *website* login only sets a session cookie, which is NOT accepted
by the MQTT broker.  This script calls the API directly to get the proper
JWT (eyJ...) that the MQTT broker requires as the password.

Usage:
    pip install requests
    python scripts/get_cloud_token.py

Handles all verification flows:
  - verifyCode: Bambu emails a code automatically — enter it at the prompt
  - tfaKey:     Same email flow via the TFA endpoint
  - tfa/mfa:    TOTP authenticator app code

Security: the token is printed ONCE to stdout only. Never logged or stored
          beyond the output file (if --output-file is used).
"""

import argparse
import getpass
import sys

try:
    import requests
except ImportError:
    print("requests is not installed.  Run:  pip install requests")
    sys.exit(1)

# Region-aware API endpoints (same as OrcaSlicer / bambu_cloud.cpp)
_API_BASES = {
    "us": "https://api.bambulab.com",
    "eu": "https://api.bambulab.com",   # EU accounts use the global endpoint
    "cn": "https://api.bambulab.cn",
}

_LOGIN_PATH      = "/v1/user-service/user/login"
_TFA_SEND_PATH   = "/v1/user-service/user/login/tfa/email"
_TFA_VERIFY_PATH = "/v1/user-service/user/login/tfa/email/code"
_MFA_PATH        = "/v1/user-service/user/login/tfa/mfa"

# Headers that mimic OrcaSlicer
_HEADERS = {
    "User-Agent":          "bambu_network_agent/01.09.05.01",
    "X-BBL-Client-Name":   "OrcaSlicer",
    "X-BBL-Client-Version":"01.09.05.51",
    "Content-Type":        "application/json",
}


def _post(session: requests.Session, url: str, body: dict) -> dict:
    """POST JSON and return parsed response body."""
    resp = session.post(url, json=body, headers=_HEADERS, verify=True, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _dump_response(label: str, resp: dict) -> None:
    """Print all fields in the API response for debugging."""
    print(f"\n── {label} ──")
    for key, value in resp.items():
        # Show full value for all fields so we can diagnose token format
        print(f"  {key}: {value!r}")
    print()


def get_token(api_base: str, email: str, password: str) -> str:
    """Log in to the Bambu API and return the JWT accessToken.

    Handles verifyCode, tfaKey, tfa/mfa flows interactively.
    Raises RuntimeError if the token cannot be obtained.
    """
    session = requests.Session()

    print("\nLogging in to Bambu Lab API …")
    try:
        resp = _post(session, api_base + _LOGIN_PATH, {
            "account": email,
            "password": password,
        })
    except requests.RequestException as exc:
        raise RuntimeError(f"Login request failed: {exc}") from exc

    login_type = resp.get("loginType", "")
    tfa_key    = resp.get("tfaKey", "")
    token      = resp.get("accessToken") or resp.get("token")

    _dump_response("Initial login response", resp)

    # ── Already have token ────────────────────────────────────────────────
    if token:
        return token

    # ── verifyCode: Bambu emails a code automatically ─────────────────────
    if login_type == "verifyCode" or (not token and not tfa_key):
        print("\nBambu Lab has sent a verification code to your email.")
        print("Check your inbox (and spam folder) for an email from Bambu Lab.")
        code = input("Enter verification code: ").strip()
        # Try with password + code first, fall back to account + code only
        try:
            resp = _post(session, api_base + _LOGIN_PATH, {
                "account":  email,
                "password": password,
                "code":     code,
            })
            _dump_response("verifyCode response (with password)", resp)
            token = resp.get("accessToken") or resp.get("token")
            if not token:
                resp = _post(session, api_base + _LOGIN_PATH, {
                    "account": email,
                    "code":    code,
                })
                _dump_response("verifyCode response (without password)", resp)
                token = resp.get("accessToken") or resp.get("token")
        except requests.RequestException as exc:
            raise RuntimeError(f"Verification failed: {exc}") from exc

    # ── tfaKey: request email code then verify ────────────────────────────
    elif tfa_key and not token:
        print("\nEmail verification required (TFA).")
        try:
            _post(session, api_base + _TFA_SEND_PATH, {"tfaKey": tfa_key})
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to send TFA email: {exc}") from exc
        print("Check your inbox for a code from Bambu Lab.")
        code = input("Enter email code: ").strip()
        try:
            resp = _post(session, api_base + _TFA_VERIFY_PATH, {
                "tfaKey": tfa_key,
                "code":   code,
            })
        except requests.RequestException as exc:
            raise RuntimeError(f"TFA verification failed: {exc}") from exc
        token = resp.get("accessToken") or resp.get("token")

    # ── tfa/mfa: TOTP authenticator app ──────────────────────────────────
    elif login_type in ("tfa", "mfa"):
        print("\n2FA required. Open your authenticator app.")
        code = input("Enter TOTP code: ").strip()
        try:
            resp = _post(session, api_base + _MFA_PATH, {
                "account": email,
                "code":    code,
            })
        except requests.RequestException as exc:
            raise RuntimeError(f"2FA failed: {exc}") from exc
        token = resp.get("accessToken") or resp.get("token")

    if not token:
        raise RuntimeError(
            f"No accessToken in response. Keys received: {list(resp.keys())}"
        )

    return token


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Bambu Lab MQTT JWT token via the Bambu API"
    )
    parser.add_argument(
        "--output-file", metavar="PATH",
        help="Write token to this file instead of stdout"
    )
    args = parser.parse_args()

    print("\nBambu Lab Cloud Token Extractor")
    print("=" * 38)
    print("Calls the same API used by Bambu Studio to get the JWT")
    print("access token required for MQTT connection.\n")

    print("Select your region:")
    print("  1) Global (US / EU / rest of world)")
    print("  2) China")
    choice = input("Enter 1 or 2 [1]: ").strip() or "1"
    region = "cn" if choice == "2" else "us"
    api_base = _API_BASES[region]

    email    = input("\nBambu Lab account email: ").strip()
    password = getpass.getpass("Password: ")

    try:
        token = get_token(api_base, email, password)
    except RuntimeError as exc:
        print(f"\nFailed: {exc}")
        sys.exit(1)

    if args.output_file:
        with open(args.output_file, "w") as fh:
            fh.write(token)
        print(f"\nToken written to {args.output_file}")
    else:
        print("\n" + "=" * 60)
        print("SUCCESS — Your Bambu Lab Cloud Token:")
        print("=" * 60)
        print(token)
        print("=" * 60)
        print("\nCopy the token above into:")
        print("  BambuHelper web portal → Connection → Cloud Token")
        print("\nToken is valid for ~3 months. Re-run this script to refresh.\n")


if __name__ == "__main__":
    main()
