# Load test

- jobs: **200** synthetic x 0.5s, submit concurrency 32
- outcome states: {'succeeded': 200}
- submit throughput: **661 jobs/s**; drain throughput: **6.5 jobs/s**

| metric | P50 | P95 | P99 | max | mean |
|---|---|---|---|---|---|
| submit latency (ms) | 41.42 | 63.63 | 80.88 | 84.22 | 44.24 |
| queue wait (s) | 14.57 | 27.69 | 28.87 | 29.42 | 14.51 |
| end-to-end (s) | 15.16 | 28.28 | 29.47 | 30.02 | 15.11 |

## Reading these numbers

- Hardware: Apple M-series laptop, single Postgres instance, API + 4 worker
  processes, all local.
- **Submit latency** (P95 64ms at 32 concurrent submitters) is the API +
  enqueue transaction cost — the platform accepts work ~100x faster than
  4 workers can execute 0.5s jobs, which is the point of a queue.
- **Queue wait** is backlog-dominated by design: 200 x 0.5s jobs across 4
  workers is ~25s of unavoidable queueing at the tail. The interesting
  number is drain throughput per worker: 6.5 jobs/s / 4 workers ≈ 0.62s
  per 0.5s job, i.e. ~0.12s of platform overhead per attempt (subprocess
  spawn + claim + finalize), consistent with docs/DECISIONS.md #2.
- Every one of the 200 jobs succeeded exactly once — no double-claims, no
  losses — under concurrent claiming; the same property is unit-tested
  with racing threads in tests/test_claim_concurrency.py.

Reproduce: `python scripts/loadtest.py --n 200 --duration-s 0.5 --out docs/loadtest.md`
