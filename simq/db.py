"""Connection pool and migration runner.

Migrations are plain .sql files applied in filename order. The runner takes
a Postgres advisory lock first, so several processes (API + N workers) can
all run migrations at startup without racing; whoever wins applies pending
files, everyone else waits and then sees them as applied.
"""

from __future__ import annotations

import logging
import pathlib

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("simq.db")

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "migrations"
# Arbitrary but fixed application-wide advisory lock key for migrations.
_MIGRATION_LOCK_KEY = 0x51_4D_51  # "SQM"

_pool: ConnectionPool | None = None


def get_pool(dsn: str) -> ConnectionPool:
    """Process-wide pool, created lazily. dict_row so rows behave as dicts."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=10,
            # autocommit=True is deliberate and load-bearing: single reads
            # commit immediately (no idle-in-transaction connections, no
            # frozen now()), while every multi-statement block in queue.py
            # opens an explicit conn.transaction(). Without it, a bare
            # SELECT leaves a transaction open and a later transaction()
            # silently becomes a SAVEPOINT in it — locks then outlive the
            # block. See docs/DECISIONS.md.
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.close()
    _pool = None


def connect(dsn: str) -> psycopg.Connection:
    """One standalone connection (workers hold exactly one).

    autocommit=True for the same reason as the pool: see get_pool().
    """
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def run_migrations(dsn: str, migrations_dir: pathlib.Path | None = None) -> list[str]:
    """Apply pending migrations; return the filenames applied."""
    migrations_dir = migrations_dir or MIGRATIONS_DIR
    applied: list[str] = []
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            # Serialise concurrent migrators. Lock is transaction-scoped, so
            # it releases automatically on commit/rollback.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename text PRIMARY KEY,
                       applied_at timestamptz NOT NULL DEFAULT now()
                   )"""
            )
            done = {
                r[0]
                for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()
            }
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                log.info("applying migration %s", path.name)
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
                applied.append(path.name)
    return applied
