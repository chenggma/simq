# Architecture

```
                 ┌────────────────────────────────────────────┐
                 │                  Postgres                   │
   POST /api/jobs│   jobs (queue = table + SKIP LOCKED)        │
  ┌──────────┐   │   job_events (append-only audit trail)      │
  │ FastAPI  │──▶│   schema_migrations                         │
  │ control  │   └────────────▲───────────────▲────────────────┘
  │ plane    │        claim/heartbeat/    claim/heartbeat/
  │ +reaper  │        finalize            finalize
  │ +metrics │   ┌────────────┴─────┐ ┌────────┴──────────┐
  └────┬─────┘   │ worker 1         │ │ worker N          │
       │         │  └─ subprocess:  │ │  └─ subprocess:   │
   static UI     │     workload run │ │     workload run  │
                 └──────────┬───────┘ └────────┬──────────┘
                            └───── artifacts ──┘
                            (shared volume: log.txt, result.json,
                             progress.jsonl per job)
```

## Components

**API (FastAPI, `simq/api.py`)** — submit/inspect/cancel jobs, serve the
UI, expose `/metrics` and `/healthz`. Runs migrations at startup and a
background *reaper* thread so expired leases are reclaimed even with zero
workers alive. Endpoints are sync `def`s executed in the threadpool; every
query is single-digit milliseconds.

**Queue (`simq/queue.py` + `jobs` table)** — plain functions over SQL.
Claiming uses `FOR UPDATE SKIP LOCKED`, so N workers contend without
blocking each other and no job is ever double-claimed
(`tests/test_claim_concurrency.py` proves it with racing threads).

**Worker (`simq/worker.py`)** — single-threaded loop: claim → spawn the
workload as a subprocess → heartbeat while it runs → finalize. The
subprocess boundary buys: hard wall-clock timeouts (SIGKILL works even if
the workload is stuck in C code), crash isolation, per-job log capture,
and prompt cancellation.

**Workloads (`simq/workloads/`)** — a registry of named job types. Each
declares `validate(params)` (called by the API at submit time → bad
requests die as 422s, not worker failures) and `run(params, progress)`
(called in the subprocess). Shipped workloads:

| name | what it is |
|---|---|
| `fleet_day` | one deterministic EV-drayage fleet day ([fleet-day-sim](https://github.com/chenggma/fleet-day-sim)) |
| `fleet_sweep` | 1-D parameter sweep, up to 500 simulator runs per job, live progress |
| `synthetic` | sleep/fail/step knobs for tests and load tests |

## Job lifecycle

```
            enqueue                    claim
 (API) ────────────────▶ queued ─────────────────▶ running
                           ▲                          │
                           │ retry w/ backoff         ├─ exit 0 ──────▶ succeeded
                           │ (attempts < max)         ├─ exit ≠ 0 ─┐
                           │                          ├─ timeout ──┤
                           └──────────────────────────┤            ├──▶ failed
                                                      │ lease expired   (attempts
                                                      │ (reaper)        exhausted)
              cancel (queued job) ──▶ cancelled ◀─────┘ cancel_requested
                                                        seen at heartbeat
```

* **Lease + heartbeat.** A claim holds a lease (`SIMQ_LEASE_S`, default
  30s), renewed on every heartbeat (default 5s) along with the latest
  progress. A worker that dies stops renewing; the reaper turns the
  expired lease into a failed attempt (requeue with backoff, or terminal
  `failed`). A worker that *lost* its lease learns this from the
  heartbeat response and abandons the subprocess.
* **Retries.** Failures requeue with exponential backoff
  (`base * 2^(attempt-1)`, capped) until `max_attempts`.
* **Cancellation.** Queued jobs cancel immediately. Running jobs get
  `cancel_requested`; the worker sees it at the next heartbeat, SIGTERMs
  (then SIGKILLs) the subprocess and confirms.
* **Graceful shutdown.** SIGTERM to a worker kills the subprocess and
  *releases* the job back to the queue with the attempt un-counted.
* **Idempotent submits.** `Idempotency-Key` header dedupes retried POSTs
  via a partial unique index.

## Observability

* `job_events`: every transition, timestamped, with worker id and detail —
  the UI renders it as a timeline.
* `/metrics` (API): queue depths by state and oldest-ready-job age,
  computed from the DB at scrape time (no drift).
* Worker `/metrics` (opt-in, own port): jobs by outcome, duration
  histogram, busy gauge.
* Artifacts per job: full subprocess log, result JSON, progress history.
