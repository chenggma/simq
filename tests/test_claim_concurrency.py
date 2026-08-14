"""The property the whole design leans on: N workers claiming concurrently
never double-claim and never deadlock."""

import threading

from simq import db, queue
from tests.conftest import TEST_DSN

N_JOBS = 120
N_WORKERS = 8


def test_concurrent_claims_are_exclusive(conn):
    for i in range(N_JOBS):
        queue.enqueue(conn, type="synthetic", params={"i": i})

    claimed: dict[str, list] = {}
    errors: list[Exception] = []

    def worker(wid: str) -> None:
        mine = claimed.setdefault(wid, [])
        try:
            c = db.connect(TEST_DSN)
            try:
                while True:
                    job = queue.claim(c, wid, lease_s=60)
                    if job is None:
                        break
                    mine.append(job["id"])
            finally:
                c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"w{i}",))
               for i in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    all_ids = [j for ids in claimed.values() for j in ids]
    assert len(all_ids) == N_JOBS, "every job claimed exactly once in total"
    assert len(set(all_ids)) == N_JOBS, "no job claimed twice"
    # sanity: work actually spread over multiple connections
    assert sum(1 for ids in claimed.values() if ids) >= 2
