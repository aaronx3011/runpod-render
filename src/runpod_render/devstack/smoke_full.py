"""Full-stack smoke test: the real control plane (API + scheduler containers) in the dev stack.

    docker compose -f devstack/compose.yaml --profile control up -d --build
    docker compose -f devstack/compose.yaml --profile control run --rm smoke-full

Creates a project, submits a job twice (idempotency), waits for it to complete through fake
RunPod + the mock worker, downloads the video via the presigned URL and checks its sha256.
Exits 0 on success.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import uuid

import httpx

from runpod_render.control.db import make_engine, make_sessionmaker
from runpod_render.control.service import create_project

API = os.environ.get("CONTROL_API_URL", "http://control-api:8080").rstrip("/")


def main() -> int:
    deadline = time.time() + 60
    while True:
        try:
            if httpx.get(f"{API}/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            print("control API not healthy")
            return 1
        time.sleep(1)
    engine = make_engine(os.environ["DATABASE_URL"])
    with make_sessionmaker(engine).begin() as s:
        _, key = create_project(s, f"smoke-{uuid.uuid4().hex[:8]}")
    h = {"Authorization": f"Bearer {key}"}
    body = {"operation_id": "smoke-1", "kind": "t2v",
            "params": {"prompt": "smoke test: a lighthouse at dusk", "num_frames": 8, "width": 320, "height": 192}}
    j1 = httpx.post(f"{API}/v1/jobs", json=body, headers=h)
    j2 = httpx.post(f"{API}/v1/jobs", json=body, headers=h)
    print("submit:", j1.status_code, j2.status_code)
    job_id = j1.json()["id"]
    ok = j1.status_code == 201 and j2.status_code == 200 and j2.json()["id"] == job_id
    deadline = time.time() + 120
    while time.time() < deadline:
        view = httpx.get(f"{API}/v1/jobs/{job_id}", headers=h).json()
        if view["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(1)
    print("job:", view)
    if view["status"] == "completed":
        dl = httpx.get(f"{API}/v1/jobs/{job_id}/download", headers=h).json()
        video = httpx.get(dl["url"]).content
        ok = ok and hashlib.sha256(video).hexdigest() == dl["video_sha256"] == view["video_sha256"]
    else:
        ok = False
    print("SMOKE-FULL OK" if ok else "SMOKE-FULL FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
