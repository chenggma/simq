"""Workload registry, validation, and the subprocess runner contract."""

import json
import os
import subprocess
import sys

import pytest

from simq import workloads
from simq.workloads.progress import ProgressReporter, read_latest


def test_registry_contents():
    assert {"synthetic", "fleet_day", "fleet_sweep"} <= set(workloads.REGISTRY)
    with pytest.raises(KeyError):
        workloads.get("nope")


# ---- validation (what the API rejects at submit time) -----------------

def test_synthetic_validation():
    workloads.get("synthetic").validate({"duration_s": 1, "steps": 3})
    with pytest.raises(ValueError):
        workloads.get("synthetic").validate({"bogus": 1})
    with pytest.raises(ValueError):
        workloads.get("synthetic").validate({"steps": 0})


def test_fleet_day_validation():
    v = workloads.get("fleet_day").validate
    v({"n_ev": 5, "overrides": {"mass_kg": 30000}})
    with pytest.raises(ValueError):
        v({"overrides": {"warp_drive": 1}})
    with pytest.raises(ValueError):
        v({"overrides": {"mass_kg": 99999}})  # outside documented range
    with pytest.raises(ValueError):
        v({"n_ev": 21})


def test_fleet_sweep_validation():
    v = workloads.get("fleet_sweep").validate
    v({"param": "battery_kwh", "lo": 400, "hi": 750, "steps": 5})
    with pytest.raises(ValueError):
        v({"param": "battery_kwh", "lo": 100, "hi": 750})  # below range
    with pytest.raises(ValueError):
        v({"param": "nope", "lo": 0, "hi": 1})
    with pytest.raises(ValueError):
        v({"lo": 400, "hi": 750})  # missing param


# ---- real workloads run in-process ------------------------------------

def test_fleet_day_runs():
    result = workloads.get("fleet_day").run(
        {"n_ev": 2}, ProgressReporter(None))
    assert result["completion_pct"] == 100.0
    assert result["cost_usd_day"] > 0


def test_fleet_sweep_runs_with_progress(tmp_path):
    ppath = str(tmp_path / "progress.jsonl")
    result = workloads.get("fleet_sweep").run(
        {"param": "battery_kwh", "lo": 400, "hi": 750, "steps": 3, "n_ev": 2},
        ProgressReporter(ppath))
    assert len(result["results"]) == 3
    assert result["summary"]["cost_usd_day_min"] <= result["summary"]["cost_usd_day_max"]
    last = read_latest(ppath)
    assert last["done"] == 3 and last["total"] == 3


# ---- the subprocess contract ------------------------------------------

def run_runner(spec, tmp_path, timeout=60):
    env = {**os.environ,
           "SIMQ_RESULT_PATH": str(tmp_path / "result.json"),
           "SIMQ_PROGRESS_PATH": str(tmp_path / "progress.jsonl")}
    return subprocess.run(
        [sys.executable, "-m", "simq.workloads.runner"],
        input=json.dumps(spec).encode(), env=env,
        capture_output=True, timeout=timeout)


def test_runner_success_writes_result(tmp_path):
    proc = run_runner({"id": "t", "type": "synthetic",
                       "params": {"duration_s": 0.05, "steps": 1,
                                  "result": {"ok": True}}}, tmp_path)
    assert proc.returncode == 0
    assert json.loads((tmp_path / "result.json").read_text()) == {"ok": True}
    assert b"simq: done" in proc.stdout


def test_runner_failure_exit_code_and_traceback(tmp_path):
    proc = run_runner({"id": "t", "type": "synthetic",
                       "params": {"duration_s": 0.01, "fail": True}}, tmp_path)
    assert proc.returncode == 1
    assert b"synthetic failure" in proc.stdout + proc.stderr
    assert not (tmp_path / "result.json").exists()


def test_progress_read_tolerates_torn_line(tmp_path):
    p = tmp_path / "p.jsonl"
    p.write_text('{"done": 1, "total": 2}\n{"done": 2, "to')  # torn write
    assert read_latest(str(p)) == {"done": 1, "total": 2}
    assert read_latest(str(tmp_path / "missing.jsonl")) is None
