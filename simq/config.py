"""Environment-driven configuration.

Every knob is a SIMQ_* environment variable with a development-friendly
default, so `uvicorn simq.api:app` and `simq-worker` run out of the box
against a local Postgres and ./artifacts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_dir: str
    # Worker behaviour
    poll_interval_s: float      # idle sleep between claim attempts
    lease_s: int                # how long a claim is valid without a heartbeat
    heartbeat_interval_s: float # how often a busy worker renews its lease
    reap_interval_s: float      # how often expired leases are reclaimed
    default_timeout_s: int      # per-job wall-clock limit unless overridden
    worker_metrics_port: int    # 0 disables the worker /metrics HTTP server
    # Retry backoff: base * 2^(attempt-1), capped.
    backoff_base_s: float
    backoff_cap_s: float


def load_settings() -> Settings:
    return Settings(
        database_url=_env("SIMQ_DATABASE_URL",
                          "postgresql://simq@127.0.0.1:54329/simq_dev"),
        artifact_dir=_env("SIMQ_ARTIFACT_DIR", "./artifacts"),
        poll_interval_s=float(_env("SIMQ_POLL_INTERVAL_S", "0.5")),
        lease_s=int(_env("SIMQ_LEASE_S", "30")),
        heartbeat_interval_s=float(_env("SIMQ_HEARTBEAT_INTERVAL_S", "5")),
        reap_interval_s=float(_env("SIMQ_REAP_INTERVAL_S", "5")),
        default_timeout_s=int(_env("SIMQ_DEFAULT_TIMEOUT_S", "600")),
        worker_metrics_port=int(_env("SIMQ_WORKER_METRICS_PORT", "0")),
        backoff_base_s=float(_env("SIMQ_BACKOFF_BASE_S", "2")),
        backoff_cap_s=float(_env("SIMQ_BACKOFF_CAP_S", "300")),
    )
