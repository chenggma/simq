"""Load test: submit a burst of jobs, then report latency percentiles.

Measures, per job, from the server's own timestamps (so worker count and
queue depth are what's being characterised, not client overhead):

    queue wait = started_at  - created_at
    e2e        = finished_at - created_at

Usage:
    python scripts/loadtest.py --url http://localhost:8000 --n 200 \
        --duration-s 0.5 --out docs/loadtest.md
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
    return values[k]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--duration-s", type=float, default=0.5,
                    help="synthetic job duration")
    ap.add_argument("--concurrency", type=int, default=32,
                    help="concurrent submitters")
    ap.add_argument("--out", default=None, help="write a markdown report here")
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        r = await client.get("/healthz")
        r.raise_for_status()

        # -- submit burst ------------------------------------------------
        sem = asyncio.Semaphore(args.concurrency)
        submit_ms: list[float] = []
        ids: list[str] = []

        async def submit(i: int) -> None:
            async with sem:
                t0 = time.perf_counter()
                r = await client.post("/api/jobs", json={
                    "type": "synthetic",
                    "params": {"duration_s": args.duration_s, "steps": 1},
                })
                r.raise_for_status()
                submit_ms.append((time.perf_counter() - t0) * 1000)
                ids.append(r.json()["id"])

        t_start = time.perf_counter()
        await asyncio.gather(*(submit(i) for i in range(args.n)))
        submit_wall = time.perf_counter() - t_start
        print(f"submitted {args.n} jobs in {submit_wall:.2f}s "
              f"({args.n / submit_wall:.0f} rps)")

        # -- wait for drain ----------------------------------------------
        while True:
            r = await client.get("/api/stats")
            by_state = r.json()["by_state"]
            pending = by_state.get("queued", 0) + by_state.get("running", 0)
            print(f"  pending={pending}", end="\r")
            if pending == 0:
                break
            await asyncio.sleep(0.5)
        drain_wall = time.perf_counter() - t_start
        print(f"\ndrained in {drain_wall:.2f}s "
              f"({args.n / drain_wall:.1f} jobs/s end-to-end)")

        # -- collect server-side timestamps ------------------------------
        waits, e2es, states = [], [], {}
        for jid in ids:
            j = (await client.get(f"/api/jobs/{jid}")).json()
            states[j["state"]] = states.get(j["state"], 0) + 1
            if j["started_at"] and j["finished_at"]:
                from datetime import datetime
                c = datetime.fromisoformat(j["created_at"])
                st = datetime.fromisoformat(j["started_at"])
                f = datetime.fromisoformat(j["finished_at"])
                waits.append((st - c).total_seconds())
                e2es.append((f - c).total_seconds())

    rows = [
        ("submit latency (ms)", submit_ms),
        ("queue wait (s)", waits),
        ("end-to-end (s)", e2es),
    ]
    lines = [
        f"# Load test\n",
        f"- jobs: **{args.n}** synthetic x {args.duration_s}s, "
        f"submit concurrency {args.concurrency}",
        f"- outcome states: {states}",
        f"- submit throughput: **{args.n / submit_wall:.0f} jobs/s**; "
        f"drain throughput: **{args.n / drain_wall:.1f} jobs/s**\n",
        "| metric | P50 | P95 | P99 | max | mean |",
        "|---|---|---|---|---|---|",
    ]
    for name, vals in rows:
        if not vals:
            continue
        lines.append(
            f"| {name} | {pct(vals, 50):.2f} | {pct(vals, 95):.2f} "
            f"| {pct(vals, 99):.2f} | {max(vals):.2f} "
            f"| {statistics.mean(vals):.2f} |")
    report = "\n".join(lines) + "\n"
    print("\n" + report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
