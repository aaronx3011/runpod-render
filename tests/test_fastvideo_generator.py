"""FastVideo generator + RunPod entrypoint, with FastVideo and the RunPod SDK mocked (no GPU)."""

import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from runpod_render.contracts import JobKind, RenderParams
from runpod_render.worker import fastvideo_generator as fvg
from runpod_render.worker.runtime import InputError, WorkerRuntime

REV = "0123456789abcdef0123456789abcdef01234567"


def make_cache(root: Path, model_id: str = fvg.T2V_MODEL_ID, revs=(REV,), main: str | None = REV) -> Path:
    repo = fvg.repo_cache_dir(model_id, root)
    for rev in revs:
        (repo / "snapshots" / rev / "transformer").mkdir(parents=True)
    if main:
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text(main + "\n")
    return repo


# ---------------------------------------------------------------- snapshot discovery


def test_snapshot_from_refs_main(tmp_path):
    make_cache(tmp_path, revs=(REV, "f" * 40))
    snap = fvg.find_snapshot(fvg.T2V_MODEL_ID, hf_cache=tmp_path)
    assert snap.revision == REV
    assert snap.path == tmp_path / "models--FastVideo--FastVideo-FastH3-8-Step-V2-NVFP4-Consumer" / "snapshots" / REV


def test_snapshot_single_without_refs(tmp_path):
    make_cache(tmp_path, main=None)
    assert fvg.find_snapshot(fvg.T2V_MODEL_ID, hf_cache=tmp_path).revision == REV


def test_snapshot_ambiguous_without_refs(tmp_path):
    make_cache(tmp_path, revs=(REV, "f" * 40), main=None)
    with pytest.raises(fvg.SnapshotNotFound, match="MODEL_REVISION"):
        fvg.find_snapshot(fvg.T2V_MODEL_ID, hf_cache=tmp_path)


def test_snapshot_explicit_revision(tmp_path):
    make_cache(tmp_path, revs=(REV, "f" * 40))
    assert fvg.find_snapshot(fvg.T2V_MODEL_ID, hf_cache=tmp_path, revision="f" * 40).revision == "f" * 40
    with pytest.raises(fvg.SnapshotNotFound, match="not cached"):
        fvg.find_snapshot(fvg.T2V_MODEL_ID, hf_cache=tmp_path, revision="e" * 40)


def test_snapshot_missing_repo(tmp_path):
    with pytest.raises(fvg.SnapshotNotFound, match="model field"):
        fvg.find_snapshot(fvg.T2V_MODEL_ID, hf_cache=tmp_path)


def test_snapshot_model_dir_override(tmp_path):
    d = tmp_path / "FastH3-8-Step-V2-NVFP4-Consumer"
    d.mkdir()
    assert fvg.find_snapshot(fvg.T2V_MODEL_ID, model_dir=d) == fvg.Snapshot(d, "FastH3-8-Step-V2-NVFP4-Consumer")
    assert fvg.find_snapshot(fvg.T2V_MODEL_ID, model_dir=d, revision="pinned").revision == "pinned"


# ---------------------------------------------------------------- params -> request


def test_t2v_request_mapping(tmp_path):
    p = RenderParams(prompt="a fox", negative_prompt="blur", width=1024, height=576, num_frames=61, fps=24, seed=7)
    req = fvg.t2v_request(p, [], tmp_path)
    assert req["prompt"] == "a fox" and req["negative_prompt"] == "blur"
    assert req["sampling"] == {"height": 576, "width": 1024, "num_frames": 61, "fps": 24, "num_inference_steps": 9,
                               "guidance_scale": 1.0, "batch_cfg": False, "seed": 7}
    assert req["output"] == {"output_path": str(tmp_path), "output_video_name": "video", "save_video": True,
                             "return_frames": False}


def test_t2v_request_rejects_other_step_counts_and_references(tmp_path):
    assert fvg.t2v_request(RenderParams(prompt="x", num_inference_steps=9), [], tmp_path)["sampling"][
        "num_inference_steps"] == 9
    with pytest.raises(InputError, match="num_inference_steps=9"):
        fvg.t2v_request(RenderParams(prompt="x", num_inference_steps=4), [], tmp_path)
    with pytest.raises(InputError, match="no references"):
        fvg.t2v_request(RenderParams(prompt="x"), [tmp_path / "ref0"], tmp_path)


def test_t2v_generator_config_matches_pro6000_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("OFFLOAD_VAE", raising=False)
    monkeypatch.setenv("OFFLOAD_TEXT_ENCODER", "1")
    cfg = fvg.t2v_generator_config(tmp_path)
    assert cfg["model_path"] == str(tmp_path)
    assert cfg["engine"]["quantization"] == {"transformer_quant": "NVFP4", "layer_profile": "h3_dit_vsa"}
    assert cfg["engine"]["offload"]["text_encoder"] is True and cfg["engine"]["offload"]["vae"] is False
    assert cfg["pipeline"]["workload_type"] == "t2v"
    assert cfg["pipeline"]["experimental"]["attention_backend"] == "VIDEO_SPARSE_ATTN_H3"


# ---------------------------------------------------------------- generator with a mocked FastVideo


