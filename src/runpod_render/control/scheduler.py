"""Admission, submission to RunPod and reconciliation. Runs as a single active process.

    python -m runpod_render.control scheduler

A second instance blocks on a Postgres session advisory lock and stays idle until the first
one exits. Admission additionally takes a transaction-level advisory lock, so any other code
path that creates attempts (the requeue logic of #491) serialises with it and the per-project
lane and the global cap are checked and consumed in one transaction.

Tick (every SCHEDULER_INTERVAL_S):
  1. admit(): pick pending jobs fairly (project admitted longest ago first, then oldest job),
     skipping projects at their max_active, up to MAX_ACTIVE_RENDERS active attempts in total;
     create each attempt (SUBMITTING, fence = job.current_fence + 1) and commit.
  2. submit(): POST /run per new attempt, outside any transaction. Accepted -> SUBMITTED;
     timeout/5xx -> UNCERTAIN (never resubmitted blindly); not delivered -> requeued with backoff.
Reconcile (every RECONCILE_INTERVAL_S, and per attempt from the RunPod webhook):
  poll /status for active attempts and settle them; polling is the source of truth.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from runpod_render.contracts import (
    AttemptStatus,
    JobKind,
    JobStatus,
    ReferenceInput,
    RenderParams,
    UploadTargets,
    WorkerJobInput,
    sign_attempt,
)

from . import service
from .context import Context
from .db import ACTIVE_ATTEMPT_STATUSES, Attempt, Job, Project, Upload, new_id, utcnow
from .runpod import RunAccepted, RunNotDelivered, RunPodError, RunUncertain
from .settings import ConfigError
from .storage import output_prefix

log = logging.getLogger("runpod_render.scheduler")

# Advisory lock keys (arbitrary constants, namespaced by value).
SCHEDULER_LOCK_KEY = 0x72720001  # session lock: one active scheduler
ADMIT_LOCK_KEY = 0x72720002  # xact lock: attempt creation

NOT_DELIVERED_BACKOFF_S = 30


@dataclass(frozen=True)
class Admitted:
    attempt_id: str
    job_id: str
    endpoint: str
    job_input: dict


# ---------------------------------------------------------------- admission


def active_counts(session: Session) -> tuple[int, dict[str, int]]:
    rows = session.execute(select(Attempt.project_id, func.count()).where(
        Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES)).group_by(Attempt.project_id)).all()
    per_project = {pid: n for pid, n in rows}
    return sum(per_project.values()), per_project


def _next_candidate(session: Session, full_projects: set[str]) -> tuple[Job, Project] | None:
    q = (select(Job, Project).join(Project, Project.id == Job.project_id)
         .where(Job.status == JobStatus.QUEUED.value, Job.current_attempt_id.is_(None),
                (Job.not_before.is_(None)) | (Job.not_before <= func.now()))
         .order_by(Project.last_admitted_at.asc().nulls_first(), Job.created_at, Job.id)
         .limit(1).with_for_update(of=Job, skip_locked=True))
    if full_projects:
        q = q.where(Job.project_id.not_in(full_projects))
    row = session.execute(q).first()
    return (row[0], row[1]) if row else None


def _build_input(ctx: Context, session: Session, job: Job, attempt: Attempt) -> dict:
    s = ctx.settings
    kind = JobKind(job.kind)
    refs: list[ReferenceInput] = []
    if job.reference_upload_ids:
        uploads = {u.id: u for u in session.scalars(select(Upload).where(Upload.id.in_(job.reference_upload_ids)))}
        for uid in job.reference_upload_ids:
            up = uploads[uid]
            refs.append(ReferenceInput(media_type=up.media_type, sha256=up.sha256,
                                       url=ctx.storage.presign_get(up.object_key, s.presign_ttl_s)))
    prefix = attempt.output_prefix
    inp = WorkerJobInput(
        job_id=job.id, attempt_id=attempt.id,
        attempt_token=sign_attempt(s.attempt_token_secret.encode(), attempt.id, attempt.fence),
        kind=kind, model_revision=s.model_revision_for(kind), params=RenderParams.model_validate(job.params),
        references=refs,
        upload=UploadTargets(
            video_url=ctx.storage.presign_put(f"{prefix}/video.mp4", "video/mp4", s.presign_ttl_s),
            manifest_url=ctx.storage.presign_put(f"{prefix}/manifest.json", "application/json", s.presign_ttl_s)),
        callback_base_url=s.callback_base_url)
    return inp.model_dump(mode="json")


def admit(ctx: Context) -> list[Admitted]:
    """One transaction: enforce lane + global cap, create attempts, commit."""
    admitted: list[Admitted] = []
    with ctx.sessions.begin() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADMIT_LOCK_KEY})
        total, per_project = active_counts(session)
        free = ctx.settings.max_active_renders - total
        caps = dict(session.execute(select(Project.id, Project.max_active)).all())
        full = {pid for pid, n in per_project.items() if n >= caps.get(pid, 1)}
        while free > 0:
            cand = _next_candidate(session, full)
            if cand is None:
                break
            job, project = cand
            try:
                endpoint = ctx.settings.endpoint_for(JobKind(job.kind))
            except ConfigError as exc:
                job.status, job.error, job.finished_at = JobStatus.FAILED.value, str(exc), utcnow()
                continue
            fence = job.current_fence + 1
            aid = new_id("att")
            attempt = Attempt(id=aid, job_id=job.id, project_id=job.project_id, fence=fence,
                              status=AttemptStatus.SUBMITTING.value, runpod_endpoint=endpoint,
                              output_prefix=output_prefix(job.project_id, job.id, aid))
            session.add(attempt)
            job.current_fence = fence
            job.current_attempt_id = attempt.id
            job.attempt_count += 1
            job.not_before = None
            project.last_admitted_at = utcnow()
            session.flush()
            admitted.append(Admitted(attempt.id, job.id, endpoint, _build_input(ctx, session, job, attempt)))
            free -= 1
            per_project[project.id] = per_project.get(project.id, 0) + 1
            if per_project[project.id] >= project.max_active:
                full.add(project.id)
    for a in admitted:
        log.info("admitted job %s as attempt %s on %s", a.job_id, a.attempt_id, a.endpoint)
    return admitted


# ---------------------------------------------------------------- submission


def submit(ctx: Context, a: Admitted) -> AttemptStatus:
    s = ctx.settings
    outcome = ctx.runpod.run(a.endpoint, a.job_input, webhook=s.webhook_url(a.attempt_id),
                             execution_timeout_s=s.runpod_execution_timeout_s)
    cancel_id: str | None = None
    with ctx.sessions.begin() as session:
        job = session.scalar(select(Job).where(Job.id == a.job_id).with_for_update())
        attempt = session.get(Attempt, a.attempt_id)
        session.refresh(attempt)
        now = utcnow()
        if isinstance(outcome, RunAccepted):
            if attempt.runpod_job_id is None:
                attempt.runpod_job_id = outcome.runpod_job_id
            attempt.submitted_at = now
            if attempt.status == AttemptStatus.SUBMITTING.value:
                attempt.status = AttemptStatus.SUBMITTED.value
            elif attempt.status == AttemptStatus.STALE.value:  # cancelled while we were submitting
                cancel_id = outcome.runpod_job_id
            log.info("attempt %s submitted as %s", attempt.id, outcome.runpod_job_id)
        elif isinstance(outcome, RunUncertain):
            attempt.submitted_at = now
            if attempt.status == AttemptStatus.SUBMITTING.value:
                attempt.status = AttemptStatus.UNCERTAIN.value
                attempt.error = f"/run outcome unknown: {outcome.reason}"[:2000]
            log.warning("attempt %s uncertain: %s", attempt.id, outcome.reason)
        elif isinstance(outcome, RunNotDelivered) and attempt.status == AttemptStatus.SUBMITTING.value:
            attempt.status = AttemptStatus.FAILED.value
            attempt.error_type, attempt.error = "submit_rejected", outcome.reason[:2000]
            attempt.retryable = not outcome.permanent
            attempt.finished_at = now
            if outcome.permanent:
                service.on_attempt_failed(job, attempt)
            elif job.current_attempt_id == attempt.id and job.status == JobStatus.QUEUED.value:
                # Never reached RunPod, so requeueing cannot double-render. The next attempt gets a
                # new fence, which also kills this attempt's token.
                job.current_attempt_id = None
                job.not_before = now + timedelta(seconds=NOT_DELIVERED_BACKOFF_S)
            log.warning("attempt %s not delivered: %s", attempt.id, outcome.reason)
        status = AttemptStatus(attempt.status)
    if cancel_id:
        _cancel_quietly(ctx, a.endpoint, cancel_id)
    return status


def _cancel_quietly(ctx: Context, endpoint: str, runpod_job_id: str) -> None:
    try:
        ctx.runpod.cancel(endpoint, runpod_job_id)
    except (RunPodError, httpx.HTTPError) as exc:
        log.warning("RunPod cancel of %s failed: %s", runpod_job_id, exc)


def tick(ctx: Context) -> list[Admitted]:
    admitted = admit(ctx)
    for a in admitted:
        try:
            submit(ctx, a)
        except Exception:
            log.exception("submit of attempt %s crashed", a.attempt_id)
    return admitted


# ---------------------------------------------------------------- reconciliation


def handle_uncertain_without_runpod_id(ctx: Context, attempt: Attempt) -> None:
    """An UNCERTAIN attempt we cannot poll: /run's response (and so the RunPod id) was lost.

    If the job did reach RunPod, the worker's claim records the RunPod id and moves the attempt to
    RUNNING, which settles it. Until then we wait. TODO(#491): once the attempt's lease/deadline
    expires without a claim, mark it STALE and requeue the job with a new fence (a late delivery
    of this attempt then fails the fence check at claim and never renders).
    """
    log.debug("attempt %s uncertain without RunPod id; waiting for a claim", attempt.id)


def reconcile_attempt(ctx: Context, attempt_id: str) -> str | None:
    """Settle one attempt from RunPod /status (+ storage). Returns the attempt status afterwards."""
    with ctx.sessions() as session:
        snap = session.get(Attempt, attempt_id)
        if snap is None:
            return None
        session.expunge(snap)
    if snap.status not in ACTIVE_ATTEMPT_STATUSES:
        return snap.status
    if snap.status == AttemptStatus.SUBMITTING.value:
        if snap.created_at < utcnow() - timedelta(seconds=ctx.settings.submitting_grace_s):
            # The scheduler died between commit and /run (or while waiting for it): unknown outcome.
            with ctx.sessions.begin() as session:
                session.scalar(select(Job).where(Job.id == snap.job_id).with_for_update())
                att = session.get(Attempt, attempt_id)
                session.refresh(att)
                if att.status == AttemptStatus.SUBMITTING.value:
                    att.status = AttemptStatus.UNCERTAIN.value
                    att.error = "submit did not finish; /run outcome unknown"
                return att.status
        return snap.status
    if not snap.runpod_job_id:
        handle_uncertain_without_runpod_id(ctx, snap)
        return snap.status
    try:
        st = ctx.runpod.status(snap.runpod_endpoint, snap.runpod_job_id)
    except (RunPodError, httpx.HTTPError) as exc:
        log.warning("status poll for attempt %s failed: %s", attempt_id, exc)
        return snap.status
    rp_status = None if st is None else st.get("status")
    out = None
    if rp_status is None or rp_status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
        # Look for outputs before taking the lock: a lost complete callback is settled from storage.
        out = service.verify_outputs(ctx.storage, snap)
    with ctx.sessions.begin() as session:
        job = session.scalar(select(Job).where(Job.id == snap.job_id).with_for_update())
        att = session.get(Attempt, attempt_id)
        session.refresh(att)
        att.last_reconciled_at = utcnow()
        att.runpod_status = rp_status or "NOT_FOUND"
        if st is not None:
            t = dict(att.timings or {})
            if "delayTime" in st:
                t["runpod_delay_ms"] = st["delayTime"]
            if "executionTime" in st:
                t["runpod_execution_ms"] = st["executionTime"]
            att.timings = t
        if att.status not in ACTIVE_ATTEMPT_STATUSES or job.current_attempt_id != att.id:
            return att.status
        if rp_status in (None, "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            if isinstance(out, service.VerifiedOutput):
                log.info("attempt %s settled from storage (RunPod %s)", att.id, att.runpod_status)
                service.mark_completed(job, att, out)
            elif rp_status is None:
                service.fail_attempt(job, att, error_type="lost", retryable=True,
                                     message="RunPod no longer knows the job and no outputs were stored")
            elif rp_status == "COMPLETED":
                service.fail_attempt(job, att, error_type="no_output", retryable=True,
                                     message=f"RunPod job completed without stored outputs: {st.get('output')}"[:2000])
            else:
                service.fail_attempt(job, att, error_type=f"runpod_{rp_status.lower()}", retryable=True,
                                     message=str(st.get("error") or rp_status)[:2000])
        return att.status


def reconcile_once(ctx: Context) -> dict[str, str | None]:
    with ctx.sessions() as session:
        ids = list(session.scalars(select(Attempt.id).where(Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
                                   .order_by(Attempt.created_at)))
    results = {}
    for aid in ids:
        try:
            results[aid] = reconcile_attempt(ctx, aid)
        except Exception:
            log.exception("reconcile of attempt %s failed", aid)
    return results


# ---------------------------------------------------------------- process loop


class Scheduler:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.stop = threading.Event()

    def run_forever(self) -> None:
        s = self.ctx.settings
        with self.ctx.engine.connect() as lock_conn:
            while not self.stop.is_set():
                got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHEDULER_LOCK_KEY}).scalar()
                lock_conn.commit()
                if got:
                    break
                log.info("another scheduler holds the lock; standing by")
                self.stop.wait(max(s.scheduler_interval_s, 1.0))
            if self.stop.is_set():
                return
            log.info("scheduler active (max %d renders)", s.max_active_renders)
            next_reconcile = 0.0
            try:
                while not self.stop.is_set():
                    try:
                        tick(self.ctx)
                        if time.monotonic() >= next_reconcile:
                            reconcile_once(self.ctx)
                            next_reconcile = time.monotonic() + s.reconcile_interval_s
                        lock_conn.execute(text("SELECT 1"))  # notice a dead lock connection
                        lock_conn.commit()
                    except Exception:
                        log.exception("scheduler tick failed")
                        if lock_conn.closed or lock_conn.invalidated:
                            raise
                    self.stop.wait(s.scheduler_interval_s)
            finally:
                if not lock_conn.closed and not lock_conn.invalidated:
                    lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEDULER_LOCK_KEY})
                    lock_conn.commit()
