"""
Shared client for the pv.polycabmonitoring.com JSON API. Used by both
collector/collect.py and alerts/daily_alert.py - see recon/notes.md for how
the signing scheme was reverse-engineered.
"""

import base64
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_URL = "https://pv.polycabmonitoring.com/dist/server/api/CodeIgniter/index.php/Senergytec/web/v2/Inverterapi"

# Hardcoded in the frontend JS bundle (umi.*.js) - not session-specific.
_SIGN_KEY1 = "05469137076236813460585715952089"
_SIGN_KEY2 = "5161557162012237"

# InverterDetailInfoNewone's DataTime field is the device's own RTC clock, reported in
# China Standard Time (UTC+8) regardless of the plant's actual location/configured
# timezone (+5:30) - confirmed empirically: a live DataTime matched real-world UTC+8
# clock time to within ~2 minutes, but was 2.5h ahead of real IST time. getAllPacDay_v1's
# `inTime` (used for backfill) is genuinely IST - it's parameterized by date/account
# timezone server-side, not the raw device clock, and lines up correctly with real time.
DEVICE_CLOCK_TZ = ZoneInfo("Asia/Shanghai")

STATUS_BY_COLOR = {"Green": "normal", "yellow": "standby", "red": "abnormal", "gray": "offline"}


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


class MonthlyYields:
    """Caches getAllPacMonth responses per (year, month) so looking up several days'
    authoritative daily kWh totals that fall in the same month only fetches it once.
    Used by both collector/collect.py (to backfill the daily total onto a day it only
    has instantaneous power for) and alerts/daily_alert.py (7-day trailing average)."""

    def __init__(self, member_auto_id: str, token: str):
        self._member_auto_id = member_auto_id
        self._token = token
        self._cache: dict[tuple[int, int], dict[str, float | None]] = {}

    def _month(self, year: int, month: int) -> dict[str, float | None]:
        key = (year, month)
        if key not in self._cache:
            result = call("getAllPacMonth", {"MemberAutoID": self._member_auto_id,
                                              "date": f"{year:04d}-{month:02d}"}, self._token)
            self._cache[key] = {p["inDate"]: num(p.get("pac")) for p in result.get("data", [])}
        return self._cache[key]

    def yield_for(self, d: date) -> float | None:
        return self._month(d.year, d.month).get(d.isoformat())


def fetch_live(goods_id: str, member_auto_id: str, token: str) -> dict:
    """The inverter's current reading (InverterDetailInfoNewone) plus health status
    (GroupList), parsed - shared by the collector's live poll and the dashboard's live
    snapshot. Energy totals are converted Wh -> kWh; `reported_at` is the device's DataTime
    as an aware datetime (see DEVICE_CLOCK_TZ); `detail` is the raw response."""
    detail = call("InverterDetailInfoNewone", {"GoodsID": goods_id}, token)
    try:
        ac = detail["ACDCInfo"]
        data_time_str = detail["DataTime"]
    except KeyError as e:
        raise SchemaError(f"InverterDetailInfoNewone: missing expected field {e}") from e
    reported_at = datetime.strptime(data_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=DEVICE_CLOCK_TZ)

    status = None
    try:
        group_list = call("GroupList", {"MemberAutoID": member_auto_id, "inputValue": ""}, token)
        inv_status = group_list["AllGroupList"][0]["InverterStatus"]
        active_color = next((c for c, n in inv_status.items() if n), None)
        status = STATUS_BY_COLOR.get(active_color)
    except (AuthError, NetworkError):
        raise  # these matter - propagate
    except Exception as e:  # noqa: BLE001 - status is best-effort, never fail the whole poll over it
        logging.warning("could not determine status from GroupList: %s", e)

    def first(arr):
        return num(arr[0]) if arr else None

    daily_wh = num(detail.get("EToday"))
    total_wh = num(detail.get("ETotal"))
    return {
        "reported_at": reported_at,
        "pv_power_w": first(ac.get("Pac")),
        "daily_yield_kwh": daily_wh / 1000 if daily_wh is not None else None,
        "total_yield_kwh": total_wh / 1000 if total_wh is not None else None,
        "ac_voltage": first(ac.get("Vac")),
        "ac_current": first(ac.get("Iac")),
        "ac_frequency": first(ac.get("Fac")),
        "temperature_c": num(detail.get("Tntc")),
        "status": status,
        "firmware": {k: detail.get(f"{k}Version") for k in ("MDSP", "SDSP", "CSB")},
        "detail": detail,
    }
