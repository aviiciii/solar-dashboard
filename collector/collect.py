"""
One-shot collector for the Polycab/Solarman solar monitoring API.

Run once per invocation (cron handles the "every 5 minutes" part - see README).
On each run it:
  1. Checks whether we're in a backoff cooldown after repeated recent failures, and
     skips (without hitting the network) if so.
  2. Backfills any missing days since the last successful poll (or since
     BACKFILL_START_DATE on a fresh/empty database), using the portal's own
     historical intraday-power endpoint. Backfilled rows only carry power (W) and
     timestamp - the finer AC/DC/temperature fields are only ever available live.
  3. Polls the current live reading and inserts it.

Every insert is `INSERT OR IGNORE` keyed on `timestamp`, so re-running this script
(including re-running backfill for a day that's already partially populated) is
always safe and never duplicates rows.
"""

import base64
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")

BASE_URL = "https://pv.polycabmonitoring.com/dist/server/api/CodeIgniter/index.php/Senergytec/web/v2/Inverterapi"

# Hardcoded in the frontend JS bundle (umi.*.js) - not session-specific, see recon/notes.md.
_SIGN_KEY1 = "05469137076236813460585715952089"
_SIGN_KEY2 = "5161557162012237"

STATUS_BY_COLOR = {"Green": "normal", "yellow": "standby", "red": "abnormal", "gray": "offline"}


def _num(value) -> float | None:
    """The API uses the literal string "-" (and sometimes "") as a placeholder for
    unavailable readings, e.g. when the inverter has gone idle/offline - never a
    numeric value in that case. Coerce anything non-numeric to None rather than
    silently storing a string into a REAL column."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AuthError(Exception):
    """Token missing/expired/rejected. Retrying immediately won't help - back off."""


class NetworkError(Exception):
    """Connection/timeout/HTTP error - transient, our side or the Polycab server's."""


class SchemaError(Exception):
    """Got a 200 response but the JSON didn't look like what we expected."""


def _sign(params: dict) -> str:
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


def _call(endpoint: str, params: dict, token: str) -> dict:
    body = dict(params)
    body["sign"] = _sign(params)
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


def to_utc_iso(local_dt: datetime) -> str:
    return local_dt.replace(tzinfo=IST).astimezone(ZoneInfo("UTC")).isoformat()


def fetch_live_reading(goods_id: str, member_auto_id: str, token: str) -> dict:
    detail = _call("InverterDetailInfoNewone", {"GoodsID": goods_id}, token)
    try:
        ac = detail["ACDCInfo"]
        data_time_str = detail["DataTime"]
    except KeyError as e:
        raise SchemaError(f"InverterDetailInfoNewone: missing expected field {e}") from e

    local_dt = datetime.strptime(data_time_str, "%Y-%m-%d %H:%M:%S")
    timestamp = to_utc_iso(local_dt)

    status = None
    try:
        group_list = _call("GroupList", {"MemberAutoID": member_auto_id, "inputValue": ""}, token)
        inv_status = group_list["AllGroupList"][0]["InverterStatus"]
        active_color = next((c for c, n in inv_status.items() if n), None)
        status = STATUS_BY_COLOR.get(active_color)
    except (AuthError, NetworkError):
        raise  # these matter - propagate
    except Exception as e:  # noqa: BLE001 - status is best-effort, never fail the whole poll over it
        logging.warning("could not determine status from GroupList: %s", e)

    def first(arr):
        return _num(arr[0]) if arr else None

    daily_yield = _num(detail.get("EToday"))
    total_yield = _num(detail.get("ETotal"))

    return {
        "timestamp": timestamp,
        "pv_power_w": first(ac.get("Pac")),
        "daily_yield_kwh": daily_yield / 1000 if daily_yield is not None else None,
        "total_yield_kwh": total_yield / 1000 if total_yield is not None else None,
        "ac_voltage": first(ac.get("Vac")),
        "ac_current": first(ac.get("Iac")),
        "ac_frequency": first(ac.get("Fac")),
        "temperature_c": _num(detail.get("Tntc")),
        "status": status,
        "source": "live",
        "raw_json": json.dumps(detail),
    }


def fetch_backfill_day(date_str: str, member_auto_id: str, token: str) -> list[dict]:
    """date_str is a local (IST) calendar date 'YYYY-MM-DD'. Returns rows with only
    pv_power_w populated - the day-curve endpoint only ever gives instantaneous power,
    never the finer electrical detail or the true cumulative yield at each instant."""
    result = _call("getAllPacDay_v1", {"MemberAutoID": member_auto_id, "date": date_str}, token)
    rows = []
    for point in result.get("data", []):
        pac_kw = _num(point.get("pac"))
        if pac_kw is None:
            continue
        local_dt = datetime.strptime(f"{date_str} {point['inTime']}", "%Y-%m-%d %H:%M:%S")
        rows.append({
            "timestamp": to_utc_iso(local_dt),
            "pv_power_w": pac_kw * 1000,
            "daily_yield_kwh": None,
            "total_yield_kwh": None,
            "ac_voltage": None,
            "ac_current": None,
            "ac_frequency": None,
            "temperature_c": None,
            "status": None,
            "source": "backfill",
            "raw_json": json.dumps(point),
        })
    return rows


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur = conn.executemany(
        """INSERT OR IGNORE INTO readings
           (timestamp, pv_power_w, daily_yield_kwh, total_yield_kwh, ac_voltage,
            ac_current, ac_frequency, temperature_c, status, source, raw_json)
           VALUES (:timestamp, :pv_power_w, :daily_yield_kwh, :total_yield_kwh, :ac_voltage,
                   :ac_current, :ac_frequency, :temperature_c, :status, :source, :raw_json)""",
        rows,
    )
    conn.commit()
    return cur.rowcount


