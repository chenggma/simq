"""Progress channel between a workload subprocess and its worker.

The subprocess appends JSON lines to a file; the worker reads the last
line on each heartbeat and stores it on the job row, where the API and UI
can see it. Append-of-one-line is atomic enough for a single writer and a
reader that tolerates a torn last line.
"""

from __future__ import annotations

import json
from typing import Any, Optional


class ProgressReporter:
    def __init__(self, path: Optional[str]):
        self.path = path

    def report(self, done: int, total: int, **extra: Any) -> None:
        payload = {"done": done, "total": total, **extra}
        if self.path:
            with open(self.path, "a") as f:
                f.write(json.dumps(payload) + "\n")
        # Also into the job log for humans tailing it.
        print(f"progress: {done}/{total} {extra if extra else ''}", flush=True)


def read_latest(path: str) -> Optional[dict]:
    """Last complete JSON line of a progress file, or None."""
    try:
        with open(path, "rb") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue  # torn write of the last line; take the previous one
    return None
