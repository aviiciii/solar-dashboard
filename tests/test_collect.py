from collect import (backoff_minutes_for, _daterange, insert_rows, integrate_kwh,
                     record_daily_total, update_top_days, _READING_COLUMNS)


def reading(ts: str, w: float | None, kwh: float | None = None, source: str = "live") -> dict:
    row = dict.fromkeys(_READING_COLUMNS)
    row.update(timestamp=ts, pv_power_w=w, daily_yield_kwh=kwh, source=source, raw_json="{}")
    return row


def test_integrate_kwh_trapezoid():
    rows = [reading("2026-10-10T06:00:00+00:00", 0),
            reading("2026-10-10T07:00:00+00:00", 1000),
            reading("2026-10-10T08:00:00+00:00", 1000)]
    assert integrate_kwh(rows) == 1.5  # 0.5 kWh ramp + 1 kWh flat


def test_integrate_kwh_treats_missing_power_as_zero():
    rows = [reading("2026-10-10T06:00:00+00:00", None), reading("2026-10-10T08:00:00+00:00", 1000)]
    assert integrate_kwh(rows) == 1.0


def test_backoff_doubles_and_caps():
    assert [backoff_minutes_for(n) for n in range(7)] == [0, 5, 10, 20, 40, 60, 60]


def test_daterange_inclusive_and_empty():
    from datetime import date
    assert _daterange(date(2026, 10, 30), date(2026, 11, 1)) == ["2026-10-30", "2026-10-31", "2026-11-01"]
    assert _daterange(date(2026, 11, 2), date(2026, 11, 1)) == []


def test_insert_rows_is_idempotent(db):
    rows = [reading("2026-10-10T06:00:00+00:00", 100), reading("2026-10-10T06:05:00+00:00", 200)]
    assert insert_rows(db, rows) == 2
    assert insert_rows(db, rows) == 0
    assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 2


def test_record_daily_total_only_raises_by_default(db):
    assert record_daily_total(db, "2026-10-10", 5.0)
    assert record_daily_total(db, "2026-10-10", 6.0)
    assert not record_daily_total(db, "2026-10-10", 4.0)
    assert not record_daily_total(db, "2026-10-10", 6.0)
    assert db.execute("SELECT kwh FROM daily_totals").fetchone()[0] == 6.0


def test_record_daily_total_overwrite_can_lower(db):
    record_daily_total(db, "2026-10-10", 6.0)
    assert record_daily_total(db, "2026-10-10", 4.0, overwrite=True)
    assert not record_daily_total(db, "2026-10-10", 4.0, overwrite=True)
    assert db.execute("SELECT kwh FROM daily_totals").fetchone()[0] == 4.0


def test_update_top_days_ranks_by_kwh_then_date(db):
    for d, kwh in [("2025-12-31", 20.0), ("2026-01-02", 9.0), ("2026-01-01", 9.0), ("2026-01-03", 12.0)]:
        record_daily_total(db, d, kwh)
    update_top_days(db, "2026-01-03")
    assert db.execute("SELECT rank, date FROM top_days_yearly WHERE year = 2026 ORDER BY rank").fetchall() == [
        (1, "2026-01-03"), (2, "2026-01-01"), (3, "2026-01-02")]
    assert db.execute("SELECT rank, date FROM top_days_alltime ORDER BY rank").fetchall() == [
        (1, "2025-12-31"), (2, "2026-01-03"), (3, "2026-01-01"), (4, "2026-01-02")]


def test_update_top_days_keeps_only_ten(db):
    for n in range(1, 13):
        record_daily_total(db, f"2026-02-{n:02d}", float(n))
    update_top_days(db, "2026-02-12")
    ranked = db.execute("SELECT date FROM top_days_alltime ORDER BY rank").fetchall()
    assert len(ranked) == 10 and ranked[0][0] == "2026-02-12" and ranked[-1][0] == "2026-02-03"


def test_uncommitted_writes_roll_back(db):
    """The Admin re-backfill relies on commit=False leaving everything in one transaction."""
    record_daily_total(db, "2026-10-10", 5.0)
    insert_rows(db, [reading("2026-10-10T06:00:00+00:00", 100)], commit=False)
    record_daily_total(db, "2026-10-10", 1.0, overwrite=True, commit=False)
    update_top_days(db, "2026-10-10", commit=False)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    assert db.execute("SELECT kwh FROM daily_totals").fetchone()[0] == 5.0
    assert db.execute("SELECT COUNT(*) FROM top_days_alltime").fetchone()[0] == 0
