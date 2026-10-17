"""
Weekly Turso -> gzipped plain-text SQL backup. Run by the backup workflow in the private
`aviiciii/solar-dashboard-backups` repo (see README), which publishes each dump as a
GitHub Release there and then runs backup/prune.py.

Copies every table + index from Turso into a local SQLite database, sanity-checks it, and
writes it out as a gzipped `.sql` dump (CREATE + INSERT statements):

    uv run backup/dump.py path/to/backup.sql.gz [--prev-rows "rows: readings=23949 ..."]

Restore with `gunzip -c backup.sql.gz | sqlite3 restored.db` (or pipe it into
`turso db shell <db>`).

Line 2 of the dump is a `-- rows: table=count ...` summary; the workflow copies it into
the release notes and passes the previous release's notes back in as --prev-rows, so a
big drop in `readings` (data loss upstream) fails the run instead of quietly publishing.

Connects with TURSO_BACKUP_TOKEN - a read-only token, so this script can't modify the
live DB even by mistake. Rows are fetched in rowid-keyset pages, so each backup reads
every row exactly once (~24k rows as of 2026-10) - negligible against the free tier's
500M rows-read/month, and unlike the per-poll scans AGENTS.md warns about, it runs weekly.
"""

import argparse
import gzip
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PAGE_SIZE = 5000
# Refuse to publish if `readings` shrank by more than this since the last backup - that
# would mean data loss upstream. Not a strict "never shrinks": the Admin page's re-backfill
# deletes and re-inserts a day's rows, which can legitimately drop the count a little.
MAX_READINGS_SHRINK = 0.10


def fetch_schema(conn) -> list[tuple[str, str, str]]:
    """(type, name, sql) for every user table + index, tables first. Skips SQLite's and
    libSQL's own internal objects, and auto-indexes (those have NULL sql)."""
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE type IN ('table', 'index') AND sql IS NOT NULL"
        " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'libsql_%'"
        " ORDER BY type = 'index', name"
    ).fetchall()


def copy_table(remote, local: sqlite3.Connection, table: str) -> int:
    """Keyset-paginate on rowid (every table here is a rowid table), so each page is an
    index seek rather than an OFFSET re-scan - rows read == rows copied."""
    copied, last_rowid = 0, None
    while True:
        if last_rowid is None:
            page = remote.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid LIMIT ?', (PAGE_SIZE,)).fetchall()
        else:
            page = remote.execute(
                f'SELECT rowid, * FROM "{table}" WHERE rowid > ? ORDER BY rowid LIMIT ?', (last_rowid, PAGE_SIZE)
            ).fetchall()
        if not page:
            return copied
        placeholders = ", ".join("?" * (len(page[0]) - 1))
        local.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', [row[1:] for row in page])
        copied += len(page)
        last_rowid = page[-1][0]


def parse_row_counts(text: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", text)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, help="where to write the gzipped .sql dump")
    parser.add_argument("--prev-rows", default="", help="the previous backup's `rows: ...` summary line")
    args = parser.parse_args()

    remote = libsql.connect(os.environ["TURSO_DATABASE_URL"], auth_token=os.environ["TURSO_BACKUP_TOKEN"])
    local = sqlite3.connect(":memory:")

    schema = fetch_schema(remote)
    tables = [name for type_, name, _ in schema if type_ == "table"]
    if "readings" not in tables:
        logging.error("no `readings` table in the remote schema - refusing to write a backup")
        return 1

    # Tables, then data, then indexes - building indexes once after the bulk insert is
    # cheaper than maintaining them row by row.
    for type_, _, sql in schema:
        if type_ == "table":
            local.execute(sql)
    counts = {table: copy_table(remote, local, table) for table in tables}
    for type_, _, sql in schema:
        if type_ == "index":
            local.execute(sql)
    local.commit()
    rows_line = "rows: " + " ".join(f"{t}={n}" for t, n in counts.items())
    logging.info(rows_line)

    integrity = local.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        logging.error("integrity_check failed on the local copy: %s", integrity)
        return 1
    if counts["readings"] == 0:
        logging.error("`readings` came back empty - refusing to write a backup")
        return 1
    prev_readings = parse_row_counts(args.prev_rows).get("readings")
    if prev_readings and counts["readings"] < prev_readings * (1 - MAX_READINGS_SHRINK):
        logging.error(
            "`readings` shrank from %d to %d rows since the last backup - possible data loss "
            "upstream, refusing to write a backup", prev_readings, counts["readings"],
        )
        return 1

    with gzip.open(args.output, "wt", compresslevel=9) as f:
        f.write(f"-- solar-dashboard Turso backup, taken {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        f.write(f"-- {rows_line}\n")
        for line in local.iterdump():
            f.write(line + "\n")
    logging.info("wrote %s (%.2f MB)", args.output, args.output.stat().st_size / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
