CREATE TABLE IF NOT EXISTS readings (
    timestamp TEXT PRIMARY KEY,   -- ISO8601 UTC, e.g. 2026-07-20T15:43:18+00:00
    pv_power_w REAL,
    daily_yield_kwh REAL,
    total_yield_kwh REAL,
    ac_voltage REAL,
    ac_current REAL,
    ac_frequency REAL,
    temperature_c REAL,
    status TEXT,                  -- normal / standby / abnormal / offline (NULL for backfilled rows)
    source TEXT NOT NULL,         -- 'live' (polled in real time) or 'backfill' (recovered after the fact)
    raw_json TEXT
);

-- PRIMARY KEY on timestamp already creates a unique index, which is what makes
-- `INSERT OR IGNORE` an idempotent upsert keyed on timestamp.
-- (source, timestamp) rather than just (source): lets the dashboard's
-- `MAX(timestamp) WHERE source = 'live'` resolve as a single index lookup instead of
-- reading every live row. Superseded the old single-column idx_readings_source.
DROP INDEX IF EXISTS idx_readings_source;
CREATE INDEX IF NOT EXISTS idx_readings_source_ts ON readings(source, timestamp);

-- Singleton row tracking collector backoff state. Lives in the DB (not a local file)
-- because the collector runs on ephemeral compute (GitHub Actions) with no disk that
-- persists between runs.
CREATE TABLE IF NOT EXISTS collector_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT
);

-- One row per day: that day's total kWh (the max daily_yield_kwh seen for it). Keyed
-- by the UTC-date substring of readings.timestamp - same convention as the rest of the
-- code (our daylight-only window never crosses UTC midnight, so UTC-date == IST-date).
-- Exists purely to keep Turso's rows-read quota down: aggregating per-day totals out of
-- `readings` needs a full-table scan (substr() can't use the PK index), and doing that
-- on every 5-min poll burned ~400M rows/month. Upserted by the collector on every
-- reading that carries a daily_yield_kwh - see collector/collect.py's
-- record_daily_total(). Initially populated from `readings` once via:
--   INSERT INTO daily_totals (date, kwh)
--   SELECT substr(timestamp, 1, 10), MAX(daily_yield_kwh) FROM readings
--   WHERE daily_yield_kwh IS NOT NULL GROUP BY 1
--   ON CONFLICT (date) DO UPDATE SET kwh = MAX(kwh, excluded.kwh);
CREATE TABLE IF NOT EXISTS daily_totals (
    date TEXT PRIMARY KEY,
    kwh REAL NOT NULL
);
-- Lets the all-time top-10 (`ORDER BY kwh DESC LIMIT 10`) read ~10 rows instead of
-- every day ever recorded. `date` breaks ties deterministically (earlier day ranks higher).
CREATE INDEX IF NOT EXISTS idx_daily_totals_kwh ON daily_totals(kwh DESC, date);

-- Maintained top-10 producing days, so the dashboard/daily alert can just SELECT
-- instead of scanning+aggregating `readings` every time. Fully recomputed (not
-- incrementally patched) by the collector whenever a day's daily_yield_kwh is set or
-- changes - see collector/collect.py's update_top_days(). Computed from
-- daily_totals, never from `readings` directly. Two separate tables (not one
-- table with a nullable "year" for the all-time scope) to avoid nullable-PK awkwardness.
CREATE TABLE IF NOT EXISTS top_days_yearly (
    year INTEGER NOT NULL,
    rank INTEGER NOT NULL,   -- 1 = best day of that year
    date TEXT NOT NULL,
    kwh REAL NOT NULL,
    PRIMARY KEY (year, rank)
);

CREATE TABLE IF NOT EXISTS top_days_alltime (
    rank INTEGER NOT NULL PRIMARY KEY,  -- 1 = best day ever
    date TEXT NOT NULL,
    kwh REAL NOT NULL
);
