import os

import pytest

from simq import db
from simq.config import Settings

TEST_DSN = os.environ.get(
    "SIMQ_TEST_DATABASE_URL",
    "postgresql://simq@127.0.0.1:54329/simq_test",
)


@pytest.fixture(scope="session")
def dsn() -> str:
    db.run_migrations(TEST_DSN)
    return TEST_DSN


@pytest.fixture
def conn(dsn):
    c = db.connect(dsn)
    with c.transaction():
        c.execute("TRUNCATE jobs, job_events RESTART IDENTITY CASCADE")
    yield c
    c.close()


@pytest.fixture
def fast_settings(dsn, tmp_path) -> Settings:
    """Settings tuned so worker-loop tests finish in tens of milliseconds."""
    return Settings(
        database_url=dsn,
        artifact_dir=str(tmp_path / "artifacts"),
        poll_interval_s=0.05,
        lease_s=5,
        heartbeat_interval_s=0.1,
        reap_interval_s=0.2,
        default_timeout_s=30,
        worker_metrics_port=0,
        backoff_base_s=0.01,
        backoff_cap_s=1.0,
    )
