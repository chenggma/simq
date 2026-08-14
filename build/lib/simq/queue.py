"""The queue: plain functions over the jobs table.

Every function takes an open psycopg connection with dict_row row factory
and manages its own transaction, so callers (API handlers, worker loop,
tests) compose them freely. State transitions are recorded in job_events.

Why Postgres instead of Redis/Celery/RabbitMQ: see docs/DECISIONS.md.
The short version: FOR UPDATE SKIP LOCKED gives contention-free claiming,
the queue shares the database's transactional guarantees (a job is never
lost between "claimed" and "recorded as claimed"), and one fewer service
has to be deployed, monitored and explained.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import psycopg

RETRYABLE_DEFAULT = True

JOB_COLUMNS = """id, type, params, state, priority, attempts, max_attempts,
    timeout_s, idempotency_key, cancel_requested, next_run_at,
    lease_expires_at, worker_id, progress, result, error,
    created_at, started_at, finished_at"""


def _event(conn: psycopg.Connection, job_id: uuid.UUID, event: str,
           worker_id: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, event, worker_id, detail)"
        " VALUES (%s, %s, %s, %s)",
        (job_id, event, worker_id, json.dumps(detail) if detail else None),
    )


def backoff_s(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff for retry N (1-based): base * 2^(N-1), capped."""
    return min(cap, base * (2 ** (attempt - 1)))


# ---------------------------------------------------------------- producers

def enqueue(conn: psycopg.Connection, *, type: str, params: dict | None = None,
            priority: int = 0, max_attempts: int = 3, timeout_s: int = 600,
            idempotency_key: str | None = None) -> dict:
    """Insert a job. With an idempotency key, re-submits return the
    original job instead of creating a duplicate."""
    with conn.transaction():
        if idempotency_key is not None:
            row = conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if row:
                return {**row, "deduplicated": True}
        row = conn.execute(
            """INSERT INTO jobs (type, params, priority, max_attempts,
                                 timeout_s, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING """ + JOB_COLUMNS,
            (type, json.dumps(params or {}), priority, max_attempts,
             timeout_s, idempotency_key),
        ).fetchone()
        _event(conn, row["id"], "enqueued")
    return {**row, "deduplicated": False}


# ----------------------------------------------------------------- workers

def claim(conn: psycopg.Connection, worker_id: str, lease_s: int) -> Optional[dict]:
    """Claim the best runnable job, or None.

    The inner SELECT ... FOR UPDATE SKIP LOCKED is the heart of the design:
    concurrent workers each lock a *different* candidate row instead of
    queueing on the same one, so claiming scales with worker count and a
    job can never be claimed twice.
    """
    with conn.transaction():
        row = conn.execute(
            """
            WITH candidate AS (
                SELECT id FROM jobs
                WHERE state = 'queued' AND next_run_at <= now()
                ORDER BY priority DESC, next_run_at, created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs j
            SET state = 'running',
                worker_id = %(worker)s,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                lease_expires_at = now() + make_interval(secs => %(lease)s)
            FROM candidate
            WHERE j.id = candidate.id
            RETURNING """ + JOB_COLUMNS.replace("id,", "j.id,", 1),
            {"worker": worker_id, "lease": lease_s},
        ).fetchone()
        if row:
            _event(conn, row["id"], "claimed", worker_id,
                   {"attempt": row["attempts"]})
    return row


def heartbeat(conn: psycopg.Connection, job_id: uuid.UUID, worker_id: str,
              lease_s: int, progress: dict | None = None) -> Optional[dict]:
    """Renew the lease; returns {'cancel_requested': bool} or None if the
    job is no longer this worker's to run (reaped, cancelled server-side,
    or finished) — the worker must then abandon the subprocess."""
    with conn.transaction():
        row = conn.execute(
            """UPDATE jobs
               SET lease_expires_at = now() + make_interval(secs => %s),
                   progress = COALESCE(%s, progress)
               WHERE id = %s AND state = 'running' AND worker_id = %s
               RETURNING cancel_requested""",
            (lease_s, json.dumps(progress) if progress is not None else None,
             job_id, worker_id),
        ).fetchone()
    return row


