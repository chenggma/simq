-- simq initial schema.
--
-- Design notes:
--  * The queue is the `jobs` table itself; claiming is an UPDATE guarded by
--    FOR UPDATE SKIP LOCKED so concurrent workers never double-claim and
--    never block each other.
--  * `state` is the single source of truth. Terminal states: succeeded,
--    failed, cancelled. A retryable failure goes *back to queued* with a
--    future next_run_at (exponential backoff); `failed` means retries are
--    exhausted or the error was non-retryable.
--  * Running jobs hold a lease (lease_expires_at), renewed by worker
--    heartbeats. If a worker dies, the reaper treats the expired lease as a
--    failed attempt and requeues or fails the job.

CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type             text NOT NULL,
    params           jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    priority         integer NOT NULL DEFAULT 0,
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    timeout_s        integer NOT NULL DEFAULT 600 CHECK (timeout_s > 0),
    idempotency_key  text,
    cancel_requested boolean NOT NULL DEFAULT false,
    next_run_at      timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    worker_id        text,
    progress         jsonb,
    result           jsonb,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz
);

-- Partial index sized for the hot path: the claim query only ever scans
-- queued jobs, ordered by priority then age.
CREATE INDEX jobs_claim_idx ON jobs (priority DESC, next_run_at, created_at)
    WHERE state = 'queued';
CREATE INDEX jobs_state_idx ON jobs (state, created_at DESC);
CREATE INDEX jobs_lease_idx ON jobs (lease_expires_at) WHERE state = 'running';
CREATE UNIQUE INDEX jobs_idempotency_idx ON jobs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Append-only audit trail of every state transition.
CREATE TABLE job_events (
    id        bigserial PRIMARY KEY,
    job_id    uuid NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    at        timestamptz NOT NULL DEFAULT now(),
    event     text NOT NULL,
    worker_id text,
    detail    jsonb
);

CREATE INDEX job_events_job_idx ON job_events (job_id, id);
