from pathlib import Path

import libsql
import pytest

SCHEMA = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()


@pytest.fixture
def db():
    """In-memory DB with the real schema, through the real libsql driver - so tests catch
    things like named (`:key`) params, which libsql rejects but sqlite3 would accept."""
    conn = libsql.connect(":memory:")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()
