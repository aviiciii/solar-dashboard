"""
Shared client for the pv.polycabmonitoring.com JSON API. Used by both
collector/collect.py and alerts/daily_alert.py - see recon/notes.md for how
the signing scheme was reverse-engineered.
"""

import base64

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_URL = "https://pv.polycabmonitoring.com/dist/server/api/CodeIgniter/index.php/Senergytec/web/v2/Inverterapi"

# Hardcoded in the frontend JS bundle (umi.*.js) - not session-specific.
_SIGN_KEY1 = "05469137076236813460585715952089"
_SIGN_KEY2 = "5161557162012237"


class AuthError(Exception):
    """Token missing/expired/rejected. Retrying immediately won't help."""


class NetworkError(Exception):
    """Connection/timeout/HTTP error - transient, our side or the Polycab server's."""


class SchemaError(Exception):
    """Got a 200 response but the JSON didn't look like what we expected."""


def num(value) -> float | None:
    """The API uses the literal string "-" (and sometimes "") as a placeholder for
    unavailable readings. Coerce anything non-numeric to None rather than crashing
    or silently storing a string where a number is expected."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sign(params: dict) -> str:
    filtered = {k: v for k, v in params.items() if v not in ("", None) and not isinstance(v, bool)}
    parts = []
    for k in sorted(filtered.keys()):
        v = filtered[k]
        v_str = "Array" if isinstance(v, list) else str(v)
        parts.append(f"{k}={v_str}")
    canonical = "&".join(parts) + "&" + _SIGN_KEY1
    cipher = AES.new(_SIGN_KEY1.encode(), AES.MODE_CBC, _SIGN_KEY2.encode())
    ct = cipher.encrypt(pad(canonical.encode(), AES.block_size))
    return base64.b64encode(ct).decode()


def call(endpoint: str, params: dict, token: str) -> dict:
    body = dict(params)
    body["sign"] = sign(params)
    try:
        resp = requests.post(
            f"{BASE_URL}/{endpoint}",
            json=body,
            headers={
                "authorization": token,
                "content-type": "application/json",
                "accept": "application/json, text/plain, */*",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        raise NetworkError(f"{endpoint}: request failed: {e}") from e

    if resp.status_code >= 400:
        try:
            err_body = resp.json()
        except ValueError:
            err_body = None
        msg = str(err_body.get("message", "")) if isinstance(err_body, dict) else ""
        looks_like_auth = resp.status_code in (401, 403) or any(
            kw in msg.lower() for kw in ("token", "auth", "login", "permission", "denied")
        )
        detail = msg or resp.text[:200]
        if looks_like_auth:
            raise AuthError(f"{endpoint}: HTTP {resp.status_code}: {detail} - token likely "
                             f"expired/invalid, re-extract it from the browser (see .env.example)")
        raise NetworkError(f"{endpoint}: HTTP {resp.status_code}: {detail}")

    try:
        data = resp.json()
    except ValueError as e:
        raise SchemaError(f"{endpoint}: response wasn't valid JSON: {e}") from e

    if isinstance(data, dict) and data.get("status") is False:
        msg = str(data.get("message", ""))
        if "token" in msg.lower() or "auth" in msg.lower() or "login" in msg.lower():
            raise AuthError(f"{endpoint}: server rejected request: {msg}")
        raise SchemaError(f"{endpoint}: server rejected request: {msg}")

    return data
