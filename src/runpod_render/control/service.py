"""Job/attempt state transitions shared by the API, the scheduler and the reconciler.

Lock order everywhere: job row (FOR UPDATE) first, then its attempts. Every transition of an
attempt happens while holding its job's row lock, so callbacks, the reconciler and cancel
serialise per job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runpod_render.contracts import (
    AttemptStatus,
    ClaimRequest,
    InvalidToken,
    JobKind,
    JobStatus,
    JobView,
    Manifest,
    Stage,
    StageTimings,
    SubmitJobRequest,
    verify_attempt,
)

from .db import (
    ACTIVE_ATTEMPT_STATUSES,
    CLAIMABLE_ATTEMPT_STATUSES,
    Attempt,
    Job,
    Project,
    Upload,
    new_id,
    utcnow,
)
from .settings import Settings
from .storage import Storage

log = logging.getLogger("runpod_render.control")

TERMINAL_JOB_STATUSES = (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)


# ---------------------------------------------------------------- keys and hashing


def new_api_key() -> str:
    return "rr_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    # Keys are 256-bit random, so a plain SHA-256 is enough (no slow KDF needed).
    return hashlib.sha256(key.encode()).hexdigest()


def create_project(session: Session, name: str, max_active: int = 1) -> tuple[Project, str]:
    key = new_api_key()
    project = Project(id=new_id("prj"), name=name, api_key_hash=hash_api_key(key), max_active=max_active)
    session.add(project)
    session.flush()
    return project, key


def project_for_key(session: Session, key: str) -> Project | None:
    return session.scalar(select(Project).where(Project.api_key_hash == hash_api_key(key)))


def params_hash(req: SubmitJobRequest) -> str:
    """Canonical hash of everything that determines the render (defaults applied)."""
    canonical = {"kind": req.kind.value, "params": req.params.model_dump(mode="json"),
                 "references": list(req.reference_upload_ids)}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- errors


class ServiceError(Exception):
    status_code = 400

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Unprocessable(ServiceError):
    status_code = 422


class Unauthorized(ServiceError):
    status_code = 401


class Forbidden(ServiceError):
    status_code = 403


class Stale(Conflict):
    """The callback's attempt is not (or no longer) the one that owns the job."""


# ---------------------------------------------------------------- client operations


def submit_job(session: Session, storage: Storage | None, project: Project,
               req: SubmitJobRequest) -> tuple[Job, bool]:
    """Create the job, or return the existing one for the same operation_id. (job, created)."""
    phash = params_hash(req)
    existing = session.scalar(select(Job).where(Job.project_id == project.id, Job.operation_id == req.operation_id))
    if existing is not None:
        if existing.params_hash != phash:
            raise Conflict("operation_id already used with different parameters")
        return existing, False
    if req.reference_upload_ids:
        if req.kind != JobKind.OMNI:
            raise Unprocessable("reference_upload_ids are only valid for omni jobs")
        uploads = {u.id: u for u in session.scalars(select(Upload).where(
            Upload.project_id == project.id, Upload.id.in_(req.reference_upload_ids)))}
        for uid in req.reference_upload_ids:
            up = uploads.get(uid)
            if up is None:
                raise Unprocessable(f"unknown upload {uid}")
            if storage is not None and storage.head(up.object_key) is None:
                raise Unprocessable(f"upload {uid} has not been uploaded yet")
    job = Job(id=new_id("job"), project_id=project.id, operation_id=req.operation_id, kind=req.kind.value,
              params=req.params.model_dump(mode="json"), reference_upload_ids=list(req.reference_upload_ids),
              params_hash=phash, status=JobStatus.QUEUED.value, current_fence=0, attempt_count=0)
    # Concurrent duplicate submits race on uq_jobs_project_operation; use a savepoint so the
    # loser can read the winner's row instead of failing.
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Job).where(Job.project_id == project.id,
                                                    Job.operation_id == req.operation_id))
        if existing is None:
            raise
        if existing.params_hash != phash:
            raise Conflict("operation_id already used with different parameters") from None
        return existing, False
    return job, True


def job_view(job: Job, current: Attempt | None) -> JobView:
    timings = None
    if job.timings:
        timings = StageTimings.model_validate({k: v for k, v in job.timings.items()
                                               if k in StageTimings.model_fields})
    stage = None
    if current is not None and current.stage and job.status == JobStatus.RUNNING.value:
        stage = Stage(current.stage) if current.stage in {s.value for s in Stage} else None
    return JobView(id=job.id, project_id=job.project_id, operation_id=job.operation_id, kind=JobKind(job.kind),
                   status=JobStatus(job.status), created_at=job.created_at.isoformat(),
                   updated_at=job.updated_at.isoformat(), attempts=job.attempt_count, error=job.error,
                   timings=timings, stage=stage, video_sha256=job.result_video_sha256,
                   video_bytes=job.result_video_bytes)


