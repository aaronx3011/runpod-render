"""Fixtures/helpers for control plane tests. They need the compose stack's Postgres and S3:

    docker compose -f devstack/compose.yaml up -d postgres s3

Override the addresses with RR_TEST_DATABASE_URL / RR_TEST_S3_ENDPOINT (e.g. when the stack
runs on other host ports). Tests skip with a message when either is unreachable.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import pytest

DEFAULT_DB = "postgresql+psycopg://render:render-dev@127.0.0.1:55432/render"
DEFAULT_S3 = "http://127.0.0.1:59000"
SECRET = "test-attempt-secret"


def reachable(url: str) -> bool:
    u = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    try:
        with socket.create_connection((u.hostname, u.port), timeout=1):
            return True
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    """Run an ASGI app with uvicorn in a background thread."""

    def __init__(self, app, port: int | None = None) -> None:
        import uvicorn

        self.port = port or free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


@dataclass
class StubRunPod:
    """In-memory RunPodClient stand-in for scheduler tests."""

    outcome: object = None  # RunOutcome to return from run(); default: accepted
    runs: list = field(default_factory=list)
    statuses: dict = field(default_factory=dict)  # runpod id -> status dict (None = 404)
    cancels: list = field(default_factory=list)

    def run(self, endpoint_id, job_input, *, webhook=None, execution_timeout_s=None):
        from runpod_render.control.runpod import RunAccepted

        self.runs.append((endpoint_id, job_input))
        if self.outcome is not None:
            return self.outcome
        rid = f"rp-{len(self.runs)}"
        self.statuses.setdefault(rid, {"id": rid, "status": "IN_QUEUE"})
        return RunAccepted(rid)

    def status(self, endpoint_id, runpod_job_id):
        return self.statuses.get(runpod_job_id)

    def cancel(self, endpoint_id, runpod_job_id):
        self.cancels.append(runpod_job_id)

    def close(self):
        pass


def make_settings(db_url: str, s3_url: str, bucket: str, **over):
    from runpod_render.control.settings import Settings

    base = {"database_url": db_url, "attempt_token_secret": SECRET, "public_base_url": "http://127.0.0.1:1",
                "s3_bucket": bucket, "s3_endpoint_url": s3_url, "s3_region": "us-east-1", "s3_access_key_id": "dev",
                "s3_secret_access_key": "dev-secret", "runpod_api_base": "http://127.0.0.1:1", "runpod_api_key": "k",
                "runpod_endpoint_t2v": "fasth3-t2v", "runpod_endpoint_omni": "fasth3-omni",
                "runpod_webhook_secret": "hook-secret", "model_revision_t2v": "mock-rev", "model_revision_omni": "mock-rev",
                "runpod_timeout_s": 2.0}
    base.update(over)
    return Settings(**base)


@pytest.fixture(scope="session")
def pg_url():
    import os

    from sqlalchemy import create_engine, text

    admin = os.environ.get("RR_TEST_DATABASE_URL", DEFAULT_DB)
    if not reachable(admin):
        pytest.skip(f"Postgres not reachable at {urlparse(admin).hostname}:{urlparse(admin).port}; start it with "
                    "`docker compose -f devstack/compose.yaml up -d postgres s3` or set RR_TEST_DATABASE_URL")
    name = f"rr_test_{secrets.token_hex(4)}"
    eng = create_engine(admin, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    from runpod_render.control.migrate import upgrade

    upgrade(url)
    yield url
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


@pytest.fixture(scope="session")
def s3_url():
    import os

    url = os.environ.get("RR_TEST_S3_ENDPOINT", DEFAULT_S3)
    if not reachable(url):
        pytest.skip(f"S3 (moto) not reachable at {url}; start the compose stack or set RR_TEST_S3_ENDPOINT")
    return url


@pytest.fixture(scope="session")
def bucket(s3_url, pg_url):
    from runpod_render.control.storage import Storage

    name = f"rr-test-{secrets.token_hex(4)}"
    Storage(make_settings(pg_url, s3_url, name)).ensure_bucket()
    return name


@pytest.fixture
def make_ctx(pg_url, s3_url, bucket):
    """Factory: make_ctx(runpod=..., **settings overrides). Tables are emptied first."""
    from sqlalchemy import text

    from runpod_render.control.context import Context
    from runpod_render.control.db import make_engine

    engine = make_engine(pg_url)
    with engine.begin() as c:
        c.execute(text("TRUNCATE attempts, jobs, uploads, projects CASCADE"))
    made = []

    def factory(runpod=None, **over):
        ctx = Context.build(make_settings(pg_url, s3_url, bucket, **over), engine=engine,
                            runpod=runpod or StubRunPod())
        made.append(ctx)
        return ctx

    yield factory
    engine.dispose()


def new_project(ctx, name=None, max_active=1):
    from runpod_render.control.service import create_project

    with ctx.sessions.begin() as s:
        project, key = create_project(s, name or f"p-{secrets.token_hex(3)}", max_active=max_active)
    return project, key


def submit(client, key, op, prompt="a lighthouse", kind="t2v", **params):
    body = {"operation_id": op, "kind": kind,
            "params": {"prompt": prompt, "num_frames": 8, "width": 320, "height": 192, **params}}
    return client.post("/v1/jobs", json=body, headers={"Authorization": f"Bearer {key}"})


def wait_for(pred, timeout=20.0, interval=0.05, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


