"""Subprocess entry point: run one workload attempt.

The worker launches `python -m simq.workloads.runner`, writes the job spec
as JSON on stdin, and reads nothing back: the contract is exit code +
files (SIMQ_RESULT_PATH, SIMQ_PROGRESS_PATH) + whatever we print, which
the worker captures as the job log. Keeping the workload in its own
process gives the worker a hard timeout (SIGKILL works when a workload
blocks in C code), crash isolation, and per-job logs for free.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from . import get
from .progress import ProgressReporter


def main() -> int:
    spec = json.load(sys.stdin)
    workload = get(spec["type"])
    progress = ProgressReporter(os.environ.get("SIMQ_PROGRESS_PATH"))
    print(f"simq: job {spec.get('id')} type={spec['type']} starting", flush=True)
    try:
        result = workload.run(spec.get("params") or {}, progress)
    except Exception:
        traceback.print_exc()
        return 1
    result_path = os.environ.get("SIMQ_RESULT_PATH")
    if result_path:
        with open(result_path, "w") as f:
            json.dump(result, f, default=str)
    print("simq: done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