def succeed(conn: psycopg.Connection, job_id: uuid.UUID, worker_id: str,
            result: Any = None) -> bool:
    with conn.transaction():
        row = conn.execute(
            """UPDATE jobs
               SET state = 'succeeded', result = %s, finished_at = now(),
                   lease_expires_at = NULL
               WHERE id = %s AND state = 'running' AND worker_id = %s
               RETURNING id""",
            (json.dumps(result), job_id, worker_id),
        ).fetchone()
        if row:
            _event(conn, job_id, "succeeded", worker_id)
    return row is not None


def fail(conn: psycopg.Connection, job_id: uuid.UUID, worker_id: str | None,
         error: str, *, retryable: bool = RETRYABLE_DEFAULT,
         backoff_base_s: float = 2.0, backoff_cap_s: float = 300.0) -> Optional[str]:
    """Record a failed attempt.

    Retryable + attempts left -> back to 'queued' with exponential backoff.
    Otherwise -> terminal 'failed'. Returns the resulting state, or None if
    the job wasn't in a failable state (e.g. already reaped).

    worker_id=None is the reaper acting on a lease that expired; a real
    worker passes its id and only touches its own claim.
    """
    with conn.transaction():
        cond = "AND worker_id = %(worker)s" if worker_id is not None else ""
        row = conn.execute(
            f"SELECT id, attempts, max_attempts FROM jobs"
            f" WHERE id = %(id)s AND state = 'running' {cond} FOR UPDATE",
            {"id": job_id, "worker": worker_id},
        ).fetchone()
        if not row:
            return None
        retry = retryable and row["attempts"] < row["max_attempts"]
        if retry:
            delay = backoff_s(row["attempts"], backoff_base_s, backoff_cap_s)
            conn.execute(
                """UPDATE jobs
                   SET state = 'queued', error = %s, worker_id = NULL,
                       lease_expires_at = NULL,
                       next_run_at = now() + make_interval(secs => %s)
                   WHERE id = %s""",
                (error, delay, job_id),
            )
            _event(conn, job_id, "retry_scheduled", worker_id,
                   {"error": error[:500], "attempt": row["attempts"],
                    "backoff_s": delay})
            return "queued"
        conn.execute(
            """UPDATE jobs
               SET state = 'failed', error = %s, finished_at = now(),
                   lease_expires_at = NULL
               WHERE id = %s""",
            (error, job_id),
        )
        _event(conn, job_id, "failed", worker_id, {"error": error[:500]})
        return "failed"


def release(conn: psycopg.Connection, job_id: uuid.UUID, worker_id: str) -> bool:
    """Give a running job back (graceful worker shutdown). The attempt is
    un-counted: shutting a worker down is not the job's fault."""
    with conn.transaction():
        row = conn.execute(
            """UPDATE jobs
               SET state = 'queued', worker_id = NULL, lease_expires_at = NULL,
                   attempts = attempts - 1, next_run_at = now()
               WHERE id = %s AND state = 'running' AND worker_id = %s
               RETURNING id""",
            (job_id, worker_id),
        ).fetchone()
        if row:
            _event(conn, job_id, "released", worker_id)
    return row is not None


# ------------------------------------------------------------- maintenance

def reap_expired(conn: psycopg.Connection, *, backoff_base_s: float = 2.0,
                 backoff_cap_s: float = 300.0) -> list[uuid.UUID]:
    """Reclaim running jobs whose lease expired (worker died or lost
    connectivity). Each becomes a failed attempt: requeue with backoff if
    attempts remain, else terminal failure.

    Everything happens under the FOR UPDATE row locks of ONE transaction:
    a live worker's concurrent heartbeat blocks on the lock and then
    matches zero rows (state/worker_id changed), so it learns it lost the
    job instead of resurrecting it.
    """
    reaped: list[uuid.UUID] = []
    error = "lease expired: worker presumed dead"
    with conn.transaction():
        rows = conn.execute(
            """SELECT id, attempts, max_attempts, worker_id FROM jobs
               WHERE state = 'running' AND lease_expires_at < now()
               FOR UPDATE SKIP LOCKED"""
        ).fetchall()
        for r in rows:
            _event(conn, r["id"], "lease_expired", r["worker_id"])
            if r["attempts"] < r["max_attempts"]:
                delay = backoff_s(r["attempts"], backoff_base_s, backoff_cap_s)
                conn.execute(
                    """UPDATE jobs
                       SET state = 'queued', error = %s, worker_id = NULL,
                           lease_expires_at = NULL,
                           next_run_at = now() + make_interval(secs => %s)
                       WHERE id = %s""",
                    (error, delay, r["id"]),
                )
                _event(conn, r["id"], "retry_scheduled", None,
                       {"error": error, "attempt": r["attempts"],
                        "backoff_s": delay})
            else:
                conn.execute(
                    """UPDATE jobs
                       SET state = 'failed', error = %s, finished_at = now(),
                           lease_expires_at = NULL
                       WHERE id = %s""",
                    (error, r["id"]),
                )
                _event(conn, r["id"], "failed", None, {"error": error})
            reaped.append(r["id"])
    return reaped


