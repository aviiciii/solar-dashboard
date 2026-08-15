"""
Streamlit dashboard. Run manually (`uv run streamlit run dashboard/app.py`) and stop when
done - never runs 24/7. Read-only against Turso; live snapshot comes from the Polycab API
directly. See AGENTS.md for the full design rationale.
"""

import base64
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import lesley
import libsql
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from polycab_client import AuthError, NetworkError, SchemaError, call, num  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
# InverterDetailInfoNewone's DataTime is the device's own RTC clock, reported in China
# Standard Time (UTC+8) regardless of the plant's actual location - confirmed empirically
# against real clock time. See the matching comment in collector/collect.py.
DEVICE_CLOCK_TZ = ZoneInfo("Asia/Shanghai")

POLYCAB_TOKEN = os.environ.get("POLYCAB_TOKEN", "").strip()
GOODS_ID = os.environ.get("GOODS_ID", "").strip()
MEMBER_AUTO_ID = os.environ.get("MEMBER_AUTO_ID", "").strip()
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
WEATHER_LAT = os.environ.get("WEATHER_LAT", "").strip()
WEATHER_LON = os.environ.get("WEATHER_LON", "").strip()

# Static specs - not available from the Polycab API.
PANEL_COUNT = 5
PANEL_WATTAGE_W = 610
PANEL_BRAND = "Waaree"
INVERTER_MODEL = "PSIS-5K0"
INSTALLED_KWP = PANEL_COUNT * PANEL_WATTAGE_W / 1000
INSTALL_DATE = date(2026, 7, 18)

STATUS_BY_COLOR = {"Green": "normal", "yellow": "standby", "red": "abnormal", "gray": "offline"}

st.set_page_config(page_title="Solar Dashboard", page_icon="☀️", layout="wide")

_missing = [name for name, val in {
    "POLYCAB_TOKEN": POLYCAB_TOKEN, "GOODS_ID": GOODS_ID, "MEMBER_AUTO_ID": MEMBER_AUTO_ID,
    "TURSO_DATABASE_URL": TURSO_DATABASE_URL, "TURSO_AUTH_TOKEN": TURSO_AUTH_TOKEN,
}.items() if not val]
if _missing:
    st.error(f"Missing required .env vars: {', '.join(_missing)}")
    st.stop()


def get_conn():
    """Deliberately NOT @st.cache_resource: a cached connection gets reused for the
    entire lifetime of the Streamlit Cloud process (which stays alive for hours/days
    between visits), and Turso's remote Hrana stream gets torn down server-side after
    being idle - the client doesn't reconnect automatically, it just fails every query
    with "stream not found" (seen in production). A fresh connection per script rerun
    is cheap (this only runs once per rerun, not per query) and avoids that entirely."""
    return libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)


@st.cache_data(ttl=60)
def fetch_live_snapshot():
    try:
        detail = call("InverterDetailInfoNewone", {"GoodsID": GOODS_ID}, POLYCAB_TOKEN)
        group_list = call("GroupList", {"MemberAutoID": MEMBER_AUTO_ID, "inputValue": ""}, POLYCAB_TOKEN)
    except (AuthError, NetworkError, SchemaError) as e:
        return None, str(e)

    ac = detail.get("ACDCInfo", {})
    inv_status = group_list.get("AllGroupList", [{}])[0].get("InverterStatus", {})
    active_color = next((c for c, n in inv_status.items() if n), None)

    today_kwh = num(detail.get("EToday"))
    total_kwh = num(detail.get("ETotal"))

    def first(arr):
        return num(arr[0]) if arr else None

    last_update_ist = None
    if detail.get("DataTime"):
        last_update_ist = (datetime.strptime(detail["DataTime"], "%Y-%m-%d %H:%M:%S")
                            .replace(tzinfo=DEVICE_CLOCK_TZ).astimezone(IST))

    return {
        "power_w": first(ac.get("Pac")),
        "today_kwh": today_kwh / 1000 if today_kwh is not None else None,
        "total_kwh": total_kwh / 1000 if total_kwh is not None else None,
        "temperature_c": num(detail.get("Tntc")),
        "status": STATUS_BY_COLOR.get(active_color),
        "last_update": last_update_ist.strftime("%Y-%m-%d %H:%M:%S") if last_update_ist else None,
        "mdsp_version": detail.get("MDSPVersion"),
        "sdsp_version": detail.get("SDSPVersion"),
        "csb_version": detail.get("CSBVersion"),
    }, None


@st.cache_data(ttl=60)
def fetch_last_sync(_conn):
    row = _conn.execute("SELECT MAX(timestamp) FROM readings WHERE source = 'live'").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


@st.cache_data(ttl=60)
def fetch_day_readings(_conn, date_str: str) -> pd.DataFrame:
    """Used by both the Today and Day tabs - a past day's data never changes, so the 60s
    TTL only really matters for today's still-accumulating readings."""
    day_start_utc = datetime.combine(date.fromisoformat(date_str), time.min, IST).astimezone(UTC)
    day_end_utc = day_start_utc + timedelta(days=1)
    rows = _conn.execute(
        "SELECT timestamp, pv_power_w, source, daily_yield_kwh, status FROM readings "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (day_start_utc.isoformat(), day_end_utc.isoformat()),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["timestamp_utc", "pv_power_w", "source", "daily_yield_kwh", "status"])
    if not df.empty:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
        # Vega-Lite has no real IANA-timezone support (only UTC or browser-local), so a
        # tz-aware IST timestamp gets silently reinterpreted and drifts. Strip tzinfo and
        # treat the IST wall-clock value as a naive timestamp instead - avoids the mismatch
        # entirely since both the data and the axis domain below use the same convention.
        df["timestamp_ist"] = df["timestamp_utc"].dt.tz_convert(IST).dt.tz_localize(None)
    return df


