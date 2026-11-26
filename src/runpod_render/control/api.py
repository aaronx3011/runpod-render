"""FastAPI app: client API (/v1), worker callbacks (/worker/v1), RunPod webhook, health.

    python -m runpod_render.control api --port 8080
"""

import base64
import hmac
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from runpod_render.contracts import (
    CallbackAck,
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    CreateUploadRequest,
    CreateUploadResponse,
    DownloadResponse,
    FailRequest,
    JobList,
    JobStatus,
    JobView,
    ProgressRequest,
    SubmitJobRequest,
)

from . import scheduler, service
from .context import Context
from .db import Attempt, Job, Project, Upload, new_id
from .settings import Settings
from .storage import upload_key

log = logging.getLogger("runpod_render.api")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise service.Unauthorized("missing bearer token")
    return authorization.removeprefix("Bearer ").strip()


def _encode_cursor(job: Job) -> str:
    return base64.urlsafe_b64encode(f"{job.created_at.isoformat()}|{job.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        ts, jid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(ts), jid
    except Exception:  # noqa: BLE001
        raise service.ServiceError("invalid cursor") from None


def create_app(ctx: Context | None = None) -> FastAPI:
    app = FastAPI(title="runpod-render control plane", version="1")
    state: dict[str, Context] = {}

    def get_ctx() -> Context:
        if "ctx" not in state:
            state["ctx"] = ctx or Context.build(Settings.from_env())
        return state["ctx"]

    def get_session(c: Annotated[Context, Depends(get_ctx)]) -> Iterator[Session]:
        with c.sessions.begin() as session:
            yield session

    def get_project(session: Annotated[Session, Depends(get_session)],
                    authorization: str | None = Header(None)) -> Project:
        project = service.project_for_key(session, _bearer(authorization))
        if project is None:
            raise service.Unauthorized("invalid API key")
        return project

    Ctx = Annotated[Context, Depends(get_ctx)]
    Db = Annotated[Session, Depends(get_session)]
    Auth = Annotated[Project, Depends(get_project)]

    @app.exception_handler(service.ServiceError)
    def _service_error(request: Request, exc: service.ServiceError):
        return JSONResponse({"detail": exc.reason, "ok": False, "reason": exc.reason}, status_code=exc.status_code)

    def _view(session: Session, job: Job) -> JobView:
        current = session.get(Attempt, job.current_attempt_id) if job.current_attempt_id else None
        return service.job_view(job, current)

    def _own_job(session: Session, project: Project, job_id: str) -> Job:
        job = session.scalar(select(Job).where(Job.id == job_id, Job.project_id == project.id))
        if job is None:  # also for other projects' jobs: do not leak existence
            raise service.NotFound("job not found")
        return job

    # ------------------------------------------------------------ health

    @app.get("/healthz")
    def healthz(session: Db):
        session.execute(text("SELECT 1"))
        return {"ok": True}

    # ------------------------------------------------------------ client API

    @app.post("/v1/jobs", response_model=JobView)
    def submit_job(req: SubmitJobRequest, response: Response, session: Db,
                   project: Auth, c: Ctx):
        job, created = service.submit_job(session, c.storage, project, req)
        response.status_code = 201 if created else 200
        session.flush()
        session.refresh(job)
        return _view(session, job)

    @app.get("/v1/jobs", response_model=JobList)
    def list_jobs(session: Db, project: Auth,
                  limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                  status: JobStatus | None = None):
        q = select(Job).where(Job.project_id == project.id)
        if status is not None:
            q = q.where(Job.status == status.value)
        if cursor:
            ts, jid = _decode_cursor(cursor)
            q = q.where(tuple_(Job.created_at, Job.id) < tuple_(ts, jid))
        jobs = list(session.scalars(q.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)))
        more = len(jobs) > limit
        jobs = jobs[:limit]
        ids = [j.current_attempt_id for j in jobs if j.current_attempt_id]
        attempts = {a.id: a for a in session.scalars(select(Attempt).where(Attempt.id.in_(ids)))} if ids else {}
        return JobList(jobs=[service.job_view(j, attempts.get(j.current_attempt_id)) for j in jobs],
                       next_cursor=_encode_cursor(jobs[-1]) if more else None)

    @app.get("/v1/jobs/{job_id}", response_model=JobView)
    def get_job(job_id: str, session: Db, project: Auth):
        return _view(session, _own_job(session, project, job_id))

    @app.get("/v1/jobs/{job_id}/download", response_model=DownloadResponse)
    def download(job_id: str, session: Db, project: Auth,
                 c: Ctx):
        job = _own_job(session, project, job_id)
        if job.status != JobStatus.COMPLETED.value or not job.result_video_key:
            raise service.Conflict(f"job is {job.status}, nothing to download")
        ttl = c.settings.download_ttl_s
        return DownloadResponse(url=c.storage.presign_get(job.result_video_key, ttl),
                                manifest_url=c.storage.presign_get(job.result_manifest_key, ttl),
                                expires_at=(datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(),
                                video_sha256=job.result_video_sha256, video_bytes=job.result_video_bytes)

    @app.post("/v1/jobs/{job_id}/cancel", response_model=JobView)
    def cancel(job_id: str, background: BackgroundTasks, session: Db,
               project: Auth, c: Ctx):
        job, attempt = service.cancel_job(session, project, job_id)
        if attempt is not None and attempt.runpod_job_id:
            background.add_task(scheduler._cancel_quietly, c, attempt.runpod_endpoint, attempt.runpod_job_id)
        return _view(session, job)

    @app.post("/v1/uploads", response_model=CreateUploadResponse, status_code=201)
    def create_upload(req: CreateUploadRequest, session: Db,
                      project: Auth, c: Ctx):
        uid = new_id("upl")
        key = upload_key(project.id, uid)
        session.add(Upload(id=uid, project_id=project.id, object_key=key, media_type=req.media_type,
                           content_type=req.content_type, sha256=req.sha256))
        ttl = c.settings.presign_ttl_s
        return CreateUploadResponse(upload_id=uid, put_url=c.storage.presign_put(key, req.content_type, ttl),
                                    put_headers={"Content-Type": req.content_type},
                                    expires_at=(datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(),
                                    sha256=req.sha256)

    # ------------------------------------------------------------ worker callbacks

    @app.post("/worker/v1/attempts/{attempt_id}/claim", response_model=ClaimResponse)
    def claim(attempt_id: str, req: ClaimRequest, session: Db,
              c: Ctx, authorization: str | None = Header(None)):
        go, reason = service.claim(session, c.settings, attempt_id, _bearer(authorization), req)
        if not go:
            log.info("claim refused for attempt %s: %s", attempt_id, reason)
        return ClaimResponse(go=go, reason=reason)

    @app.post("/worker/v1/attempts/{attempt_id}/progress", response_model=CallbackAck)
    def progress(attempt_id: str, req: ProgressRequest, session: Db,
                 c: Ctx, authorization: str | None = Header(None)):
        service.progress(session, c.settings, attempt_id, _bearer(authorization), req.stage, req.fraction)
        return CallbackAck(ok=True)

    @app.post("/worker/v1/attempts/{attempt_id}/complete", response_model=CallbackAck)
    def complete(attempt_id: str, req: CompleteRequest, session: Db,
                 c: Ctx, authorization: str | None = Header(None)):
        service.complete(session, c.settings, c.storage, attempt_id, _bearer(authorization),
                         req.video_sha256, req.video_bytes, req.manifest_sha256, req.timings)
        return CallbackAck(ok=True)

    @app.post("/worker/v1/attempts/{attempt_id}/fail", response_model=CallbackAck)
    def fail(attempt_id: str, req: FailRequest, session: Db,
             c: Ctx, authorization: str | None = Header(None)):
        service.fail(session, c.settings, attempt_id, _bearer(authorization),
                     req.error_type, req.message, req.retryable)
        return CallbackAck(ok=True)

    # ------------------------------------------------------------ RunPod webhook

    @app.post("/runpod/webhook/{secret}")
    async def runpod_webhook(secret: str, request: Request, background: BackgroundTasks,
                             attempt_id: str | None = None):
        c = get_ctx()
        expected = c.settings.runpod_webhook_secret
        if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
            raise service.NotFound("not found")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        runpod_job_id = body.get("id") if isinstance(body, dict) else None
        if attempt_id is None and runpod_job_id:
            with c.sessions() as session:
                attempt_id = session.scalar(select(Attempt.id).where(Attempt.runpod_job_id == runpod_job_id))
        if attempt_id and runpod_job_id:
            # /run's response may have been lost (UNCERTAIN, id unknown): learn the id from RunPod.
            with c.sessions.begin() as session:
                att = session.get(Attempt, attempt_id)
                if att is not None and att.runpod_job_id is None and att.status in ("submitting", "uncertain"):
                    att.runpod_job_id = str(runpod_job_id)[:100]
        if attempt_id:
            # Polling stays the source of truth: the webhook only triggers a reconcile now.
            background.add_task(scheduler.reconcile_attempt, c, attempt_id)
        return {"ok": True}

    return app


def app_factory() -> FastAPI:  # uvicorn --factory entry
    return create_app()
