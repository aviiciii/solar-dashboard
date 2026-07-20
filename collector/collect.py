"""
One-shot collector for the Polycab/Solarman solar monitoring API.

Run once per invocation (cron handles the "every 5 minutes" part - see README).
On each run it:
  1. Checks whether we're in a backoff cooldown after repeated recent failures, and
     skips (without hitting the network) if so.
  2. Backfills any missing days since the last successful poll (or since
     BACKFILL_START_DATE on a fresh/empty database), using the portal's own
     historical intraday-power endpoint. Backfilled rows carry power (W) and
     timestamp for every interval, plus the day's authoritative total kWh (from
     getAllPacMonth) on the day's last row - the finer AC/DC/temperature/status
     fields are only ever available live.
  3. Polls the current live reading and inserts it.

Every insert is `INSERT OR IGNORE` keyed on `timestamp`, so re-running this script
(including re-running backfill for a day that's already partially populated) is
always safe and never duplicates rows.
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from polycab_client import call, num, MonthlyYields, AuthError, NetworkError, SchemaError  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

STATUS_BY_COLOR = {"Green": "normal", "yellow": "standby", "red": "abnormal", "gray": "offline"}


def to_utc_iso(local_dt: datetime) -> str:
    return local_dt.replace(tzinfo=IST).astimezone(ZoneInfo("UTC")).isoformat()


def fetch_live_reading(goods_id: str, member_auto_id: str, token: str) -> dict:
    detail = call("InverterDetailInfoNewone", {"GoodsID": goods_id}, token)
    try:
        ac = detail["ACDCInfo"]
        data_time_str = detail["DataTime"]
    except KeyError as e:
        raise SchemaError(f"InverterDetailInfoNewone: missing expected field {e}") from e

    local_dt = datetime.strptime(data_time_str, "%Y-%m-%d %H:%M:%S")
    timestamp = to_utc_iso(local_dt)

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

    daily_yield = num(detail.get("EToday"))
    total_yield = num(detail.get("ETotal"))

    return {
        "timestamp": timestamp,
        "pv_power_w": first(ac.get("Pac")),
        "daily_yield_kwh": daily_yield / 1000 if daily_yield is not None else None,
        "total_yield_kwh": total_yield / 1000 if total_yield is not None else None,
        "ac_voltage": first(ac.get("Vac")),
        "ac_current": first(ac.get("Iac")),
        "ac_frequency": first(ac.get("Fac")),
        "temperature_c": num(detail.get("Tntc")),
        "status": status,
        "source": "live",
        "raw_json": json.dumps(detail),
    }


def fetch_backfill_day(date_str: str, member_auto_id: str, token: str, monthly_yields: MonthlyYields) -> list[dict]:
    """date_str is a local (IST) calendar date 'YYYY-MM-DD'. The day-curve endpoint
    (getAllPacDay_v1) only ever gives instantaneous power, never the finer electrical
    detail or the true cumulative yield at each instant - so those stay None on every
    row here. But the day's *final* cumulative yield is knowable (getAllPacMonth's
    authoritative daily total), so it's set on the last/latest row of the day - the
    same place a live poll's last-of-the-day reading would put it. Without this, a day
    covered only by backfill (e.g. one from before the collector was actually running)
    would have no daily kWh total anywhere in the DB at all."""
    result = call("getAllPacDay_v1", {"MemberAutoID": member_auto_id, "date": date_str}, token)
    rows = []
    for point in result.get("data", []):
        pac_kw = num(point.get("pac"))
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

    if rows:
        day_total = monthly_yields.yield_for(date.fromisoformat(date_str))
        if day_total is not None:
            rows[-1]["daily_yield_kwh"] = day_total  # rows are chronological; last = latest

    return rows


_READING_COLUMNS = [
    "timestamp", "pv_power_w", "daily_yield_kwh", "total_yield_kwh", "ac_voltage",
    "ac_current", "ac_frequency", "temperature_c", "status", "source", "raw_json",
]


def insert_rows(conn, rows: list[dict]) -> int:
    """libsql (unlike sqlite3) only supports positional `?` params, not named `:key`
    ones - so rows are converted to tuples in _READING_COLUMNS order here."""
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _READING_COLUMNS)
    values = [tuple(row[col] for col in _READING_COLUMNS) for row in rows]
    cur = conn.executemany(
        f"INSERT OR IGNORE INTO readings ({', '.join(_READING_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cur.rowcount


def dates_needing_backfill(conn, backfill_start: date | None, stale_minutes: int,
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


def load_backoff_state(conn) -> dict:
    """Backoff state lives in the DB, not a local file - the collector runs on
    ephemeral compute (GitHub Actions) with no disk that persists between runs."""
    row = conn.execute("SELECT consecutive_failures, last_attempt FROM collector_state WHERE id = 1").fetchone()
    if row is None:
        return {"consecutive_failures": 0, "last_attempt": None}
    return {"consecutive_failures": row[0], "last_attempt": row[1]}


def save_backoff_state(conn, state: dict) -> None:
    conn.execute(
        """INSERT INTO collector_state (id, consecutive_failures, last_attempt) VALUES (1, ?, ?)
           ON CONFLICT (id) DO UPDATE SET consecutive_failures = excluded.consecutive_failures,
                                           last_attempt = excluded.last_attempt""",
        (state["consecutive_failures"], state["last_attempt"]),
    )
    conn.commit()


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

    log_path = Path(os.environ.get("LOG_PATH", PROJECT_ROOT / "data" / "collector.log"))
    stale_minutes = int(os.environ.get("STALE_MINUTES", "20"))
    max_backfill_days = int(os.environ.get("MAX_BACKFILL_DAYS_PER_RUN", "60"))
    backfill_start_str = os.environ.get("BACKFILL_START_DATE", "").strip()
    backfill_start = date.fromisoformat(backfill_start_str) if backfill_start_str else None

    setup_logging(log_path)

    token = os.environ.get("POLYCAB_TOKEN", "").strip()
    goods_id = os.environ.get("GOODS_ID", "").strip()
    member_auto_id = os.environ.get("MEMBER_AUTO_ID", "").strip()
    turso_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    turso_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not (token and goods_id and member_auto_id and turso_url and turso_token):
        logging.error("missing required .env vars: POLYCAB_TOKEN / GOODS_ID / MEMBER_AUTO_ID / "
                       "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN")
        return 1

    conn = libsql.connect(turso_url, auth_token=turso_token)
    conn.executescript((PROJECT_ROOT / "db" / "schema.sql").read_text())

    state = load_backoff_state(conn)
    if state["consecutive_failures"] > 0 and state["last_attempt"]:
        cooldown = backoff_minutes_for(state["consecutive_failures"])
        elapsed = datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(state["last_attempt"])
        if elapsed < timedelta(minutes=cooldown):
            logging.warning("in backoff after %d consecutive failures, skipping this cycle "
                             "(retry in %.0f min)", state["consecutive_failures"],
                             (timedelta(minutes=cooldown) - elapsed).total_seconds() / 60)
            return 0

    state["last_attempt"] = datetime.now(ZoneInfo("UTC")).isoformat()

    try:
        monthly_yields = MonthlyYields(member_auto_id, token)
        for day in dates_needing_backfill(conn, backfill_start, stale_minutes, max_backfill_days):
            rows = fetch_backfill_day(day, member_auto_id, token, monthly_yields)
            inserted = insert_rows(conn, rows)
            logging.info("backfill %s: %d rows inserted (%d returned)", day, inserted, len(rows))

        live_row = fetch_live_reading(goods_id, member_auto_id, token)
        inserted = insert_rows(conn, [live_row])
        logging.info(
            "live poll ok: %s power=%sW today=%skWh total=%skWh status=%s (%s)",
            live_row["timestamp"], live_row["pv_power_w"], live_row["daily_yield_kwh"],
            live_row["total_yield_kwh"], live_row["status"], "inserted" if inserted else "already had it",
        )
        state["consecutive_failures"] = 0
        save_backoff_state(conn, state)
        return 0
    except AuthError as e:
        logging.error("AUTH FAILURE: %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(conn, state)
        return 1
    except NetworkError as e:
        logging.error("NETWORK FAILURE: %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(conn, state)
        return 1
    except SchemaError as e:
        logging.error("SCHEMA FAILURE (API response shape changed?): %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(conn, state)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
