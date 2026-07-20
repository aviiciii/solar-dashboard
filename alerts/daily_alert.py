"""
One-shot daily summary + push notification, run once per day (e.g. 9 PM IST via cron/
GitHub Actions - see README). For "today" (IST calendar date) it:
  1. Fetches today's and the trailing 7 days' production totals directly from the
     Polycab API (authoritative daily kWh, not derived from our own possibly-gappy
     readings table) and computes % vs yesterday and vs the 7-day average.
  2. Fetches today's peak power + the time it occurred.
  3. Checks our own readings table (Turso) for any >20 min gap during daylight hours
     - a likely collector/inverter outage, since normal polling is every 5 min.
  4. Fetches today's weather (Open-Meteo, no API key needed) for context.
  5. Sends it all as one push notification via ntfy.sh.
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import libsql
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from polycab_client import call, num, MonthlyYields, AuthError, NetworkError, SchemaError  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

WMO_WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stdout))


def peak_power_today(member_auto_id: str, token: str, today: date) -> tuple[float | None, str | None]:
    result = call("getAllPacDay_v1", {"MemberAutoID": member_auto_id, "date": today.isoformat()}, token)
    best_kw, best_time = None, None
    for point in result.get("data", []):
        kw = num(point.get("pac"))
        if kw is not None and (best_kw is None or kw > best_kw):
            best_kw, best_time = kw, point.get("inTime")
    return best_kw, best_time[:5] if best_time else best_time


def find_daylight_gaps(conn, today: date, stale_minutes: int) -> list[tuple[str, str]]:
    """Returns (from, to) IST time strings for any gap > stale_minutes between
    consecutive readings, restricted to the 06:00-19:00 IST daylight window."""
    day_start_utc = datetime.combine(today, datetime.min.time(), IST).astimezone(UTC)
    day_end_utc = datetime.combine(today + timedelta(days=1), datetime.min.time(), IST).astimezone(UTC)
    rows = conn.execute(
        "SELECT timestamp FROM readings WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (day_start_utc.isoformat(), day_end_utc.isoformat()),
    ).fetchall()

    timestamps = [datetime.fromisoformat(r[0]).astimezone(IST) for r in rows]
    daylight = [t for t in timestamps if (6, 0) <= (t.hour, t.minute) < (19, 0)]

    gaps = []
    for prev, curr in zip(daylight, daylight[1:]):
        if (curr - prev) > timedelta(minutes=stale_minutes):
            gaps.append((prev.strftime("%H:%M"), curr.strftime("%H:%M")))
    return gaps


def fetch_weather(lat: str, lon: str, today: date) -> dict | None:
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_sum,cloud_cover_mean,sunshine_duration",
                "timezone": "Asia/Kolkata",
                "start_date": today.isoformat(), "end_date": today.isoformat(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        d = resp.json()["daily"]
        return {
            "condition": WMO_WEATHER_CODES.get(d["weather_code"][0], f"code {d['weather_code'][0]}"),
            "temp_max": d["temperature_2m_max"][0],
            "temp_min": d["temperature_2m_min"][0],
            "precipitation_mm": d["precipitation_sum"][0],
            "cloud_cover_pct": d["cloud_cover_mean"][0],
            "sunshine_hours": round(d["sunshine_duration"][0] / 3600, 1),
        }
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        logging.warning("could not fetch weather: %s", e)
        return None


def pct_change(new: float | None, baseline: float | None) -> float | None:
    if new is None or baseline in (None, 0):
        return None
    return (new - baseline) / baseline * 100


def build_message(today: date, today_kwh, yesterday_kwh, avg7_kwh, peak_kw, peak_time,
                   gaps: list[tuple[str, str]], weather: dict | None) -> str:
    lines = [f"Solar summary for {today.isoformat()}"]

    if today_kwh is not None:
        lines.append(f"Total production: {today_kwh:.2f} kWh")
    else:
        lines.append("Total production: unavailable")

    vs_yesterday = pct_change(today_kwh, yesterday_kwh)
    if vs_yesterday is not None:
        lines.append(f"vs yesterday ({yesterday_kwh:.2f} kWh): {vs_yesterday:+.0f}%")
    vs_avg7 = pct_change(today_kwh, avg7_kwh)
    if vs_avg7 is not None:
        lines.append(f"vs 7-day avg ({avg7_kwh:.2f} kWh): {vs_avg7:+.0f}%")

    if peak_kw is not None:
        lines.append(f"Peak power: {peak_kw:.2f} kW at {peak_time}")

    if gaps:
        gap_str = ", ".join(f"{a}-{b}" for a, b in gaps)
        lines.append(f"Possible outage - data gaps during daylight: {gap_str}")

    if weather:
        lines.append(
            f"Weather: {weather['condition']}, {weather['temp_min']:.0f}-{weather['temp_max']:.0f}°C, "
            f"{weather['sunshine_hours']}h sunshine, {weather['cloud_cover_pct']:.0f}% cloud cover"
            + (f", {weather['precipitation_mm']:.1f}mm rain" if weather["precipitation_mm"] else "")
        )

    return "\n".join(lines)


def send_ntfy(topic: str, message: str) -> None:
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": "Solar daily summary"},
        timeout=15,
    )
    resp.raise_for_status()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    setup_logging(Path(os.environ.get("LOG_PATH", PROJECT_ROOT / "data" / "daily_alert.log")))

    token = os.environ.get("POLYCAB_TOKEN", "").strip()
    member_auto_id = os.environ.get("MEMBER_AUTO_ID", "").strip()
    turso_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    turso_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    weather_lat = os.environ.get("WEATHER_LAT", "").strip()
    weather_lon = os.environ.get("WEATHER_LON", "").strip()
    stale_minutes = int(os.environ.get("STALE_MINUTES", "20"))

    if not (token and member_auto_id and turso_url and turso_token and ntfy_topic):
        logging.error("missing required .env vars: POLYCAB_TOKEN / MEMBER_AUTO_ID / "
                       "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN / NTFY_TOPIC")
        return 1

    today = datetime.now(IST).date()
    yesterday = today - timedelta(days=1)
    last_7_days = [today - timedelta(days=n) for n in range(1, 8)]

    try:
        yields = MonthlyYields(member_auto_id, token)
        today_kwh = yields.yield_for(today)
        yesterday_kwh = yields.yield_for(yesterday)
        week_values = [v for d in last_7_days if (v := yields.yield_for(d)) is not None]
        avg7_kwh = sum(week_values) / len(week_values) if week_values else None

        peak_kw, peak_time = peak_power_today(member_auto_id, token, today)

        conn = libsql.connect(turso_url, auth_token=turso_token)
        gaps = find_daylight_gaps(conn, today, stale_minutes)
        conn.close()

        weather = fetch_weather(weather_lat, weather_lon, today) if weather_lat and weather_lon else None

        message = build_message(today, today_kwh, yesterday_kwh, avg7_kwh, peak_kw, peak_time,
                                 gaps, weather)
        logging.info("sending notification:\n%s", message)
        send_ntfy(ntfy_topic, message)
        logging.info("notification sent ok")
        return 0
    except AuthError as e:
        logging.error("AUTH FAILURE: %s", e)
        return 1
    except NetworkError as e:
        logging.error("NETWORK FAILURE: %s", e)
        return 1
    except SchemaError as e:
        logging.error("SCHEMA FAILURE (API response shape changed?): %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
