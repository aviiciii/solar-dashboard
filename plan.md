# DIY Solar Monitoring System — Project Plan

## Context
- Hardware: 5x Waaree 610W panels (~3.05 kWp) → Polycab PSIS-5K0 inverter.
- Existing monitoring: Polycab's own portal at `pv.polycabmonitoring.com` (this is a
  white-labeled Solarman deployment — same family as many Growatt/Sofar/Deye OEM
  portals in India). We're piggybacking on this account's data, not building new hardware.
- Goal: replace the Polycab dashboard with a self-hosted one.

## Strategy
**Plan A (do this first):** reverse-engineer the JSON API that `pv.polycabmonitoring.com`
already calls (the one the Vue SPA uses under the hood) and poll it ourselves.
No new hardware, fastest path to data.

**Plan B (fallback, only if Plan A's data is too sparse/coarse):** talk to the WiFi
datalogger stick directly on the LAN using the Solarman V5 protocol
(`pysolarmanv5` library) or tap the inverter's RS485 port directly. Fully local,
no cloud dependency. Don't build this yet — just confirm during Plan A recon
whether the datalogger is reachable on the LAN (note its IP/mDNS name if so,
for later).

## Deliverable shape
```
solar-monitor/
├── collector/
│   └── collect.py        # one-shot script: fetch latest reading -> insert into SQLite
├── alerts/
│   └── daily_alert.py     # one-shot script: summarize yesterday -> push notification
├── dashboard/
│   └── app.py              # Streamlit app, run manually, reads SQLite
├── db/
│   └── schema.sql
├── data/
│   └── readings.db          # created at runtime, gitignored
├── .env                      # credentials/config, gitignored
├── requirements.txt
└── README.md                 # setup + cron instructions
```

Collector and alert script are cron jobs (always-on box, e.g. Raspberry Pi/NAS/old PC).
Dashboard is started manually (`streamlit run dashboard/app.py`) and stopped when done —
never runs 24/7.

---

## Phase 0 — Recon (do this manually first, before writing code)

1. Open `pv.polycabmonitoring.com` in a desktop browser, log in, open DevTools → Network
   tab, filter by `Fetch/XHR`, and click into a station/device to view its live data page.
2. Identify:
   - The **login/auth call** — is it a simple POST with username/password? Does it
     return a bearer token / session cookie? Is there a token refresh flow?
   - The **station list call** and **device/realtime data call** — note the exact
     URL, method, headers (esp. `Authorization`), and full JSON response shape.
   - Look specifically for fields like: current power (W), today's yield (kWh),
     total yield (kWh), AC voltage/current/frequency, DC voltage/current per string,
     inverter temperature, fault/status codes, and the **update interval** the
     portal itself polls at (this tells us the real data granularity — Solarman
     backends are often only refreshed every 5–10 min even if the UI feels "live").
   - Note whether the response includes historical/time-series data (a "day curve"
     endpoint) — if so we can backfill history too, not just poll going forward.
3. Save 2–3 example raw JSON responses (station list, device realtime, and — if it
   exists — a historical/day-curve endpoint) into `recon/sample_responses/` for
   reference while building the collector.
4. Note whether the datalogger stick is visible on the local LAN (check router's
   DHCP client list for a device that looks like a "Solarman"/"LSW"/serial-number
   named entry). Not needed yet, just record it for Plan B.

**Acceptance for Phase 0:** we have a working curl/Python snippet that logs in and
returns current inverter data as JSON, and we know the real polling granularity.

---

## Phase 1 — Collector script

`collector/collect.py`:
- Reads credentials/station id from `.env` (never hardcode).
- Logs in (handle token expiry/refresh if the API needs it — cache the token to
  disk between runs if it's long-lived, to avoid re-authing every 5 minutes).
- Fetches current reading.
- Normalizes into a single row:
  `timestamp (UTC), pv_power_w, daily_yield_kwh, total_yield_kwh, ac_voltage,
  ac_current, ac_frequency, temperature_c, status, raw_json`
  (keep `raw_json` as a text column — cheap insurance if we later want a field
  we didn't originally parse out).
- Inserts into SQLite (`db/schema.sql` defines the `readings` table + an index on
  `timestamp`). Use `INSERT OR IGNORE`/upsert keyed on timestamp to make repeated
  runs idempotent.
- Logs failures clearly (auth failure vs network failure vs unexpected schema) to
  a rotating log file — this matters since it'll run unattended via cron.
- Script should run once and exit (not loop) — cron handles the "periodic" part.

**Acceptance:** running `python collect.py` manually appends exactly one new row
and exits 0; running it again a second later doesn't duplicate/crash.

## Phase 2 — Cron setup

- Add a cron entry (document in README, don't silently edit the user's crontab)
  running `collect.py` every 5 minutes, restricted to daylight hours e.g. `*/5 6-19 * * *`
  to avoid pointless calls at night.
- Use a venv + absolute paths in the cron line; redirect stdout/stderr to a logfile.

## Phase 3 — Dashboard (Streamlit)

`dashboard/app.py`:
- Reads `data/readings.db` (read-only connection).
- Sections:
  - Today: current power gauge, today's yield so far, a line chart of power over
    the day.
  - History: date-range picker, daily yield bar chart, best/worst day.
  - System health: last successful collector run timestamp (flag if stale >30 min
    during daylight — signals the collector or the API broke).
- No write access, no auth needed since it's local-only and run on demand.

**Acceptance:** `streamlit run dashboard/app.py` opens in browser and shows real
data once a few hours of collection have accumulated.

## Phase 4 — Daily alert

`alerts/daily_alert.py`:
- Run once daily via cron (e.g. 9 PM `0 21 * * *`).
- Queries the DB for the day's rows, computes: total kWh generated, peak power
  + time it occurred, any gap >20 min during daylight (possible inverter/collector
  outage), and comparison vs the trailing 7-day average.
- Sends a push notification. Default to **ntfy** (simplest — one HTTPS POST, free
  Android app, no account needed, self-hostable later if desired):
  ```python
  import requests
  requests.post("https://ntfy.sh/<your-private-topic>",
                 data=message.encode("utf-8"),
                 headers={"Title": "Solar daily summary"})
  ```
  Pick a random, unguessable topic name since public ntfy.sh topics aren't private
  by obscurity alone — treat the topic name like a secret, or self-host ntfy later.
  (Telegram bot is a fine alternative if preferred — swap this one function.)

**Acceptance:** running the script manually sends a real notification to the
Android ntfy app.

## Phase 5 — README

Document: environment setup (`venv`, `requirements.txt`), `.env` variables needed,
cron lines to add, how to run the dashboard, and — importantly — a short "Plan B
fallback" section describing pivoting to `pysolarmanv5` for local polling if the
cloud API proves too coarse or unreliable, including what we learned about LAN
reachability of the datalogger in Phase 0.

---

## Guardrails while building
- All credentials/tokens/topic names in `.env`, never committed.
- Collector must fail loudly but never crash-loop — respect basic backoff on
  repeated auth failures rather than hammering the login endpoint.
- Keep it to this stack: Python + `requests` + `sqlite3` + `streamlit` — resist
  the urge to reach for Docker/InfluxDB/Grafana for a 5-panel home system; add
  those later only if this outgrows SQLite.
