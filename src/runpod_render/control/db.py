"""SQLAlchemy 2 schema and session helpers.

The Alembic migration in `migrations/versions` must match this module; a test compares them.

Job lifecycle (job.status is the client-facing JobStatus):
  queued (current_attempt_id NULL)  waiting for admission
  queued (current_attempt_id set)   attempt SUBMITTING/SUBMITTED/UNCERTAIN, not yet claimed
  running                           the current attempt was claimed by a worker
  completed | failed | cancelled    terminal
Fencing: job.current_fence is the fence of the only attempt allowed to act for the job.
Superseding an attempt (cancel now, lease expiry in #491) bumps the fence or marks the attempt
STALE, which kills its token even though the signature is still valid.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from runpod_render.contracts import AttemptStatus

# Attempt states that occupy a render slot (project lane + global cap).
ACTIVE_ATTEMPT_STATUSES = (
    AttemptStatus.SUBMITTING.value,
    AttemptStatus.SUBMITTED.value,
    AttemptStatus.UNCERTAIN.value,
    AttemptStatus.RUNNING.value,
)
# States from which a worker may claim the attempt.
CLAIMABLE_ATTEMPT_STATUSES = (
    AttemptStatus.SUBMITTING.value,  # /run response not processed yet, but the worker already started
    AttemptStatus.SUBMITTED.value,
    AttemptStatus.UNCERTAIN.value,  # /run response was lost; a claim proves the job reached RunPod
)

JsonB = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex of the key
    max_active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Round-robin cursor: the project admitted longest ago goes first.
    last_admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(20))
    content_type: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "operation_id", name="uq_jobs_project_operation"),
        # Scheduler: oldest pending jobs per project.
        Index("ix_jobs_pending", "project_id", "created_at",
              postgresql_where=text("status = 'queued' AND current_attempt_id IS NULL")),
        Index("ix_jobs_project_created", "project_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    operation_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))
    params: Mapped[dict] = mapped_column(JsonB)
    reference_upload_ids: Mapped[list] = mapped_column(JsonB, default=list)
    params_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    current_fence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_attempt_id: Mapped[str | None] = mapped_column(String(40))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Earliest time the scheduler may admit the job again (backoff after a non-delivered /run).
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    result_video_key: Mapped[str | None] = mapped_column(String(500))
    result_manifest_key: Mapped[str | None] = mapped_column(String(500))
    result_video_sha256: Mapped[str | None] = mapped_column(String(64))
    result_video_bytes: Mapped[int | None] = mapped_column(BigInteger)
    timings: Mapped[dict | None] = mapped_column(JsonB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "fence", name="uq_attempts_job_fence"),
        # Lane + global cap counts and the reconciler scan.
        Index("ix_attempts_active", "project_id", "status",
              postgresql_where=text("status IN ('submitting','submitted','uncertain','running')")),
        Index("ix_attempts_runpod_job", "runpod_job_id"),
        Index("ix_attempts_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))  # denormalized for the lane check
    fence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    runpod_endpoint: Mapped[str] = mapped_column(String(100))
    runpod_job_id: Mapped[str | None] = mapped_column(String(100))
    runpod_status: Mapped[str | None] = mapped_column(String(30))  # last status seen by the reconciler
    worker_id: Mapped[str | None] = mapped_column(String(200))
    stage: Mapped[str | None] = mapped_column(String(30))
    progress: Mapped[float | None] = mapped_column(Float)
    output_prefix: Mapped[str] = mapped_column(String(500))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(50))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    # Per-stage timings: worker-reported StageTimings plus RunPod delay/execution times.
    timings: Mapped[dict | None] = mapped_column(JsonB)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


def make_engine(database_url: str, **kw) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True, **kw)


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False, future=True)
