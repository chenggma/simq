"""Real workload: one deterministic EV-drayage fleet day.

Wraps fleetops.uq.harness.run_representative_day from
https://github.com/chenggma/fleet-day-sim (a stdlib-only discrete-event
simulator). Parameter overrides are validated against the harness's own
documented ranges at submit time.
"""

from __future__ import annotations

from typing import Any

from . import Workload, register
from .progress import ProgressReporter

ALLOWED = {"overrides", "n_ev", "policy", "chargers"}


def _harness():
    try:
        from fleetops.uq import harness
    except ImportError as e:  # pragma: no cover - exercised only without extra
        raise RuntimeError(
            "fleet-day-sim is not installed; pip install 'simq[fleet]'"
        ) from e
    return harness


def _validate(params: dict) -> None:
    unknown = set(params) - ALLOWED
    if unknown:
        raise ValueError(f"unknown params: {sorted(unknown)}")
    overrides = params.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object")
    ranges = _harness().RANGES
    bad = set(overrides) - set(ranges)
    if bad:
        raise ValueError(
            f"unknown override(s) {sorted(bad)}; known: {sorted(ranges)}")
    for k, v in overrides.items():
        lo, hi = ranges[k][0], ranges[k][1]
        if not (lo <= float(v) <= hi):
            raise ValueError(f"{k}={v} outside documented range [{lo}, {hi}]")
    n_ev = int(params.get("n_ev", 20))
    if not (0 <= n_ev <= 20):
        raise ValueError("n_ev must be in [0, 20]")


def _run(params: dict, progress: ProgressReporter) -> Any:
    h = _harness()
    progress.report(0, 1, phase="simulating")
    metrics = h.run_representative_day(
        overrides=params.get("overrides") or {},
        n_ev=int(params.get("n_ev", 20)),
        policy=params.get("policy", "smart_baseline"),
        chargers=params.get("chargers", "depot_only"),
    )
    progress.report(1, 1, phase="done")
    return metrics


register(Workload(
    name="fleet_day",
    description="One deterministic EV-drayage fleet day (fleet-day-sim)",
    example_params={"n_ev": 20, "overrides": {"mass_kg": 32000}},
    validate=_validate,
    run=_run,
))
