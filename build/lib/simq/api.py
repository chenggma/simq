"""FastAPI control plane.

Endpoints are sync `def`s (FastAPI runs them in its threadpool) using the
shared psycopg pool — no async ORM machinery for what are single-digit-
millisecond queries. The app also runs the lease reaper in a background
thread so the system heals even with zero workers alive.
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import threading
import uuid as uuidlib
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST

from . import __version__, artifacts, db, queue, workloads
from .config import Settings, load_settings
from .metrics import QueueCollector
from .models import (EventOut, JobCreate, JobDetailOut, JobOut, StatsOut,
                     WorkloadOut)

log = logging.getLogger("simq.api")
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or load_settings()
    registry = CollectorRegistry()
    reaper_stop = threading.Event()

    def _reaper() -> None:
        """Reclaim expired leases even when no worker is alive to do it."""
        while not reaper_stop.wait(s.reap_interval_s):
            try:
                with db.get_pool(s.database_url).connection() as conn:
                    reaped = queue.reap_expired(
                        conn, backoff_base_s=s.backoff_base_s,
                        backoff_cap_s=s.backoff_cap_s)
                if reaped:
                    log.warning("api reaper: reclaimed %s", reaped)
            except Exception:
                log.exception("api reaper cycle failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        applied = db.run_migrations(s.database_url)
        if applied:
            log.info("applied migrations: %s", applied)
        registry.register(QueueCollector(db.get_pool(s.database_url)))
        t = threading.Thread(target=_reaper, name="simq-reaper", daemon=True)
        t.start()
        yield
        reaper_stop.set()
        t.join(timeout=2)
        db.close_pool()

    app = FastAPI(title="simq", version=__version__, lifespan=lifespan)

    def _conn():
        return db.get_pool(s.database_url).connection()

    # ---- jobs ------------------------------------------------------

    @app.post("/api/jobs", response_model=JobOut, status_code=201)
    def submit_job(body: JobCreate, response: Response,
                   idempotency_key: Optional[str] = Header(default=None)):
        try:
            w = workloads.get(body.type)
            w.validate(body.params)
        except KeyError as e:
            raise HTTPException(422, str(e))
        except ValueError as e:
            raise HTTPException(422, f"invalid params: {e}")
        with _conn() as conn:
            job = queue.enqueue(
                conn, type=body.type, params=body.params,
                priority=body.priority, max_attempts=body.max_attempts,
                timeout_s=body.timeout_s or s.default_timeout_s,
                idempotency_key=idempotency_key)
        if job["deduplicated"]:
            response.status_code = 200
        return job

    @app.get("/api/jobs", response_model=list[JobOut])
    def list_jobs(state: Optional[str] = None, type: Optional[str] = None,
                  limit: int = Query(default=50, le=500), offset: int = 0):
        with _conn() as conn:
            return queue.list_jobs(conn, state=state, type=type,
                                   limit=limit, offset=offset)

    @app.get("/api/jobs/{job_id}", response_model=JobDetailOut)
    def get_job(job_id: uuidlib.UUID):
        with _conn() as conn:
            job = queue.get_job(conn, job_id)
            if not job:
                raise HTTPException(404, "no such job")
            events = queue.get_events(conn, job_id)
        return {**job, "events": events}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: uuidlib.UUID):
        with _conn() as conn:
            state = queue.cancel(conn, job_id)
        if state is None:
            raise HTTPException(404, "no such job")
        return {"id": str(job_id), "state": state}

    @app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
    def get_log(job_id: uuidlib.UUID, tail_bytes: int = Query(default=65536, le=1 << 20)):
        path = artifacts.log_path(s.artifact_dir, str(job_id))
        text = artifacts.log_tail(path, tail_bytes)
        if not text:
            raise HTTPException(404, "no log (job not started, or artifacts elsewhere)")
        return text

    # ---- introspection --------------------------------------------

    @app.get("/api/workloads", response_model=list[WorkloadOut])
    def list_workloads():
        return [WorkloadOut(name=w.name, description=w.description,
                            example_params=w.example_params)
                for w in workloads.REGISTRY.values()]

    @app.get("/api/stats", response_model=StatsOut)
    def get_stats():
        with _conn() as conn:
            return queue.stats(conn)

    @app.get("/healthz")
    def healthz():
        with _conn() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "version": __version__}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry),
                        media_type=CONTENT_TYPE_LATEST)

    # UI last, so /api/* and /metrics take precedence.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


app = create_app()
