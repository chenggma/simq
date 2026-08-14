"""Synthetic workload: configurable duration/failure, for tests and load
tests. Deliberately boring — its job is to exercise the *platform*."""

from __future__ import annotations

import time
from typing import Any

from . import Workload, register
from .progress import ProgressReporter


def _validate(params: dict) -> None:
    allowed = {"duration_s", "steps", "fail", "fail_at_step", "result"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown params: {sorted(unknown)}")
    if float(params.get("duration_s", 1)) < 0:
        raise ValueError("duration_s must be >= 0")
    if int(params.get("steps", 5)) < 1:
        raise ValueError("steps must be >= 1")


def _run(params: dict, progress: ProgressReporter) -> Any:
    duration = float(params.get("duration_s", 1))
    steps = int(params.get("steps", 5))
    fail = bool(params.get("fail", False))
    fail_at = params.get("fail_at_step")
    for i in range(steps):
        time.sleep(duration / steps)
        if fail and (fail_at is None or i + 1 >= int(fail_at)):
            raise RuntimeError(f"synthetic failure at step {i + 1}/{steps}")
        progress.report(i + 1, steps)
    return params.get("result", {"slept_s": duration, "steps": steps})


register(Workload(
    name="synthetic",
    description="Sleep in N steps, optionally fail; for testing the platform",
    example_params={"duration_s": 10, "steps": 10},
    validate=_validate,
    run=_run,
))
