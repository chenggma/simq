"""Per-job artifact layout on a shared volume.

    <artifact_dir>/<job_id>/log.txt        combined stdout+stderr of the run
    <artifact_dir>/<job_id>/result.json    written by the workload subprocess
    <artifact_dir>/<job_id>/progress.jsonl progress lines (see workloads.progress)

The API and workers only need to share this directory (a Docker volume in
compose); nothing else is stored outside Postgres.
"""

from __future__ import annotations

import os
import pathlib


def job_dir(root: str, job_id: str) -> pathlib.Path:
    p = pathlib.Path(root) / str(job_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_path(root: str, job_id: str) -> pathlib.Path:
    return pathlib.Path(root) / str(job_id) / "log.txt"


def result_path(root: str, job_id: str) -> pathlib.Path:
    return pathlib.Path(root) / str(job_id) / "result.json"


def progress_path(root: str, job_id: str) -> pathlib.Path:
    return pathlib.Path(root) / str(job_id) / "progress.jsonl"


def log_tail(path: os.PathLike, max_bytes: int = 4000) -> str:
    """Last max_bytes of a log file (for error messages and the API)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
