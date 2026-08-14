# Deploying

Everything is one image (API and worker differ only in command) plus
Postgres plus a shared artifacts volume.

## Anywhere with Docker

```bash
docker compose up -d --build --scale worker=4
# UI on http://localhost:8000
```

Set a real `POSTGRES_PASSWORD` and pin `SIMQ_DATABASE_URL` accordingly.

## Fly.io (small, cheap, has volumes)

```bash
fly launch --no-deploy                 # generates fly.toml from the Dockerfile
fly postgres create --name simq-db
fly postgres attach simq-db            # sets DATABASE_URL secret
fly secrets set SIMQ_DATABASE_URL="$(fly ssh console -C 'printenv DATABASE_URL' 2>/dev/null || echo use-attached-url)"
fly volumes create artifacts --size 1
# fly.toml: mount the volume at /data/artifacts; add a `worker` process
#   [processes]
#     app = "uvicorn simq.api:app --host 0.0.0.0 --port 8000"
#     worker = "simq-worker"
fly deploy
fly scale count worker=2
```

Notes:
* API and workers must share `SIMQ_ARTIFACT_DIR` to serve logs from the
  API. If they can't share a volume, logs stay on the worker — everything
  else still works (results live in Postgres).
* The API runs migrations at startup under an advisory lock; scaling to
  multiple API machines is safe.

## Railway / Render

Same shape: one service from the Dockerfile (default command = API), a
second service from the same image with command `simq-worker`, managed
Postgres, `SIMQ_DATABASE_URL` env var on both.

## Ops knobs

| env | default | meaning |
|---|---|---|
| `SIMQ_DATABASE_URL` | local dev DSN | Postgres DSN |
| `SIMQ_ARTIFACT_DIR` | `./artifacts` | per-job logs/results/progress |
| `SIMQ_LEASE_S` | 30 | claim validity without a heartbeat |
| `SIMQ_HEARTBEAT_INTERVAL_S` | 5 | lease renewal + progress + cancel check |
| `SIMQ_REAP_INTERVAL_S` | 5 | how often expired leases are reclaimed |
| `SIMQ_DEFAULT_TIMEOUT_S` | 600 | per-job wall clock unless set at submit |
| `SIMQ_POLL_INTERVAL_S` | 0.5 | idle worker claim poll |
| `SIMQ_BACKOFF_BASE_S` / `SIMQ_BACKOFF_CAP_S` | 2 / 300 | retry backoff |
| `SIMQ_WORKER_METRICS_PORT` | 0 (off) | worker Prometheus port |
