"""RunPod Serverless entrypoint for the GPU worker (`python -m runpod_render.worker.runpod_entry`).

Importing this module loads the model (once per worker process) and builds the
`WorkerRuntime`; `main()` hands the handler to the RunPod SDK with concurrency 1, since
one render fills the GPU.

Env: see worker/README.md. `WARMUP=small|full` runs one throwaway render before the
worker takes jobs, so compile/JIT cost lands in the cold start instead of the first job.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

import runpod

from runpod_render.contracts import RenderParams

from .fastvideo_generator import from_env
from .runtime import WorkerRuntime

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("runpod_render.worker.entry")

WARMUP_PARAMS = {
    "small": RenderParams(prompt="A calm lake at sunrise, gentle ripples.", width=512, height=320, num_frames=25),
    "full": RenderParams(prompt="A calm lake at sunrise, gentle ripples."),  # the production shape
}


def build_runtime() -> WorkerRuntime:
    generator = from_env()
    return WorkerRuntime(generator, progress=runpod.serverless.progress_update)


def warmup(rt: WorkerRuntime, mode: str | None) -> float | None:
    mode = (mode or "").strip().lower()
    if mode in {"", "0", "off", "false", "no"}:
        return None
    if mode not in WARMUP_PARAMS:
        raise ValueError(f"WARMUP must be one of off|small|full, got {mode!r}")
    gen = rt.generator
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rr-warmup-") as tmp:
        gen.generate(gen.kind.value, WARMUP_PARAMS[mode], [], Path(tmp))
    took = time.monotonic() - t0
    log.info("warmup (%s) took %.1fs", mode, took)
    return took


runtime = build_runtime()


def handler(job: dict) -> dict:
    return runtime.handle(job)


def concurrency(_current: int) -> int:
    return 1


def main() -> None:
    warmup(runtime, os.environ.get("WARMUP"))
    runpod.serverless.start({"handler": handler, "concurrency_modifier": concurrency})


if __name__ == "__main__":
    main()
