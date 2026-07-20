# Solar Dashboard

Self-hosted monitoring for a 5x Waaree 610W (~3.05 kWp) system on a Polycab PSIS-5K0
inverter, replacing the vendor's `pv.polycabmonitoring.com` portal. Instead of new
hardware, this polls the same JSON API the vendor's own web app uses (reverse-engineered
in `recon/`), storing readings in a cloud SQLite (Turso) database so collection can run
on free GitHub Actions instead of needing an always-on local box.

The plant went live **2026-07-18**. Data recorded before that (the account's setup/testing
period, 2026-07-15 to 2026-07-17) has been purged and is excluded from backfill.

## Architecture

```
GitHub Actions (cron)                                    Run manually, on demand
┌─────────────────────┐                                  ┌──────────────────────┐
│ collector/collect.py │──┐                            ┌─▶│ dashboard/app.py      │
└─────────────────────┘  │                             │  │ (Streamlit, planned - │
┌─────────────────────┐  ├──▶  Turso (cloud SQLite)  ───┤  │  not yet built)       │
│ alerts/daily_alert.py│──┘        `readings` table     │  └──────────────────────┘
└─────────────────────┘                                 │
         │                                               └─▶ ntfy.sh push notification
         ▼
  pv.polycabmonitoring.com API (polycab_client.py)   +   Open-Meteo weather API (free, no key)
```

## Modules

### `polycab_client.py`
Shared HTTP client for the Polycab/Solarman JSON API. Handles the request-signing scheme
(AES-256-CBC over a canonical sorted query string, reverse-engineered from the vendor's
frontend JS bundle - see `recon/notes.md` for the full derivation) and categorizes failures
into `AuthError` / `NetworkError` / `SchemaError`. Used by both `collector/collect.py` and
`alerts/daily_alert.py` - don't duplicate this logic elsewhere.

### `collector/collect.py`
One-shot script: poll the inverter's current reading, backfill any missing history, insert
into Turso. Run once per invocation - a scheduler handles repetition (see below).
- Live poll: `InverterDetailInfoNewone` (AC/DC voltage/current/frequency, temperature, energy
  totals) + `GroupList` (health status).
- Backfill: `getAllPacDay_v1` (5-min power curve) for any day since the last successful poll
  that's missing data, or since `BACKFILL_START_DATE` on first run. Backfilled rows only ever
  have `pv_power_w` + `timestamp` (`source='backfill'`) - the finer electrical detail is only
  available live (`source='live'`).
- Every insert is `INSERT OR IGNORE` keyed on `timestamp`, so re-running (including
  re-backfilling an already-partly-filled day) never duplicates rows.
- Failure backoff state lives in Turso's `collector_state` table (not a local file), since
  this runs on ephemeral GitHub Actions runners with no persistent disk between runs.

### `alerts/daily_alert.py`
One-shot script: sends a push notification (via ntfy.sh) summarizing the day.
- Total production (kWh), % vs yesterday, % vs trailing 7-day average - from `getAllPacMonth`
  (authoritative daily totals from Polycab, not derived from our own readings).
- Peak power + time it occurred - from `getAllPacDay_v1`.
- Any >20 min data gap during daylight hours (06:00-19:00 IST) - checked against our own
  `readings` table, flags a likely collector/inverter outage.
- Today's weather (condition, temp range, sunshine hours, cloud cover, rain) - Open-Meteo,
  no API key needed.

### `dashboard/app.py` - **planned, not yet built**
Streamlit app, run manually and stopped when done (never runs 24/7). See `AGENTS.md` for
the full agreed design (Today tab + Month tab, data sources, and the specific decisions
already made about temperature source, chart library, etc.) before implementing this.

### `db/schema.sql`
Two tables in Turso: `readings` (the actual data, `source` column distinguishes
`live`/`backfill` provenance) and `collector_state` (singleton row for backoff bookkeeping).

### `recon/`
Reverse-engineering documentation: `notes.md` (full writeup of the API, auth, and signing
scheme), `api_client.py` (throwaway proof-of-concept predating `polycab_client.py`),
`sample_responses/` (real captured JSON for reference).