def cancel_job(session: Session, project: Project, job_id: str) -> tuple[Job, Attempt | None]:
    """Cancel; returns (job, superseded attempt to cancel at RunPod, if any)."""
    job = session.scalar(select(Job).where(Job.id == job_id, Job.project_id == project.id).with_for_update())
    if job is None:
        raise NotFound("job not found")
    if job.status == JobStatus.CANCELLED.value:
        return job, None
    if job.status in TERMINAL_JOB_STATUSES:
        raise Conflict(f"job is already {job.status}")
    attempt = session.get(Attempt, job.current_attempt_id) if job.current_attempt_id else None
    if attempt is not None and attempt.status in ACTIVE_ATTEMPT_STATUSES:
        attempt.status = AttemptStatus.STALE.value
        attempt.error = "job cancelled"
        attempt.finished_at = utcnow()
    else:
        attempt = None
    job.current_fence += 1  # kill any outstanding token for this job
    job.status = JobStatus.CANCELLED.value
    job.finished_at = utcnow()
    return job, attempt


# ---------------------------------------------------------------- worker callbacks


def load_fenced(session: Session, settings: Settings, attempt_id: str, token: str) -> tuple[Job, Attempt]:
    """Authenticate a worker callback and lock the job. Raises Stale when superseded."""
    try:
        claims = verify_attempt(settings.attempt_token_secret.encode(), token)
    except InvalidToken as exc:
        raise Unauthorized(str(exc)) from None
    if claims.attempt_id != attempt_id:
        raise Forbidden("token is for another attempt")
    attempt = session.get(Attempt, attempt_id)
    if attempt is None:
        raise NotFound("attempt not found")
    job = session.scalar(select(Job).where(Job.id == attempt.job_id).with_for_update())
    session.refresh(attempt)  # re-read under the job lock
    if claims.fence != attempt.fence:
        raise Stale("token fence does not match the attempt")
    if attempt.fence != job.current_fence or job.current_attempt_id != attempt.id:
        raise Stale("attempt superseded by a newer fence")
    if attempt.status == AttemptStatus.STALE.value:
        raise Stale("attempt is stale")
    return job, attempt


def claim(session: Session, settings: Settings, attempt_id: str, token: str,
          req: ClaimRequest) -> tuple[bool, str]:
    """The exactly-once gate: at most one delivery of the job's current attempt gets go=True."""
    try:
        job, attempt = load_fenced(session, settings, attempt_id, token)
    except Stale as exc:
        return False, exc.reason
    if job.status in TERMINAL_JOB_STATUSES:
        return False, f"job is {job.status}"
    if attempt.status not in CLAIMABLE_ATTEMPT_STATUSES:
        return False, f"attempt already {attempt.status}"
    if job.status != JobStatus.QUEUED.value:
        return False, f"job is {job.status}"
    if attempt.runpod_job_id and attempt.runpod_job_id != req.runpod_job_id:
        return False, "attempt belongs to a different RunPod job"
    now = utcnow()
    if attempt.status == AttemptStatus.UNCERTAIN.value:
        log.info("attempt %s: uncertain /run settled by claim (runpod id %s)", attempt.id, req.runpod_job_id)
    attempt.status = AttemptStatus.RUNNING.value
    attempt.runpod_job_id = req.runpod_job_id
    attempt.worker_id = req.worker_id
    attempt.claimed_at = now
    attempt.stage = Stage.CLAIMED.value
    attempt.lease_expires_at = now + timedelta(seconds=settings.lease_s)
    job.status = JobStatus.RUNNING.value
    job.started_at = job.started_at or now
    return True, ""


def progress(session: Session, settings: Settings, attempt_id: str, token: str,
             stage: Stage, fraction: float | None) -> None:
    _job, attempt = load_fenced(session, settings, attempt_id, token)
    if attempt.status != AttemptStatus.RUNNING.value:
        raise Conflict(f"attempt is {attempt.status}")
    attempt.stage = stage.value
    attempt.progress = fraction
    attempt.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_s)


@dataclass(frozen=True)
class VerifiedOutput:
    manifest: Manifest
    video_key: str
    manifest_key: str


