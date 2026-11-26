"""FastVideo-backed `Generator` for the RunPod worker.

One process serves one model (one RunPod endpoint per model). The model is loaded once,
at construction, from the RunPod model cache; `generate()` then maps each job's
`RenderParams` onto a FastVideo `GenerationRequest`.

Only text-to-video (FastH3 8-Step V2 NVFP4 Consumer) is implemented here. Omni (#488)
plugs in through `MODEL_SPECS`: add a `ModelSpec` with its own `generator_config` and
`build_request` (and whatever directory composition it needs) and select it with
`RENDER_KIND=omni`.

FastVideo is imported lazily inside `FastVideoGenerator.__init__`, so this module imports
on machines without FastVideo or a GPU (tests, the control plane).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runpod_render.contracts import JobKind, RenderParams

from .runtime import GenerationResult, InputError

log = logging.getLogger("runpod_render.worker.fastvideo")

# RunPod's model cache mounts Hugging Face snapshots here (HF hub cache layout).
DEFAULT_HF_CACHE = "/runpod-volume/huggingface-cache/hub"

T2V_MODEL_ID = "FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer"
OMNI_MODEL_ID = "FastVideo/FastH3-Omni-NVFP4-QAD"

# FastH3 8-Step V2: eight DMD forwards on a nine-point sigma grid. The checkpoint's
# trained ladder has eight rungs; FastVideo rejects any other grid for it.
T2V_GRID_POINTS = 9


# ---------------------------------------------------------------- snapshot discovery


@dataclass(frozen=True)
class Snapshot:
    path: Path
    revision: str  # HF commit hash (snapshot directory name) or MODEL_REVISION override


class SnapshotNotFound(RuntimeError):
    pass


def repo_cache_dir(model_id: str, hf_cache: str | os.PathLike[str]) -> Path:
    org, name = model_id.split("/", 1)
    return Path(hf_cache) / f"models--{org}--{name}"


def find_snapshot(model_id: str, *, hf_cache: str | os.PathLike[str] | None = None,
                  revision: str | None = None, model_dir: str | os.PathLike[str] | None = None) -> Snapshot:
    """Locate the model snapshot to load.

    Order: an explicit `model_dir` (env MODEL_DIR, for local GPU tests against a plain
    `hf download --local-dir` folder); else `{hf_cache}/models--{org}--{name}/snapshots/{rev}`
    where rev is `revision` if given, else the commit `refs/main` points to, else the only
    snapshot present. Several snapshots and no way to choose is an error, not a guess.
    """
    if model_dir:
        path = Path(model_dir)
        if not path.is_dir():
            raise SnapshotNotFound(f"MODEL_DIR {path} is not a directory")
        return Snapshot(path=path, revision=revision or path.name)

    repo = repo_cache_dir(model_id, hf_cache or DEFAULT_HF_CACHE)
    snapshots = repo / "snapshots"
    if not snapshots.is_dir():
        raise SnapshotNotFound(f"no cached snapshots for {model_id} under {repo}; "
                               "is the endpoint's model field set to this repo?")
    if revision:
        path = snapshots / revision
        if not path.is_dir():
            present = sorted(p.name for p in snapshots.iterdir() if p.is_dir())
            raise SnapshotNotFound(f"revision {revision} of {model_id} not cached (have: {present})")
        return Snapshot(path=path, revision=revision)

    ref = repo / "refs" / "main"
    if ref.is_file():
        rev = ref.read_text().strip()
        if (snapshots / rev).is_dir():
            return Snapshot(path=snapshots / rev, revision=rev)
    present = sorted(p for p in snapshots.iterdir() if p.is_dir())
    if len(present) == 1:
        return Snapshot(path=present[0], revision=present[0].name)
    raise SnapshotNotFound(f"{len(present)} snapshots of {model_id} under {snapshots} and no refs/main; "
                           "set MODEL_REVISION")


# ---------------------------------------------------------------- per-model settings


def t2v_generator_config(model_path: Path) -> dict[str, Any]:
    """`generator:` section of cloud/fasth3_pro6000.yaml: one RTX PRO 6000, everything resident."""
    return {
        "model_path": str(model_path),
        "engine": {
            "num_gpus": 1,
            "use_fsdp_inference": False,
            "quantization": {"transformer_quant": "NVFP4", "layer_profile": "h3_dit_vsa"},
            "parallelism": {"tp_size": 1, "sp_size": 1},
            "offload": {
                "dit": False,
                "dit_layerwise": False,
                "text_encoder": _env_bool("OFFLOAD_TEXT_ENCODER", False),  # OOM fallback: true
                "image_encoder": True,
                "vae": _env_bool("OFFLOAD_VAE", False),  # OOM fallback: true
                "pin_cpu_memory": True,
                "lazy_module_load": False,
            },
            "compile": {"enabled": False, "vae_enabled": True},
        },
        "pipeline": {
            "workload_type": "t2v",
            "experimental": {
                "attention_backend": "VIDEO_SPARSE_ATTN_H3",
                "inference_torch_compile": False,
                "h3_sequential_load": False,
            },
        },
    }


def t2v_request(params: RenderParams, references: list[Path], out_dir: Path) -> dict[str, Any]:
    """A `GenerationRequest` mapping (FastVideo parses it with `parse_config`).

    Passing a mapping rather than the dataclass keeps unset fields "not explicit", so the
    checkpoint's own defaults (scheduler ladder, shifts) still apply.
    """
    if references:
        raise InputError("t2v takes no references")
    steps = params.num_inference_steps or T2V_GRID_POINTS
    if steps != T2V_GRID_POINTS:
        raise InputError(f"FastH3 8-Step V2 needs num_inference_steps={T2V_GRID_POINTS} (got {steps})")
    return {
        "prompt": params.prompt,
        "negative_prompt": params.negative_prompt,
        "sampling": {
            "height": params.height,
            "width": params.width,
            "num_frames": params.num_frames,
            "fps": params.fps,
            "num_inference_steps": steps,
            "guidance_scale": 1.0,
            "batch_cfg": False,
            "seed": params.seed,
        },
        "output": {
            "output_path": str(out_dir),
            "output_video_name": "video",
            "save_video": True,
            "return_frames": False,
        },
    }


def t2v_env(snapshot: Path) -> dict[str, str]:
    """Env the t2v pipeline reads from the snapshot (set before the model loads)."""
    table = snapshot / "transformer" / "adaln_tables.pt"
    return {"FASTVIDEO_H3_ADALN_TABLE": str(table)} if table.is_file() else {}


@dataclass(frozen=True)
class ModelSpec:
    kind: JobKind
    model_id: str
    generator_config: Callable[[Path], dict[str, Any]]
    build_request: Callable[[RenderParams, list[Path], Path], dict[str, Any]]
    snapshot_env: Callable[[Path], dict[str, str]] = lambda _snapshot: {}


MODEL_SPECS: dict[JobKind, ModelSpec] = {
    JobKind.T2V: ModelSpec(kind=JobKind.T2V, model_id=T2V_MODEL_ID, generator_config=t2v_generator_config,
                           build_request=t2v_request, snapshot_env=t2v_env),
    # JobKind.OMNI: #488. Needs the composed model directory (see cloud/vast_omni.sh /
    # FastVideo examples/inference/basic/basic_fasth3_omniref_pdd.py), the Ref2VA pipeline
    # override, the h3_vsa_fp4 settings and references mapped to `inputs.references`.
}


# ---------------------------------------------------------------- generator


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


class FastVideoGenerator:
    """Loads one FastVideo model at construction and renders jobs of that kind."""

    def __init__(self, kind: JobKind | str = JobKind.T2V, *, model_id: str | None = None,
                 hf_cache: str | None = None, revision: str | None = None, model_dir: str | None = None,
                 video_generator_factory: Callable[[dict[str, Any]], Any] | None = None) -> None:
        kind = JobKind(kind)
        if kind not in MODEL_SPECS:
            raise NotImplementedError(f"no FastVideo generator for kind {kind.value!r} yet (Omni is #488)")
        self.spec = MODEL_SPECS[kind]
        self.kind = kind
        self.snapshot = find_snapshot(model_id or self.spec.model_id, hf_cache=hf_cache, revision=revision,
                                      model_dir=model_dir)
        self.model_revision = self.snapshot.revision

        for key, value in self.spec.snapshot_env(self.snapshot.path).items():
            os.environ.setdefault(key, value)  # an explicit env var wins

        config = self.spec.generator_config(self.snapshot.path)
        factory = video_generator_factory or _default_factory
        log.info("loading %s @ %s from %s", self.spec.model_id, self.model_revision, self.snapshot.path)
        t0 = time.monotonic()
        self._vg = factory(config)
        self.load_s = time.monotonic() - t0
        log.info("model loaded in %.1fs", self.load_s)

    def generate(self, kind: str, params: RenderParams, references: list[Path], out_dir: Path) -> GenerationResult:
        if JobKind(kind) != self.kind:
            raise InputError(f"this endpoint serves {self.kind.value}, job is {kind}")
        request = self.spec.build_request(params, references, out_dir)
        t0 = time.monotonic()
        result = self._vg.generate(request)
        render_s = time.monotonic() - t0
        if isinstance(result, list):  # prompt-batch expansion; we always send one prompt
            result = result[0]
        produced = getattr(result, "video_path", None)
        if not produced or not Path(produced).is_file():
            raise RuntimeError(f"FastVideo returned no video file (video_path={produced!r})")
        target = out_dir / "video.mp4"
        if Path(produced) != target:
            shutil.move(produced, target)
        return GenerationResult(video_path=target, render_s=render_s)


def _default_factory(config: dict[str, Any]) -> Any:
    from fastvideo import VideoGenerator  # heavy: torch, CUDA, kernels

    return VideoGenerator.from_config(config)


def from_env() -> FastVideoGenerator:
    """Build the generator from the worker's environment (see worker/README.md)."""
    return FastVideoGenerator(
        os.environ.get("RENDER_KIND", JobKind.T2V.value),
        model_id=os.environ.get("MODEL_ID") or None,
        hf_cache=os.environ.get("HF_CACHE_ROOT") or None,
        revision=os.environ.get("MODEL_REVISION") or None,
        model_dir=os.environ.get("MODEL_DIR") or None,
    )