@st.cache_data(ttl=60)
def fetch_week_readings(_conn, week_start_str: str, week_end_str: str) -> pd.DataFrame:
    """All readings in [week_start, week_end] (inclusive) - a superset query used to
    build the "average this week" overlay lines, keyed by each row's calendar date and
    5-min-floored time-of-day so per-day curves can be grouped and averaged bucket-by-
    bucket. 5-min floor matches the collector's actual polling cadence (see AGENTS.md),
    so it buckets same-time-of-day readings across days without needing exact alignment."""
    week_start, week_end = date.fromisoformat(week_start_str), date.fromisoformat(week_end_str)
    start_utc = datetime.combine(week_start, time.min, IST).astimezone(UTC)
    end_utc = datetime.combine(week_end + timedelta(days=1), time.min, IST).astimezone(UTC)
    rows = _conn.execute(
        "SELECT timestamp, pv_power_w FROM readings WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["timestamp_utc", "pv_power_w"])
    if not df.empty:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
        df["timestamp_ist"] = df["timestamp_utc"].dt.tz_convert(IST).dt.tz_localize(None)
        df["date"] = df["timestamp_ist"].dt.date
        df["time_of_day"] = df["timestamp_ist"].dt.floor("5min").dt.time
    return df


def add_cumulative_kwh(df: pd.DataFrame) -> pd.DataFrame:
    """Trapezoidal-rule integration of pv_power_w over time -> cumulative_kwh column.
    `df` must already be sorted by timestamp_ist. Shared by the main cumulative chart
    and the per-day curves behind the "average this week" overlay, so the same
    approximation is used consistently rather than duplicated in two places."""
    df = df.copy()
    dt_hours = df["timestamp_ist"].diff().dt.total_seconds().fillna(0) / 3600
    power_filled = df["pv_power_w"].fillna(0)
    avg_power = (power_filled + power_filled.shift(1).fillna(power_filled)) / 2
    df["cumulative_kwh"] = (dt_hours * avg_power / 1000).cumsum()
    return df


def compute_week_avg_power(df_week: pd.DataFrame, overlay_date: date) -> pd.DataFrame:
    """Average pv_power_w per time-of-day bucket across every day present in df_week,
    replotted onto `overlay_date` so it lines up on the same x-axis as that day's chart."""
    if df_week.empty:
        return pd.DataFrame(columns=["timestamp_ist", "avg_power_w"])
    # Collapse to one value per (date, bucket) first - live and backfill rows can both
    # land in the same 5-min bucket on a given day, and without this a day with more
    # such duplicates would silently outweigh other days in the across-day average.
    per_day_bucket = df_week.groupby(["date", "time_of_day"], as_index=False)["pv_power_w"].mean()
    grouped = per_day_bucket.groupby("time_of_day", as_index=False)["pv_power_w"].mean() \
                             .rename(columns={"pv_power_w": "avg_power_w"})
    grouped["timestamp_ist"] = grouped["time_of_day"].apply(lambda t: datetime.combine(overlay_date, t))
    return grouped[["timestamp_ist", "avg_power_w"]]


def compute_week_avg_cumulative(df_week: pd.DataFrame, overlay_date: date) -> pd.DataFrame:
    """Per-day cumulative-kWh curves (same integration as the main chart) for every day
    in df_week, then averaged per time-of-day bucket and replotted onto `overlay_date`."""
    if df_week.empty:
        return pd.DataFrame(columns=["timestamp_ist", "avg_cum_kwh"])
    per_day_cum = []
    for _, day_df in df_week.groupby("date"):
        day_df = add_cumulative_kwh(day_df.sort_values("timestamp_ist").reset_index(drop=True))
        # Collapse duplicate same-bucket rows within this day to their max (cumulative
        # is monotonic, so the max in a bucket is the latest/true value as of that
        # time-of-day) before pooling across days - same reasoning as compute_week_avg_power.
        collapsed = day_df.groupby("time_of_day", as_index=False)["cumulative_kwh"].max()
        per_day_cum.append(collapsed)
    grouped = pd.concat(per_day_cum).groupby("time_of_day", as_index=False)["cumulative_kwh"].mean() \
                                     .rename(columns={"cumulative_kwh": "avg_cum_kwh"})
    grouped["timestamp_ist"] = grouped["time_of_day"].apply(lambda t: datetime.combine(overlay_date, t))
    return grouped[["timestamp_ist", "avg_cum_kwh"]]


def compute_day_metrics(df: pd.DataFrame) -> dict:
    """Everything here comes straight from our own DB (not the live API) - this is what
    powers the Day tab's metrics for an arbitrary (including past) date."""
    if df.empty:
        return {"total_kwh": None, "peak_w": None, "peak_time": None, "status": None, "last_reading": None}

    total_kwh = df["daily_yield_kwh"].max() if df["daily_yield_kwh"].notna().any() else None

    peak_w = peak_time = None
    if df["pv_power_w"].notna().any():
        peak_idx = df["pv_power_w"].idxmax()
        peak_w = df.loc[peak_idx, "pv_power_w"]
        peak_time = df.loc[peak_idx, "timestamp_ist"].strftime("%H:%M")

    status_series = df["status"].dropna()
    status = status_series.iloc[-1] if not status_series.empty else None
    last_reading = df["timestamp_ist"].max().strftime("%Y-%m-%d %H:%M:%S")

    return {"total_kwh": total_kwh, "peak_w": peak_w, "peak_time": peak_time,
            "status": status, "last_reading": last_reading}


@st.cache_data(ttl=300)
def fetch_daily_kwh(_conn, start_date_str: str, end_date_str: str) -> dict:
    """Keyed by the UTC calendar date substring of `timestamp`. Our daylight-only
    collection window (00:30-13:30 UTC) never crosses UTC midnight for a given IST
    day, so UTC-date == IST-date here - no per-row timezone conversion needed.

    Not restricted to source='live': the collector also backfills daily_yield_kwh onto
    the last row of a backfilled day (from getAllPacMonth), so backfill-only days have a
    correct total here too - see collector/collect.py."""
    start_utc = datetime.combine(date.fromisoformat(start_date_str), time.min, IST).astimezone(UTC)
    end_utc = datetime.combine(date.fromisoformat(end_date_str) + timedelta(days=1), time.min, IST).astimezone(UTC)
    rows = _conn.execute(
        "SELECT substr(timestamp, 1, 10) AS d, MAX(daily_yield_kwh) FROM readings "
        "WHERE timestamp >= ? AND timestamp < ? GROUP BY d",
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


@st.cache_data(ttl=300)
def fetch_top_days_yearly(_conn, year: int) -> dict:
    """{date_str: rank} for that year's top 10 - maintained by the collector
    (update_top_days in collect.py), not recomputed here."""
    rows = _conn.execute("SELECT date, rank FROM top_days_yearly WHERE year = ?", (year,)).fetchall()
    return {r[0]: r[1] for r in rows}


@st.cache_data(ttl=300)
def fetch_top_days_alltime(_conn) -> list[tuple[str, int, float]]:
    """[(date_str, rank, kwh), ...] ordered by rank - maintained by the collector."""
    return _conn.execute("SELECT date, rank, kwh FROM top_days_alltime ORDER BY rank").fetchall()


def week_bounds(d: date) -> tuple[date, date]:
    """Monday-Sunday bounds of the week containing d (date.weekday(): Monday=0)."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    next_first = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return first, next_first - timedelta(days=1)


def avg_kwh_in_range(kwh_by_date: dict, start: date, end: date) -> float | None:
    """Average over days actually recorded in [start, end] - missing days (e.g. before
    install, or not yet collected) are excluded rather than treated as zero, so an
    average early in a period isn't misleadingly dragged down by days with no data."""
    vals = [v for n in range((end - start).days + 1)
            if (v := kwh_by_date.get((start + timedelta(days=n)).isoformat())) is not None]
    return sum(vals) / len(vals) if vals else None


def _fetch_hourly_weather_days(lat: str, lon: str, date_str: str, archive: bool) -> tuple[pd.DataFrame, dict]:
    base_url = ("https://archive-api.open-meteo.com/v1/archive" if archive
                else "https://api.open-meteo.com/v1/forecast")
    try:
        resp = requests.get(
            base_url,
            params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
                    "daily": "sunrise,sunset",
                    "timezone": "Asia/Kolkata", "start_date": date_str, "end_date": date_str},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, KeyError, ValueError) as e:
        st.warning(f"Could not fetch weather for {date_str} ({e}).")
        return pd.DataFrame(columns=["time", "temperature_c"]), {}

    h = body.get("hourly", {})
    # Open-Meteo already returns these as local (Asia/Kolkata) wall-clock strings with no
    # UTC offset - keep them naive, matching fetch_day_readings' convention, rather than
    # attaching tzinfo Vega-Lite can't correctly interpret anyway.
    temp_df = pd.DataFrame({"time": pd.to_datetime(h.get("time", [])), "temperature_c": h.get("temperature_2m", [])})
    daily = body.get("daily", {})
    sun = {
        "sunrise": daily["sunrise"][0].split("T")[1] if daily.get("sunrise") else None,
        "sunset": daily["sunset"][0].split("T")[1] if daily.get("sunset") else None,
    }
    return temp_df, sun


@st.cache_data(ttl=600)
def fetch_hourly_weather_for_date(lat: str, lon: str, date_str: str) -> tuple[pd.DataFrame, dict]:
    """The forecast endpoint reliably covers ~90 days back (and today/near future); the
    archive endpoint covers arbitrary history but lags ~5 days before being finalized -
    same split rationale as fetch_daily_weather_range, just at hourly granularity, and
    confirmed the archive endpoint also serves sunrise/sunset for arbitrary past dates
    (it's pure astronomy, not weather-dependent, so no lag issue there specifically -
    but it's simpler to fetch both from whichever single endpoint the split picks)."""
    d = date.fromisoformat(date_str)
    now_date = datetime.now(IST).date()
    use_archive = d < now_date - timedelta(days=89)
    return _fetch_hourly_weather_days(lat, lon, date_str, archive=use_archive)


def _fetch_weather_days(lat: str, lon: str, start_date_str: str, end_date_str: str, archive: bool) -> dict:
    base_url = ("https://archive-api.open-meteo.com/v1/archive" if archive
                else "https://api.open-meteo.com/v1/forecast")
    try:
        resp = requests.get(
            base_url,
            params={"latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Kolkata", "start_date": start_date_str, "end_date": end_date_str},
            timeout=20,
        )
        resp.raise_for_status()
        d = resp.json()["daily"]
    except (requests.RequestException, KeyError, ValueError) as e:
        st.warning(f"Could not fetch historical weather ({e}) - heatmap will show without temperature.")
        return {}
    return {t: {"max": tmax, "min": tmin}
            for t, tmax, tmin in zip(d["time"], d["temperature_2m_max"], d["temperature_2m_min"])}


@st.cache_data(ttl=86400)
def fetch_daily_weather_range(lat: str, lon: str, start_date_str: str, end_date_str: str) -> dict:
    """The forecast endpoint reliably covers roughly the last ~90 days (including today),
    but rejects anything further back. The archive endpoint covers arbitrary history, but
    its reanalysis data has a few days' lag before it's finalized - a request touching
    very recent days gets a hard 400, not just missing data. Split the request at that
    boundary and use whichever endpoint actually supports each part, rather than routing
    everything through archive and clamping away days it could never serve anyway (this
    silently dropped ALL weather for a brand-new install, since its whole history so far
    is more recent than archive's lag window)."""
    start, end = date.fromisoformat(start_date_str), date.fromisoformat(end_date_str)
    now_date = datetime.now(IST).date()
    forecast_start = max(start, now_date - timedelta(days=90))

    result = {}
    if forecast_start <= end:
        result.update(_fetch_weather_days(lat, lon, forecast_start.isoformat(), end.isoformat(), archive=False))
    if start < forecast_start:
        archive_end = min(end, forecast_start - timedelta(days=1), now_date - timedelta(days=5))
        if start <= archive_end:
            result.update(_fetch_weather_days(lat, lon, start.isoformat(), archive_end.isoformat(), archive=True))
    return result


# Font Awesome Free "star" (this year) and "trophy" (all-time) glyphs, distinct icons so
# the two badges/heatmap overlays are visually distinguishable at a glance. Encoded as
# base64 data URIs (not inlined as raw <svg>) because st.html() runs everything through
# DOMPurify, which strips inline <svg> markup by default - an <img src="data:..."> is
# always allowed and renders identically. The same data URIs are reused as Vega-Lite
# mark_image "url" values for the Month tab heatmap overlay below.
TOP_YEAR_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640">'
    '<path fill="rgb(255, 212, 59)" d="M341.5 45.1C337.4 37.1 329.1 32 320.1 32'
    'C311.1 32 302.8 37.1 298.7 45.1L225.1 189.3L65.2 214.7C56.3 216.1 48.9 222.4 46.1 231C43.3 239.6 45.6 249'
    ' 51.9 255.4L166.3 369.9L141.1 529.8C139.7 538.7 143.4 547.7 150.7 553C158 558.3 167.6 559.1 175.7 555'
    'L320.1 481.6L464.4 555C472.4 559.1 482.1 558.3 489.4 553C496.7 547.7 500.4 538.8 499 529.8L473.7 369.9'
    'L588.1 255.4C594.5 249 596.7 239.6 593.9 231C591.1 222.4 583.8 216.1 574.8 214.7L415 189.3L341.5 45.1z"/></svg>'
)
TOP_ALLTIME_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640">'
    '<path fill="rgb(255, 212, 59)" d="M208.3 64L432.3 64C458.8 64 480.4 85.8 '
    '479.4 112.2C479.2 117.5 479 122.8 478.7 128L528.3 128C554.4 128 577.4 149.6 575.4 177.8C567.9 281.5 514.9 '
    '338.5 457.4 368.3C441.6 376.5 425.5 382.6 410.2 387.1C390 415.7 369 430.8 352.3 438.9L352.3 512L416.3 '
    '512C434 512 448.3 526.3 448.3 544C448.3 561.7 434 576 416.3 576L224.3 576C206.6 576 192.3 561.7 192.3 '
    '544C192.3 526.3 206.6 512 224.3 512L288.3 512L288.3 438.9C272.3 431.2 252.4 416.9 233 390.6C214.6 385.8 '
    '194.6 378.5 175.1 367.5C121 337.2 72.2 280.1 65.2 177.6C63.3 149.5 86.2 127.9 112.3 127.9L161.9 127.9C161.6 '
    '122.7 161.4 117.5 161.2 112.1C160.2 85.6 181.8 63.9 208.3 63.9zM165.5 176L113.1 176C119.3 260.7 158.2 303.1 '
    '198.3 325.6C183.9 288.3 172 239.6 165.5 176zM444 320.8C484.5 297 521.1 254.7 527.3 176L475 176C468.8 236.9 '
    '457.6 284.2 444 320.8z"/></svg>'
)


