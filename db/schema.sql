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
CREATE INDEX IF NOT EXISTS idx_readings_source ON readings(source);

-- Singleton row tracking collector backoff state. Lives in the DB (not a local file)
-- because the collector runs on ephemeral compute (GitHub Actions) with no disk that
-- persists between runs.
CREATE TABLE IF NOT EXISTS collector_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT
);

-- Maintained top-10 producing days, so the dashboard/daily alert can just SELECT
-- instead of scanning+aggregating `readings` every time. Fully recomputed (not
-- incrementally patched) by the collector whenever a day's daily_yield_kwh is set or
-- changes - see collector/collect.py's update_top_days(). Two separate tables (not one
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
