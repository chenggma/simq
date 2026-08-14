"""Prometheus metrics.

API process: queue state is *derived from the database at scrape time* via
a custom collector — no drift between what /metrics says and what the
queue actually holds, and no per-process counter aggregation problem.

Worker process: process-local counters/histograms served on their own
port (SIMQ_WORKER_METRICS_PORT), the standard pattern for scraping a
fleet of workers.
"""

from __future__ import annotations

from prometheus_client import (CollectorRegistry, Counter, Gauge, Histogram,
                               start_http_server)
from prometheus_client.core import GaugeMetricFamily

from . import queue

STATES = ("queued", "running", "succeeded", "failed", "cancelled")


class QueueCollector:
    """DB-backed gauges, computed on scrape."""

    def __init__(self, pool):
        self.pool = pool

    def collect(self):
        with self.pool.connection() as conn:
            s = queue.stats(conn)
        by_state = GaugeMetricFamily(
            "simq_jobs", "Jobs by state", labels=["state"])
        for state in STATES:
            by_state.add_metric([state], s["by_state"].get(state, 0))
        yield by_state
        oldest = GaugeMetricFamily(
            "simq_queue_oldest_ready_age_seconds",
            "Age of the oldest job that is ready to run but still queued")
        oldest.add_metric([], s["oldest_queued_age_s"] or 0.0)
        yield oldest


class WorkerMetrics:
    """Per-worker-process metrics on a dedicated HTTP port."""

    def __init__(self, port: int):
        self.registry = CollectorRegistry()
        self.jobs = Counter(
            "simq_worker_jobs_total", "Jobs finished by this worker",
            ["type", "outcome"], registry=self.registry)
        self.duration = Histogram(
            "simq_worker_job_duration_seconds", "Job attempt wall clock",
            ["type"],
            buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 1800, float("inf")),
            registry=self.registry)
        self.busy = Gauge(
            "simq_worker_busy", "1 while running a job",
            registry=self.registry)
        start_http_server(port, registry=self.registry)

    def observe(self, job_type: str, outcome: str, duration_s: float) -> None:
        self.jobs.labels(job_type, outcome).inc()
        self.duration.labels(job_type).observe(duration_s)
        self.busy.set(0)
