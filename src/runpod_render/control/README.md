# Control plane

FastAPI + Postgres + one scheduler process. Accepts client jobs, admits at most one active render
per project (`projects.max_active`, default 1) and `MAX_ACTIVE_RENDERS` (7) in total across the t2v
and omni RunPod endpoints, submits RunPod `/run`, and settles attempts from worker callbacks and
RunPod `/status` polling.

| Module | What |
|---|---|
| `settings.py` | Configuration from the environment |
| `db.py`, `migrations/` | SQLAlchemy models and the Alembic migration (a test checks they match) |
| `service.py` | Job/attempt state transitions: submit, cancel, claim/progress/complete/fail, fencing |
| `scheduler.py` | Admission, `/run` submission, reconciler, single-instance loop |
| `api.py` | Client API, worker callbacks, RunPod webhook, health |
| `runpod.py` | RunPod queue API client (real API or `devstack/fake_runpod`) |
| `storage.py` | S3/R2 presigning and output verification |
| `cli.py` | `python -m runpod_render.control …` (also installed as `rr-control`) |

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,control]"
docker compose -f devstack/compose.yaml up -d postgres s3 fake-runpod

export DATABASE_URL=postgresql+psycopg://render:render-dev@127.0.0.1:55432/render
export ATTEMPT_TOKEN_SECRET=dev-only-secret RUNPOD_WEBHOOK_SECRET=dev-webhook-secret
export S3_ENDPOINT_URL=http://127.0.0.1:59000 S3_BUCKET=runpod-render S3_REGION=us-east-1
export S3_ACCESS_KEY_ID=dev S3_SECRET_ACCESS_KEY=dev-secret
export RUNPOD_API_BASE=http://127.0.0.1:58000 RUNPOD_API_KEY=dev-runpod-key
export RUNPOD_ENDPOINT_T2V=fasth3-t2v RUNPOD_ENDPOINT_OMNI=fasth3-omni MODEL_REVISION=mock-rev

.venv/bin/python -m runpod_render.control migrate
.venv/bin/python -m runpod_render.control ensure-bucket          # local S3 only
.venv/bin/python -m runpod_render.control create-project demo     # prints project_id and api_key ONCE
.venv/bin/python -m runpod_render.control api --port 8080 &
.venv/bin/python -m runpod_render.control scheduler
```

Note: the fake RunPod container runs the worker, which must reach `PUBLIC_BASE_URL` and the presigned
S3 URLs. With the API on the host, that works only if those addresses resolve from inside the
container; the simplest fully working local setup is the compose profile, which runs everything in
one network:

```bash
docker compose -f devstack/compose.yaml --profile control up -d --build
docker compose -f devstack/compose.yaml --profile control run --rm smoke-full
```

Tests (`pytest`) run the API, fake RunPod and mock worker in-process and need only Postgres and S3
from the stack. Point them elsewhere with `RR_TEST_DATABASE_URL` / `RR_TEST_S3_ENDPOINT`.

## Environment

Required: `DATABASE_URL`, `ATTEMPT_TOKEN_SECRET`. For real work also `PUBLIC_BASE_URL`,
`S3_*`, `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_T2V`, `RUNPOD_ENDPOINT_OMNI`, `MODEL_REVISION_T2V`,
`MODEL_REVISION_OMNI` (fallback `MODEL_REVISION`), `RUNPOD_WEBHOOK_SECRET` (no webhook is sent if
empty). `RUNPOD_API_BASE` may be `https://api.runpod.ai` or `https://api.runpod.ai/v2`.
Optional tuning, with defaults, is listed in [`.env.example`](../../../.env.example).

## API

Client (Authorization: `Bearer <project api key>`; the project always comes from the key):

| | |
|---|---|
| `POST /v1/jobs` | `SubmitJobRequest` → `JobView`. 201 new, 200 same `operation_id` + same params, 409 same `operation_id` + different params |
| `GET /v1/jobs/{id}` | `JobView` (404 for other projects' jobs) |
| `GET /v1/jobs?limit=&cursor=&status=` | `JobList`, newest first, keyset paged |
| `GET /v1/jobs/{id}/download` | `DownloadResponse`: presigned GET for video + manifest (409 unless completed) |
| `POST /v1/jobs/{id}/cancel` | cancels; an active attempt is fenced off and cancelled at RunPod |
| `POST /v1/uploads` | `CreateUploadRequest` → presigned PUT for an Omni reference; pass `upload_id` in `reference_upload_ids` |
| `GET /healthz` | DB check |

Worker (`Authorization: Bearer <attempt_token>`), see `contracts.models`:
`POST /worker/v1/attempts/{id}/claim|progress|complete|fail`. Claim returns `go=false` with a reason
when the attempt is stale, already claimed, or the job is terminal; the other callbacks return 409 for a
stale attempt. `complete` checks `manifest.json` (sha256 as reported, same attempt) and the size of
`video.mp4` in storage before completing the job.

RunPod: `POST /runpod/webhook/{RUNPOD_WEBHOOK_SECRET}?attempt_id=…` only triggers a reconcile of that
attempt (and records the RunPod id if `/run`'s response was lost). Polling stays the source of truth.

## State machine

```
job:     queued ──admit──► queued(+attempt) ──claim──► running ──complete──► completed
                                                          └──fail──► failed      (any) ──cancel──► cancelled
attempt: submitting ──/run ok──► submitted ──claim──► running ──► completed | failed
             │  └─timeout/5xx──► uncertain ──claim──► running        (stale: superseded/cancelled)
             └─connect error/429──► failed, job requeued after 30 s backoff (never reached RunPod)
```

- **Fencing.** Each attempt gets `fence = job.current_fence + 1` and a token signed over
  `(attempt_id, fence)`. A callback is accepted only if its fence equals `job.current_fence`, the
  attempt is `job.current_attempt_id`, and the attempt is not stale.
- **Exactly-once claim.** Claim locks the job row and moves the current attempt from
  submitting/submitted/uncertain to running; every other delivery gets `go=false`.
- **Uncertain** attempts are never resubmitted. They hold their lane until settled: by the worker's
  claim (which records the RunPod id), by the webhook, or (#491) by lease expiry + requeue.
- **Admission** runs in one transaction under `pg_advisory_xact_lock`; the scheduler process holds a
  session advisory lock so a second instance stays on standby.

## Hooks for #491 (reliability)

- `service.on_attempt_failed()`: M3 fails the job. Requeue here for retryable failures
  (clear `current_attempt_id`, set `not_before`); admission then creates the next fence.
- `scheduler.handle_uncertain_without_runpod_id()`: expire UNCERTAIN attempts with no claim and requeue.
- `attempts.lease_expires_at` is set on claim and extended on progress (`LEASE_S`); nothing expires it yet.
- Anything that creates attempts must take `pg_advisory_xact_lock(scheduler.ADMIT_LOCK_KEY)`.
