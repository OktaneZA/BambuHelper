"""Bambu Lab cloud token extractor.

Adapted from the Python helper tool in Keralots/BambuHelper (tools/get_token.py).
Uses curl_cffi to impersonate a browser and bypass Cloudflare, handling 2FA
(TOTP authenticator app or email verification code).

Usage:
    pip install curl_cffi
    python scripts/get_cloud_token.py

The extracted token can then be pasted into the BambuHelper web portal
(http://<pi-ip>:8080) under Connection → Cloud Token.

Tokens are valid for approximately 3 months.

Security: the token is printed ONCE to stdout only. Do not log it further.
"""

import json
import sys

import requests as cffi_requests

# Region selection
REGIONS = {
    "1": ("us", "https://api.bambulab.com"),
    "2": ("cn", "https://api.bambulab.cn"),
}

LOGIN_PATH    = "/v1/user-service/user/login"
TOTP_PATH     = "/v1/user-service/user/login/tfa/mfa"
EMAIL_SEND    = "/v1/user-service/user/login/tfa/email"
EMAIL_PATH    = "/v1/user-service/user/login/tfa/email/code"

# Headers that mimic OrcaSlicer (same as bambu_cloud.cpp)
_HEADERS = {
    "User-Agent": "bambu_network_agent/01.09.05.01",
    "X-BBL-Client-Name": "OrcaSlicer",
    "X-BBL-Client-Version": "01.09.05.51",
    "Content-Type": "application/json",
}


def _post(session, url: str, body: dict) -> dict:
    """POST JSON and return parsed response."""
    resp = session.post(url, json=body, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    print("\nBambu Lab Cloud Token Extractor")
    print("=" * 38)
    print("Tokens are valid for ~3 months.\n")

    # Region selection
    print("Select your region:")
    print("  1) Global (US / EU)")
    print("  2) China (CN)")
    region_choice = input("Enter 1 or 2 [1]: ").strip() or "1"
    region_key, api_base = REGIONS.get(region_choice, REGIONS["1"])

    email = input("\nBambu Lab account email: ").strip()
    import getpass
    password = getpass.getpass("Password: ")

    session = cffi_requests.Session()
    session.verify = True

    print("\nLogging in …")
    try:
        resp = _post(session, api_base + LOGIN_PATH, {
            "account": email,
            "password": password,
        })
    except Exception as exc:
        print(f"\nLogin request failed: {exc}")
        sys.exit(1)

    # Handle 2FA — check for tfaKey flow first (email verification via key)
    tfa_key = resp.get("tfaKey")
    login_type = resp.get("loginType")

    if tfa_key and not resp.get("accessToken"):
        # Bambu sent a tfaKey — request email code then verify
        print("\nEmail verification required (tfaKey flow).")
        try:
            _post(session, api_base + EMAIL_SEND, {"tfaKey": tfa_key})
        except Exception as exc:
            print(f"\nFailed to send verification email: {exc}")
            sys.exit(1)
        print("Check your email for a verification code.")
        email_code = input("Enter email code: ").strip()
        try:
            resp = _post(session, api_base + EMAIL_PATH, {
                "tfaKey": tfa_key,
                "code": email_code,
            })
        except Exception as exc:
            print(f"\nEmail code request failed: {exc}")
            sys.exit(1)

    elif login_type in ("tfa", "mfa"):
        print("\n2FA required (authenticator app).")
        totp_code = input("Enter TOTP code: ").strip()
        try:
            resp = _post(session, api_base + TOTP_PATH, {
                "account": email,
                "code": totp_code,
            })
        except Exception as exc:
            print(f"\n2FA request failed: {exc}")
            sys.exit(1)

    elif login_type == "email_code":
        print("\nEmail verification required.")
        print("Check your email for a verification code.")
        email_code = input("Enter email code: ").strip()
        try:
            resp = _post(session, api_base + EMAIL_PATH, {
                "account": email,
                "code": email_code,
            })
        except Exception as exc:
            print(f"\nEmail code request failed: {exc}")
            sys.exit(1)

    # Extract token
    token = resp.get("accessToken") or resp.get("token") or resp.get("access_token")
    if not token:
        print("\nFailed to extract token from response.")
        print("Response keys:", list(resp.keys()))
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS — Your Bambu Lab Cloud Token:")
    print("=" * 60)
    print(token)
    print("=" * 60)
    print("\nCopy the token above and paste it into:")
    print(f"  BambuHelper web portal → Connection → Cloud Token")
    print(f"\nRegion: {region_key}")
    print("Token is valid for ~3 months. Run this script again to refresh.\n")


if __name__ == "__main__":
    main()
