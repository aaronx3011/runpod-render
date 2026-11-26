# GPU worker image

RunPod Serverless worker for FastVideo FastH3 on an **RTX PRO 6000 Blackwell (96 GB, sm_120a)**,
CUDA 13.0. One image, one model per endpoint: the endpoint's RunPod model cache supplies the
weights; nothing is downloaded at run time (`HF_HUB_OFFLINE=1`).

Process start: `python -m runpod_render.worker.runpod_entry` finds the cached snapshot, loads
FastVideo's `VideoGenerator` once (settings of `cloud/fasth3_pro6000.yaml`), builds a
`WorkerRuntime`, optionally runs a warmup render, then serves jobs with concurrency 1. Per job the
runtime claims the attempt (fenced token), renders, uploads `video.mp4` + `manifest.json` to the
presigned URLs, then reports completion. Code: `src/runpod_render/worker/fastvideo_generator.py`,
`runpod_entry.py`, `runtime.py`.

## Build

Build from the repository root on an x86_64 machine; no GPU is needed.

```bash
docker buildx build -f worker/Dockerfile --platform linux/amd64 \
  --build-arg IMAGE_TAG=$(git rev-parse --short HEAD) --build-arg MAX_JOBS=4 \
  -t runpod-render-worker:$(git rev-parse --short HEAD) .
```

| Build arg | Default | Notes |
|---|---|---|
| `IMAGE_TAG` | `dev` | Git SHA of this repo; baked in as env `IMAGE_TAG`, written to every manifest |
| `MAX_JOBS` | `4` | Parallel nvcc jobs for the kernel build. Each can use 2–4 GB RAM; keep it ≤ 4 on a 15 GB box |
| `FASTVIDEO_COMMIT` | `5af9897e…` | hao-ai-lab/FastVideo commit; the kernel is built from this tree |
| `FASTVIDEO_REPO` | GitHub upstream | |
| `TORCH_CUDA_ARCH_LIST` | `12.0a` | Kernel arch. `build.sh` uses it instead of probing a GPU |
| `TORCH_BACKEND` | `cu130` | `UV_TORCH_BACKEND` for torch 2.12.0 |
| `CUDA_VERSION` | `13.0.2` | `nvidia/cuda:<ver>-cudnn-devel-ubuntu24.04` |
| `RUNTIME_IMAGE` | cudnn **devel** | Final-stage base. Kept on devel because FlashInfer/CuTe JIT-compile for sm_120a at run time and need nvcc; try `-cudnn-runtime-` only after a GPU run proves nothing JIT-compiles |
| `UV_VERSION` | `0.13.10` | |

Stages: `build` installs Python 3.12 + torch cu130, builds `fastvideo-kernel` from source with
`build.sh --wheel-dir` for 12.0a, installs FastVideo `[fasth3]` against that wheel
(`--no-sources-package fastvideo-kernel`, so uv does not build it a second time), then this repo
with the `worker` extra. `runtime` copies only `/opt/venv` and adds ffmpeg. Expect a long first
build (CUTLASS sources) and an image of roughly 15–20 GB.

## Runtime environment

| Env | Default | Meaning |
|---|---|---|
| `RENDER_KIND` | `t2v` | Which model this endpoint serves (`omni` lands in #488) |
| `MODEL_ID` | per kind | HF repo id; t2v: `FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer` |
| `HF_CACHE_ROOT` | `/runpod-volume/huggingface-cache/hub` | RunPod model-cache root (HF hub layout) |
| `MODEL_REVISION` | unset | Snapshot to load. Unset: `refs/main`, else the only snapshot. Reported as `model_revision` and must equal the job's `model_revision` |
| `MODEL_DIR` | unset | Load from this plain directory instead (local tests); its name is the revision unless `MODEL_REVISION` is set |
| `WARMUP` | `off` | `small` (512×320×25) or `full` (1344×768×124) throwaway render before taking jobs |
| `OFFLOAD_TEXT_ENCODER`, `OFFLOAD_VAE` | `0` | OOM fallbacks from `cloud/fasth3_pro6000.yaml` |
| `IMAGE_TAG` | build arg | |
| `LOG_LEVEL` | `INFO` | |

Baked-in Blackwell settings (from `cloud/vast_setup.sh` / `cloud/vast_omni.sh`):
`FASTVIDEO_ATTENTION_BACKEND=VIDEO_SPARSE_ATTN_H3`, `FASTVIDEO_VSA_TRITON=1`,
`FASTVIDEO_VSA_PTX_BLACKWELL=0`, `FASTVIDEO_FA4=0`, `FLASHINFER_CUDA_ARCH_LIST=12.0a`,
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, `FASTVIDEO_H3_VAE_TILE_BATCH=8`.
`FASTVIDEO_H3_ADALN_TABLE` is set at start to `<snapshot>/transformer/adaln_tables.pt` when that
file exists. The Omni-only switches (`FASTVIDEO_H3_VSA_FP4*`, `FASTVIDEO_H3_GATE_CHUNK_ROWS`,
`FASTVIDEO_MINIMAX_H3_FUSIONS`) belong on the Omni endpoint.

t2v jobs must leave `num_inference_steps` unset or at 9 (eight DMD forwards on a nine-point grid);
anything else fails as `invalid_input`, and t2v jobs with references are rejected.

## Local GPU test

On any Linux box with an RTX PRO 6000 (or another sm_120 card), driver ≥ 580:

```bash
hf download FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer --local-dir ./models/t2v
docker run --rm --gpus all -v "$PWD/models/t2v:/models/t2v:ro" \
  -e MODEL_DIR=/models/t2v -e MODEL_REVISION=local -e WARMUP=small \
  runpod-render-worker:<tag> \
  python -m runpod_render.worker.runpod_entry --rp_serve_api --rp_api_host 0.0.0.0
```

`--rp_serve_api` is the RunPod SDK's local test server (port 8000). A job needs a reachable
control plane for the claim/complete callbacks; the dev stack in `devstack/` provides one (point
`callback_base_url` and the upload URLs at it). To exercise only the model, run the generator
directly inside the container:

```bash
python -c "from pathlib import Path; from runpod_render.contracts import RenderParams; \
from runpod_render.worker.fastvideo_generator import from_env; g = from_env(); \
print(g.load_s, g.generate('t2v', RenderParams(prompt='a red fox in snow'), [], Path('/tmp')))"
```

## RunPod endpoint settings

| Setting | Value |
|---|---|
| GPU | RTX PRO 6000 (96 GB) |
| Allowed CUDA versions | 13.0 and newer only |
| Max workers / active workers | per plan (the control plane allows 7 renders globally) |
| Execution timeout | 1800 s |
| Idle timeout | 120–300 s |
| FlashBoot | on |
| Model (model caching) | `FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer` for t2v, `FastVideo/FastH3-Omni-NVFP4-QAD` for Omni; pin the revision and set the same value as `MODEL_REVISION_*` in the control plane |
| Container disk | ≥ 30 GB (image only; weights live in the model cache) |
| Env | `RENDER_KIND`, optionally `MODEL_REVISION`, `WARMUP` |
