import pytest
from pydantic import ValidationError

from runpod_render.contracts import (
    InvalidToken,
    JobKind,
    RenderParams,
    UploadTargets,
    WorkerJobInput,
    sign_attempt,
    verify_attempt,
)


def test_token_roundtrip_and_tamper():
    tok = sign_attempt(b"s3cret", "att_123", 4)
    claims = verify_attempt(b"s3cret", tok)
    assert (claims.attempt_id, claims.fence) == ("att_123", 4)
    with pytest.raises(InvalidToken):
        verify_attempt(b"other", tok)
    aid, _, mac = tok.split(".")
    with pytest.raises(InvalidToken):
        verify_attempt(b"s3cret", f"{aid}.5.{mac}")  # bumped fence without re-signing
    with pytest.raises(InvalidToken):
        verify_attempt(b"s3cret", "garbage")


def test_render_limits():
    RenderParams(prompt="ok")
    for bad in ({"width": 1360}, {"height": 770}, {"num_frames": 125}, {"width": 100}, {"prompt": ""}):
        with pytest.raises(ValidationError):
            RenderParams(**{"prompt": "x", **bad})


def test_worker_input_roundtrip_and_strict():
    inp = WorkerJobInput(
        job_id="j", attempt_id="a", attempt_token="t", kind=JobKind.T2V, model_revision="r",
        params=RenderParams(prompt="p"),
        upload=UploadTargets(video_url="http://s3/v", manifest_url="http://s3/m"),
        callback_base_url="http://cp/worker/v1")
    again = WorkerJobInput.model_validate(inp.model_dump(mode="json"))
    assert again == inp
    with pytest.raises(ValidationError):
        WorkerJobInput.model_validate({**inp.model_dump(mode="json"), "unexpected": 1})
