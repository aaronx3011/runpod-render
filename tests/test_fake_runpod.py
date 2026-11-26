import time

from fastapi.testclient import TestClient

from runpod_render.devstack.fake_runpod import create_app

H = {"Authorization": "Bearer k"}


def wait(client, ep, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/v2/{ep}/status/{job_id}", headers=H).json()
        if st["status"] not in ("IN_QUEUE", "IN_PROGRESS"):
            return st
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_run_status_complete_and_auth():
    seen = []
    client = TestClient(create_app(handler=lambda job: seen.append(job) or {"echo": job["input"]["x"]}))
    assert client.post("/v2/ep/run", json={"input": {}}).status_code == 401
    job_id = client.post("/v2/ep/run", json={"input": {"x": 1}}, headers=H).json()["id"]
    st = wait(client, "ep", job_id)
    assert st["status"] == "COMPLETED" and st["output"] == {"echo": 1}
    assert seen[0]["id"] == job_id
    assert client.get(f"/v2/other/status/{job_id}", headers=H).status_code == 404


def test_handler_exception_is_failed():
    def boom(job):
        raise ValueError("nope")
    client = TestClient(create_app(handler=boom))
    job_id = client.post("/v2/ep/run", json={"input": {}}, headers=H).json()["id"]
    st = wait(client, "ep", job_id)
    assert st["status"] == "FAILED" and "nope" in st["error"]


def test_faults_duplicate_and_dropped_response():
    calls = []
    client = TestClient(create_app(handler=lambda job: calls.append(job["id"]) or {}))
    client.post("/_faults", json={"duplicate_delivery": True, "drop_run_response": True})
    r = client.post("/v2/ep/run", json={"input": {}}, headers=H)
    assert r.status_code == 504  # caller never learns the id ...
    time.sleep(0.3)
    assert len(calls) == 2 and calls[0] == calls[1]  # ... but the job ran, twice