def _svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


TOP_YEAR_ICON_URI = _svg_data_uri(TOP_YEAR_ICON_SVG)
TOP_ALLTIME_ICON_URI = _svg_data_uri(TOP_ALLTIME_ICON_SVG)

TOP_BADGE_STYLE = (
    "display:inline-flex;align-items:center;gap:6px;background:#fff8e1;border:1px solid #ffe082;"
    "border-radius:16px;padding:4px 12px;margin-right:8px;margin-bottom:8px;font-size:0.85rem;color:#5c4a00;"
)


def render_top_days_banner(conn, selected_date: date) -> None:
    """Shown on both the Today and Day tabs - one badge per list (yearly / all-time)
    the viewed date ranks in (maintained by the collector), each with its own icon."""
    date_str = selected_date.isoformat()
    yearly_rank = fetch_top_days_yearly(conn, selected_date.year).get(date_str)
    alltime_rank = next((r for d, r, _ in fetch_top_days_alltime(conn) if d == date_str), None)

    if yearly_rank is None and alltime_rank is None:
        return
    badges = []
    if yearly_rank is not None:
        badges.append(
            f'<div style="{TOP_BADGE_STYLE}"><img src="{TOP_YEAR_ICON_URI}" width="16" height="16"> '
            f'#{yearly_rank} this year</div>'
        )
    if alltime_rank is not None:
        badges.append(
            f'<div style="{TOP_BADGE_STYLE}"><img src="{TOP_ALLTIME_ICON_URI}" width="16" height="16"> '
            f'#{alltime_rank} all-time</div>'
        )
    st.html(f'<div style="display:flex;flex-wrap:wrap;">{"".join(badges)}</div>')