class FakeVideoGenerator:
    instances: ClassVar[list["FakeVideoGenerator"]] = []

    def __init__(self, config):
        self.config, self.requests = config, []
        FakeVideoGenerator.instances.append(self)

    @classmethod
    def from_config(cls, config):
        return cls(config)

    def generate(self, request):
        self.requests.append(request)
        out = Path(request["output"]["output_path"]) / "a fox.mp4"  # FastVideo names files itself
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + json.dumps(request["sampling"]).encode())
        return [types.SimpleNamespace(video_path=str(out))]


@pytest.fixture
def fake_fastvideo(monkeypatch):
    FakeVideoGenerator.instances.clear()
    monkeypatch.setitem(sys.modules, "fastvideo", types.SimpleNamespace(VideoGenerator=FakeVideoGenerator))
    return FakeVideoGenerator


def test_generator_loads_once_and_renders(tmp_path, fake_fastvideo, monkeypatch):
    monkeypatch.delenv("FASTVIDEO_H3_ADALN_TABLE", raising=False)
    repo = make_cache(tmp_path / "hub")
    table = repo / "snapshots" / REV / "transformer" / "adaln_tables.pt"
    table.write_bytes(b"t")
    gen = fvg.FastVideoGenerator(hf_cache=str(tmp_path / "hub"))
    assert gen.model_revision == REV and gen.load_s >= 0
    assert fake_fastvideo.instances[0].config["model_path"] == str(repo / "snapshots" / REV)
    assert os.environ["FASTVIDEO_H3_ADALN_TABLE"] == str(table)

    out_dir = tmp_path / "work"
    out_dir.mkdir()
    res = gen.generate("t2v", RenderParams(prompt="a fox", num_frames=8), [], out_dir)
    assert res.video_path == out_dir / "video.mp4" and res.video_path.is_file()
    assert not (out_dir / "a fox.mp4").exists()
    assert len(fake_fastvideo.instances) == 1


def test_generator_rejects_other_kind_and_omni(tmp_path, fake_fastvideo):
    make_cache(tmp_path)
    gen = fvg.FastVideoGenerator(hf_cache=str(tmp_path))
    with pytest.raises(InputError, match="serves t2v"):
        gen.generate("omni", RenderParams(prompt="x"), [], tmp_path)
    with pytest.raises(NotImplementedError, match="#488"):
        fvg.FastVideoGenerator(JobKind.OMNI, hf_cache=str(tmp_path))


def test_generator_without_video_file_fails(tmp_path):
    make_cache(tmp_path)
    gen = fvg.FastVideoGenerator(hf_cache=str(tmp_path), video_generator_factory=lambda cfg: types.SimpleNamespace(
        generate=lambda req: types.SimpleNamespace(video_path=None)))
    with pytest.raises(RuntimeError, match="no video file"):
        gen.generate("t2v", RenderParams(prompt="x"), [], tmp_path)


# ---------------------------------------------------------------- entrypoint wiring


@pytest.fixture
def fake_runpod(monkeypatch):
    calls = {"progress": [], "start": []}
    serverless = types.SimpleNamespace(progress_update=lambda job, msg: calls["progress"].append((job["id"], msg)),
                                       start=lambda cfg: calls["start"].append(cfg))
    monkeypatch.setitem(sys.modules, "runpod", types.SimpleNamespace(serverless=serverless))
    return calls


def load_entry(monkeypatch, tmp_path):
    make_cache(tmp_path / "hub")
    monkeypatch.setenv("HF_CACHE_ROOT", str(tmp_path / "hub"))
    monkeypatch.delenv("MODEL_DIR", raising=False)
    monkeypatch.delenv("MODEL_REVISION", raising=False)
    monkeypatch.delenv("RENDER_KIND", raising=False)
    sys.modules.pop("runpod_render.worker.runpod_entry", None)
    return importlib.import_module("runpod_render.worker.runpod_entry")


def test_entry_builds_runtime_at_import_and_starts(monkeypatch, tmp_path, fake_fastvideo, fake_runpod):
    entry = load_entry(monkeypatch, tmp_path)
    assert isinstance(entry.runtime, WorkerRuntime)
    assert isinstance(entry.runtime.generator, fvg.FastVideoGenerator)
    assert len(fake_fastvideo.instances) == 1  # model loaded at import
    monkeypatch.setenv("WARMUP", "off")
    entry.main()
    (cfg,) = fake_runpod["start"]
    assert cfg["handler"] is entry.handler and cfg["concurrency_modifier"](5) == 1
    assert len(fake_fastvideo.instances[0].requests) == 0


def test_entry_warmup_renders_once(monkeypatch, tmp_path, fake_fastvideo, fake_runpod):
    entry = load_entry(monkeypatch, tmp_path)
    monkeypatch.setenv("WARMUP", "small")
    entry.main()
    (req,) = fake_fastvideo.instances[0].requests
    assert (req["sampling"]["width"], req["sampling"]["height"]) == (512, 320)
    with pytest.raises(ValueError):
        entry.warmup(entry.runtime, "huge")


def test_entry_handler_runs_job_with_progress(monkeypatch, tmp_path, fake_fastvideo, fake_runpod):
    from test_worker_runtime import FakeWorld, make_input

    entry = load_entry(monkeypatch, tmp_path)
    world = FakeWorld()
    entry.runtime.http = httpx.Client(transport=httpx.MockTransport(world))
    out = entry.handler({"id": "rp1", "input": make_input(model_revision=REV)})
    assert out["ok"] is True
    assert [a for a, _, _ in world.calls][-1] == "complete"
    assert ("rp1", "rendering") in fake_runpod["progress"]
