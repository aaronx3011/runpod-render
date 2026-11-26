"""Wire contracts shared by the control plane, the RunPod worker and the dev stack.

Every payload that crosses a process boundary is defined here, so the worker and the
control plane can be built in parallel against the same shapes. Bump SCHEMA_VERSION on
any breaking change and keep the worker tolerant of one version back.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

SCHEMA_VERSION = 1

# Hard limits enforced at submit time (decision #482). The worker re-checks them.
MAX_WIDTH = 1344
MAX_HEIGHT = 768
MAX_FRAMES = 124
MAX_REFERENCES = 8


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JobKind(StrEnum):
    T2V = "t2v"
    OMNI = "omni"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    SUBMITTING = "submitting"  # row written, /run not yet acknowledged
    SUBMITTED = "submitted"  # RunPod returned a job id
    UNCERTAIN = "uncertain"  # /run timed out or errored ambiguously; must be reconciled, never resubmitted blindly
    RUNNING = "running"  # worker claimed it
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"  # superseded by a newer attempt (lease expired / cancelled); its fencing token is dead


class Stage(StrEnum):
    CLAIMED = "claimed"
    FETCHING_INPUTS = "fetching_inputs"
    RENDERING = "rendering"
    ENCODING = "encoding"
    UPLOADING = "uploading"


# ---------------------------------------------------------------- render inputs


class ReferenceInput(_Strict):
    """An Omni reference (ordered). The worker downloads it from a presigned GET URL."""

    media_type: Literal["image", "video", "audio"]
    url: HttpUrl
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RenderParams(_Strict):
    prompt: Annotated[str, Field(min_length=1, max_length=4000)]
    negative_prompt: Annotated[str, Field(max_length=4000)] = ""
    width: Annotated[int, Field(ge=64, le=MAX_WIDTH, multiple_of=16)] = MAX_WIDTH
    height: Annotated[int, Field(ge=64, le=MAX_HEIGHT, multiple_of=16)] = MAX_HEIGHT
    num_frames: Annotated[int, Field(ge=1, le=MAX_FRAMES)] = MAX_FRAMES
    fps: Annotated[int, Field(ge=1, le=60)] = 24
    # None = the model contract's default (FastH3 t2v: 9 sigma points; Omni: contract num_inference_steps).
    num_inference_steps: Annotated[int, Field(ge=1, le=64)] | None = None
    seed: Annotated[int, Field(ge=0, le=2**32 - 1)] = 1000


# ---------------------------------------------------------------- control plane -> worker


class UploadTargets(_Strict):
    """Presigned PUT URLs for this attempt's immutable output paths."""

    video_url: HttpUrl
    manifest_url: HttpUrl


class WorkerJobInput(_Strict):
    """The `input` object of a RunPod /run request."""

    schema_version: Literal[1] = SCHEMA_VERSION
    job_id: str
    attempt_id: str
    # Opaque signed token (see contracts.tokens). Sent back on every callback; a stale
    # token is rejected, which is how a superseded attempt is fenced off.
    attempt_token: str
    kind: JobKind
    model_revision: str  # pinned HF revision the endpoint caches; worker refuses a mismatch
    params: RenderParams
    references: Annotated[list[ReferenceInput], Field(max_length=MAX_REFERENCES)] = []
    upload: UploadTargets
    callback_base_url: HttpUrl  # e.g. https://render-dev.example.com/worker/v1


# ---------------------------------------------------------------- worker -> control plane callbacks
# POST {callback_base_url}/attempts/{attempt_id}/claim|progress|complete|fail
# Header: Authorization: Bearer <attempt_token>


class ClaimRequest(_Strict):
    runpod_job_id: str
    worker_id: str  # RUNPOD_POD_ID or hostname
    model_revision: str  # what the worker actually loaded


class ClaimResponse(_Strict):
    go: bool
    # When go is False the worker must return immediately without rendering
    # (duplicate delivery, cancelled job, or a newer attempt owns the job).
    reason: str = ""


class ProgressRequest(_Strict):
    stage: Stage
    fraction: Annotated[float, Field(ge=0, le=1)] | None = None


class StageTimings(_Strict):
    """Seconds spent per stage inside the worker. Cold start is measured at worker boot."""

    model_load_s: float | None = None  # only on the first job of a worker (cold start)
    fetch_inputs_s: float = 0
    render_s: float = 0
    encode_s: float = 0
    upload_s: float = 0
    cold_start: bool = False


class CompleteRequest(_Strict):
    video_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    video_bytes: Annotated[int, Field(gt=0)]
    manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    timings: StageTimings


class FailRequest(_Strict):
    error_type: str  # e.g. "oom", "invalid_input", "model_mismatch", "internal"
    message: Annotated[str, Field(max_length=2000)]
    retryable: bool


class CallbackAck(_Strict):
    ok: bool
    reason: str = ""


# ---------------------------------------------------------------- stored artifact


class Manifest(_Strict):
    """manifest.json written next to video.mp4 in the attempt's output prefix."""

    schema_version: Literal[1] = SCHEMA_VERSION
    job_id: str
    attempt_id: str
    kind: JobKind
    model_revision: str
    params: RenderParams
    reference_sha256: list[str] = []
    video_sha256: str
    video_bytes: int
    worker_id: str
    runpod_job_id: str
    image_tag: str  # worker image git SHA
    timings: StageTimings
    created_at: str  # ISO 8601 UTC


# ---------------------------------------------------------------- client-facing API


class SubmitJobRequest(_Strict):
    operation_id: Annotated[str, Field(min_length=1, max_length=200)]  # idempotency key within a project
    kind: JobKind
    params: RenderParams
    # Upload ids returned by POST /uploads (Omni only), in reference order.
    reference_upload_ids: Annotated[list[str], Field(max_length=MAX_REFERENCES)] = []


class JobView(_Strict):
    id: str
    project_id: str
    operation_id: str
    kind: JobKind
    status: JobStatus
    created_at: str
    updated_at: str
    attempts: int
    error: str | None = None
    timings: StageTimings | None = None
