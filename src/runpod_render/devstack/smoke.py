"""End-to-end smoke test of the dev stack, with a stand-in control plane.

Runs a tiny callback server (claim/progress/complete/fail with real token checks), presigns
S3 URLs for one attempt, submits a job to fake RunPod, waits for COMPLETED, then checks
that video.mp4 and manifest.json exist in the bucket with the reported checksum.

    docker compose -f devstack/compose.yaml run --rm smoke

Exits 0 on success. The real control plane (#489) replaces the stand-in.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
import uuid

import boto3
import httpx
import uvicorn
from botocore.config import Config
from fastapi import FastAPI, Header, HTTPException

from runpod_render.contracts import (
    ClaimRequest,
    CompleteRequest,
    JobKind,
    RenderParams,
    UploadTargets,
    WorkerJobInput,
    sign_attempt,
    verify_attempt,
)

SECRET = os.environ.get("ATTEMPT_TOKEN_SECRET", "dev-only-secret").encode()
RUNPOD = os.environ.get("RUNPOD_API_BASE", "http://fake-runpod:8000").rstrip("/")
ENDPOINT = os.environ.get("RUNPOD_ENDPOINT_T2V", "fasth3-t2v")
S3 = os.environ.get("S3_ENDPOINT_URL", "http://s3:9000")
BUCKET = os.environ.get("S3_BUCKET", "runpod-render")
# `compose run` containers get no service alias; Docker DNS does resolve the short container id.
CALLBACK_HOST = os.environ.get("SMOKE_CALLBACK_HOST") or socket.gethostname()
PORT = 8790

events: list[tuple[str, dict]] = []
app = FastAPI()


def _check(attempt_id: str, authorization: str | None) -> None:
    claims = verify_attempt(SECRET, (authorization or "").removeprefix("Bearer "))
    if claims.attempt_id != attempt_id:
        raise HTTPException(403, "token for another attempt")


@app.post("/worker/v1/attempts/{attempt_id}/{action}")
def callback(attempt_id: str, action: str, body: dict, authorization: str | None = Header(None)):
    _check(attempt_id, authorization)
    if action == "claim":
        ClaimRequest.model_validate(body)
    elif action == "complete":
        CompleteRequest.model_validate(body)
    events.append((action, body))
    if action == "claim":
        return {"go": True, "reason": ""}
    return {"ok": True, "reason": ""}


def main() -> int:
    threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning"),
                     daemon=True).start()
    s3 = boto3.client("s3", endpoint_url=S3, aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"], region_name=os.environ.get("S3_REGION", "auto"),
                      config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    try:  # the local S3 starts empty; R2 buckets are created by the owner
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    job_id, attempt_id = f"job_{uuid.uuid4().hex[:12]}", f"att_{uuid.uuid4().hex[:12]}"
    prefix = f"outputs/smoke/{job_id}/{attempt_id}"
    presign = lambda key, ctype: s3.generate_presigned_url(
        "put_object", Params={"Bucket": BUCKET, "Key": key, "ContentType": ctype}, ExpiresIn=3600)
    inp = WorkerJobInput(
        job_id=job_id, attempt_id=attempt_id, attempt_token=sign_attempt(SECRET, attempt_id, 1),
        kind=JobKind.T2V, model_revision=os.environ.get("MODEL_REVISION", "mock-rev"),
        params=RenderParams(prompt="smoke test: a lighthouse at dusk", num_frames=8, width=320, height=192),
        upload=UploadTargets(video_url=presign(f"{prefix}/video.mp4", "video/mp4"),
                             manifest_url=presign(f"{prefix}/manifest.json", "application/json")),
        callback_base_url=f"http://{CALLBACK_HOST}:{PORT}/worker/v1")
    headers = {"Authorization": "Bearer dev-runpod-key"}
    time.sleep(0.5)
    r = httpx.post(f"{RUNPOD}/v2/{ENDPOINT}/run", json={"input": inp.model_dump(mode="json")}, headers=headers)
    r.raise_for_status()
    rp_id = r.json()["id"]
    print("submitted", rp_id)
    deadline = time.time() + 60
    while time.time() < deadline:
        st = httpx.get(f"{RUNPOD}/v2/{ENDPOINT}/status/{rp_id}", headers=headers).json()
        if st["status"] in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            break
        time.sleep(0.5)
    print("runpod status:", json.dumps(st))
    actions = [a for a, _ in events]
    print("callbacks:", actions)
    video = s3.get_object(Bucket=BUCKET, Key=f"{prefix}/video.mp4")["Body"].read()
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json")["Body"].read())
    complete = next(b for a, b in events if a == "complete")
    ok = (st["status"] == "COMPLETED" and actions[0] == "claim" and actions[-1] == "complete"
          and hashlib.sha256(video).hexdigest() == complete["video_sha256"] == manifest["video_sha256"])
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