def cancel(conn: psycopg.Connection, job_id: uuid.UUID) -> Optional[str]:
    """Cancel a job.

    queued  -> cancelled immediately.
    running -> cancel_requested flag; the worker sees it on its next
               heartbeat, kills the subprocess and marks the job cancelled.
    Returns the job's state after the call, or None if unknown id."""
    with conn.transaction():
        row = conn.execute(
            "SELECT id, state FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
        ).fetchone()
        if not row:
            return None
        if row["state"] == "queued":
            conn.execute(
                """UPDATE jobs SET state = 'cancelled', finished_at = now()
                   WHERE id = %s""", (job_id,))
            _event(conn, job_id, "cancelled")
            return "cancelled"
        if row["state"] == "running":
            conn.execute(
                "UPDATE jobs SET cancel_requested = true WHERE id = %s",
                (job_id,))
            _event(conn, job_id, "cancel_requested")
            return "running"
        return row["state"]  # already terminal; no-op


def mark_cancelled(conn: psycopg.Connection, job_id: uuid.UUID,
                   worker_id: str) -> bool:
    """Worker confirms it killed the subprocess of a cancel-requested job."""
    with conn.transaction():
        row = conn.execute(
            """UPDATE jobs
               SET state = 'cancelled', finished_at = now(),
                   lease_expires_at = NULL
               WHERE id = %s AND state = 'running' AND worker_id = %s
               RETURNING id""",
            (job_id, worker_id),
        ).fetchone()
        if row:
            _event(conn, job_id, "cancelled", worker_id)
    return row is not None


# ---------------------------------------------------------------- queries

def get_job(conn: psycopg.Connection, job_id: uuid.UUID) -> Optional[dict]:
    return conn.execute(
        f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job_id,)
    ).fetchone()


def get_events(conn: psycopg.Connection, job_id: uuid.UUID) -> list[dict]:
    return conn.execute(
        """SELECT at, event, worker_id, detail FROM job_events
           WHERE job_id = %s ORDER BY id""", (job_id,)
    ).fetchall()


def list_jobs(conn: psycopg.Connection, *, state: str | None = None,
              type: str | None = None, limit: int = 50,
              offset: int = 0) -> list[dict]:
    conds: list[str] = []
    args: list[Any] = []
    if state:
        conds.append("state = %s")
        args.append(state)
    if type:
        conds.append("type = %s")
        args.append(type)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return conn.execute(
        f"""SELECT {JOB_COLUMNS} FROM jobs {where}
            ORDER BY created_at DESC LIMIT %s OFFSET %s""",
        (*args, limit, offset),
    ).fetchall()


def stats(conn: psycopg.Connection) -> dict:
    by_state = {
        r["state"]: r["n"]
        for r in conn.execute(
            "SELECT state, count(*) AS n FROM jobs GROUP BY state"
        ).fetchall()
    }
    oldest = conn.execute(
        """SELECT extract(epoch FROM now() - min(next_run_at)) AS age
           FROM jobs WHERE state = 'queued' AND next_run_at <= now()"""
    ).fetchone()
    return {
        "by_state": by_state,
        "oldest_queued_age_s": float(oldest["age"]) if oldest and oldest["age"] is not None else None,
    }
