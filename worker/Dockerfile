# syntax=docker/dockerfile:1.7
#
# RunPod Serverless GPU worker: FastVideo FastH3 on RTX PRO 6000 Blackwell (sm_120a), CUDA 13.0.
# Build from the repository root (the context must contain pyproject.toml and src/):
#
#   docker buildx build -f worker/Dockerfile --platform linux/amd64 \
#     --build-arg IMAGE_TAG=$(git rev-parse --short HEAD) --build-arg MAX_JOBS=4 -t runpod-render-worker .
#
# No model weights are baked in: the endpoint's RunPod model cache mounts them under
# /runpod-volume/huggingface-cache/hub. See worker/README.md.

ARG CUDA_VERSION=13.0.2
ARG UBUNTU=ubuntu24.04
ARG BUILD_IMAGE=nvidia/cuda:${CUDA_VERSION}-cudnn-devel-${UBUNTU}
# The final stage keeps nvcc and the CUDA headers: FlashInfer (FLASHINFER_CUDA_ARCH_LIST) and the
# CuTe DSL kernels JIT-compile for sm_120a at run time. A -cudnn-runtime base is smaller but has
# no nvcc; only switch after a GPU run shows nothing JIT-compiles.
ARG RUNTIME_IMAGE=nvidia/cuda:${CUDA_VERSION}-cudnn-devel-${UBUNTU}
ARG UV_VERSION=0.13.10

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ------------------------------------------------------------------ build
FROM ${BUILD_IMAGE} AS build

# FastVideo commit (hao-ai-lab/FastVideo). Bump deliberately; the kernel is built from this tree.
ARG FASTVIDEO_REPO=https://github.com/hao-ai-lab/FastVideo.git
ARG FASTVIDEO_COMMIT=5af9897e75007f9c5745f3446c18a4852c9c04a8
# The build box has 15 GB RAM: each nvcc job on the CUTLASS sources can take 2-4 GB.
ARG MAX_JOBS=4
ARG TORCH_CUDA_ARCH_LIST=12.0a
ARG TORCH_BACKEND=cu130

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates build-essential \
        python3.12 python3.12-dev python3.12-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/cuda/bin:${PATH} \
    CUDA_HOME=/usr/local/cuda \
    CUDACXX=/usr/local/cuda/bin/nvcc \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_TORCH_BACKEND=${TORCH_BACKEND} \
    TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} \
    MAX_JOBS=${MAX_JOBS} \
    CMAKE_BUILD_PARALLEL_LEVEL=${MAX_JOBS}
RUN uv venv --python /usr/bin/python3.12 --seed ${VIRTUAL_ENV}

# The kernel build needs cusparse/cublas/cusolver headers (cuda-libraries-dev).
RUN test -f ${CUDA_HOME}/include/cusparse.h || { echo "cusparse.h missing in the build image"; exit 1; }

WORKDIR /src
RUN git clone --filter=blob:none ${FASTVIDEO_REPO} FastVideo \
    && git -C FastVideo checkout --detach ${FASTVIDEO_COMMIT} \
    && git -C FastVideo submodule update --init --recursive --depth 1 \
        fastvideo-kernel/include/cutlass fastvideo-kernel/include/tk

# torch first: the kernel builds with --no-build-isolation against the torch it will run with.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install "torch==2.12.0" torchvision torchaudio setuptools wheel

# fastvideo-kernel from source for sm_120a only. build.sh honours an explicit TORCH_CUDA_ARCH_LIST
# (no GPU probe), so this works on a GPU-less builder.
WORKDIR /src/FastVideo/fastvideo-kernel
RUN --mount=type=cache,target=/root/.cache/uv \
    ./build.sh --wheel-dir /wheels
WORKDIR /src

# FastVideo with the fasth3 extra. --no-sources-package keeps uv from rebuilding the kernel from
# its in-tree path source; naming the wheel built above (same version, 0.3.5) makes uv use it
# rather than the PyPI build.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-sources-package fastvideo-kernel /wheels/fastvideo_kernel-*.whl \
        "./FastVideo[fasth3]" "huggingface_hub>=1.5,<2.0" \
    && python -c "import importlib.metadata as m; print('fastvideo-kernel', m.version('fastvideo-kernel'))"

# This repository, worker extra only.
COPY pyproject.toml README.md /src/runpod-render/
COPY src /src/runpod-render/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install "/src/runpod-render[worker]"

# ------------------------------------------------------------------ runtime
FROM ${RUNTIME_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg python3.12 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

ARG IMAGE_TAG=dev
ENV IMAGE_TAG=${IMAGE_TAG} \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/cuda/bin:${PATH} \
    CUDA_HOME=/usr/local/cuda \
    PYTHONUNBUFFERED=1 \
    # Weights come only from the RunPod model cache; never download at run time.
    HF_HUB_OFFLINE=1 \
    HF_CACHE_ROOT=/runpod-volume/huggingface-cache/hub \
    RENDER_KIND=t2v \
    # Consumer-Blackwell (sm_120a) settings from cloud/vast_setup.sh and cloud/vast_omni.sh.
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    FASTVIDEO_ATTENTION_BACKEND=VIDEO_SPARSE_ATTN_H3 \
    FASTVIDEO_VSA_TRITON=1 \
    FASTVIDEO_VSA_PTX_BLACKWELL=0 \
    FASTVIDEO_FA4=0 \
    FLASHINFER_CUDA_ARCH_LIST=12.0a \
    FASTVIDEO_H3_VAE_TILE_BATCH=8
# The rest of cloud/vast_omni.sh's profile (FASTVIDEO_H3_VSA_FP4*, FASTVIDEO_H3_GATE_CHUNK_ROWS,
# FASTVIDEO_MINIMAX_H3_FUSIONS=all) is set on the Omni endpoint (#488), not here: the t2v recipe in
# cloud/vast_setup.sh was run without them and they change which kernels t2v uses.
# FASTVIDEO_H3_ADALN_TABLE is set at start-up to <snapshot>/transformer/adaln_tables.pt when present.

LABEL org.opencontainers.image.source="https://github.com/aaronx3011/runpod-render" \
      org.opencontainers.image.revision="${IMAGE_TAG}"

WORKDIR /app
CMD ["python", "-m", "runpod_render.worker.runpod_entry"]
