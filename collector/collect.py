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

Exit codes: 0 = success/backoff-skip, 1 = transient network failure (self-heals via
the backoff+retry below, doesn't need a human), 2 = auth failure, 3 = API schema
change - the last two need a human, so they also send an ntfy alert (once per new
failure streak, not every cycle) and are treated as job failures by collect.yml's
loop (unlike exit 1, which the loop logs and continues past).
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from common import IST, UTC, cumulative_kwh, setup_logging  # noqa: E402
from polycab_client import (call, fetch_live, num, MonthlyYields, AuthError, NetworkError,  # noqa: E402
                            SchemaError)
from ntfy_client import send_ntfy  # noqa: E402


def alert_once_per_failure_streak(ntfy_topic: str, was_already_failing: bool, title: str, message: str) -> None:
    """Only notify on the *first* failure of a new streak - otherwise a stuck token
    would fire an alert every 5 minutes for hours. Never lets a notify failure mask
    the real error being reported."""
    if was_already_failing or not ntfy_topic:
        return
    try:
        send_ntfy(ntfy_topic, message, title=title)
    except Exception as e:  # noqa: BLE001 - reporting the failure must not itself crash the run
        logging.warning("could not send ntfy alert: %s", e)


def to_utc_iso(local_dt: datetime) -> str:
    return local_dt.replace(tzinfo=IST).astimezone(UTC).isoformat()


def fetch_live_reading(goods_id: str, member_auto_id: str, token: str) -> dict:
    live = fetch_live(goods_id, member_auto_id, token)
    return {
        "timestamp": live["reported_at"].astimezone(UTC).isoformat(),
        **{col: live[col] for col in ("pv_power_w", "daily_yield_kwh", "total_yield_kwh", "ac_voltage",
                                      "ac_current", "ac_frequency", "temperature_c", "status")},
        "source": "live",
        "raw_json": json.dumps(live["detail"]),
    }


def integrate_kwh(rows: list[dict]) -> float:
    """Total kWh under a chronological list of row dicts' pv_power_w - the fallback day
    total in fetch_backfill_day. Same integration as the dashboard's cumulative charts
    (common.cumulative_kwh)."""
    totals = cumulative_kwh([(datetime.fromisoformat(r["timestamp"]), r["pv_power_w"]) for r in rows])
    return totals[-1] if totals else 0.0


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
        # getAllPacMonth's total is derived from the device's own last-reported EToday
        # for that day - if the device was disconnected near end-of-day (e.g. the exact
        # outage this backfill is meant to fix), the portal never got a final EToday and
        # reports 0 indefinitely, even though the day-curve endpoint above clearly shows
        # real production. Fall back to integrating our own power curve rather than
        # trusting a portal total that contradicts the data we just fetched.
        has_production = any(row["pv_power_w"] for row in rows)
        if (day_total is None or day_total == 0) and has_production:
            day_total = integrate_kwh(rows)
        if day_total is not None:
            rows[-1]["daily_yield_kwh"] = day_total  # rows are chronological; last = latest

    return rows


_READING_COLUMNS = [
    "timestamp", "pv_power_w", "daily_yield_kwh", "total_yield_kwh", "ac_voltage",
    "ac_current", "ac_frequency", "temperature_c", "status", "source", "raw_json",
]


def insert_rows(conn, rows: list[dict], commit: bool = True) -> int:
    """libsql (unlike sqlite3) only supports positional `?` params, not named `:key`
    ones - so rows are converted to tuples in _READING_COLUMNS order here.

    commit=False (here and in record_daily_total / update_top_days) leaves the write in
    the caller's open transaction - the Admin tab's re-backfill uses it to make its
    delete + reinsert + totals + rankings a single all-or-nothing commit."""
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _READING_COLUMNS)
    values = [tuple(row[col] for col in _READING_COLUMNS) for row in rows]
    cur = conn.executemany(
        f"INSERT OR IGNORE INTO readings ({', '.join(_READING_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    if commit:
        conn.commit()
    return cur.rowcount


def record_daily_total(conn, date_str: str, kwh: float, overwrite: bool = False, commit: bool = True) -> bool:
    """Upserts one day's total into daily_totals and reports whether it actually
    changed - callers skip update_top_days() when it didn't (e.g. an idle overnight
    poll re-reporting the same EToday), which is most of the collector's read savings.

    Default mode only ever raises a day's total (`MAX` semantics, same as the old
    `MAX(daily_yield_kwh)` aggregation over `readings`), and the conditional DO UPDATE
    is atomic, so it's safe under collect.yml's deliberately-overlapping runs.
    `overwrite=True` is for the Admin tab's re-backfill, which can legitimately lower a
    day's total when replacing corrupted readings."""
    condition = "excluded.kwh IS NOT daily_totals.kwh" if overwrite else "excluded.kwh > daily_totals.kwh"
    cur = conn.execute(
        f"INSERT INTO daily_totals (date, kwh) VALUES (?, ?) "
        f"ON CONFLICT (date) DO UPDATE SET kwh = excluded.kwh WHERE {condition}",
        (date_str, kwh),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def update_top_days(conn, date_str: str, commit: bool = True) -> int:
    """Recomputes both the affected year's top-10 and the all-time top-10 whenever a
    day's total changed (a live poll or a backfill's day-total fix), then writes only the
    ranks whose (date, kwh) actually differ - returns how many rows were written. Full
    recompute rather than an incremental patch sidesteps edge cases like a backfill
    correcting an earlier day's total after it was already ranked; diffing the write
    avoids rewriting all 20 rows on every poll (that was ~95% of the DB's rows written,
    since today's total rises on nearly every daylight poll).

    Reads only daily_totals (one row/day, date-range and kwh-indexed), never
    `readings`: aggregating per-day totals out of `readings` needs a full-table scan,
    which on every poll burned ~400M Turso rows-read/month (see db/schema.sql)."""
    year = date.fromisoformat(date_str).year

    desired_yearly = conn.execute(
        "SELECT date, kwh FROM daily_totals WHERE date >= ? AND date < ? "
        "ORDER BY kwh DESC, date LIMIT 10",
        (f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
    ).fetchall()
    current_yearly = {r: (d, k) for r, d, k in conn.execute(
        "SELECT rank, date, kwh FROM top_days_yearly WHERE year = ?", (year,)).fetchall()}
    yearly_changes = [(year, rank, d, k) for rank, (d, k) in enumerate(desired_yearly, 1)
                      if current_yearly.get(rank) != (d, k)]
    if yearly_changes:
        conn.executemany("INSERT OR REPLACE INTO top_days_yearly (year, rank, date, kwh) VALUES (?, ?, ?, ?)",
                         yearly_changes)
    yearly_dropped = sum(1 for rank in current_yearly if rank > len(desired_yearly))
    if yearly_dropped:
        conn.execute("DELETE FROM top_days_yearly WHERE year = ? AND rank > ?", (year, len(desired_yearly)))

    desired_alltime = conn.execute(
        "SELECT date, kwh FROM daily_totals ORDER BY kwh DESC, date LIMIT 10",
    ).fetchall()
    current_alltime = {r: (d, k) for r, d, k in conn.execute(
        "SELECT rank, date, kwh FROM top_days_alltime").fetchall()}
    alltime_changes = [(rank, d, k) for rank, (d, k) in enumerate(desired_alltime, 1)
                       if current_alltime.get(rank) != (d, k)]
    if alltime_changes:
        conn.executemany("INSERT OR REPLACE INTO top_days_alltime (rank, date, kwh) VALUES (?, ?, ?)",
                         alltime_changes)
    alltime_dropped = sum(1 for rank in current_alltime if rank > len(desired_alltime))
    if alltime_dropped:
        conn.execute("DELETE FROM top_days_alltime WHERE rank > ?", (len(desired_alltime),))

    if commit:
        conn.commit()
    return len(yearly_changes) + yearly_dropped + len(alltime_changes) + alltime_dropped


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
        or (datetime.now(UTC) - last_ts_utc) > timedelta(minutes=stale_minutes)
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
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not (token and goods_id and member_auto_id and turso_url and turso_token):
        logging.error("missing required .env vars: POLYCAB_TOKEN / GOODS_ID / MEMBER_AUTO_ID / "
                       "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN")
        return 1

    # Schema is applied once per workflow job by db/apply_schema.py, not on every poll.
    conn = libsql.connect(turso_url, auth_token=turso_token)

    state = load_backoff_state(conn)
    if state["consecutive_failures"] > 0 and state["last_attempt"]:
        cooldown = backoff_minutes_for(state["consecutive_failures"])
        elapsed = datetime.now(UTC) - datetime.fromisoformat(state["last_attempt"])
        if elapsed < timedelta(minutes=cooldown):
            logging.warning("in backoff after %d consecutive failures, skipping this cycle "
                             "(retry in %.0f min)", state["consecutive_failures"],
                             (timedelta(minutes=cooldown) - elapsed).total_seconds() / 60)
            return 0

    state["last_attempt"] = datetime.now(UTC).isoformat()

    try:
        monthly_yields = MonthlyYields(member_auto_id, token)
        for day in dates_needing_backfill(conn, backfill_start, stale_minutes, max_backfill_days):
            rows = fetch_backfill_day(day, member_auto_id, token, monthly_yields)
            inserted = insert_rows(conn, rows)
            logging.info("backfill %s: %d rows inserted (%d returned)", day, inserted, len(rows))
            if rows and rows[-1]["daily_yield_kwh"] is not None:
                day_key = rows[-1]["timestamp"][:10]
                if record_daily_total(conn, day_key, rows[-1]["daily_yield_kwh"]):
                    update_top_days(conn, day_key)

        live_row = fetch_live_reading(goods_id, member_auto_id, token)
        inserted = insert_rows(conn, [live_row])
        logging.info(
            "live poll ok: %s power=%sW today=%skWh total=%skWh status=%s (%s)",
            live_row["timestamp"], live_row["pv_power_w"], live_row["daily_yield_kwh"],
            live_row["total_yield_kwh"], live_row["status"], "inserted" if inserted else "already had it",
        )
        if live_row["daily_yield_kwh"] is not None:
            day_key = live_row["timestamp"][:10]
            if record_daily_total(conn, day_key, live_row["daily_yield_kwh"]):
                update_top_days(conn, day_key)
        state["consecutive_failures"] = 0
        save_backoff_state(conn, state)
        return 0
    except AuthError as e:
        logging.error("AUTH FAILURE: %s", e)
        was_already_failing = state["consecutive_failures"] > 0
        state["consecutive_failures"] += 1
        save_backoff_state(conn, state)
        alert_once_per_failure_streak(
            ntfy_topic, was_already_failing, "Solar collector: AUTH FAILURE",
            f"{e}\n\nToken likely expired/invalid - re-extract it from the browser and "
            f"update the POLYCAB_TOKEN secret.",
        )
        return 2
    except NetworkError as e:
        logging.error("NETWORK FAILURE: %s", e)
        state["consecutive_failures"] += 1
        save_backoff_state(conn, state)
        return 1
    except SchemaError as e:
        logging.error("SCHEMA FAILURE (API response shape changed?): %s", e)
        was_already_failing = state["consecutive_failures"] > 0
        state["consecutive_failures"] += 1
        save_backoff_state(conn, state)
        alert_once_per_failure_streak(
            ntfy_topic, was_already_failing, "Solar collector: SCHEMA FAILURE",
            f"{e}\n\nThe Polycab API's response shape may have changed - needs investigation.",
        )
        return 3
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
