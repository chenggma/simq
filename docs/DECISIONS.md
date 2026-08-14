# Design decisions

Short ADRs. Each records what was chosen, what was rejected, and why.

## 1. Postgres as the queue, not Redis/Celery/RabbitMQ

**Chosen:** the `jobs` table + `FOR UPDATE SKIP LOCKED` claiming.

**Rejected:** Celery/RQ (Redis), RabbitMQ.

* One fewer stateful service to deploy, monitor, back up and explain.
* The queue shares the database's transactional guarantees: a job cannot
  be lost between "handed to a worker" and "recorded as handed over",
  because those are the same UPDATE.
* Queue state is queryable with SQL — the stats endpoint, the UI and the
  metrics collector are `SELECT`s, not bespoke introspection APIs.
* `SKIP LOCKED` exists precisely for this pattern and scales fine to the
  hundreds-of-jobs/s regime this system targets (measured: ~660 submits/s
  and zero double-claims under 8 racing claimants; see docs/loadtest.md).

**Cost:** at tens of thousands of jobs/s a dedicated broker wins; that is
far beyond simulation-campaign scale.

## 2. Workloads run in a subprocess, not in the worker process

* A hard timeout must work even when the workload is stuck in C/Fortran
  (SUMO, numpy): only SIGKILL guarantees that; you cannot kill a thread.
* A segfaulting workload takes down one attempt, not the worker.
* stdout/stderr redirection gives per-job logs for free.
* Cancellation is SIGTERM→SIGKILL with a grace period, not cooperative
  flag-checking inside numeric code.

**Cost:** ~0.1-0.3s Python startup per attempt — irrelevant for jobs that
run seconds to hours (and visible honestly in the load-test numbers).

## 3. Leases + heartbeats, not connection-held locks

The obvious alternative — hold the claim's row lock (or an advisory lock)
for the duration of the run — ties job ownership to connection liveness:
a worker that loses its TCP connection for 10s loses the job even though
the subprocess is fine, and a long transaction pins vacuum. Leases make
ownership explicit and renewable, and give a clean place to piggyback
progress updates and cancellation checks (the heartbeat round-trip).

## 4. `autocommit=True` connections + explicit `conn.transaction()`

With psycopg3's default (autocommit off), the first bare `SELECT` on a
connection silently opens a transaction and freezes `now()`; a later
`with conn.transaction():` block then becomes a **savepoint** inside that
open transaction, so its locks and writes outlive the block until someone
commits. That cost a real debugging session (a cancelled job's row lock
blocked a worker heartbeat forever). autocommit connections make reads
commit immediately, and every multi-statement section in `queue.py` opens
an explicit transaction. Regression-tested by the worker cancellation
test.

## 5. Sync endpoints, threadpool, no ORM

Every API query is a single-digit-millisecond primary-key or small-index
SQL statement. FastAPI runs sync `def` endpoints in a threadpool, which
handles this fine, and hand-written SQL keeps the queue's locking
behaviour visible in one file (`queue.py`) instead of hidden behind an
ORM's lazy-loading semantics.

## 6. Validation at submit time, in the API

Each workload ships a `validate(params)` the API calls before enqueueing:
garbage params are a 422 with a message at submit time, not a failed
attempt discovered minutes later in a worker log. The worker still treats
any subprocess failure as retryable — validation is a UX layer, not a
correctness dependency.