def render_production_section(conn, selected_date: date, key_prefix: str) -> None:
    """Intraday power chart (+ optional ambient-temperature overlay) and the cumulative-
    production chart, for any date - shared by both the Today and Day tabs. `key_prefix`
    keeps the checkbox's Streamlit widget key unique between the two tabs."""
    today = datetime.now(IST).date()
    date_str = selected_date.isoformat()
    df = fetch_day_readings(conn, date_str)
    is_today = selected_date == today

    st.subheader("Today's production" if is_today else f"Production on {date_str}")
    show_temp = st.checkbox("Overlay ambient temperature", value=False, key=f"{key_prefix}_temp_toggle")

    # Naive (no tzinfo) to match the naive IST wall-clock values in the dataframes above -
    # see the comment in fetch_day_readings for why.
    x_start = datetime.combine(selected_date, time(6, 0))
    x_end = datetime.combine(selected_date, time(19, 0))
    x_scale = alt.Scale(domain=[x_start.isoformat(), x_end.isoformat()])

    if is_today:
        # Open-Meteo returns the whole day's forecast regardless of current time, so
        # without clipping to "now" the temperature line would extend into hours that
        # haven't happened yet - misleading alongside a power line that correctly stops
        # at "now" (or doesn't exist yet at all, e.g. before sunrise/first poll).
        now_naive = datetime.now(IST).replace(tzinfo=None)
        weather_plot_end = min(x_end, now_naive)
    else:
        weather_plot_end = x_end  # the whole day has already happened, no "future" to clip

    # "Average this week" overlay data - the week (Mon-Sun) containing selected_date,
    # capped at today since future days have no readings yet. Computed once here and
    # reused by both the power and cumulative charts below. Restricted to the same
    # timestamp range as selected_date's own data (not the full day) per the request to
    # overlay the average "during the data points" - a day with only a few readings so
    # far shouldn't get an average line stretching across hours that haven't happened.
    df_week = pd.DataFrame()
    if not df.empty:
        week_start, week_end = week_bounds(selected_date)
        df_week = fetch_week_readings(conn, week_start.isoformat(), min(week_end, today).isoformat())
        t_min, t_max = df["timestamp_ist"].min(), df["timestamp_ist"].max()

    power_chart = None
    if not df.empty:
        power_chart = alt.Chart(df).mark_line(color="#f5a623", point=alt.OverlayMarkDef(size=30)).encode(
            x=alt.X("timestamp_ist:T", title="Time (IST)", scale=x_scale),
            y=alt.Y("pv_power_w:Q", title="Power (W)"),
            tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                     alt.Tooltip("pv_power_w:Q", title="Power (W)"),
                     alt.Tooltip("source:N", title="Source")],
        )

    avg_power_chart = None
    if not df_week.empty:
        avg_power_df = compute_week_avg_power(df_week, selected_date)
        avg_power_df = avg_power_df[(avg_power_df["timestamp_ist"] >= t_min) & (avg_power_df["timestamp_ist"] <= t_max)]
        if not avg_power_df.empty:
            avg_power_chart = alt.Chart(avg_power_df).mark_line(
                color="#888888", strokeDash=[4, 3], strokeWidth=1.5,
            ).encode(
                x=alt.X("timestamp_ist:T", scale=x_scale),
                y=alt.Y("avg_power_w:Q"),
                tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                         alt.Tooltip("avg_power_w:Q", title="Week avg power (W)", format=".0f")],
            )

    temp_chart = None
    if show_temp and WEATHER_LAT and WEATHER_LON and weather_plot_end > x_start:
        df_weather, _ = fetch_hourly_weather_for_date(WEATHER_LAT, WEATHER_LON, date_str)
        df_weather = df_weather[(df_weather["time"] >= x_start) & (df_weather["time"] <= weather_plot_end)]
        if not df_weather.empty:
            temp_chart = alt.Chart(df_weather).mark_line(
                color="#4a90d9", strokeDash=[4, 2], point=alt.OverlayMarkDef(size=30, color="#4a90d9"),
            ).encode(
                x=alt.X("time:T", scale=x_scale),
                y=alt.Y("temperature_c:Q", title="Temp (°C)", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("time:T", title="Time"),
                         alt.Tooltip("temperature_c:Q", title="Temp (°C)")],
            )

    # power_chart + avg_power_chart share the primary y-axis (directly comparable W
    # values), so they're layered together first; temp_chart then layers on top with its
    # own independent y-axis, since °C isn't on the same scale as W.
    main_layers = [c for c in (power_chart, avg_power_chart) if c is not None]
    main_chart = alt.layer(*main_layers) if len(main_layers) > 1 else (main_layers[0] if main_layers else None)

    if main_chart is None and temp_chart is None:
        st.info("No readings yet for today." if is_today else "No readings recorded for this day.")
    elif main_chart is not None and temp_chart is not None:
        st.altair_chart(alt.layer(main_chart, temp_chart).resolve_scale(y="independent")
                         .properties(height=400), width='stretch')
    else:
        st.altair_chart((main_chart or temp_chart).properties(height=400), width='stretch')
    if avg_power_chart is not None:
        st.caption("┄┄ Average power at the same time of day, across this week")

    if not df.empty:
        st.subheader("Cumulative production today" if is_today else "Cumulative production")
        # Derived by integrating pv_power_w over time (trapezoidal rule) rather than
        # plotting the DB's own daily_yield_kwh column directly - that field is only ever
        # populated on `live` rows, which can be sparse (e.g. on a day mostly covered by
        # backfill), so it wouldn't give a continuous curve. This approximation converges
        # to roughly the real EToday total by end of day at 5-min sampling resolution.
        cum_df = add_cumulative_kwh(df.sort_values("timestamp_ist").reset_index(drop=True))

        cum_chart = alt.Chart(cum_df).mark_line(color="#2e8b57", point=alt.OverlayMarkDef(size=30, color="#2e8b57")).encode(
            x=alt.X("timestamp_ist:T", title="Time (IST)", scale=x_scale),
            y=alt.Y("cumulative_kwh:Q", title="Cumulative energy (kWh)"),
            tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                     alt.Tooltip("cumulative_kwh:Q", title="Cumulative kWh", format=".2f"),
                     alt.Tooltip("source:N", title="Source")],
        )

        cum_layers = [cum_chart]
        has_avg_cum = False
        if not df_week.empty:
            avg_cum_df = compute_week_avg_cumulative(df_week, selected_date)
            avg_cum_df = avg_cum_df[(avg_cum_df["timestamp_ist"] >= t_min) & (avg_cum_df["timestamp_ist"] <= t_max)]
            if not avg_cum_df.empty:
                cum_layers.append(alt.Chart(avg_cum_df).mark_line(
                    color="#888888", strokeDash=[4, 3], strokeWidth=1.5,
                ).encode(
                    x=alt.X("timestamp_ist:T", scale=x_scale),
                    y=alt.Y("avg_cum_kwh:Q"),
                    tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                             alt.Tooltip("avg_cum_kwh:Q", title="Week avg cumulative (kWh)", format=".2f")],
                ))
                has_avg_cum = True

        final_cum_chart = alt.layer(*cum_layers) if len(cum_layers) > 1 else cum_layers[0]
        st.altair_chart(final_cum_chart.properties(height=300), width='stretch')
        if has_avg_cum:
            st.caption("┄┄ Average cumulative energy at the same time of day, across this week")