def dates_needing_backfill(conn: sqlite3.Connection, backfill_start: date | None, stale_minutes: int,
                            max_days: int) -> list[str]:
    today_local = datetime.now(IST).date()

    row = conn.execute("SELECT MAX(timestamp) FROM readings").fetchone()
    last_ts_utc = datetime.fromisoformat(row[0]) if row and row[0] else None
    last_local_date = last_ts_utc.astimezone(IST).date() if last_ts_utc else None

    if last_local_date is None:
        start = backfill_start or today_local
        gap_days = _daterange(start, today_local - timedelta(days=1))
    else:
        gap_days = _daterange(last_local_date + timedelta(days=1), today_local - timedelta(days=1))

    needs_today = (
        last_local_date is None
        or last_local_date < today_local
        or (datetime.now(ZoneInfo("UTC")) - last_ts_utc) > timedelta(minutes=stale_minutes)
    )

    days = gap_days + ([today_local.isoformat()] if needs_today else [])
    if len(days) > max_days:
        logging.warning("backfill would cover %d days, capping at %d this run (rest will "
                         "catch up on subsequent runs)", len(days), max_days)
        days = days[:max_days]
    return days


def _daterange(start: date, end: date) -> list[str]:
    if start > end:
        return []
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def load_backoff_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"consecutive_failures": 0, "last_attempt": None}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"consecutive_failures": 0, "last_attempt": None}


def save_backoff_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state))


def backoff_minutes_for(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 0
    return min(5 * (2 ** (consecutive_failures - 1)), 60)


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stdout))


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    db_path = Path(os.environ.get("DB_PATH", PROJECT_ROOT / "data" / "readings.db"))
    log_path = Path(os.environ.get("LOG_PATH", PROJECT_ROOT / "data" / "collector.log"))
    state_path = Path(os.environ.get("STATE_PATH", PROJECT_ROOT / "data" / "collector_state.json"))
    stale_minutes = int(os.environ.get("STALE_MINUTES", "20"))
    max_backfill_days = int(os.environ.get("MAX_BACKFILL_DAYS_PER_RUN", "60"))
    backfill_start_str = os.environ.get("BACKFILL_START_DATE", "").strip()
    backfill_start = date.fromisoformat(backfill_start_str) if backfill_start_str else None

    setup_logging(log_path)

    token = os.environ.get("POLYCAB_TOKEN", "").strip()
    goods_id = os.environ.get("GOODS_ID", "").strip()
    member_auto_id = os.environ.get("MEMBER_AUTO_ID", "").strip()
    if not (token and goods_id and member_auto_id):
        logging.error("missing required .env vars: POLYCAB_TOKEN / GOODS_ID / MEMBER_AUTO_ID")
        return 1

    state = load_backoff_state(state_path)
    if state["consecutive_failures"] > 0 and state["last_attempt"]:
        cooldown = backoff_minutes_for(state["consecutive_failures"])
        elapsed = datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(state["last_attempt"])
        if elapsed < timedelta(minutes=cooldown):
            logging.warning("in backoff after %d consecutive failures, skipping this cycle "
                             "(retry in %.0f min)", state["consecutive_failures"],
                             (timedelta(minutes=cooldown) - elapsed).total_seconds() / 60)
            return 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript((PROJECT_ROOT / "db" / "schema.sql").read_text())

    state["last_attempt"] = datetime.now(ZoneInfo("UTC")).isoformat()

    try:
        for day in dates_needing_backfill(conn, backfill_start, stale_minutes, max_backfill_days):
            rows = fetch_backfill_day(day, member_auto_id, token)
            inserted = insert_rows(conn, rows)
            logging.info("backfill %s: %d rows inserted (%d returned)", day, inserted, len(rows))

        live_row = fetch_live_reading(goods_id, member_auto_id, token)
        inserted = insert_rows(conn, [live_row])
        logging.info(
            "live poll ok: %s power=%sW today=%skWh total=%skWh status=%s (%s)",
            live_row["timestamp"], live_row["pv_power_w"], live_row["daily_yield_kwh"],
            live_row["total_yield_kwh"], live_row["status"], "inserted" if inserted else "already had it",
        )
    except AuthError as e:
        logging.error("AUTH FAILURE: %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(state_path, state)
        return 1
    except NetworkError as e:
        logging.error("NETWORK FAILURE: %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(state_path, state)
        return 1
    except SchemaError as e:
        logging.error("SCHEMA FAILURE (API response shape changed?): %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(state_path, state)
        return 1
    finally:
        conn.close()

    state["consecutive_failures"] = 0
    save_backoff_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
