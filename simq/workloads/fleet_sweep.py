"""Real long-running workload: 1-D parameter sweep of the fleet day.

This is the job type that makes the platform earn its keep: hundreds of
deterministic simulator runs inside one job, with live progress, a
meaningful result payload, and enough wall-clock time for timeouts,
cancellation and lease renewal to actually matter.
"""

from __future__ import annotations

from typing import Any

from . import Workload, register
from .fleet_day import _harness
from .progress import ProgressReporter

MAX_STEPS = 500


def _validate(params: dict) -> None:
    allowed = {"param", "lo", "hi", "steps", "base_overrides", "n_ev",
               "policy", "chargers"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown params: {sorted(unknown)}")
    for req in ("param", "lo", "hi"):
        if req not in params:
            raise ValueError(f"missing required param {req!r}")
    ranges = _harness().RANGES
    name = params["param"]
    if name not in ranges:
        raise ValueError(f"unknown sweep param {name!r}; known: {sorted(ranges)}")
    lo, hi = float(params["lo"]), float(params["hi"])
    rlo, rhi = ranges[name][0], ranges[name][1]
    if not (rlo <= lo <= hi <= rhi):
        raise ValueError(
            f"sweep [{lo}, {hi}] must lie inside documented range [{rlo}, {rhi}]")
    steps = int(params.get("steps", 20))
    if not (2 <= steps <= MAX_STEPS):
        raise ValueError(f"steps must be in [2, {MAX_STEPS}]")
    base = params.get("base_overrides") or {}
    bad = set(base) - set(ranges)
    if bad:
        raise ValueError(f"unknown base override(s) {sorted(bad)}")


def _run(params: dict, progress: ProgressReporter) -> Any:
    h = _harness()
    name = params["param"]
    lo, hi = float(params["lo"]), float(params["hi"])
    steps = int(params.get("steps", 20))
    base = dict(params.get("base_overrides") or {})
    n_ev = int(params.get("n_ev", 20))
    policy = params.get("policy", "smart_baseline")
    chargers = params.get("chargers", "depot_only")

    results = []
    for i in range(steps):
        value = lo + (hi - lo) * i / (steps - 1)
        metrics = h.run_representative_day(
            overrides={**base, name: value}, n_ev=n_ev,
            policy=policy, chargers=chargers)
        results.append({"value": value,
                        "cost_usd_day": metrics.get("cost_usd_day"),
                        "grid_kwh_day": metrics.get("grid_kwh_day"),
                        "completion_pct": metrics.get("completion_pct"),
                        "n_late": metrics.get("n_late")})
        progress.report(i + 1, steps, param=name, value=round(value, 4))

    costs = [r["cost_usd_day"] for r in results if r["cost_usd_day"] is not None]
    return {
        "param": name, "lo": lo, "hi": hi, "steps": steps,
        "results": results,
        "summary": {
            "cost_usd_day_min": min(costs) if costs else None,
            "cost_usd_day_max": max(costs) if costs else None,
            "argmin_value": results[costs.index(min(costs))]["value"] if costs else None,
        },
    }


register(Workload(
    name="fleet_sweep",
    description="1-D parameter sweep over N fleet-day runs with live progress",
    example_params={"param": "battery_kwh", "lo": 400, "hi": 750, "steps": 50},
    validate=_validate,
    run=_run,
))