conn = get_conn()
tab_today, tab_day, tab_month = st.tabs(["Today", "Day", "Month"])

def pct_delta(current, previous):
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous * 100


with tab_today:
    live, live_error = fetch_live_snapshot()
    if live_error:
        st.warning(f"Could not reach the Polycab API for a live reading ({live_error}) - "
                   f"showing historical data only.")

    today_date = datetime.now(IST).date()
    yesterday = today_date - timedelta(days=1)
    this_week_start, _ = week_bounds(today_date)
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = this_week_start - timedelta(days=1)
    this_month_start, _ = month_bounds(today_date)
    last_month_start, last_month_end = month_bounds(this_month_start - timedelta(days=1))

    kwh_by_date_recent = fetch_daily_kwh(conn, min(last_week_start, last_month_start).isoformat(),
                                          today_date.isoformat())
    avg_this_week = avg_kwh_in_range(kwh_by_date_recent, this_week_start, today_date)
    avg_last_week = avg_kwh_in_range(kwh_by_date_recent, last_week_start, last_week_end)
    avg_this_month = avg_kwh_in_range(kwh_by_date_recent, this_month_start, today_date)
    avg_last_month = avg_kwh_in_range(kwh_by_date_recent, last_month_start, last_month_end)
    week_delta = pct_delta(avg_this_week, avg_last_week)
    month_delta = pct_delta(avg_this_month, avg_last_month)

    yesterday_kwh = kwh_by_date_recent.get(yesterday.isoformat())
    today_kwh = live["today_kwh"] if live else None
    yield_delta = pct_delta(today_kwh, yesterday_kwh)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Current Power", f"{live['power_w']:.0f} W" if live and live["power_w"] is not None else "-")
    col2.metric(
        "Today's Yield",
        f"{today_kwh:.2f} kWh" if today_kwh is not None else "-",
        delta=f"{yield_delta:+.0f}% vs yesterday" if yield_delta is not None else None,
    )
    col3.metric("Status", (live["status"] or "unknown").capitalize() if live else "-")
    col4.metric("Last Update (IST)", live["last_update"] if live else "-")

    today_str_for_sun = today_date.isoformat()
    sun = {}
    if WEATHER_LAT and WEATHER_LON:
        _, sun = fetch_hourly_weather_for_date(WEATHER_LAT, WEATHER_LON, today_str_for_sun)

    row2_col1, row2_col2, row2_col3, row2_col4 = st.columns(4)
    row2_col1.metric("Sunrise (IST)", sun.get("sunrise") or "-")
    row2_col2.metric("Sunset (IST)", sun.get("sunset") or "-")
    row2_col3.metric(
        "Avg Production This Week",
        f"{avg_this_week:.2f} kWh/day" if avg_this_week is not None else "-",
        delta=f"{week_delta:+.0f}% vs last week" if week_delta is not None else None,
    )
    row2_col4.metric(
        "Avg Production This Month",
        f"{avg_this_month:.2f} kWh/day" if avg_this_month is not None else "-",
        delta=f"{month_delta:+.0f}% vs last month" if month_delta is not None else None,
    )

    with st.expander("Technical specs"):
        spec_col1, spec_col2 = st.columns(2)
        spec_col1.markdown(
            f"**Inverter model:** {INVERTER_MODEL}  \n"
            f"**Serial:** {GOODS_ID}  \n"
            f"**Rated capacity:** 5 kW  \n"
            + (f"**Firmware (MDSP/SDSP/CSB):** {live['mdsp_version']} / "
               f"{live['sdsp_version']} / {live['csb_version']}" if live else "**Firmware:** unavailable")
        )
        spec_col2.markdown(
            f"**Panels:** {PANEL_COUNT} x {PANEL_BRAND} {PANEL_WATTAGE_W}W  \n"
            f"**Installed capacity:** {INSTALLED_KWP:.2f} kWp  \n"
            f"**Install date:** {INSTALL_DATE.isoformat()}"
        )

    last_sync = fetch_last_sync(conn)
    now_ist = datetime.now(IST)
    in_daylight = time(6, 0) <= now_ist.time() < time(19, 0)
    if last_sync:
        stale_minutes = (datetime.now(UTC) - last_sync).total_seconds() / 60
        sync_str = last_sync.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        if in_daylight and stale_minutes > 30:
            st.error(f"⚠️ Collector stale - last sync {sync_str} ({stale_minutes:.0f} min ago)")
        else:
            st.caption(f"✅ Collector last synced {sync_str}")
    else:
        st.warning("No collector runs recorded yet.")

    render_top_days_banner(conn, now_ist.date())
    render_production_section(conn, now_ist.date(), key_prefix="today")