### `plan.md`
The original project plan this was built from. Mostly executed at this point; kept for
historical context.

## Running locally

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                          # installs deps into .venv, using pyproject.toml + uv.lock
cp .env.example .env              # fill in real values - see the comments in that file
uv run collector/collect.py       # one-shot: poll + backfill + insert
uv run alerts/daily_alert.py       # one-shot: send today's summary notification
# uv run streamlit run dashboard/app.py   # once built
```

`.env` is gitignored and never committed. It needs (see `.env.example` for exactly how to
obtain each of these):
- `POLYCAB_TOKEN`, `GOODS_ID`, `MEMBER_AUTO_ID` - Polycab account/device identifiers
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` - the cloud database connection
- `TURSO_API_TOKEN` - only needed if re-provisioning Turso resources, not read by any script
- `NTFY_TOPIC`, `WEATHER_LAT`, `WEATHER_LON` - for the daily alert
- `BACKFILL_START_DATE=2026-07-18` - the true go-live date; don't change this back further

## Running in GitHub Actions / the cloud

Two workflows, both using `uv sync --locked` + `uv run <script>`:

- **`.github/workflows/collect.yml`** - polls every 5 minutes during IST daylight hours
  (06:00-19:00). GitHub's `schedule:` trigger is unreliable for ~150 precise firings/day, so
  instead there are only **4 daily kickoffs**, each looping internally (real `sleep`, not
  GitHub's queue) every 5 minutes for its ~4h15m segment. Segments **overlap by 1 hour** at
  each boundary as redundancy against a late/missed kickoff - this means `collect.py` *will*
  run concurrently during those windows, which is safe by design (verified: `INSERT OR
  IGNORE` + an `ON CONFLICT DO UPDATE` upsert on the single `collector_state` row are both
  atomic; tested with 4 truly concurrent processes racing on the same empty data with zero
  errors or duplicates).
- **`.github/workflows/daily_alert.yml`** - single daily cron at 21:00 IST (15:30 UTC). A
  once-a-day trigger doesn't hit the same jitter problem, so no special handling needed.

Both need these **repository secrets** (Settings -> Secrets and variables -> Actions):
`POLYCAB_TOKEN`, `GOODS_ID`, `MEMBER_AUTO_ID`, `BACKFILL_START_DATE`, `TURSO_DATABASE_URL`,
`TURSO_AUTH_TOKEN`, `NTFY_TOPIC`, `WEATHER_LAT`, `WEATHER_LON`. (`TURSO_API_TOKEN` is
deliberately **not** a secret here - it's a platform/management token only used for one-time
manual provisioning, never read by any script.)

Both are manually triggerable too: `gh workflow run collect.yml` / `gh workflow run
daily_alert.yml`, or via the Actions tab.

## Data model

`readings` table (Turso):

| Column | Meaning |
|---|---|
| `timestamp` | ISO8601 UTC, primary key |
| `pv_power_w` | Instantaneous AC power (W) |
| `daily_yield_kwh` / `total_yield_kwh` | Cumulative energy for the day / lifetime (kWh) - `live` rows only |
| `ac_voltage` / `ac_current` / `ac_frequency` | `live` rows only |
| `temperature_c` | Inverter temperature - `live` rows only |
| `status` | `normal` / `standby` / `abnormal` / `offline` - `live` rows only |
| `source` | `live` (polled in real time) or `backfill` (recovered after the fact) |
| `raw_json` | Full API response for that reading, for fields not otherwise parsed out |

## Known limitations

See `recon/notes.md`'s "Open questions" section for the full list (no fault/error-code
endpoint found yet, real hardware reporting interval not empirically confirmed, LAN
datalogger reachability for a Plan B fallback not checked). None of these block current
functionality.

## Plan B (fallback, not built)

If the cloud API ever proves too coarse/unreliable: pivot to polling the WiFi datalogger
stick directly on the LAN via the Solarman V5 protocol (`pysolarmanv5`), or tap the
inverter's RS485 port directly. See `plan.md` for context - this was never needed since
Plan A (this repo) worked out.
