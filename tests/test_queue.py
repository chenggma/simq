import time

from simq import queue


def test_enqueue_and_get(conn):
    job = queue.enqueue(conn, type="synthetic", params={"duration_s": 1},
                        priority=3, max_attempts=2, timeout_s=60)
    assert job["state"] == "queued"
    assert job["deduplicated"] is False
    got = queue.get_job(conn, job["id"])
    assert got["priority"] == 3
    assert got["params"] == {"duration_s": 1}
    events = queue.get_events(conn, job["id"])
    assert [e["event"] for e in events] == ["enqueued"]


def test_idempotency_key_dedupes(conn):
    a = queue.enqueue(conn, type="synthetic", idempotency_key="k1")
    b = queue.enqueue(conn, type="synthetic", idempotency_key="k1")
    assert a["id"] == b["id"]
    assert b["deduplicated"] is True
    assert len(queue.list_jobs(conn)) == 1


def test_claim_order_priority_then_fifo(conn):
    low = queue.enqueue(conn, type="synthetic", priority=0)
    high = queue.enqueue(conn, type="synthetic", priority=5)
    low2 = queue.enqueue(conn, type="synthetic", priority=0)
    ids = [queue.claim(conn, "w", 30)["id"] for _ in range(3)]
    assert ids == [high["id"], low["id"], low2["id"]]
    assert queue.claim(conn, "w", 30) is None


def test_claim_respects_next_run_at(conn):
    job = queue.enqueue(conn, type="synthetic")
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET next_run_at = now() + interval '1 hour'"
            " WHERE id = %s", (job["id"],))
    assert queue.claim(conn, "w", 30) is None


def test_succeed(conn):
    job = queue.enqueue(conn, type="synthetic")
    claimed = queue.claim(conn, "w1", 30)
    assert claimed["state"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["lease_expires_at"] is not None
    assert queue.succeed(conn, job["id"], "w1", {"answer": 42})
    got = queue.get_job(conn, job["id"])
    assert got["state"] == "succeeded"
    assert got["result"] == {"answer": 42}
    assert got["finished_at"] is not None
    assert [e["event"] for e in queue.get_events(conn, job["id"])] == [
        "enqueued", "claimed", "succeeded"]


def test_succeed_requires_owner(conn):
    job = queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w1", 30)
    assert not queue.succeed(conn, job["id"], "imposter", {})
    assert queue.get_job(conn, job["id"])["state"] == "running"


def test_fail_retries_then_terminal(conn):
    job = queue.enqueue(conn, type="synthetic", max_attempts=2)
    queue.claim(conn, "w1", 30)
    state = queue.fail(conn, job["id"], "w1", "boom", backoff_base_s=0.01)
    assert state == "queued"
    got = queue.get_job(conn, job["id"])
    assert got["attempts"] == 1 and got["error"] == "boom"
    time.sleep(0.05)  # let the 10ms backoff elapse
    assert queue.claim(conn, "w1", 30)["attempts"] == 2
    state = queue.fail(conn, job["id"], "w1", "boom again")
    assert state == "failed"
    assert queue.get_job(conn, job["id"])["state"] == "failed"


def test_fail_nonretryable_is_terminal(conn):
    job = queue.enqueue(conn, type="synthetic", max_attempts=3)
    queue.claim(conn, "w1", 30)
    assert queue.fail(conn, job["id"], "w1", "bad params",
                      retryable=False) == "failed"


def test_backoff_schedule():
    assert queue.backoff_s(1, 2, 300) == 2
    assert queue.backoff_s(2, 2, 300) == 4
    assert queue.backoff_s(5, 2, 300) == 32
    assert queue.backoff_s(10, 2, 300) == 300  # capped


def test_heartbeat_extends_and_reports_cancel(conn):
    job = queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w1", 30)
    hb = queue.heartbeat(conn, job["id"], "w1", 60, progress={"done": 1, "total": 5})
    assert hb == {"cancel_requested": False}
    assert queue.get_job(conn, job["id"])["progress"] == {"done": 1, "total": 5}
    # wrong worker -> lost
    assert queue.heartbeat(conn, job["id"], "w2", 60) is None
    queue.cancel(conn, job["id"])
    assert queue.heartbeat(conn, job["id"], "w1", 60)["cancel_requested"]


def test_cancel_queued(conn):
    job = queue.enqueue(conn, type="synthetic")
    assert queue.cancel(conn, job["id"]) == "cancelled"
    assert queue.get_job(conn, job["id"])["state"] == "cancelled"
    assert queue.claim(conn, "w", 30) is None


def test_cancel_running_then_worker_confirms(conn):
    job = queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w1", 30)
    assert queue.cancel(conn, job["id"]) == "running"
    assert queue.get_job(conn, job["id"])["cancel_requested"]
    assert queue.mark_cancelled(conn, job["id"], "w1")
    assert queue.get_job(conn, job["id"])["state"] == "cancelled"


def test_cancel_terminal_noop(conn):
    job = queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w1", 30)
    queue.succeed(conn, job["id"], "w1", {})
    assert queue.cancel(conn, job["id"]) == "succeeded"


def test_release_uncounts_attempt(conn):
    job = queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w1", 30)
    assert queue.release(conn, job["id"], "w1")
    got = queue.get_job(conn, job["id"])
    assert got["state"] == "queued" and got["attempts"] == 0
    assert got["worker_id"] is None


def test_reap_expired_requeues(conn):
    job = queue.enqueue(conn, type="synthetic", max_attempts=2)
    queue.claim(conn, "w1", 30)
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second'"
            " WHERE id = %s", (job["id"],))
    assert queue.reap_expired(conn, backoff_base_s=0.01) == [job["id"]]
    got = queue.get_job(conn, job["id"])
    assert got["state"] == "queued" and got["attempts"] == 1
    assert "lease expired" in got["error"]
    # the dead worker's heartbeat now finds nothing
    assert queue.heartbeat(conn, job["id"], "w1", 30) is None


def test_reap_expired_terminal_when_attempts_exhausted(conn):
    job = queue.enqueue(conn, type="synthetic", max_attempts=1)
    queue.claim(conn, "w1", 30)
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second'"
            " WHERE id = %s", (job["id"],))
    queue.reap_expired(conn)
    assert queue.get_job(conn, job["id"])["state"] == "failed"


def test_reap_ignores_live_leases(conn):
    queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w1", 300)
    assert queue.reap_expired(conn) == []


def test_stats_and_list_filters(conn):
    a = queue.enqueue(conn, type="synthetic")
    queue.enqueue(conn, type="synthetic")
    queue.claim(conn, "w", 30)
    s = queue.stats(conn)
    assert s["by_state"] == {"queued": 1, "running": 1}
    assert s["oldest_queued_age_s"] >= 0
    assert len(queue.list_jobs(conn, state="running")) == 1
    assert len(queue.list_jobs(conn, type="nope")) == 0
    assert queue.list_jobs(conn, limit=1)[0]["id"] in {a["id"], queue.list_jobs(conn)[0]["id"]}
