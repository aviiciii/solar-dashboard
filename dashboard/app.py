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
def fetch_today_readings(_conn, today_str: str) -> pd.DataFrame:
    day_start_utc = datetime.combine(date.fromisoformat(today_str), time.min, IST).astimezone(UTC)
    day_end_utc = day_start_utc + timedelta(days=1)
    rows = _conn.execute(
        "SELECT timestamp, pv_power_w, source FROM readings "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (day_start_utc.isoformat(), day_end_utc.isoformat()),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["timestamp_utc", "pv_power_w", "source"])
    if not df.empty:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
        # Vega-Lite has no real IANA-timezone support (only UTC or browser-local), so a
        # tz-aware IST timestamp gets silently reinterpreted and drifts. Strip tzinfo and
        # treat the IST wall-clock value as a naive timestamp instead - avoids the mismatch
        # entirely since both the data and the axis domain below use the same convention.
        df["timestamp_ist"] = df["timestamp_utc"].dt.tz_convert(IST).dt.tz_localize(None)
    return df


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


@st.cache_data(ttl=600)
def fetch_hourly_weather_today(lat: str, lon: str, today_str: str) -> tuple[pd.DataFrame, dict]:
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
                "daily": "sunrise,sunset",
                "timezone": "Asia/Kolkata", "start_date": today_str, "end_date": today_str},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    h = body["hourly"]
    # Open-Meteo already returns these as local (Asia/Kolkata) wall-clock strings with no
    # UTC offset - keep them naive, matching fetch_today_readings' convention, rather than
    # attaching tzinfo Vega-Lite can't correctly interpret anyway.
    temp_df = pd.DataFrame({"time": pd.to_datetime(h["time"]), "temperature_c": h["temperature_2m"]})
    daily = body.get("daily", {})
    sun = {
        "sunrise": daily["sunrise"][0].split("T")[1] if daily.get("sunrise") else None,
        "sunset": daily["sunset"][0].split("T")[1] if daily.get("sunset") else None,
    }
    return temp_df, sun


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


conn = get_conn()
tab_today, tab_month = st.tabs(["Today", "Month"])

with tab_today:
    live, live_error = fetch_live_snapshot()
    if live_error:
        st.warning(f"Could not reach the Polycab API for a live reading ({live_error}) - "
                   f"showing historical data only.")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Current Power", f"{live['power_w']:.0f} W" if live and live["power_w"] is not None else "-")
    col2.metric("Today's Yield", f"{live['today_kwh']:.2f} kWh" if live and live["today_kwh"] is not None else "-")
    col3.metric("Status", (live["status"] or "unknown").capitalize() if live else "-")
    col4.metric("Last Update (IST)", live["last_update"] if live else "-")

    today_str_for_sun = datetime.now(IST).date().isoformat()
    sun = {}
    if WEATHER_LAT and WEATHER_LON:
        _, sun = fetch_hourly_weather_today(WEATHER_LAT, WEATHER_LON, today_str_for_sun)
    sun_col1, sun_col2 = st.columns(2)
    sun_col1.metric("Sunrise (IST)", sun.get("sunrise") or "-")
    sun_col2.metric("Sunset (IST)", sun.get("sunset") or "-")

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

    st.subheader("Today's production")
    show_temp = st.checkbox("Overlay ambient temperature", value=False)

    today = now_ist.date()
    today_str = today.isoformat()
    df_today = fetch_today_readings(conn, today_str)

    # Naive (no tzinfo) to match the naive IST wall-clock values in the dataframes above -
    # see the comment in fetch_today_readings for why.
    x_start = datetime.combine(today, time(6, 0))
    x_end = datetime.combine(today, time(19, 0))
    x_scale = alt.Scale(domain=[x_start.isoformat(), x_end.isoformat()])

    # Open-Meteo returns the whole day's forecast regardless of current time, so without
    # clipping to "now" the temperature line would extend into hours that haven't happened
    # yet - misleading alongside a power line that correctly stops at "now" (or doesn't
    # exist yet at all, e.g. before sunrise/before the collector's first poll of the day).
    now_naive = now_ist.replace(tzinfo=None)
    weather_plot_end = min(x_end, now_naive)

    power_chart = None
    if not df_today.empty:
        power_chart = alt.Chart(df_today).mark_line(color="#f5a623", point=alt.OverlayMarkDef(size=30)).encode(
            x=alt.X("timestamp_ist:T", title="Time (IST)", scale=x_scale),
            y=alt.Y("pv_power_w:Q", title="Power (W)"),
            tooltip=[alt.Tooltip("timestamp_ist:T", title="Time"),
                     alt.Tooltip("pv_power_w:Q", title="Power (W)"),
                     alt.Tooltip("source:N", title="Source")],
        )

    temp_chart = None
    if show_temp and WEATHER_LAT and WEATHER_LON and weather_plot_end > x_start:
        df_weather, _ = fetch_hourly_weather_today(WEATHER_LAT, WEATHER_LON, today_str)
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
        st.info("No readings yet for today.")
    elif power_chart is not None and temp_chart is not None:
        st.altair_chart(alt.layer(power_chart, temp_chart).resolve_scale(y="independent")
                         .properties(height=400), width='stretch')
    else:
        st.altair_chart((power_chart or temp_chart).properties(height=400), width='stretch')

    if not df_today.empty:
        st.subheader("Cumulative production today")
        # Derived by integrating pv_power_w over time (trapezoidal rule) rather than
        # plotting the DB's own daily_yield_kwh column directly - that field is only ever
        # populated on `live` rows, which can be sparse (e.g. on a day mostly covered by
        # backfill), so it wouldn't give a continuous curve. This approximation converges
        # to roughly the real EToday total by end of day at 5-min sampling resolution.
        cum_df = df_today.sort_values("timestamp_ist").reset_index(drop=True)
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

        chart = lesley.cal_heatmap(pd.to_datetime(obs_dates), obs_values, cmap="Oranges",
                                    days_of_week=["Mon", "Wed", "Fri"], height=260)
        # cal_heatmap only sets padding via a global rectBandPaddingInner (0.1, i.e. shared
        # by both axes) - override it specifically on the day-of-week axis for more
        # vertical breathing room between rows, without changing the week-to-week spacing.
        chart.encoding.y.scale = alt.Scale(paddingInner=0.4)

        # chart.data covers the FULL year (prep_data left-merges onto a Jan1-Dec31 range),
        # so the temperature column has to match that same full-year length/order, not
        # just the subset of dates passed in above.
        temps = []
        for d in chart.data["dates"]:
            w = weather_by_date.get(d.date().isoformat(), {})
            tmin, tmax = w.get("min"), w.get("max")
            temps.append((tmin + tmax) / 2 if tmin is not None and tmax is not None else None)
        chart.data["avg_temp"] = temps
        chart.encoding.tooltip = [
            alt.Tooltip("dates:T", title="Date"),
            alt.Tooltip("values:Q", title="kWh", format=".2f"),
            alt.Tooltip("avg_temp:Q", title="Avg temp (°C)", format=".1f"),
        ]
        st.altair_chart(chart, width="stretch")

    if not any_data:
        st.caption("No production data recorded yet in this range - expected for a plant "
                   f"that went live on {INSTALL_DATE.isoformat()}, this will fill in over time.")
