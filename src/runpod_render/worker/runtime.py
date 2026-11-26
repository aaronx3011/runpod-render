"""GPU-agnostic worker runtime: claim -> fetch inputs -> render -> upload -> complete.

The RunPod entrypoint (worker/runpod_entry.py, task #485) builds one `WorkerRuntime` at
process start, which loads the model once, and calls `runtime.handle(job)` per job.
Generators are pluggable: `MockGenerator` here for the dev stack and tests, the FastVideo
generator in #485.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from runpod_render.contracts import (
    CallbackAck,
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    FailRequest,
    Manifest,
    ProgressRequest,
    ReferenceInput,
    RenderParams,
    Stage,
    StageTimings,
    WorkerJobInput,
)

log = logging.getLogger("runpod_render.worker")


@dataclass
class GenerationResult:
    video_path: Path
    render_s: float
    encode_s: float = 0.0


class Generator(Protocol):
    model_revision: str
    load_s: float  # seconds the model took to load at startup

    def generate(self, kind: str, params: RenderParams, references: list[Path], out_dir: Path) -> GenerationResult: ...


class InputError(Exception):
    """Bad job input; not retryable."""


class WorkerRuntime:
    def __init__(
        self,
        generator: Generator,
        *,
        http: httpx.Client | None = None,
        worker_id: str | None = None,
        image_tag: str | None = None,
        progress: Callable[[dict, str], None] | None = None,
    ) -> None:
        self.generator = generator
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))
        self.worker_id = worker_id or os.environ.get("RUNPOD_POD_ID") or socket.gethostname()
        self.image_tag = image_tag or os.environ.get("IMAGE_TAG", "dev")
        self._progress = progress or (lambda job, msg: None)
        self._jobs_done = 0

    # ------------------------------------------------------------ callbacks
    def _post(self, inp: WorkerJobInput, action: str, body) -> dict:
        url = f"{str(inp.callback_base_url).rstrip('/')}/attempts/{inp.attempt_id}/{action}"
        r = self.http.post(url, json=body.model_dump(mode="json"),
                           headers={"Authorization": f"Bearer {inp.attempt_token}"})
        r.raise_for_status()
        return r.json()

    def _stage(self, job: dict, inp: WorkerJobInput, stage: Stage, fraction: float | None = None) -> None:
        self._progress(job, stage.value)
        try:
            self._post(inp, "progress", ProgressRequest(stage=stage, fraction=fraction))
        except httpx.HTTPError as exc:  # progress is best effort
            log.warning("progress callback failed: %s", exc)

    # ------------------------------------------------------------ job
    def handle(self, job: dict) -> dict:
        """RunPod handler body. Returns a small JSON result; the real output lives in object storage."""
        inp = WorkerJobInput.model_validate(job["input"])
        claim = ClaimResponse.model_validate(self._post(inp, "claim", ClaimRequest(
            runpod_job_id=job["id"], worker_id=self.worker_id, model_revision=self.generator.model_revision)))
        if not claim.go:
            log.info("attempt %s not claimed: %s", inp.attempt_id, claim.reason)
            return {"skipped": True, "reason": claim.reason}
        try:
            return self._run(job, inp)
        except InputError as exc:
            self._fail(inp, "invalid_input", str(exc), retryable=False)
            return {"ok": False, "error": "invalid_input"}
        except Exception as exc:  # report, then let RunPod mark the job failed too
            kind = "oom" if "out of memory" in str(exc).lower() else "internal"
            self._fail(inp, kind, f"{type(exc).__name__}: {exc}", retryable=kind != "oom")
            raise

    def _fail(self, inp: WorkerJobInput, error_type: str, message: str, retryable: bool) -> None:
        try:
            self._post(inp, "fail", FailRequest(error_type=error_type, message=message[:2000], retryable=retryable))
        except httpx.HTTPError as exc:
            log.error("fail callback failed: %s", exc)

    def _run(self, job: dict, inp: WorkerJobInput) -> dict:
        if inp.model_revision != self.generator.model_revision:
            raise InputError(f"endpoint serves {self.generator.model_revision}, job wants {inp.model_revision}")
        cold = self._jobs_done == 0
        with tempfile.TemporaryDirectory(prefix=f"rr-{inp.attempt_id}-") as tmp:
            work = Path(tmp)
            self._stage(job, inp, Stage.FETCHING_INPUTS)
            t0 = time.monotonic()
            refs = [self._fetch(ref, work / f"ref{i}") for i, ref in enumerate(inp.references)]
            fetch_s = time.monotonic() - t0

            self._stage(job, inp, Stage.RENDERING)
            result = self.generator.generate(inp.kind.value, inp.params, refs, work)

            self._stage(job, inp, Stage.UPLOADING)
            t0 = time.monotonic()
            video = result.video_path.read_bytes()
            video_sha = hashlib.sha256(video).hexdigest()
            timings = StageTimings(model_load_s=self.generator.load_s if cold else None, fetch_inputs_s=fetch_s,
                                   render_s=result.render_s, encode_s=result.encode_s, cold_start=cold)
            self._put(str(inp.upload.video_url), video, "video/mp4")
            manifest = Manifest(
                job_id=inp.job_id, attempt_id=inp.attempt_id, kind=inp.kind, model_revision=inp.model_revision,
                params=inp.params, reference_sha256=[r.sha256 for r in inp.references], video_sha256=video_sha,
                video_bytes=len(video), worker_id=self.worker_id, runpod_job_id=job["id"],
                image_tag=self.image_tag, timings=timings, created_at=datetime.now(UTC).isoformat())
            manifest_bytes = manifest.model_dump_json(indent=2).encode()
            self._put(str(inp.upload.manifest_url), manifest_bytes, "application/json")
            timings = timings.model_copy(update={"upload_s": time.monotonic() - t0})

        # Upload first, publish completion second. A lost ack is recovered by the reconciler (#491).
        ack = CallbackAck.model_validate(self._post(inp, "complete", CompleteRequest(
            video_sha256=video_sha, video_bytes=len(video),
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), timings=timings)))
        self._jobs_done += 1
        return {"ok": ack.ok, "reason": ack.reason, "video_sha256": video_sha}

    def _fetch(self, ref: ReferenceInput, dest: Path) -> Path:
        r = self.http.get(str(ref.url))
        r.raise_for_status()
        if hashlib.sha256(r.content).hexdigest() != ref.sha256:
            raise InputError("reference checksum mismatch")
        dest.write_bytes(r.content)
        return dest

    def _put(self, url: str, data: bytes, content_type: str) -> None:
        r = self.http.put(url, content=data, headers={"Content-Type": content_type})
        r.raise_for_status()


class MockGenerator:
    """GPU-less stand-in. Writes a deterministic fake 'video' after a short sleep."""

    def __init__(self, model_revision: str = "mock-rev", render_seconds: float = 0.05) -> None:
        self.model_revision = model_revision
        self.render_seconds = render_seconds
        self.load_s = 0.0

    def generate(self, kind, params, references, out_dir):
        t0 = time.monotonic()
        time.sleep(self.render_seconds)
        out = out_dir / "video.mp4"
        payload = f"MOCKMP4|{kind}|{params.model_dump_json()}|{len(references)}".encode()
        out.write_bytes(payload)
        return GenerationResult(video_path=out, render_s=time.monotonic() - t0)
