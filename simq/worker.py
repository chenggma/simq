"""The worker: claim -> run in subprocess -> heartbeat -> finalize.

One worker process runs one job at a time (scale out by running more
workers; they coordinate only through the queue). The workload runs in a
*subprocess* so the worker can enforce a hard wall-clock timeout with
SIGKILL, survive workload crashes, and capture a per-job log file.

Outcomes and their transitions:
    exit 0            -> succeeded (result.json becomes the job result)
    exit != 0         -> failed attempt (retry with backoff, or terminal)
    wall-clock exceeded -> kill, failed attempt
    cancel_requested  -> kill, cancelled
    heartbeat lost    -> kill, walk away (the reaper owns the job now)
    SIGTERM to worker -> kill, release job back to queue (attempt uncounted)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

from . import artifacts, db, queue
from .config import Settings, load_settings
from .workloads.progress import read_latest

log = logging.getLogger("simq.worker")

_GRACE_S = 5  # SIGTERM -> SIGKILL grace for the workload subprocess


class Worker:
    def __init__(self, settings: Settings, worker_id: str | None = None):
        self.s = settings
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.shutdown_requested = False
        self.conn = db.connect(settings.database_url)
        self._metrics = None
        if settings.worker_metrics_port:
            from . import metrics
            self._metrics = metrics.WorkerMetrics(port=settings.worker_metrics_port)

    # -- lifecycle -----------------------------------------------------

    def install_signal_handlers(self) -> None:
        def _handler(signum, frame):
            log.info("worker %s: received %s, shutting down after current job",
                     self.id, signal.Signals(signum).name)
            self.shutdown_requested = True

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def run_forever(self) -> None:
        log.info("worker %s: starting (lease=%ss, poll=%ss)",
                 self.id, self.s.lease_s, self.s.poll_interval_s)
        last_reap = 0.0
        while not self.shutdown_requested:
            now = time.monotonic()
            if now - last_reap >= self.s.reap_interval_s:
                reaped = queue.reap_expired(
                    self.conn, backoff_base_s=self.s.backoff_base_s,
                    backoff_cap_s=self.s.backoff_cap_s)
                if reaped:
                    log.warning("reaped %d expired lease(s): %s",
                                len(reaped), reaped)
                last_reap = now
            job = queue.claim(self.conn, self.id, self.s.lease_s)
            if job is None:
                time.sleep(self.s.poll_interval_s)
                continue
            self.execute(job)
        log.info("worker %s: bye", self.id)

    # -- one job -------------------------------------------------------

    def execute(self, job: dict) -> None:
        job_id: uuid.UUID = job["id"]
        log.info("worker %s: job %s type=%s attempt %d/%d",
                 self.id, job_id, job["type"], job["attempts"],
                 job["max_attempts"])
        if self._metrics:
            self._metrics.busy.set(1)
        started = time.monotonic()
        outcome, detail = self._run_subprocess(job)
        duration = time.monotonic() - started

        if outcome == "success":
            result = self._read_result(job_id)
            queue.succeed(self.conn, job_id, self.id, result)
        elif outcome == "error":
            queue.fail(self.conn, job_id, self.id, detail,
                       backoff_base_s=self.s.backoff_base_s,
                       backoff_cap_s=self.s.backoff_cap_s)
        elif outcome == "timeout":
            queue.fail(self.conn, job_id, self.id, detail,
                       backoff_base_s=self.s.backoff_base_s,
                       backoff_cap_s=self.s.backoff_cap_s)
        elif outcome == "cancelled":
            queue.mark_cancelled(self.conn, job_id, self.id)
        elif outcome == "shutdown":
            queue.release(self.conn, job_id, self.id)
        elif outcome == "lost":
            pass  # reaper or server-side action took the job from us
        if self._metrics:
            self._metrics.observe(job["type"], outcome, duration)
        log.info("worker %s: job %s -> %s (%.2fs)",
                 self.id, job_id, outcome, duration)

    def _run_subprocess(self, job: dict) -> tuple[str, str]:
        """Run the workload; return (outcome, error_detail)."""
        job_id = str(job["id"])
        adir = artifacts.job_dir(self.s.artifact_dir, job_id)
        result_p = artifacts.result_path(self.s.artifact_dir, job_id)
        progress_p = artifacts.progress_path(self.s.artifact_dir, job_id)
        log_p = artifacts.log_path(self.s.artifact_dir, job_id)

        env = {**os.environ,
               "SIMQ_RESULT_PATH": str(result_p),
               "SIMQ_PROGRESS_PATH": str(progress_p)}
        with open(log_p, "ab") as logf:
            logf.write(f"--- attempt {job['attempts']} on {self.id} ---\n".encode())
            logf.flush()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "simq.workloads.runner"],
                stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
                env=env, cwd=str(adir),
            )
            spec = {"id": job_id, "type": job["type"], "params": job["params"]}
            proc.stdin.write(json.dumps(spec).encode())
            proc.stdin.close()

            deadline = time.monotonic() + job["timeout_s"]
            next_hb = time.monotonic() + self.s.heartbeat_interval_s
            while True:
                rc = proc.poll()
                if rc is not None:
                    if rc == 0:
                        return "success", ""
                    tail = artifacts.log_tail(log_p, 2000)
                    return "error", f"workload exited {rc}; log tail:\n{tail}"
                now = time.monotonic()
                if now >= deadline:
                    self._kill(proc)
                    return "timeout", (f"timeout: exceeded {job['timeout_s']}s "
                                       f"wall clock on attempt {job['attempts']}")
                if self.shutdown_requested:
                    self._kill(proc)
                    return "shutdown", ""
                if now >= next_hb:
                    hb = queue.heartbeat(
                        self.conn, job["id"], self.id, self.s.lease_s,
                        progress=read_latest(str(progress_p)))
                    next_hb = now + self.s.heartbeat_interval_s
                    if hb is None:
                        log.warning("job %s: heartbeat lost, abandoning", job_id)
                        self._kill(proc)
                        return "lost", ""
                    if hb["cancel_requested"]:
                        self._kill(proc)
                        return "cancelled", ""
                time.sleep(0.05)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _read_result(self, job_id: uuid.UUID):
        path = artifacts.result_path(self.s.artifact_dir, str(job_id))
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SIMQ_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    db.run_migrations(settings.database_url)
    w = Worker(settings)
    w.install_signal_handlers()
    if settings.worker_metrics_port:
        log.info("worker metrics on :%d", settings.worker_metrics_port)
    w.run_forever()


if __name__ == "__main__":
    main()
