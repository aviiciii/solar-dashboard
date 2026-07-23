"""
Streamlit dashboard. Run manually (`uv run streamlit run dashboard/app.py`) and stop when
done - never runs 24/7. Read-only against Turso; live snapshot comes from the Polycab API
directly. See AGENTS.md for the full design rationale.
"""

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


@st.cache_resource
def get_conn():
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

    power_chart = None
    if not df.empty:
        power_chart = alt.Chart(df).mark_line(color="#f5a623", point=alt.OverlayMarkDef(size=30)).encode(
            x=alt.X("timestamp_ist:T", title="Time (IST)", scale=x_scale),
            y=alt.Y("pv_power_w:Q", title="Power (W)"),
            tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                     alt.Tooltip("pv_power_w:Q", title="Power (W)"),
                     alt.Tooltip("source:N", title="Source")],
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

    if power_chart is None and temp_chart is None:
        st.info("No readings yet for today." if is_today else "No readings recorded for this day.")
    elif power_chart is not None and temp_chart is not None:
        st.altair_chart(alt.layer(power_chart, temp_chart).resolve_scale(y="independent")
                         .properties(height=400), width='stretch')
    else:
        st.altair_chart((power_chart or temp_chart).properties(height=400), width='stretch')

    if not df.empty:
        st.subheader("Cumulative production today" if is_today else "Cumulative production")
        # Derived by integrating pv_power_w over time (trapezoidal rule) rather than
        # plotting the DB's own daily_yield_kwh column directly - that field is only ever
        # populated on `live` rows, which can be sparse (e.g. on a day mostly covered by
        # backfill), so it wouldn't give a continuous curve. This approximation converges
        # to roughly the real EToday total by end of day at 5-min sampling resolution.
        cum_df = df.sort_values("timestamp_ist").reset_index(drop=True)
        dt_hours = cum_df["timestamp_ist"].diff().dt.total_seconds().fillna(0) / 3600
        power_filled = cum_df["pv_power_w"].fillna(0)
        avg_power = (power_filled + power_filled.shift(1).fillna(power_filled)) / 2
        cum_df["cumulative_kwh"] = (dt_hours * avg_power / 1000).cumsum()

        cum_chart = alt.Chart(cum_df).mark_line(color="#2e8b57", point=alt.OverlayMarkDef(size=30, color="#2e8b57")).encode(
            x=alt.X("timestamp_ist:T", title="Time (IST)", scale=x_scale),
            y=alt.Y("cumulative_kwh:Q", title="Cumulative energy (kWh)"),
            tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                     alt.Tooltip("cumulative_kwh:Q", title="Cumulative kWh", format=".2f"),
                     alt.Tooltip("source:N", title="Source")],
        )
        st.altair_chart(cum_chart.properties(height=300), width='stretch')


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
    day_sun_col1, day_sun_col2 = st.columns(2)
    day_sun_col1.metric("Sunrise (IST)", sun.get("sunrise") or "-")
    day_sun_col2.metric("Sunset (IST)", sun.get("sunset") or "-")

    with st.expander("Technical specs"):
        st.markdown(
            f"**Inverter model:** {INVERTER_MODEL}  \n"
            f"**Serial:** {GOODS_ID}  \n"
            f"**Rated capacity:** 5 kW  \n"
            f"**Panels:** {PANEL_COUNT} x {PANEL_BRAND} {PANEL_WATTAGE_W}W  \n"
            f"**Installed capacity:** {INSTALLED_KWP:.2f} kWp  \n"
            f"**Install date:** {INSTALL_DATE.isoformat()}"
        )

    render_production_section(conn, selected_date, key_prefix="day")

with tab_month:
    st.subheader("Past 12 months")

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
                                    days_of_week=["Mon", "Wed", "Fri"], height=260)
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
        chart.encoding.tooltip = [
            alt.Tooltip("dates:T", title="Date"),
            alt.Tooltip("values:Q", title="kWh", format=".2f"),
            alt.Tooltip("avg_temp:Q", title="Avg temp (°C)", format=".1f"),
        ]
        st.altair_chart(chart, width="stretch")

    if not any_data:
        st.caption("No production data recorded yet in this range - expected for a plant "
                   f"that went live on {INSTALL_DATE.isoformat()}, this will fill in over time.")
