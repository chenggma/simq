"""End-to-end worker tests with the real subprocess runner and the
synthetic workload. Timings use fast_settings (50-100ms polls)."""

import threading
import time

from simq import db, queue
from simq.worker import Worker


def make_worker(fast_settings, wid="wtest"):
    return Worker(fast_settings, worker_id=wid)


def drive_one(w):
    """Claim one job and execute it synchronously."""
    job = queue.claim(w.conn, w.id, w.s.lease_s)
    assert job is not None
    w.execute(job)
    return job["id"]


def test_success_path(conn, fast_settings):
    queue.enqueue(conn, type="synthetic",
                  params={"duration_s": 0.3, "steps": 3}, timeout_s=30)
    w = make_worker(fast_settings)
    jid = drive_one(w)
    got = queue.get_job(conn, jid)
    assert got["state"] == "succeeded"
    assert got["result"]["slept_s"] == 0.3
    # heartbeats stored progress from the progress file
    assert got["progress"] is not None and got["progress"]["total"] == 3
    events = [e["event"] for e in queue.get_events(conn, jid)]
    assert events == ["enqueued", "claimed", "succeeded"]
    # per-job log artifact captured the subprocess output
    from simq import artifacts
    text = artifacts.log_tail(artifacts.log_path(w.s.artifact_dir, str(jid)))
    assert "simq: done" in text


def test_failure_retries_then_dead(conn, fast_settings):
    queue.enqueue(conn, type="synthetic",
                  params={"duration_s": 0.05, "fail": True},
                  max_attempts=2, timeout_s=30)
    w = make_worker(fast_settings)
    jid = drive_one(w)
    got = queue.get_job(conn, jid)
    assert got["state"] == "queued" and got["attempts"] == 1
    assert "synthetic failure" in got["error"]
    time.sleep(0.05)  # backoff_base_s=0.01
    drive_one(w)
    got = queue.get_job(conn, jid)
    assert got["state"] == "failed" and got["attempts"] == 2


def test_timeout_kills_and_fails(conn, fast_settings):
    queue.enqueue(conn, type="synthetic",
                  params={"duration_s": 30, "steps": 30},
                  max_attempts=1, timeout_s=1)
    w = make_worker(fast_settings)
    t0 = time.monotonic()
    jid = drive_one(w)
    elapsed = time.monotonic() - t0
    got = queue.get_job(conn, jid)
    assert got["state"] == "failed"
    assert "timeout" in got["error"]
    assert elapsed < 10, f"kill should be prompt, took {elapsed:.1f}s"


def test_cancel_running_job(conn, fast_settings):
    job = queue.enqueue(conn, type="synthetic",
                        params={"duration_s": 30, "steps": 30}, timeout_s=60)
    w = make_worker(fast_settings)
    t = threading.Thread(target=drive_one, args=(w,))
    t.start()
    # wait until running, then cancel
    for _ in range(100):
        if queue.get_job(conn, job["id"])["state"] == "running":
            break
        time.sleep(0.02)
    queue.cancel(conn, job["id"])
    t.join(timeout=15)
    assert not t.is_alive()
    got = queue.get_job(conn, job["id"])
    assert got["state"] == "cancelled"
    events = [e["event"] for e in queue.get_events(conn, job["id"])]
    assert "cancel_requested" in events and events[-1] == "cancelled"


def test_graceful_shutdown_releases_job(conn, fast_settings):
    job = queue.enqueue(conn, type="synthetic",
                        params={"duration_s": 30}, timeout_s=60)
    w = make_worker(fast_settings)
    t = threading.Thread(target=drive_one, args=(w,))
    t.start()
    for _ in range(100):
        if queue.get_job(conn, job["id"])["state"] == "running":
            break
        time.sleep(0.02)
    w.shutdown_requested = True
    t.join(timeout=15)
    assert not t.is_alive()
    got = queue.get_job(conn, job["id"])
    # released: back in queue, attempt uncounted
    assert got["state"] == "queued" and got["attempts"] == 0


def test_lost_lease_abandons_subprocess(conn, fast_settings):
    job = queue.enqueue(conn, type="synthetic",
                        params={"duration_s": 30}, timeout_s=60)
    w = make_worker(fast_settings)
    t = threading.Thread(target=drive_one, args=(w,))
    t.start()
    for _ in range(100):
        if queue.get_job(conn, job["id"])["state"] == "running":
            break
        time.sleep(0.02)
    # simulate the lease expiring + another node's reaper taking the job
    other = db.connect(w.s.database_url)
    try:
        with other.transaction():
            other.execute(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second'"
                " WHERE id = %s", (job["id"],))
        queue.reap_expired(other, backoff_base_s=0.01)
    finally:
        other.close()
    t.join(timeout=15)
    assert not t.is_alive()
    got = queue.get_job(conn, job["id"])
    # the reaper requeued it; the old worker must NOT have overwritten that
    assert got["state"] == "queued" and got["worker_id"] is None


def test_run_forever_processes_and_stops(conn, fast_settings):
    for _ in range(3):
        queue.enqueue(conn, type="synthetic",
                      params={"duration_s": 0.05, "steps": 1}, timeout_s=30)
    w = make_worker(fast_settings)
    t = threading.Thread(target=w.run_forever)
    t.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        states = [j["state"] for j in queue.list_jobs(conn)]
        if states and all(s == "succeeded" for s in states):
            break
        time.sleep(0.05)
    w.shutdown_requested = True
    t.join(timeout=10)
    assert not t.is_alive()
    assert all(j["state"] == "succeeded" for j in queue.list_jobs(conn))
