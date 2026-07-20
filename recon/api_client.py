"""
Working proof-of-concept client for the pv.polycabmonitoring.com JSON API
(reverse-engineered from the Vue/umi SPA — see notes.md for how).

Usage:
    export POLYCAB_TOKEN="<value of localStorage.getItem('token') from a logged-in session>"
    python3 api_client.py

The token is a long-lived JWT (~360 days, see notes.md) so this does not
implement a login flow — just extract it once from the browser and reuse it.
"""

import base64
import json
import os

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_URL = "https://pv.polycabmonitoring.com/dist/server/api/CodeIgniter/index.php/Senergytec/web/v2/Inverterapi"

# Hardcoded in the frontend JS bundle (umi.*.js), not session-specific.
_SIGN_KEY1 = "05469137076236813460585715952089"
_SIGN_KEY2 = "5161557162012237"


def sign(params: dict) -> str:
    """Reproduces ct()/U$() from the frontend bundle."""
    filtered = {
        k: v for k, v in params.items()
        if v not in ("", None) and not isinstance(v, bool)
    }
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
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    token = os.environ["POLYCAB_TOKEN"]
    goods_id = "2620-119401326P"  # our inverter serial number

    detail = call("InverterDetailInfoNewone", {"GoodsID": goods_id}, token)
    print(json.dumps(detail, indent=2))
