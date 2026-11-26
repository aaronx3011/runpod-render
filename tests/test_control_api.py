"""Client API: auth, idempotency, listing, uploads, cancel, schema/migration parity."""

import hashlib

import httpx
from control_support import new_project, submit
from fastapi.testclient import TestClient

from runpod_render.control.api import create_app


def H(key):
    return {"Authorization": f"Bearer {key}"}


def test_migration_matches_models(make_ctx):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from runpod_render.control.db import Base

    ctx = make_ctx()
    with ctx.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_healthz_and_auth(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/v1/jobs").status_code == 401
    assert client.get("/v1/jobs", headers=H("rr_wrong")).status_code == 401


def test_submit_is_idempotent_and_param_mismatch_conflicts(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    r1 = submit(client, key, "op-1")
    assert r1.status_code == 201, r1.text
    job = r1.json()
    assert job["status"] == "queued" and job["attempts"] == 0
    # Same request (defaults spelled out explicitly still hash the same) -> the same job.
    r2 = submit(client, key, "op-1", fps=24, seed=1000)
    assert r2.status_code == 200 and r2.json()["id"] == job["id"]
    # Different params under the same operation_id -> 409.
    r3 = submit(client, key, "op-1", prompt="something else")
    assert r3.status_code == 409
    # Another project may reuse the operation id; it gets its own job and cannot see the first.
    _, key2 = new_project(ctx)
    r4 = submit(client, key2, "op-1")
    assert r4.status_code == 201 and r4.json()["id"] != job["id"]
    assert client.get(f"/v1/jobs/{job['id']}", headers=H(key2)).status_code == 404
    assert client.get(f"/v1/jobs/{job['id']}", headers=H(key)).json()["id"] == job["id"]
    # project_id in a body is rejected outright (project comes from the key only).
    bad = client.post("/v1/jobs", headers=H(key), json={"operation_id": "x", "kind": "t2v", "project_id": "p",
                                                         "params": {"prompt": "p"}})
    assert bad.status_code == 422


def test_list_is_paged(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    ids = [submit(client, key, f"op-{i}").json()["id"] for i in range(5)]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/jobs", headers=H(key), params=params).json()
        seen += [j["id"] for j in page["jobs"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(ids))
    assert client.get("/v1/jobs", headers=H(key), params={"status": "completed"}).json()["jobs"] == []


def test_uploads_and_omni_references(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    data = b"fake png bytes"
    sha = hashlib.sha256(data).hexdigest()
    up = client.post("/v1/uploads", headers=H(key), json={"media_type": "image", "sha256": sha,
                                                          "content_type": "image/png"})
    assert up.status_code == 201, up.text
    up = up.json()
    assert up["sha256"] == sha
    body = {"operation_id": "omni-1", "kind": "omni", "params": {"prompt": "p"},
            "reference_upload_ids": [up["upload_id"]]}
    # Not uploaded yet -> 422.
    assert client.post("/v1/jobs", headers=H(key), json=body).status_code == 422
    assert httpx.put(up["put_url"], content=data, headers=up["put_headers"]).status_code == 200
    assert client.post("/v1/jobs", headers=H(key), json=body).status_code == 201
    # References only for omni; unknown / other project's uploads are rejected.
    assert client.post("/v1/jobs", headers=H(key), json={**body, "operation_id": "t", "kind": "t2v"}).status_code == 422
    _, key2 = new_project(ctx)
    assert client.post("/v1/jobs", headers=H(key2), json=body).status_code == 422


def test_cancel_queued_job_and_download_conflict(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    job = submit(client, key, "op").json()
    assert client.get(f"/v1/jobs/{job['id']}/download", headers=H(key)).status_code == 409
    r = client.post(f"/v1/jobs/{job['id']}/cancel", headers=H(key))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=H(key)).status_code == 200  # idempotent
