"""Scheduler admission, fencing, claim gate, submission outcomes and reconciliation (stub RunPod)."""

import hashlib
import threading

from control_support import SECRET, StubRunPod, new_project, submit, wait_for
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from runpod_render.contracts import Manifest, RenderParams, StageTimings, sign_attempt
from runpod_render.control import scheduler
from runpod_render.control.api import create_app
from runpod_render.control.db import ACTIVE_ATTEMPT_STATUSES, Attempt, Job
from runpod_render.control.runpod import RunNotDelivered, RunUncertain


def active(ctx):
    with ctx.sessions() as s:
        return list(s.scalars(select(Attempt).where(Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES))))


def finish_all_active(ctx):
    """Simulate workers finishing every active attempt (bypasses callbacks)."""
    with ctx.sessions.begin() as s:
        for a in s.scalars(select(Attempt).where(Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES))):
            a.status = "completed"
            s.get(Job, a.job_id).status = "completed"


def token(att):
    return {"Authorization": f"Bearer {sign_attempt(SECRET.encode(), att.id, att.fence)}"}


def claim_body(rp="rp-x", worker="w1"):
    return {"runpod_job_id": rp, "worker_id": worker, "model_revision": "mock-rev"}


def drain(ctx, client, keys, jobs_per_project):
    for key in keys:
        for i in range(jobs_per_project):
            assert submit(client, key, f"op-{i}").status_code == 201
    order, max_total, max_per_project = [], 0, 0
    for _ in range(len(keys) * jobs_per_project + 5):
        for a in scheduler.tick(ctx):
            order.append(a.job_input["job_id"])
        act = active(ctx)
        per = {}
        for a in act:
            per[a.project_id] = per.get(a.project_id, 0) + 1
        max_total = max(max_total, len(act))
        max_per_project = max([max_per_project, *per.values()])
        finish_all_active(ctx)
    return order, max_total, max_per_project


def project_of(ctx, job_ids):
    with ctx.sessions() as s:
        m = dict(s.execute(select(Job.id, Job.project_id).where(Job.id.in_(job_ids))).all())
    return [m[j] for j in job_ids]