with tab_day:
    today = datetime.now(IST).date()
    selected_date = st.date_input(
        "Select a date", value=today, min_value=INSTALL_DATE, max_value=today,
        help=f"Data is only available from the plant's install date, {INSTALL_DATE.isoformat()}, onward.",
    )

    df_day = fetch_day_readings(conn, selected_date.isoformat())
    metrics = compute_day_metrics(df_day)

    dcol1, dcol2, dcol3, dcol4 = st.columns(4)
    dcol1.metric("Total Production", f"{metrics['total_kwh']:.2f} kWh" if metrics["total_kwh"] is not None else "-")
    dcol2.metric("Peak Power", f"{metrics['peak_w']:.0f} W at {metrics['peak_time']}"
                 if metrics["peak_w"] is not None else "-")
    dcol3.metric("Status", (metrics["status"] or "N/A (backfill only)").capitalize()
                 if metrics["status"] else "N/A (backfill only)")
    dcol4.metric("Last Reading (IST)", metrics["last_reading"] or "-")

    sun = {}
    if WEATHER_LAT and WEATHER_LON:
        _, sun = fetch_hourly_weather_for_date(WEATHER_LAT, WEATHER_LON, selected_date.isoformat())

    day_week_start, day_week_end = week_bounds(selected_date)
    day_last_week_start = day_week_start - timedelta(days=7)
    day_last_week_end = day_week_start - timedelta(days=1)
    day_month_start, day_month_end = month_bounds(selected_date)
    day_last_month_start, day_last_month_end = month_bounds(day_month_start - timedelta(days=1))

    kwh_by_date_day = fetch_daily_kwh(
        conn, min(day_last_week_start, day_last_month_start).isoformat(),
        min(max(day_week_end, day_month_end), today).isoformat(),
    )
    avg_day_week = avg_kwh_in_range(kwh_by_date_day, day_week_start, min(day_week_end, today))
    avg_day_last_week = avg_kwh_in_range(kwh_by_date_day, day_last_week_start, day_last_week_end)
    avg_day_month = avg_kwh_in_range(kwh_by_date_day, day_month_start, min(day_month_end, today))
    avg_day_last_month = avg_kwh_in_range(kwh_by_date_day, day_last_month_start, day_last_month_end)
    day_week_delta = pct_delta(avg_day_week, avg_day_last_week)
    day_month_delta = pct_delta(avg_day_month, avg_day_last_month)

    day_sun_col1, day_sun_col2, day_sun_col3, day_sun_col4 = st.columns(4)
    day_sun_col1.metric("Sunrise (IST)", sun.get("sunrise") or "-")
    day_sun_col2.metric("Sunset (IST)", sun.get("sunset") or "-")
    day_sun_col3.metric(
        "Avg Production This Week",
        f"{avg_day_week:.2f} kWh/day" if avg_day_week is not None else "-",
        delta=f"{day_week_delta:+.0f}% vs last week" if day_week_delta is not None else None,
    )
    day_sun_col4.metric(
        "Avg Production This Month",
        f"{avg_day_month:.2f} kWh/day" if avg_day_month is not None else "-",
        delta=f"{day_month_delta:+.0f}% vs last month" if day_month_delta is not None else None,
    )

    with st.expander("Technical specs"):
        st.markdown(
            f"**Inverter model:** {INVERTER_MODEL}  \n"
            f"**Serial:** {GOODS_ID}  \n"
            f"**Rated capacity:** 5 kW  \n"
            f"**Panels:** {PANEL_COUNT} x {PANEL_BRAND} {PANEL_WATTAGE_W}W  \n"
            f"**Installed capacity:** {INSTALLED_KWP:.2f} kWp  \n"
            f"**Install date:** {INSTALL_DATE.isoformat()}"
        )

    render_top_days_banner(conn, selected_date)
    render_production_section(conn, selected_date, key_prefix="day")