def verify_outputs(storage: Storage, attempt: Attempt, *, video_sha256: str | None = None,
                   video_bytes: int | None = None, manifest_sha256: str | None = None) -> VerifiedOutput | str:
    """Check video.mp4 + manifest.json in the attempt's prefix. Returns the result or an error string."""
    video_key, manifest_key = f"{attempt.output_prefix}/video.mp4", f"{attempt.output_prefix}/manifest.json"
    raw = storage.get_bytes(manifest_key)
    if raw is None:
        return "manifest.json not found in storage"
    if manifest_sha256 is not None and hashlib.sha256(raw).hexdigest() != manifest_sha256:
        return "manifest.json checksum does not match the reported sha256"
    try:
        manifest = Manifest.model_validate_json(raw)
    except ValueError as exc:
        return f"manifest.json is invalid: {exc}"[:500]
    if manifest.attempt_id != attempt.id or manifest.job_id != attempt.job_id:
        return "manifest.json belongs to another attempt"
    if video_sha256 is not None and manifest.video_sha256 != video_sha256:
        return "manifest video_sha256 does not match the reported sha256"
    info = storage.head(video_key)
    if info is None:
        return "video.mp4 not found in storage"
    expected = video_bytes if video_bytes is not None else manifest.video_bytes
    if info.size != expected or info.size != manifest.video_bytes:
        return f"video.mp4 is {info.size} bytes, expected {expected}"
    return VerifiedOutput(manifest=manifest, video_key=video_key, manifest_key=manifest_key)


def mark_completed(job: Job, attempt: Attempt, out: VerifiedOutput, timings: StageTimings | None = None) -> None:
    now = utcnow()
    t = (timings or out.manifest.timings).model_dump(mode="json")
    attempt.status = AttemptStatus.COMPLETED.value
    attempt.finished_at = now
    attempt.stage = None
    attempt.timings = {**(attempt.timings or {}), **t}
    job.status = JobStatus.COMPLETED.value
    job.result_video_key = out.video_key
    job.result_manifest_key = out.manifest_key
    job.result_video_sha256 = out.manifest.video_sha256
    job.result_video_bytes = out.manifest.video_bytes
    job.timings = t
    job.error = None
    job.finished_at = now


def complete(session: Session, settings: Settings, storage: Storage, attempt_id: str, token: str,
             video_sha256: str, video_bytes: int, manifest_sha256: str, timings: StageTimings) -> None:
    job, attempt = load_fenced(session, settings, attempt_id, token)
    if attempt.status == AttemptStatus.COMPLETED.value and job.result_video_sha256 == video_sha256:
        return  # retried callback after a lost ack
    if attempt.status != AttemptStatus.RUNNING.value:
        raise Conflict(f"attempt is {attempt.status}")
    out = verify_outputs(storage, attempt, video_sha256=video_sha256, video_bytes=video_bytes,
                         manifest_sha256=manifest_sha256)
    if isinstance(out, str):
        raise Unprocessable(out)
    mark_completed(job, attempt, out, timings)


def fail_attempt(job: Job, attempt: Attempt, *, error_type: str, message: str, retryable: bool) -> None:
    now = utcnow()
    attempt.status = AttemptStatus.FAILED.value
    attempt.error_type = error_type
    attempt.error = message
    attempt.retryable = retryable
    attempt.finished_at = now
    on_attempt_failed(job, attempt)


def on_attempt_failed(job: Job, attempt: Attempt) -> None:
    """Decide what happens to the job after its current attempt failed.

    M3 policy: the job fails. TODO(#491): if attempt.retryable and the job has attempts left,
    requeue instead: keep job.status queued, set current_attempt_id = None, set not_before for
    backoff. The scheduler then admits it again with fence = current_fence + 1, which fences off
    any late callback from this attempt.
    """
    job.status = JobStatus.FAILED.value
    job.error = f"{attempt.error_type or 'error'}: {attempt.error or ''}"[:2000]
    job.finished_at = utcnow()


def fail(session: Session, settings: Settings, attempt_id: str, token: str,
         error_type: str, message: str, retryable: bool) -> None:
    job, attempt = load_fenced(session, settings, attempt_id, token)
    if attempt.status == AttemptStatus.FAILED.value:
        return  # retried callback
    if attempt.status != AttemptStatus.RUNNING.value:
        raise Conflict(f"attempt is {attempt.status}")
    fail_attempt(job, attempt, error_type=error_type, message=message, retryable=retryable)