def test_fairness_two_projects_ten_jobs(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    (pa, ka), (pb, kb) = new_project(ctx, "a"), new_project(ctx, "b")
    order, max_total, max_per = drain(ctx, client, [ka, kb], 10)
    assert len(order) == 20
    assert max_total <= 7 and max_per == 1
    seq = project_of(ctx, order)
    assert seq == [pa.id, pb.id] * 10  # strictly alternating although all of A was submitted first


def test_round_robin_with_global_cap_one(make_ctx):
    ctx = make_ctx(max_active_renders=1)
    client = TestClient(create_app(ctx))
    (pa, ka), (pb, kb) = new_project(ctx, "a"), new_project(ctx, "b")
    order, max_total, _ = drain(ctx, client, [ka, kb], 3)
    assert max_total == 1
    assert project_of(ctx, order) == [pa.id, pb.id] * 3


def test_global_cap_seven_across_many_projects(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    projects = [new_project(ctx, f"p{i}") for i in range(10)]
    for _, key in projects:
        for i in range(2):
            submit(client, key, f"op-{i}")
    first = scheduler.tick(ctx)
    assert len(first) == 7 and len(active(ctx)) == 7
    assert scheduler.tick(ctx) == []  # full
    finish_all_active(ctx)
    second = project_of(ctx, [a.job_id for a in scheduler.tick(ctx)])
    # The three projects that got nothing in round one go first.
    never = {p.id for p, _ in projects} - set(project_of(ctx, [a.job_id for a in first]))
    assert set(second[:3]) == never and len(second) == 7


def test_project_max_active_above_one(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx, "wide", max_active=3)
    _, key2 = new_project(ctx, "narrow")
    for i in range(5):
        submit(client, key, f"w-{i}")
        submit(client, key2, f"n-{i}")
    scheduler.tick(ctx)
    per = {}
    for a in active(ctx):
        per[a.project_id] = per.get(a.project_id, 0) + 1
    assert sorted(per.values()) == [1, 3]


def test_admission_creates_fenced_attempt_and_submits(make_ctx):
    rp = StubRunPod()
    ctx = make_ctx(runpod=rp)
    client = TestClient(create_app(ctx))
    p, key = new_project(ctx)
    job = submit(client, key, "op").json()
    [adm] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
        j = s.get(Job, job["id"])
    assert att.status == "submitted" and att.runpod_job_id == "rp-1" and att.fence == 1
    assert j.current_fence == 1 and j.current_attempt_id == att.id and j.attempt_count == 1
    assert att.output_prefix == f"outputs/{p.id}/{job['id']}/{att.id}"
    endpoint, inp = rp.runs[0]
    assert endpoint == "fasth3-t2v"
    assert f"/{att.output_prefix}/video.mp4" in inp["upload"]["video_url"]
    assert inp["callback_base_url"].endswith("/worker/v1")


def test_claim_gate_duplicate_and_stale_fence(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    job_id = submit(client, key, "op").json()["id"]
    [adm] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    url = f"/worker/v1/attempts/{att.id}"
    # Bad tokens.
    assert client.post(f"{url}/claim", json=claim_body("rp-1"), headers={"Authorization": "Bearer x.1.y"}).status_code == 401
    forged = sign_attempt(b"other-secret", att.id, att.fence)
    assert client.post(f"{url}/claim", json=claim_body("rp-1"),
                       headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    other = sign_attempt(SECRET.encode(), "att_other", 1)
    assert client.post(f"{url}/claim", json=claim_body("rp-1"),
                       headers={"Authorization": f"Bearer {other}"}).status_code == 403
    # Wrong RunPod job id for an attempt whose id we know -> refused.
    assert client.post(f"{url}/claim", json=claim_body("rp-999"), headers=token(att)).json()["go"] is False
    # First claim wins, duplicate delivery is refused.
    assert client.post(f"{url}/claim", json=claim_body("rp-1"), headers=token(att)).json() == {"go": True, "reason": ""}
    dup = client.post(f"{url}/claim", json=claim_body("rp-1", "w2"), headers=token(att)).json()
    assert dup["go"] is False and "running" in dup["reason"]
    assert client.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {key}"}).json()["status"] == "running"
    assert client.post(f"{url}/progress", json={"stage": "rendering", "fraction": 0.5}, headers=token(att)).status_code == 200

    # Supersede the attempt the way #491's requeue will: new attempt with fence+1.
    with ctx.sessions.begin() as s:
        j = s.get(Job, att.job_id)
        s.get(Attempt, att.id).status = "stale"
        newer = Attempt(id="att_newer", job_id=j.id, project_id=j.project_id, fence=j.current_fence + 1,
                        status="submitted", runpod_endpoint="fasth3-t2v", runpod_job_id="rp-2",
                        output_prefix=f"outputs/{j.project_id}/{j.id}/att_newer")
        s.add(newer)
        j.current_fence, j.current_attempt_id, j.status = newer.fence, newer.id, "queued"
    stale = client.post(f"{url}/claim", json=claim_body("rp-1"), headers=token(att)).json()
    assert stale["go"] is False and "superseded" in stale["reason"]
    for action, body in [("progress", {"stage": "rendering"}),
                         ("fail", {"error_type": "internal", "message": "x", "retryable": True}),
                         ("complete", {"video_sha256": "0" * 64, "video_bytes": 1, "manifest_sha256": "0" * 64,
                                       "timings": {}})]:
        assert client.post(f"{url}/{action}", json=body, headers=token(att)).status_code == 409, action
    # A token for the old fence but the new attempt id is rejected too (fence mismatch).
    old_fence = sign_attempt(SECRET.encode(), "att_newer", att.fence)
    r = client.post("/worker/v1/attempts/att_newer/claim", json=claim_body("rp-2"),
                    headers={"Authorization": f"Bearer {old_fence}"}).json()
    assert r["go"] is False
    with ctx.sessions() as s:
        newer = s.get(Attempt, "att_newer")
    assert client.post("/worker/v1/attempts/att_newer/claim", json=claim_body("rp-2"),
                       headers=token(newer)).json()["go"] is True


def test_concurrent_claims_only_one_wins(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    submit(client, key, "op")
    [adm] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    results, barrier = [], threading.Barrier(8)

    def go(i):
        c = TestClient(create_app(ctx))
        barrier.wait()
        results.append(c.post(f"/worker/v1/attempts/{att.id}/claim", json=claim_body("rp-1", f"w{i}"),
                              headers=token(att)).json()["go"])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [False] * 7 + [True]


def test_uncertain_run_is_never_resubmitted_and_blocks_lane(make_ctx):
    rp = StubRunPod(outcome=RunUncertain("ReadTimeout"))
    ctx = make_ctx(runpod=rp)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    submit(client, key, "op-1")
    submit(client, key, "op-2")
    [adm] = scheduler.tick(ctx)
    for _ in range(3):
        assert scheduler.tick(ctx) == []
        scheduler.reconcile_once(ctx)
    assert len(rp.runs) == 1
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    assert att.status == "uncertain" and att.runpod_job_id is None
    # The worker's claim proves delivery and settles it.
    r = client.post(f"/worker/v1/attempts/{att.id}/claim", json=claim_body("rp-late"), headers=token(att)).json()
    assert r["go"] is True
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    assert att.status == "running" and att.runpod_job_id == "rp-late"


def test_webhook_teaches_runpod_id_and_triggers_reconcile(make_ctx):
    rp = StubRunPod(outcome=RunUncertain("504"))
    ctx = make_ctx(runpod=rp)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    submit(client, key, "op")
    [adm] = scheduler.tick(ctx)
    rp.statuses["rp-hook"] = {"id": "rp-hook", "status": "IN_PROGRESS"}
    assert client.post("/runpod/webhook/wrong", json={"id": "rp-hook"}).status_code == 404
    r = client.post(f"/runpod/webhook/hook-secret?attempt_id={adm.attempt_id}", json={"id": "rp-hook",
                                                                                       "status": "IN_PROGRESS"})
    assert r.status_code == 200
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    assert att.runpod_job_id == "rp-hook" and att.runpod_status == "IN_PROGRESS"


def test_not_delivered_requeues_or_fails(make_ctx):
    rp = StubRunPod(outcome=RunNotDelivered("connect error", permanent=False))
    ctx = make_ctx(runpod=rp)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    job_id = submit(client, key, "op").json()["id"]
    [adm] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        j, att = s.get(Job, job_id), s.get(Attempt, adm.attempt_id)
    assert att.status == "failed" and j.status == "queued" and j.current_attempt_id is None and j.not_before
    assert scheduler.tick(ctx) == []  # backoff
    with ctx.sessions.begin() as s:
        s.get(Job, job_id).not_before = None
    rp.outcome = RunNotDelivered("/run rejected: 401", permanent=True)
    [adm2] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        j, att2 = s.get(Job, job_id), s.get(Attempt, adm2.attempt_id)
    assert att2.fence == 2 and j.status == "failed" and "401" in j.error


def _put_outputs(ctx, att, video=b"MOCKVIDEO"):
    sha = hashlib.sha256(video).hexdigest()
    m = Manifest(job_id=att.job_id, attempt_id=att.id, kind="t2v", model_revision="mock-rev",
                 params=RenderParams(prompt="p"), video_sha256=sha, video_bytes=len(video), worker_id="w",
                 runpod_job_id=att.runpod_job_id or "", image_tag="t", timings=StageTimings(render_s=1.0),
                 created_at="2026-01-01T00:00:00Z")
    s3 = ctx.storage.s3
    s3.put_object(Bucket=ctx.storage.bucket, Key=f"{att.output_prefix}/video.mp4", Body=video)
    s3.put_object(Bucket=ctx.storage.bucket, Key=f"{att.output_prefix}/manifest.json", Body=m.model_dump_json().encode())
    return sha


def test_reconcile_settles_from_runpod_status_and_storage(make_ctx):
    rp = StubRunPod()
    ctx = make_ctx(runpod=rp)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    j1 = submit(client, key, "op-1").json()["id"]
    [a1] = scheduler.tick(ctx)
    # RunPod says FAILED (e.g. the complete callback was lost and the handler raised) but outputs are
    # in storage -> completed from storage.
    with ctx.sessions() as s:
        att = s.get(Attempt, a1.attempt_id)
    sha = _put_outputs(ctx, att)
    rp.statuses["rp-1"] = {"id": "rp-1", "status": "FAILED", "error": "callback timeout", "delayTime": 5,
                           "executionTime": 900}
    assert scheduler.reconcile_attempt(ctx, att.id) == "completed"
    view = client.get(f"/v1/jobs/{j1}", headers={"Authorization": f"Bearer {key}"}).json()
    assert view["status"] == "completed" and view["video_sha256"] == sha
    with ctx.sessions() as s:
        assert s.get(Attempt, att.id).timings["runpod_execution_ms"] == 900
    # COMPLETED without outputs -> failed; RunPod forgot the job (404) -> failed as lost.
    j2 = submit(client, key, "op-2").json()["id"]
    [a2] = scheduler.tick(ctx)
    rp.statuses["rp-2"] = {"id": "rp-2", "status": "COMPLETED", "output": {"skipped": True}}
    assert scheduler.reconcile_attempt(ctx, a2.attempt_id) == "failed"
    j3 = submit(client, key, "op-3").json()["id"]
    [a3] = scheduler.tick(ctx)
    rp.statuses["rp-3"] = None
    assert scheduler.reconcile_attempt(ctx, a3.attempt_id) == "failed"
    with ctx.sessions() as s:
        assert s.get(Job, j2).error.startswith("no_output") and s.get(Job, j3).error.startswith("lost")
    # Still running at RunPod -> untouched.
    submit(client, key, "op-4")
    [a4] = scheduler.tick(ctx)
    rp.statuses["rp-4"] = {"id": "rp-4", "status": "IN_PROGRESS"}
    assert scheduler.reconcile_attempt(ctx, a4.attempt_id) == "submitted"


def test_stuck_submitting_becomes_uncertain(make_ctx):
    ctx = make_ctx(submitting_grace_s=0)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    submit(client, key, "op")
    [adm] = scheduler.admit(ctx)  # crash before /run: never submitted
    assert scheduler.reconcile_attempt(ctx, adm.attempt_id) == "uncertain"


def test_complete_requires_matching_outputs(make_ctx):
    ctx = make_ctx()
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    job_id = submit(client, key, "op").json()["id"]
    [adm] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    url = f"/worker/v1/attempts/{att.id}"
    assert client.post(f"{url}/claim", json=claim_body("rp-1"), headers=token(att)).json()["go"]
    body = {"video_sha256": "a" * 64, "video_bytes": 9, "manifest_sha256": "b" * 64, "timings": {"render_s": 1}}
    r = client.post(f"{url}/complete", json=body, headers=token(att))
    assert r.status_code == 422 and "manifest" in r.json()["reason"]
    sha = _put_outputs(ctx, att)
    raw = ctx.storage.get_bytes(f"{att.output_prefix}/manifest.json")
    body = {**body, "video_sha256": sha, "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    assert client.post(f"{url}/complete", json={**body, "video_sha256": "c" * 64}, headers=token(att)).status_code == 422
    assert client.post(f"{url}/complete", json=body, headers=token(att)).status_code == 200
    assert client.post(f"{url}/complete", json=body, headers=token(att)).status_code == 200  # retried ack
    dl = client.get(f"/v1/jobs/{job_id}/download", headers={"Authorization": f"Bearer {key}"}).json()
    assert dl["video_sha256"] == sha and dl["video_bytes"] == 9


def test_cancel_active_attempt_fences_it(make_ctx):
    rp = StubRunPod()
    ctx = make_ctx(runpod=rp)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    job_id = submit(client, key, "op").json()["id"]
    [adm] = scheduler.tick(ctx)
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers={"Authorization": f"Bearer {key}"}).json()["status"] == "cancelled"
    assert rp.cancels == ["rp-1"]
    assert client.post(f"/worker/v1/attempts/{att.id}/claim", json=claim_body("rp-1"),
                       headers=token(att)).json()["go"] is False
    assert active(ctx) == []


def test_second_scheduler_stays_idle(make_ctx):
    ctx = make_ctx(scheduler_interval_s=0.05, reconcile_interval_s=0.05)
    client = TestClient(create_app(ctx))
    _, key = new_project(ctx)
    job_id = submit(client, key, "op").json()["id"]
    with ctx.engine.connect() as holder:  # stands in for the active scheduler
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": scheduler.SCHEDULER_LOCK_KEY})
        holder.commit()
        standby = scheduler.Scheduler(ctx)
        t = threading.Thread(target=standby.run_forever, daemon=True)
        t.start()
        threading.Event().wait(0.5)
        with ctx.sessions() as s:
            assert s.get(Job, job_id).attempt_count == 0
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": scheduler.SCHEDULER_LOCK_KEY})
        holder.commit()
    # The standby takes over once the lock is free.
    def admitted():
        with ctx.sessions() as s:
            return s.get(Job, job_id).attempt_count == 1
    wait_for(admitted, timeout=5, what="standby scheduler to take over")
    standby.stop.set()
    t.join(5)
    assert not t.is_alive()
