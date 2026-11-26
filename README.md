# runpod-render

Modal-like batch video rendering for FastVideo's FastH3 models on **RunPod Serverless**:
a CPU control plane accepts and schedules jobs, GPU workers render bounded tasks, R2 holds
the results, and RunPod's model cache keeps the weights next to the GPUs.

```
Clients ─► control plane (FastAPI + Postgres + scheduler)        src/runpod_render/control/
              │ POST /run (only when the project's lane is free, ≤7 active)
              ▼
        RunPod endpoint fasth3-t2v / fasth3-omni ─► worker       src/runpod_render/worker/
              ▲ claim → progress → complete (signed, fenced)       │
              └────────────────────────────────────────────────── ▼
                                         R2: outputs/{project}/{job}/{attempt}/video.mp4 + manifest.json
```

All cross-process payloads are in [`src/runpod_render/contracts`](src/runpod_render/contracts/models.py).

## Layout

| Path | What |
|---|---|
| `src/runpod_render/contracts/` | Wire models (worker input, callbacks, manifest, client API) and signed attempt tokens |
| `src/runpod_render/worker/` | GPU-agnostic runtime (claim → render → upload → complete), mock generator; FastVideo generator + image in `worker/` (#485) |
| `src/runpod_render/control/` | Control plane: schema, API, scheduler, reconciler (#489, #491) |
| `src/runpod_render/devstack/` | Fake RunPod queue API (with fault injection) and the end-to-end smoke test |
| `devstack/compose.yaml` | Local Postgres, moto S3 (R2 stand-in), fake RunPod running the mock worker |

## Local development (no GPU, no accounts)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
docker compose -f devstack/compose.yaml up -d --build
docker compose -f devstack/compose.yaml run --rm smoke     # prints SMOKE OK
docker compose -f devstack/compose.yaml down
```

Host ports (loopback only): Postgres `55432`, S3 `59000`, fake RunPod `58000`, control API `58080`
(override with `RR_PG_PORT`, `RR_S3_PORT`, `RR_RUNPOD_PORT`, `RR_API_PORT`).

Control plane: see [`src/runpod_render/control/README.md`](src/runpod_render/control/README.md). With the
stack up, `pip install -e ".[dev,control]"` and `pytest` also runs the control plane tests against it
(they skip when Postgres/S3 are not reachable). Full stack including the real control plane:

```bash
docker compose -f devstack/compose.yaml --profile control up -d --build
docker compose -f devstack/compose.yaml --profile control run --rm smoke-full   # prints SMOKE-FULL OK
```

## Environments

| | Control plane | CI/CD + image builds | GPU |
|---|---|---|---|
| dev | `personal-database` behind Cloudflare Tunnel | self-hosted runner on `lesbian` | RunPod Serverless |
| prod | Hetzner VPS | `lesbian` | RunPod Serverless |

Configuration comes from the environment; see [`.env.example`](.env.example). Never commit `.env`.
This repository is public: no secrets, hostnames of private machines or credentials in commits.

## Key rules

- Jobs are idempotent on `(project, operation_id, params)`; the project comes from authentication.
- One active render per project, `MAX_ACTIVE_RENDERS` (7) globally across both endpoints.
- A `/run` that times out marks the attempt **uncertain**; it is reconciled, never blindly resubmitted.
- Workers must **claim** an attempt before rendering; a stale fencing token is refused.
- Upload video + manifest first, then report completion.
