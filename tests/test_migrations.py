from simq import db
from tests.conftest import TEST_DSN


def test_migrations_idempotent(dsn):
    # Session fixture already applied them; a second run applies nothing.
    assert db.run_migrations(TEST_DSN) == []


def test_schema_present(conn):
    tables = {
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public'"
        ).fetchall()
    }
    assert {"jobs", "job_events", "schema_migrations"} <= tables