with tab_month:
    st.subheader("Past 12 months")

    # lesley's calendar heatmap has a fixed pixel width (52-53 week-columns, sized for
    # legibility - shrinking it to fit a phone screen would make the cells illegible
    # rather than actually readable). Neither Streamlit's width="stretch" nor
    # width="content" nor even passing an explicit int width stopped it from being
    # squashed to the screen width on mobile - Streamlit's element containers are flex
    # items, and flex children shrink below their content's natural size by default
    # regardless of what width the chart itself asks for (a well-known Streamlit/
    # flexbox gotcha, more fundamental than anything in Vega-Lite's own sizing API).
    # Fix: disable flex-shrink and force a real min-width on the actual chart wrapper,
    # so it can't be compressed - then overflow-x: auto on the outer container scrolls
    # instead, same as GitHub's own contribution graph does on mobile web.
    HEATMAP_HEIGHT_PX = 260
    HEATMAP_MIN_WIDTH_PX = HEATMAP_HEIGHT_PX * 5  # matches lesley.cal_heatmap's own internal width formula
    st.html(f"""<style>
        div[class*="st-key-heatmap_scroll_"] {{
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            flex-shrink: 0;
        }}
        div[class*="st-key-heatmap_scroll_"] * {{
            flex-shrink: 0;
        }}
        div[class*="st-key-heatmap_scroll_"] [data-testid="stVegaLiteChart"] {{
            min-width: {HEATMAP_MIN_WIDTH_PX}px;
        }}
        div[class*="st-key-heatmap_scroll_"] svg {{
            min-width: {HEATMAP_MIN_WIDTH_PX}px;
        }}
    </style>""")

    today = datetime.now(IST).date()
    # Clamp to the plant's actual install date - there's nothing meaningful to show before
    # it, and lesley would otherwise render a whole empty 2025 calendar block for no reason.
    start_date = max(today - timedelta(weeks=53), INSTALL_DATE)

    kwh_by_date = fetch_daily_kwh(conn, start_date.isoformat(), today.isoformat())

    weather_by_date = (fetch_daily_weather_range(WEATHER_LAT, WEATHER_LON, start_date.isoformat(), today.isoformat())
                        if WEATHER_LAT and WEATHER_LON else {})

    # lesley.cal_heatmap only ever renders a single Jan-Dec calendar year (it infers the
    # year from min(dates) and silently drops anything outside it) - a 12-month lookback
    # spans 2 calendar years, so it has to be called once per year and stacked, not once
    # for the whole range.
    any_data = False
    for year in sorted({start_date.year, today.year}):
        year_start, year_end = date(year, 1, 1), date(year, 12, 31)
        obs_dates, obs_values = [], []
        for n in range((year_end - year_start).days + 1):
            d = year_start + timedelta(days=n)
            if start_date <= d <= today:
                kwh = kwh_by_date.get(d.isoformat())
                obs_dates.append(d)
                obs_values.append(kwh if kwh is not None else 0)
                any_data = any_data or kwh is not None

        if not obs_dates:
            continue  # this year isn't part of our lookback window at all

        chart = lesley.cal_heatmap(pd.to_datetime(obs_dates), obs_values, cmap="Reds",
                                    days_of_week=["Mon", "Wed", "Fri"], height=HEATMAP_HEIGHT_PX)
        # cal_heatmap only sets padding via a global rectBandPaddingInner (0.1, i.e. shared
        # by both axes) - override it specifically on the day-of-week axis for more
        # vertical breathing room between rows, without changing the week-to-week spacing.
        chart.encoding.y.scale = alt.Scale(paddingInner=0.4)

        # chart.data covers the FULL year (prep_data left-merges onto a Jan1-Dec31 range),
        # so the temperature column has to match that same full-year length/order, not
        # just the subset of dates passed in above. Also flag days outside the plant's
        # actual data period (before install, or future days within this year that
        # haven't happened yet) - prep_data defaults both to values=0, indistinguishable
        # from a genuine zero-production day unless we mark them separately.
        temps = []
        in_valid_period = []
        for d in chart.data["dates"]:
            d_date = d.date()
            w = weather_by_date.get(d_date.isoformat(), {})
            tmin, tmax = w.get("min"), w.get("max")
            temps.append((tmin + tmax) / 2 if tmin is not None and tmax is not None else None)
            in_valid_period.append(INSTALL_DATE <= d_date <= today)
        chart.data["avg_temp"] = temps
        chart.data["in_valid_period"] = in_valid_period
        chart.encoding.color = alt.condition(
            "!datum.in_valid_period",
            alt.value("#ebedf0"),  # grey - before install or a future day not yet happened
            # bin, not a smooth continuous gradient, to get the visually-stepped
            # ColorBrewer-style "Reds" look (light -> dark maroon in distinct bands).
            alt.Color("values:Q", bin=alt.Bin(maxbins=9), scale=alt.Scale(scheme="reds"),
                      title="kWh", legend=alt.Legend(orient="right")),
        )
        top_rank_by_date = fetch_top_days_yearly(conn, year)
        alltime_rank_by_date = {d: r for d, r, _ in fetch_top_days_alltime(conn)}
        top_rank_col = [top_rank_by_date.get(d.date().isoformat()) for d in chart.data["dates"]]
        alltime_rank_col = [alltime_rank_by_date.get(d.date().isoformat()) for d in chart.data["dates"]]
        # All-time icon takes priority over the yearly icon when a day is in both lists,
        # per the requested overlay behavior - a day can only show one glyph per cell.
        # Computed from these plain Python lists (not chart.data's columns) because
        # assigning a list containing None onto a pandas column silently upcasts it to
        # float64 with NaN in place of None - and `NaN is not None` is True, which would
        # make every single day (including grey non-data days) match the "has a rank"
        # branch below.
        top_icon_col = [
            TOP_ALLTIME_ICON_URI if a is not None else (TOP_YEAR_ICON_URI if y is not None else None)
            for a, y in zip(alltime_rank_col, top_rank_col)
        ]
        chart.data["top_rank"] = top_rank_col
        chart.data["alltime_rank"] = alltime_rank_col
        chart.data["top_icon_url"] = top_icon_col

        chart.encoding.tooltip = [
            alt.Tooltip("dates:T", title="Date"),
            alt.Tooltip("values:Q", title="kWh", format=".2f"),
            alt.Tooltip("avg_temp:Q", title="Avg temp (°C)", format=".1f"),
            alt.Tooltip("top_rank:O", title="Top 10 rank (this year)"),
            alt.Tooltip("alltime_rank:O", title="Top 10 rank (all-time)"),
        ]

        # Icon overlay for top-10 days - reuses the exact same x/y encoding objects as
        # the rect layer so the glyph aligns with the right cell, and the same tooltip
        # so hovering the (small) icon itself still shows the full info. mark_image (not
        # mark_text) since these are the actual FontAwesome SVGs, not emoji glyphs.
        sun_layer = alt.Chart(chart.data).mark_image(width=12, height=12).encode(
            x=chart.encoding.x, y=chart.encoding.y, url="top_icon_url:N", tooltip=chart.encoding.tooltip,
        ).transform_filter("datum.top_icon_url != null")
        # lesley's chart sets a top-level .config (via its own .configure_*() calls),
        # which Altair refuses inside a LayerChart's sub-charts ("Objects with 'config'
        # attribute cannot be used within LayerChart") - move it to the outer layer.
        chart_config = chart.config
        chart.config = alt.Undefined
        chart = alt.layer(chart, sun_layer)
        chart.config = chart_config

        with st.container(key=f"heatmap_scroll_{year}"):
            # An explicit int (not "stretch" or "content"): Streamlit's own "content"
            # mode is documented to still cap at the parent container's width, which is
            # exactly what was squashing all 53 week-columns into an illegible smear on
            # mobile - "stretch" has the same effect. Passing the chart's own actual
            # width bypasses that capping, so it renders at its true fixed size and the
            # container's overflow-x: auto (above) has real overflow to scroll.
            st.altair_chart(chart, width=HEATMAP_MIN_WIDTH_PX)

    if not any_data:
        st.caption("No production data recorded yet in this range - expected for a plant "
                   f"that went live on {INSTALL_DATE.isoformat()}, this will fill in over time.")

    st.subheader("🏆 All-time top 10")
    alltime = fetch_top_days_alltime(conn)
    if alltime:
        st.dataframe(
            pd.DataFrame(alltime, columns=["Date", "Rank", "kWh"]).set_index("Rank"),
            width="stretch",
        )
    else:
        st.caption("No production data recorded yet.")
