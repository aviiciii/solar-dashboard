import sqlite3
from datetime import date, timedelta

import libsql

from dump import copy_table, fetch_schema, parse_row_counts
from prune import pick_deletions


def weekly_backups(first: date, last: date, size: int = 1_000_000) -> list[dict]:
    backups, d = [], first
    while d <= last:
        backups.append({"tag": f"backup-{d}", "day": d, "size": size})
        d += timedelta(days=7)
    return backups


def kept(backups, deletions):
    gone = {b["tag"] for b in deletions}
    return [b["day"].isoformat() for b in backups if b["tag"] not in gone]


def test_prune_deletes_nothing_under_budget():
    backups = weekly_backups(date(2026, 7, 5), date(2026, 10, 18))
    assert pick_deletions(backups, 100_000_000, date(2026, 10, 18)) == ([], 16_000_000)


def test_prune_thins_oldest_month_first_keeping_its_last_week():
    backups = weekly_backups(date(2026, 7, 5), date(2026, 10, 18))
    deletions, total = pick_deletions(backups, 12_000_000, date(2026, 10, 18))
    assert [b["day"].isoformat() for b in deletions] == ["2026-07-05", "2026-07-12", "2026-07-19", "2026-08-02"]
    assert total == 12_000_000
    assert "2026-07-26" in kept(backups, deletions)  # July keeps its last backup


def test_prune_never_touches_current_month_or_a_months_last_backup():
    backups = weekly_backups(date(2026, 7, 5), date(2026, 10, 18))
    deletions, total = pick_deletions(backups, 1, date(2026, 10, 18))
    remaining = kept(backups, deletions)
    assert remaining == ["2026-07-26", "2026-08-30", "2026-09-27", "2026-10-04", "2026-10-11", "2026-10-18"]
    assert total > 1  # caller turns this into a non-zero exit instead of deleting more


def test_parse_row_counts():
    assert parse_row_counts("rows: readings=23949 daily_totals=92") == {"readings": 23949, "daily_totals": 92}
    assert parse_row_counts("") == {}


def test_dump_copies_every_row_across_pages(db, monkeypatch):
    import dump
    monkeypatch.setattr(dump, "PAGE_SIZE", 3)  # force several keyset pages
    db.executemany("INSERT INTO daily_totals (date, kwh) VALUES (?, ?)",
                   [(f"2026-08-{n:02d}", float(n)) for n in range(1, 11)])
    db.commit()

    local = sqlite3.connect(":memory:")
    schema = fetch_schema(db)
    for type_, _, sql in schema:
        if type_ == "table":
            local.execute(sql)
    assert copy_table(db, local, "daily_totals") == 10
    assert local.execute("SELECT COUNT(*), SUM(kwh) FROM daily_totals").fetchone() == (10, 55.0)
    assert "idx_readings_source_ts" in [name for type_, name, _ in schema if type_ == "index"]
