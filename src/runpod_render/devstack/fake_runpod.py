"""Minimal in-memory imitation of the RunPod Serverless queue API, for local development.

Implements the endpoint routes the control plane uses, with RunPod's job states:
  POST /v2/{endpoint_id}/run            {"input": {...}, "webhook"?: url, "policy"?: {...}}
  GET  /v2/{endpoint_id}/status/{id}
  POST /v2/{endpoint_id}/cancel/{id}
  GET  /v2/{endpoint_id}/health
Jobs run in a thread pool of MAX_WORKERS per endpoint, calling the handler named by
WORKER_HANDLER ("module:function"). Auth: any non-empty Bearer token.

Fault injection for reliability tests (#491), via env or POST /_faults:
  drop_run_response  -> the job is queued but /run returns 504 (simulates a lost response)
  duplicate_delivery -> each job is delivered to the handler twice
"""

from __future__ import annotations

import importlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

MAX_WORKERS = int(os.environ.get("FAKE_RUNPOD_MAX_WORKERS", "7"))


def _load_handler():
    mod, fn = os.environ.get("WORKER_HANDLER", "runpod_render.worker.mock_handler:handler").split(":")
    return getattr(importlib.import_module(mod), fn)


def create_app(handler=None) -> FastAPI:
    app = FastAPI(title="fake-runpod")
    handler = handler or _load_handler()
    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    pools: dict[str, ThreadPoolExecutor] = {}
    faults = {"drop_run_response": os.environ.get("FAULT_DROP_RUN_RESPONSE") == "1",
              "duplicate_delivery": os.environ.get("FAULT_DUPLICATE_DELIVERY") == "1"}

    def auth(authorization: str | None):
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) <= 7:
            raise HTTPException(401, "missing API key")

    def run_job(job_id: str) -> None:
        with lock:
            job = jobs[job_id]
            if job["status"] == "CANCELLED":
                return
            job.update(status="IN_PROGRESS", started=time.time())
        deliveries = 2 if faults["duplicate_delivery"] else 1
        out, err = None, None
        for _ in range(deliveries):
            try:
                out = handler({"id": job_id, "input": job["input"]})
            except Exception as exc:  # noqa: BLE001 - RunPod reports handler exceptions as FAILED
                err = f"{type(exc).__name__}: {exc}"
        with lock:
            if job["status"] == "CANCELLED":
                return
            job.update(status="FAILED" if err else "COMPLETED", output=out, error=err, finished=time.time())
        if job.get("webhook"):
            try:
                httpx.post(job["webhook"], json=_view(job), timeout=10)
            except httpx.HTTPError:
                pass

    def _view(job: dict) -> dict:
        v = {"id": job["id"], "status": job["status"]}
        if "started" in job:
            v["delayTime"] = int((job["started"] - job["created"]) * 1000)
        if "finished" in job:
            v["executionTime"] = int((job["finished"] - job["started"]) * 1000)
        if job.get("output") is not None:
            v["output"] = job["output"]
        if job.get("error"):
            v["error"] = job["error"]
        return v

    @app.post("/v2/{endpoint_id}/run")
    async def run(endpoint_id: str, request: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await request.json()
        if "input" not in body:
            raise HTTPException(400, "input required")
        job_id = f"{uuid.uuid4()}-u1"
        with lock:
            jobs[job_id] = {"id": job_id, "endpoint": endpoint_id, "status": "IN_QUEUE", "input": body["input"],
                            "webhook": body.get("webhook"), "created": time.time()}
        pool = pools.setdefault(endpoint_id, ThreadPoolExecutor(MAX_WORKERS, thread_name_prefix=endpoint_id))
        pool.submit(run_job, job_id)
        if faults["drop_run_response"]:
            raise HTTPException(504, "gateway timeout (injected)")
        return {"id": job_id, "status": "IN_QUEUE"}

    @app.get("/v2/{endpoint_id}/status/{job_id}")
    def status(endpoint_id: str, job_id: str, authorization: str | None = Header(None)):
        auth(authorization)
        with lock:
            job = jobs.get(job_id)
            if not job or job["endpoint"] != endpoint_id:
                raise HTTPException(404, "job not found")
            return _view(job)

    @app.post("/v2/{endpoint_id}/cancel/{job_id}")
    def cancel(endpoint_id: str, job_id: str, authorization: str | None = Header(None)):
        auth(authorization)
        with lock:
            job = jobs.get(job_id)
            if not job or job["endpoint"] != endpoint_id:
                raise HTTPException(404, "job not found")
            if job["status"] in ("IN_QUEUE", "IN_PROGRESS"):
                job["status"] = "CANCELLED"
            return _view(job)

    @app.get("/v2/{endpoint_id}/health")
    def health(endpoint_id: str, authorization: str | None = Header(None)):
        auth(authorization)
        with lock:
            mine = [j for j in jobs.values() if j["endpoint"] == endpoint_id]
            count = lambda s: sum(1 for j in mine if j["status"] == s)
            return {"jobs": {"inQueue": count("IN_QUEUE"), "inProgress": count("IN_PROGRESS"),
                             "completed": count("COMPLETED"), "failed": count("FAILED")},
                    "workers": {"idle": 0, "running": min(MAX_WORKERS, count("IN_PROGRESS"))}}

    @app.post("/_faults")
    def set_faults(body: dict):
        faults.update({k: bool(v) for k, v in body.items() if k in faults})
        return faults

    return app


def app_factory() -> FastAPI:  # uvicorn --factory entry
    return create_app()
