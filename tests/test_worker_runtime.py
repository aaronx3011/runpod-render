import hashlib
import json

import httpx
import pytest

from runpod_render.contracts import JobKind, RenderParams, UploadTargets, WorkerJobInput, sign_attempt
from runpod_render.worker.runtime import MockGenerator, WorkerRuntime

SECRET = b"test"


def make_input(**over) -> dict:
    inp = WorkerJobInput(
        job_id="job1", attempt_id="att1", attempt_token=sign_attempt(SECRET, "att1", 1), kind=JobKind.T2V,
        model_revision="mock-rev", params=RenderParams(prompt="p", num_frames=4),
        upload=UploadTargets(video_url="http://s3.test/v.mp4", manifest_url="http://s3.test/m.json"),
        callback_base_url="http://cp.test/worker/v1")
    return {**inp.model_dump(mode="json"), **over}


class FakeWorld:
    def __init__(self, claim_go=True):
        self.calls, self.objects, self.claim_go = [], {}, claim_go

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "s3.test":
            self.objects[req.url.path] = req.content
            return httpx.Response(200)
        action = req.url.path.rsplit("/", 1)[-1]
        self.calls.append((action, json.loads(req.content), req.headers["authorization"]))
        if action == "claim":
            return httpx.Response(200, json={"go": self.claim_go, "reason": "" if self.claim_go else "duplicate"})
        return httpx.Response(200, json={"ok": True, "reason": ""})


def runtime(world):
    return WorkerRuntime(MockGenerator(render_seconds=0), http=httpx.Client(transport=httpx.MockTransport(world)),
                         worker_id="w1", image_tag="sha-test")


def test_happy_path_uploads_before_complete():
    world = FakeWorld()
    out = runtime(world).handle({"id": "rp1", "input": make_input()})
    actions = [a for a, _, _ in world.calls]
    assert actions[0] == "claim" and actions[-1] == "complete"
    assert set(world.objects) == {"/v.mp4", "/m.json"}
    complete = world.calls[-1][1]
    assert complete["video_sha256"] == hashlib.sha256(world.objects["/v.mp4"]).hexdigest() == out["video_sha256"]
    manifest = json.loads(world.objects["/m.json"])
    assert manifest["runpod_job_id"] == "rp1" and manifest["image_tag"] == "sha-test"
    assert complete["timings"]["cold_start"] is True
    assert all(auth.startswith("Bearer ") for _, _, auth in world.calls)


def test_claim_refused_does_not_render():
    world = FakeWorld(claim_go=False)
    out = runtime(world).handle({"id": "rp1", "input": make_input()})
    assert out == {"skipped": True, "reason": "duplicate"}
    assert world.objects == {} and [a for a, _, _ in world.calls] == ["claim"]


def test_model_mismatch_reports_fail_not_retryable():
    world = FakeWorld()
    out = runtime(world).handle({"id": "rp1", "input": make_input(model_revision="other")})
    assert out["ok"] is False
    fail = next(b for a, b, _ in world.calls if a == "fail")
    assert fail["error_type"] == "invalid_input" and fail["retryable"] is False


def test_second_job_is_warm():
    world = FakeWorld()
    rt = runtime(world)
    rt.handle({"id": "rp1", "input": make_input()})
    rt.handle({"id": "rp2", "input": make_input()})
    completes = [b for a, b, _ in world.calls if a == "complete"]
    assert [c["timings"]["cold_start"] for c in completes] == [True, False]


def test_generator_crash_reports_and_raises():
    class Boom(MockGenerator):
        def generate(self, *a, **k):
            raise RuntimeError("CUDA out of memory")
    world = FakeWorld()
    rt = WorkerRuntime(Boom(), http=httpx.Client(transport=httpx.MockTransport(world)), worker_id="w")
    with pytest.raises(RuntimeError):
        rt.handle({"id": "rp1", "input": make_input()})
    fail = next(b for a, b, _ in world.calls if a == "fail")
    assert fail["error_type"] == "oom" and fail["retryable"] is False
