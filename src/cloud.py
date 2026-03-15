"""Bambu Lab Cloud helpers: JWT-based user ID extraction and API fallback.

Ported from bambu_cloud.cpp (Keralots/BambuHelper).
Handles region-aware broker selection and token-to-user-ID resolution.

Security: bambu_token is NEVER logged. (SEC-01)
"""

import base64
import json
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Region-aware MQTT broker endpoints (NET-07)
MQTT_BROKERS: dict[str, str] = {
    "us": "us.mqtt.bambulab.com",
    "eu": "us.mqtt.bambulab.com",   # EU accounts use US endpoint
    "cn": "cn.mqtt.bambulab.com",
}

# Region-aware REST API bases
_API_BASES: dict[str, str] = {
    "us": "https://api.bambulab.com",
    "eu": "https://api.bambulab.com",
    "cn": "https://api.bambulab.cn",
}

# HTTP headers that mimic OrcaSlicer (same as original C++)
_CLIENT_HEADERS = {
    "User-Agent": "bambu_network_agent/01.09.05.01",
    "X-BBL-Client-Name": "OrcaSlicer",
    "X-BBL-Client-Version": "01.09.05.51",
    "Content-Type": "application/json",
}


def get_broker(region: str) -> str:
    """Return the MQTT broker hostname for *region* (NET-07).

    >>> get_broker("eu")
    'us.mqtt.bambulab.com'
    >>> get_broker("cn")
    'cn.mqtt.bambulab.com'
    """
    return MQTT_BROKERS.get(region, MQTT_BROKERS["us"])


def extract_user_id_from_jwt(token: str) -> Optional[str]:
    """Decode JWT payload and return 'u_{uid}' string, or None on failure.

    JWT format: header.payload.signature (base64url encoded).
    Looks for uid, sub, or user_id fields in the payload JSON.
    Does NOT verify the signature — we trust the token from the user. (NET-06)

    The token value is never logged. (SEC-01)
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            logger.debug("Token does not look like a JWT (expected 3 parts)")
            return None

        # base64url → base64 padding
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_b64 = payload_b64.replace("-", "+").replace("_", "/")

        payload_bytes = base64.b64decode(payload_b64)
        payload: dict = json.loads(payload_bytes)

        uid = payload.get("uid") or payload.get("sub") or payload.get("user_id")
        if not uid:
            logger.debug("JWT payload has no uid/sub/user_id field")
            return None

        user_id = f"u_{uid}"
        logger.debug("Extracted cloud user_id from JWT: %s", user_id)
        return user_id

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to decode JWT payload: %s", exc)
        return None


def fetch_user_id_from_api(token: str, region: str, timeout: int = 10) -> str:
    """Fetch user ID from Bambu Lab profile API as fallback.

    Calls GET /v1/user-service/my/profile with Bearer auth.
    Returns 'u_{uid}' string.

    The token value is never logged. (SEC-01, SEC-07)
    """
    api_base = _API_BASES.get(region, _API_BASES["us"])
    url = f"{api_base}/v1/user-service/my/profile"

    headers = {
        **_CLIENT_HEADERS,
        "Authorization": f"Bearer {token}",  # token not logged
    }

    logger.info("Fetching cloud user ID from API (region=%s)", region)
    resp = requests.get(url, headers=headers, verify=True, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()
    uid = data.get("uidStr") or str(data.get("uid", ""))
    if not uid:
        raise ValueError("API response contained no uid or uidStr field")

    user_id = f"u_{uid}"
    logger.info("Fetched cloud user_id from API: %s", user_id)
    return user_id


def resolve_user_id(token: str, region: str) -> str:
    """Resolve cloud MQTT username from token.

    Tries JWT decode first; falls back to Bambu API. (NET-06)
    Raises ValueError if both methods fail.

    The token value is never logged. (SEC-01)
    """
    user_id = extract_user_id_from_jwt(token)
    if user_id:
        return user_id

    logger.info("JWT decode did not yield user ID — trying API fallback")
    try:
        return fetch_user_id_from_api(token, region)
    except (requests.RequestException, ValueError) as exc:
        raise ValueError(
            f"Could not resolve Bambu cloud user ID via JWT or API: {exc}"
        ) from exc
