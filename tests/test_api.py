import uuid

import pytest
from fastapi.testclient import TestClient

from simq import queue
from simq.api import create_app


@pytest.fixture
def client(conn, fast_settings):
    app = create_app(fast_settings)
    with TestClient(app) as c:
        yield c


def test_submit_and_get(client):
    r = client.post("/api/jobs", json={
        "type": "synthetic", "params": {"duration_s": 1}, "priority": 2})
    assert r.status_code == 201
    job = r.json()
    assert job["state"] == "queued" and job["priority"] == 2

    r = client.get(f"/api/jobs/{job['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["events"][0]["event"] == "enqueued"


def test_submit_unknown_type_rejected(client):
    r = client.post("/api/jobs", json={"type": "nope", "params": {}})
    assert r.status_code == 422
    assert "unknown workload" in r.json()["detail"]


def test_submit_bad_params_rejected(client):
    r = client.post("/api/jobs", json={
        "type": "fleet_day", "params": {"overrides": {"mass_kg": 1}}})
    assert r.status_code == 422
    assert "outside documented range" in r.json()["detail"]


def test_idempotency_header(client):
    h = {"Idempotency-Key": "abc"}
    a = client.post("/api/jobs", json={"type": "synthetic"}, headers=h)
    b = client.post("/api/jobs", json={"type": "synthetic"}, headers=h)
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_list_and_filter(client):
    client.post("/api/jobs", json={"type": "synthetic"})
    client.post("/api/jobs", json={"type": "synthetic"})
    assert len(client.get("/api/jobs").json()) == 2
    assert len(client.get("/api/jobs", params={"state": "running"}).json()) == 0
    assert len(client.get("/api/jobs", params={"limit": 1}).json()) == 1


def test_cancel_flow(client):
    job = client.post("/api/jobs", json={"type": "synthetic"}).json()
    r = client.post(f"/api/jobs/{job['id']}/cancel")
    assert r.json()["state"] == "cancelled"
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"


def test_404s(client):
    missing = str(uuid.uuid4())
    assert client.get(f"/api/jobs/{missing}").status_code == 404
    assert client.post(f"/api/jobs/{missing}/cancel").status_code == 404
    assert client.get(f"/api/jobs/{missing}/log").status_code == 404
    assert client.get("/api/jobs/not-a-uuid").status_code == 422


def test_stats_and_health(client, conn):
    client.post("/api/jobs", json={"type": "synthetic"})
    queue.claim(conn, "w", 30)
    s = client.get("/api/stats").json()
    assert s["by_state"] == {"running": 1}
    assert client.get("/healthz").json()["ok"] is True


def test_metrics_endpoint(client):
    client.post("/api/jobs", json={"type": "synthetic"})
    text = client.get("/metrics").text
    assert 'simq_jobs{state="queued"} 1.0' in text
    assert "simq_queue_oldest_ready_age_seconds" in text


def test_workloads_listing(client):
    names = {w["name"] for w in client.get("/api/workloads").json()}
    assert {"synthetic", "fleet_day", "fleet_sweep"} <= names


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>simq</title>" in r.text
