# AGENTS.md

Context for a coding agent picking this project up in a future session. Read this before
changing anything - several things here look like they could be "simplified" or "fixed" by
someone unfamiliar with the history, but are deliberate.

## What this is

Self-hosted replacement for the Polycab/Solarman solar monitoring portal
(`pv.polycabmonitoring.com`), for a single grid-tied inverter (no battery/storage, no EV
charger - despite the portal's UI showing a "Storage" badge, this account has no actual
battery hardware). Plant went live **2026-07-18**; don't reintroduce data from before that
date (it was purged deliberately - see `README.md`).

Data flow: `collector/collect.py` and `alerts/daily_alert.py` run on **GitHub Actions**
(scheduled), writing to/reading from **Turso** (cloud SQLite, accessed via the `libsql`
Python package). `dashboard/app.py` (Streamlit, planned, not yet built) will be run manually
on demand, never 24/7.

## Things that look wrong but aren't

- **`libsql` only supports positional `?` params, not sqlite3's named `:key` params.**
  Discovered the hard way (a `ValueError: Expected a list or tuple for parameters`). If you
  add a new query, use positional params - `insert_rows()` in `collect.py` builds tuples in
  a fixed column order (`_READING_COLUMNS`) for exactly this reason.
- **Collector backoff state lives in a Turso table (`collector_state`), not a local file.**
  This was originally a local JSON file; it was moved to the DB specifically because the
  collector runs on ephemeral GitHub Actions runners - a local file would silently reset to
  zero every run, defeating the whole point of backoff. Don't move it back to a file.
- **`collect.yml`'s cron fires only 4 times/day, each kicking off a ~4h15m internal loop**
  (`sleep`-based, 5 min cadence), not ~150 separate 5-minute cron triggers. GitHub's
  `schedule:` trigger is unreliable at that frequency (can jitter by 10s of minutes); firing
  once and looping internally sidesteps that entirely.
- **Those 4 segments deliberately overlap by 1 hour each**, as redundancy against a
  late/missed kickoff. This means `collect.py` genuinely runs concurrently with itself during
  overlaps - **verified safe**, not just assumed: `INSERT OR IGNORE` (keyed on `timestamp`)
  and the `collector_state` row's `ON CONFLICT DO UPDATE` upsert are both atomic at the DB
  level. Tested by firing 4 truly concurrent processes at the same empty data - zero errors,
  zero duplicates. There's no `concurrency:` lock in the workflow - that's intentional, adding
  one back would force queuing instead of the intended parallel redundancy.
- **The Polycab API requires a `sign` field on every request** - an AES-256-CBC scheme
  reverse-engineered from the frontend JS bundle, implemented in `polycab_client.py`. Both
  keys are hardcoded in the vendor's own frontend, not session-specific. Full derivation is
  in `recon/notes.md` if this ever needs re-verifying (e.g. if the vendor rotates the keys).
- **The API returns the literal string `"-"` for unavailable numeric fields** (not `0` or
  `null`), e.g. when the inverter's been idle. Always pass API numeric fields through
  `polycab_client.num()` rather than `float()` directly, or you'll silently write a string
  into a `REAL` column (SQLite allows this without error, so it fails silently, not loudly).
- **Backfilled rows only ever populate `pv_power_w` + `timestamp`** (`source='backfill'`).
  The day-curve endpoint (`getAllPacDay_v1`) only ever gives instantaneous power, never
  voltage/current/frequency/temperature or true cumulative yield - don't assume those columns
  are populated without checking `source`.
- **`TURSO_API_TOKEN` is intentionally not a GitHub Actions secret.** It's a
  platform/management-scope token (decodes to `{org_id}`, no database scope) used only for
  one-time manual provisioning (creating the group/database/token via `api.turso.tech`) -
  `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` are what the scripts actually connect with.

## Key identifiers (this specific account, not generic)

- `GOODS_ID` (inverter serial): `2620-119401326P`, model `PSIS-5K0`
- `groupID` (plant/station id): `516036`
- `MemberAutoID`: `1137916`
- Location: Mangadu, Chennai (`WEATHER_LAT=13.016`, `WEATHER_LON=80.097`)
- Panels: 5x Waaree 610W (~3.05 kWp); inverter rated capacity shown as 5kW/5000W depending
  on endpoint (known cosmetic unit inconsistency, see `recon/notes.md`)

## Where to look for more detail

- `recon/notes.md` - full API reverse-engineering writeup: every endpoint found, exact
  params, the complete signing algorithm derivation, open questions (no fault-code endpoint
  found yet, real hardware polling interval not empirically confirmed, LAN datalogger
  reachability for a Plan B fallback never checked).
- `plan.md` - original project plan. Mostly executed; historical context only at this point.
- `README.md` - user-facing setup/run instructions for both local and GitHub Actions.

## Testing approach so far

No automated test suite exists. Everything has been validated by running the real scripts
against the live Turso DB and the live Polycab API directly (see conversation history /
commit messages for specifics: idempotency re-runs, simulated outage/gap recovery, auth
failure -> backoff -> recovery cycles, and the 4-way concurrency race). If you add automated
tests, keep using the real `polycab_client.call()` signing logic rather than mocking it away
- the signing scheme is exactly the kind of thing that's easy to accidentally break in a way
mocks wouldn't catch.

## `dashboard/app.py` - agreed design, not yet implemented

Streamlit, run manually (`streamlit run dashboard/app.py`), read-only against Turso, never
run 24/7. Two tabs:

### Today tab
| Module | Data points | Source |
|---|---|---|
| Current Production | current power (W), today's yield (kWh), health status, last-update time | **API** live call: `InverterDetailInfoNewone` + `GroupList` (same shape as `collect.py`'s `fetch_live_reading`) |
| Technical Specs | inverter model, serial, firmware | **API** (same live call, no extra request) |
| | panel count x wattage (5x610W Waaree), total kWp, install date (2026-07-18) | **Static config** - not available from the API |
| Collector health | last successful poll timestamp, stale flag if >30 min during daylight | **DB** - `MAX(timestamp)` from `readings` |
| Intraday production chart | 5-min power (W) for today | **DB** - `readings` filtered to today's IST date (both `live`+`backfill` rows), **fixed x-axis 06:00-19:00 IST** (confirmed - not auto-scaled to available data) |
| Temperature overlay (toggle) | hourly ambient temp today | **Weather API** (Open-Meteo forecast endpoint) - ambient/outdoor temp, *not* inverter `temperature_c` (deliberate choice, for consistency with the Month tab's weather correlation view) |

### Month tab
| Module | Data points | Source |
|---|---|---|
| GitHub-style heatmap calendar | daily total kWh, one cell/day, past 12 months | **DB** - `MAX(daily_yield_kwh)` per IST day from `readings`. Days with no `live` rows render empty/no-data - expect most of the 12-month grid to be empty initially since the plant is new (2026-07-18) and the collector only started recently. This is correct, not a bug. |
| Temperature per day (tooltip) | daily mean/max temp, past 12 months | **Weather API** (Open-Meteo **archive** endpoint - one single call for the whole year range, cache ~24h) |

Chart library: **Altair** (already bundled with Streamlit, no new dependency) - needed for
the dual-independent-y-axis overlay (`resolve_scale(y='independent')`) and for a DIY
GitHub-style calendar heatmap (`mark_rect`, week-of-year x day-of-week grid) since Streamlit
has no native widget for either.

Caching: live API calls via `st.cache_data(ttl=60)` (so UI interactions like toggling the
temperature overlay don't refire API calls every rerun); DB/history queries cached longer.
