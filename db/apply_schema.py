"""
Applies db/schema.sql to the Turso DB (idempotent: CREATE ... IF NOT EXISTS, plus the
one-off DROP INDEX IF EXISTS migrations in it). collect.yml runs this once per job, before
polling starts - collect.py itself no longer does, since re-running ~10 statements on
every 5-min poll was pure overhead. Run it by hand after creating a fresh database or
editing schema.sql:

    uv run db/apply_schema.py
"""

import os
import sys
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    conn = libsql.connect(os.environ["TURSO_DATABASE_URL"], auth_token=os.environ["TURSO_AUTH_TOKEN"])
    conn.executescript((PROJECT_ROOT / "db" / "schema.sql").read_text())
    conn.commit()
    conn.close()
    print("schema applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
