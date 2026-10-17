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
GitHub Actions                                           Streamlit Community Cloud
┌──────────────────────┐                                 ┌──────────────────────┐
│ collector/collect.py  │──┐                           ┌─▶│ dashboard/app.py      │
│ (via run_loop.py)     │  │                           │  │ (also runs locally)   │
└──────────────────────┘  ├──▶  Turso (cloud SQLite) ──┤  └──────────────────────┘
┌──────────────────────┐  │                           │
│ alerts/daily_alert.py │──┘                           └─▶ ntfy.sh push notification
└──────────────────────┘
         │
         ▼
  pv.polycabmonitoring.com API (polycab_client.py)   +   Open-Meteo weather API (free, no key)
```

## Modules

### `polycab_client.py`
Shared HTTP client for the Polycab/Solarman JSON API. Handles the request-signing scheme
(AES-256-CBC over a canonical sorted query string, reverse-engineered from the vendor's
frontend JS bundle - see `recon/notes.md` for the full derivation) and categorizes failures
into `AuthError` / `NetworkError` / `SchemaError`. `fetch_live()` parses the inverter's
current reading + status (including the device-clock timezone fix) for both the collector
and the dashboard - don't duplicate this logic elsewhere. Small cross-cutting helpers
(timezones, logging setup, % change, kWh integration) live in `common.py`.

### `collector/collect.py`
One-shot script: poll the inverter's current reading, backfill any missing history, insert
into Turso. Run once per invocation - `collector/run_loop.py` repeats it every 5 minutes
(used by `collect.yml`, and locally to cover a window by hand: `uv run collector/run_loop.py
--until 17:00`).
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

### `dashboard/app.py`
Streamlit app, deployed on Streamlit Community Cloud (public URL in the `DASHBOARD_URL`
secret; `collect.yml` pings it so it doesn't sleep) and runnable locally. Tabs:
- **Today** - live snapshot from the Polycab API (power, yield, status, firmware), collector
  health, week/month average KPIs, intraday power + cumulative-energy charts with a "this
  week's average" overlay and an optional ambient-temperature overlay.
- **Day** - the same charts and KPIs for any past date, from our own DB.
- **Month** - GitHub-style calendar heatmap of daily kWh (with temperature tooltips and
  top-10 day icons) plus the all-time top-10 table.
- **Admin** - password-gated (`ADMIN_PASSWORD`) re-backfill of a single day, for fixing a
  day whose readings an outage corrupted.

### `db/schema.sql`
Applied by `db/apply_schema.py` (once per collector job, or by hand after editing it - not
on every poll). Tables in Turso: `readings` (the actual data, `source` column distinguishes
`live`/`backfill` provenance), `collector_state` (singleton row for backoff bookkeeping),
`daily_totals` (one row/day of total kWh, maintained by the collector), and
`top_days_yearly`/`top_days_alltime` (top-10 rankings computed from `daily_totals`).

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
uv run db/apply_schema.py          # once for a fresh DB, or after editing db/schema.sql
uv run collector/collect.py       # one-shot: poll + backfill + insert
uv run collector/run_loop.py      # poll every 5 min until 19:00 IST
uv run alerts/daily_alert.py       # one-shot: send today's summary notification
uv run streamlit run dashboard/app.py
uv run pytest                      # test suite (no network needed)
```

`.env` is gitignored and never committed. It needs (see `.env.example` for exactly how to
obtain each of these):
- `POLYCAB_TOKEN`, `GOODS_ID`, `MEMBER_AUTO_ID` - Polycab account/device identifiers
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` - the cloud database connection
- `TURSO_API_TOKEN` - only needed if re-provisioning Turso resources, not read by any script
- `NTFY_TOPIC`, `WEATHER_LAT`, `WEATHER_LON` - for the daily alert
- `BACKFILL_START_DATE=2026-07-18` - the true go-live date; don't change this back further

## Running in GitHub Actions / the cloud

Workflows, all using `uv sync --locked` + `uv run <script>`:

- **`.github/workflows/collect.yml`** - polls every 5 minutes through 05:30-19:00 IST as a
  **chain of runs**: each run loops internally (`collector/run_loop.py`, real `sleep`) for
  up to 340 min (GitHub kills jobs at 6h), then dispatches its own successor via `gh
  workflow run` ~10 min before its cap. GitHub's `schedule:` trigger has been starting runs
  3.5-4.5h late, but `workflow_dispatch` starts within seconds, so only the first run of the
  day depends on the scheduler. The `schedule:` entries just start (or revive) the chain and
  exit immediately if a run is already in progress. Successive runs overlap briefly, so
  `collect.py` *does* run concurrently with itself - safe by design (verified: `INSERT OR
  IGNORE` + an `ON CONFLICT DO UPDATE` upsert on the single `collector_state` row are both
  atomic; tested with 4 truly concurrent processes racing on the same empty data with zero
  errors or duplicates). Manual run: `gh workflow run collect.yml` polls once; add
  `-f mode=chain` to start a chain.
- **`.github/workflows/daily_alert.yml`** - single daily cron at 21:00 IST (15:30 UTC). GitHub
  often starts it 1-4h late (sometimes past IST midnight), so `daily_alert.py` treats any
  run before 06:00 IST as a late run for the previous day. To re-send a specific day, run it
  manually with the `report_date` input (or `REPORT_DATE=YYYY-MM-DD` locally).

They need these **repository secrets** (Settings -> Secrets and variables -> Actions):
`POLYCAB_TOKEN`, `GOODS_ID`, `MEMBER_AUTO_ID`, `BACKFILL_START_DATE`, `TURSO_DATABASE_URL`,
`TURSO_AUTH_TOKEN`, `NTFY_TOPIC`, `WEATHER_LAT`, `WEATHER_LON`, `DASHBOARD_URL`. (`TURSO_API_TOKEN` is
deliberately **not** a secret here - it's a platform/management token only used for one-time
manual provisioning, never read by any script.)

Both are manually triggerable too (`gh workflow run daily_alert.yml`, or the Actions tab).

**`.github/workflows/keepalive.yml`** pushes an empty `keepalive: YYYY-MM` commit to `main`
on the 1st of each month. GitHub disables scheduled workflows in public repos after 60 days
with no repo activity, and the workflows above don't create any. `git pull` before
pushing local work.

### Weekly backup

The workflow lives in the **private** `aviiciii/solar-dashboard-backups` repo, not here
(this repo is public). Sundays 23:00 IST it checks out this repo, runs `backup/dump.py`
(whole Turso DB -> gzipped plain-text SQL, ~0.7 MB as of 2026-10) and publishes the dump as
a GitHub Release there, tagged `backup-YYYY-MM-DD`. It refuses to publish (and alerts via
ntfy) if `readings` shrank >10% since the previous backup.

Retention (`backup/prune.py`): nothing is deleted while all backups fit in 1 GB; past that,
past months are thinned oldest-first to one backup each (that month's last), only until it
fits again. The current month is never thinned and no month ever loses its last backup.

Its secrets live in that repo: `TURSO_DATABASE_URL`, `TURSO_BACKUP_TOKEN` (read-only Turso
token, see `.env.example`), `NTFY_TOPIC`.

Restore: `gh release download backup-YYYY-MM-DD -R aviiciii/solar-dashboard-backups`, then
`gunzip -c solar-dashboard-YYYY-MM-DD.sql.gz | sqlite3 restored.db` (or pipe into
`turso db shell <db>`). Locally: `uv run backup/dump.py some/path/backup.sql.gz`.

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
