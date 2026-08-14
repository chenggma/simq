"""Workload registry.

A workload is what a job *does*. Each one declares:
  validate(params) -> None   raise ValueError for bad params (called by the
                             API at submit time, so garbage is rejected with
                             a 422 instead of failing later in a worker)
  run(params, progress) -> JSON-serialisable result (called in a worker
                             subprocess; may take minutes)

Workloads are registered by name; the API exposes the registry at
GET /api/workloads so the UI can offer presets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from .progress import ProgressReporter


@dataclass(frozen=True)
class Workload:
    name: str
    description: str
    example_params: Dict[str, Any]
    validate: Callable[[dict], None]
    run: Callable[[dict, ProgressReporter], Any]


REGISTRY: Dict[str, Workload] = {}


def register(w: Workload) -> Workload:
    REGISTRY[w.name] = w
    return w


def get(name: str) -> Workload:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown workload {name!r}; known: {sorted(REGISTRY)}") from None


# Import for side effect: each module registers itself.
from . import synthetic  # noqa: E402,F401
from . import fleet_day  # noqa: E402,F401
from . import fleet_sweep  # noqa: E402,F401
