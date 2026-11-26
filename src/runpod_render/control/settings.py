"""Control plane configuration, read from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from runpod_render.contracts import JobKind


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    attempt_token_secret: str
    public_base_url: str  # what RunPod workers call back to, e.g. https://render-dev.example.com
    s3_bucket: str
    s3_endpoint_url: str | None = None
    # Endpoint used when presigning, if clients/workers reach S3 at a different address than we do.
    s3_presign_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    runpod_api_base: str = "https://api.runpod.ai"
    runpod_api_key: str = ""
    runpod_endpoint_t2v: str = ""
    runpod_endpoint_omni: str = ""
    runpod_webhook_secret: str = ""
    runpod_timeout_s: float = 10.0
    runpod_execution_timeout_s: int = 1800  # RunPod policy.executionTimeout
    model_revision_t2v: str = ""
    model_revision_omni: str = ""
    max_active_renders: int = 7
    scheduler_interval_s: float = 2.0
    reconcile_interval_s: float = 15.0
    presign_ttl_s: int = 12 * 3600  # PUT URLs must outlive RunPod queueing + render
    download_ttl_s: int = 3600
    lease_s: int = 600  # claim/progress extend attempt.lease_expires_at by this much (expiry: #491)
    submitting_grace_s: int = 60  # a SUBMITTING row older than this is treated as UNCERTAIN

    @property
    def callback_base_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/worker/v1"

    def webhook_url(self, attempt_id: str) -> str | None:
        if not self.runpod_webhook_secret:
            return None
        return f"{self.public_base_url.rstrip('/')}/runpod/webhook/{self.runpod_webhook_secret}?attempt_id={attempt_id}"

    def endpoint_for(self, kind: JobKind) -> str:
        ep = self.runpod_endpoint_t2v if kind == JobKind.T2V else self.runpod_endpoint_omni
        if not ep:
            raise ConfigError(f"no RunPod endpoint configured for kind {kind}")
        return ep

    def model_revision_for(self, kind: JobKind) -> str:
        return self.model_revision_t2v if kind == JobKind.T2V else self.model_revision_omni

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        fallback_rev = _env("MODEL_REVISION", "")
        values: dict = {
            "database_url": _env("DATABASE_URL"),
            "attempt_token_secret": _env("ATTEMPT_TOKEN_SECRET"),
            "public_base_url": _env("PUBLIC_BASE_URL", "http://localhost:8080"),
            "s3_bucket": _env("S3_BUCKET", "runpod-render"),
            "s3_endpoint_url": _env("S3_ENDPOINT_URL"),
            "s3_presign_endpoint_url": _env("S3_PRESIGN_ENDPOINT_URL"),
            "s3_region": _env("S3_REGION", "auto"),
            "s3_access_key_id": _env("S3_ACCESS_KEY_ID"),
            "s3_secret_access_key": _env("S3_SECRET_ACCESS_KEY"),
            "runpod_api_base": _env("RUNPOD_API_BASE", "https://api.runpod.ai"),
            "runpod_api_key": _env("RUNPOD_API_KEY", ""),
            "runpod_endpoint_t2v": _env("RUNPOD_ENDPOINT_T2V", ""),
            "runpod_endpoint_omni": _env("RUNPOD_ENDPOINT_OMNI", ""),
            "runpod_webhook_secret": _env("RUNPOD_WEBHOOK_SECRET", ""),
            "model_revision_t2v": _env("MODEL_REVISION_T2V", fallback_rev),
            "model_revision_omni": _env("MODEL_REVISION_OMNI", fallback_rev),
        }
        numeric = {
            "runpod_timeout_s": ("RUNPOD_TIMEOUT_S", float),
            "runpod_execution_timeout_s": ("RUNPOD_EXECUTION_TIMEOUT_S", int),
            "max_active_renders": ("MAX_ACTIVE_RENDERS", int),
            "scheduler_interval_s": ("SCHEDULER_INTERVAL_S", float),
            "reconcile_interval_s": ("RECONCILE_INTERVAL_S", float),
            "presign_ttl_s": ("PRESIGN_TTL_S", int),
            "download_ttl_s": ("DOWNLOAD_TTL_S", int),
            "lease_s": ("LEASE_S", int),
            "submitting_grace_s": ("SUBMITTING_GRACE_S", int),
        }
        for key, (env, conv) in numeric.items():
            raw = _env(env)
            if raw is not None:
                values[key] = conv(raw)
        values.update(overrides)
        missing = [k for k in ("database_url", "attempt_token_secret") if not values.get(k)]
        if missing:
            raise ConfigError(f"missing required settings: {', '.join(m.upper() for m in missing)}")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in known})
