"""Handler used by the fake RunPod in the dev stack: a WorkerRuntime around MockGenerator."""

import os

from .runtime import MockGenerator, WorkerRuntime

runtime = WorkerRuntime(MockGenerator(model_revision=os.environ.get("MODEL_REVISION", "mock-rev"),
                                      render_seconds=float(os.environ.get("MOCK_RENDER_SECONDS", "0.5"))))


def handler(job: dict) -> dict:
    return runtime.handle(job)
