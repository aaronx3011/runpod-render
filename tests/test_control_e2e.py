"""End to end: control API (HTTP) + scheduler + fake RunPod running the mock worker + S3."""

import hashlib
import threading

import httpx
import pytest
from control_support import ServerThread, new_project, wait_for
from sqlalchemy import select

from runpod_render.control import scheduler
from runpod_render.control.api import create_app
from runpod_render.control.db import Attempt
from runpod_render.control.runpod import RunPodClient
from runpod_render.devstack.fake_runpod import create_app as fake_runpod_app
from runpod_render.worker.runtime import MockGenerator, WorkerRuntime


class CountingGenerator(MockGenerator):
    def __init__(self):
        super().__init__(model_revision="mock-rev", render_seconds=0.1)
        self.renders = 0
        self.lock = threading.Lock()

    def generate(self, *a, **kw):
        with self.lock:
            self.renders += 1
        return super().generate(*a, **kw)


@pytest.fixture
def stack(make_ctx):
    gen = CountingGenerator()
    runtime = WorkerRuntime(gen, worker_id="mock-worker", image_tag="test")
    claims = []

    def handler(job):
        out = runtime.handle(job)
        claims.append(not out.get("skipped"))
        return out

    api_port = None
    with ServerThread(fake_runpod_app(handler=handler)) as rp:
        from control_support import free_port

        api_port = free_port()
        ctx = make_ctx(runpod=RunPodClient(rp.url, "dev-runpod-key", timeout_s=5),
                       public_base_url=f"http://127.0.0.1:{api_port}")
        with ServerThread(create_app(ctx), port=api_port) as api:
            yield {"ctx": ctx, "api": api.url, "rp": rp.url, "gen": gen, "claims": claims}


def drive_until(ctx, pred, timeout=30, what="job"):
    def step():
        scheduler.tick(ctx)
        scheduler.reconcile_once(ctx)
        return pred()
    return wait_for(step, timeout=timeout, interval=0.1, what=what)


def job_status(api, key, job_id):
    return httpx.get(f"{api}/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {key}"}).json()


def submit(api, key, op, **params):
    r = httpx.post(f"{api}/v1/jobs", headers={"Authorization": f"Bearer {key}"},
                   json={"operation_id": op, "kind": "t2v",
                         "params": {"prompt": "a lighthouse at dusk", "num_frames": 8, "width": 320, "height": 192,
                                    **params}})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_end_to_end_completed_and_downloadable(stack):
    ctx, api = stack["ctx"], stack["api"]
    _, key = new_project(ctx)
    job = submit(api, key, "e2e-1")
    done = drive_until(ctx, lambda: job_status(api, key, job["id"])["status"] in ("completed", "failed") and
                       job_status(api, key, job["id"]))
    assert done["status"] == "completed", done
    assert done["attempts"] == 1 and done["timings"]["render_s"] > 0
    dl = httpx.get(f"{api}/v1/jobs/{job['id']}/download", headers={"Authorization": f"Bearer {key}"}).json()
    video = httpx.get(dl["url"]).content
    assert hashlib.sha256(video).hexdigest() == dl["video_sha256"] == done["video_sha256"]
    manifest = httpx.get(dl["manifest_url"]).json()
    assert manifest["job_id"] == job["id"] and manifest["video_sha256"] == dl["video_sha256"]
    assert stack["gen"].renders == 1
    # Duplicate submit after completion returns the same completed job.
    assert submit(api, key, "e2e-1")["status"] == "completed"


def test_two_projects_ten_jobs_through_fake_runpod(stack):
    ctx, api = stack["ctx"], stack["api"]
    keys = [new_project(ctx, n)[1] for n in ("a", "b")]
    jobs = [(k, submit(api, k, f"shot-{i}")["id"]) for i in range(10) for k in keys]
    peak = {"total": 0, "per": 0}

    def all_done():
        with ctx.sessions() as s:
            act = list(s.scalars(select(Attempt.project_id).where(
                Attempt.status.in_(("submitting", "submitted", "uncertain", "running")))))
        peak["total"] = max(peak["total"], len(act))
        peak["per"] = max([peak["per"], *[act.count(p) for p in set(act)]])
        return all(job_status(api, k, j)["status"] == "completed" for k, j in jobs)

    drive_until(ctx, all_done, timeout=60, what="20 jobs")
    assert peak["total"] <= 7 and peak["per"] == 1
    assert stack["gen"].renders == 20


def test_dropped_run_response_is_uncertain_then_settled_without_second_render(stack):
    ctx, api = stack["ctx"], stack["api"]
    httpx.post(f"{stack['rp']}/_faults", json={"drop_run_response": True})
    _, key = new_project(ctx)
    job = submit(api, key, "dropped")
    [adm] = scheduler.admit(ctx)
    assert scheduler.submit(ctx, adm).value in ("uncertain", "running")  # 504 from /run
    with ctx.sessions() as s:
        att = s.get(Attempt, adm.attempt_id)
    assert att.error and "unknown" in att.error
    httpx.post(f"{stack['rp']}/_faults", json={"drop_run_response": False})
    done = drive_until(ctx, lambda: job_status(api, key, job["id"])["status"] == "completed")
    assert done
    health = httpx.get(f"{stack['rp']}/v2/fasth3-t2v/health", headers={"Authorization": "Bearer k"}).json()
    assert health["jobs"]["completed"] == 1 and health["jobs"]["inQueue"] == 0  # exactly one /run reached RunPod
    assert stack["gen"].renders == 1
    with ctx.sessions() as s:
        atts = list(s.scalars(select(Attempt).where(Attempt.job_id == job["id"])))
    assert len(atts) == 1 and atts[0].status == "completed" and atts[0].runpod_job_id
    # The reconciler now knows the RunPod id (learned from the claim) and confirms it.
    scheduler.reconcile_once(ctx)
    with ctx.sessions() as s:
        assert s.get(Attempt, adm.attempt_id).status == "completed"


def test_duplicate_delivery_renders_once(stack):
    ctx, api = stack["ctx"], stack["api"]
    httpx.post(f"{stack['rp']}/_faults", json={"duplicate_delivery": True})
    _, key = new_project(ctx)
    job = submit(api, key, "dup")
    drive_until(ctx, lambda: job_status(api, key, job["id"])["status"] == "completed")
    wait_for(lambda: len(stack["claims"]) == 2, timeout=10, what="second delivery")
    httpx.post(f"{stack['rp']}/_faults", json={"duplicate_delivery": False})
    assert stack["gen"].renders == 1
    assert sorted(stack["claims"]) == [False, True]
