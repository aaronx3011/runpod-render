"""Client for the RunPod Serverless queue API (also served by devstack.fake_runpod).

Base URL is configurable: `https://api.runpod.ai` (the `/v2` prefix may be included or not)
or the fake at `http://localhost:58000`.

`run()` distinguishes three outcomes, because they need different handling:
  RunAccepted      -> RunPod returned a job id
  RunNotDelivered  -> the request certainly never reached RunPod (connect error) or was
                      refused (4xx); safe to retry later with a new attempt
  RunUncertain     -> timeout / 5xx / broken response: the job may or may not be queued.
                      The caller must mark the attempt UNCERTAIN and never resubmit blindly.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

TERMINAL_RUNPOD_STATUSES = ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT")


@dataclass(frozen=True)
class RunAccepted:
    runpod_job_id: str


@dataclass(frozen=True)
class RunNotDelivered:
    reason: str
    permanent: bool  # 4xx: retrying the same request will not help (config/auth/bad input)


@dataclass(frozen=True)
class RunUncertain:
    reason: str


RunOutcome = RunAccepted | RunNotDelivered | RunUncertain


class RunPodError(RuntimeError):
    pass


class RunPodClient:
    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        base = base_url.rstrip("/")
        self.base = base if base.endswith("/v2") else f"{base}/v2"
        self.http = httpx.Client(timeout=httpx.Timeout(timeout_s), transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}"})

    def close(self) -> None:
        self.http.close()

    def run(self, endpoint_id: str, job_input: dict, *, webhook: str | None = None,
            execution_timeout_s: int | None = None) -> RunOutcome:
        body: dict = {"input": job_input}
        if webhook:
            body["webhook"] = webhook
        if execution_timeout_s:
            body["policy"] = {"executionTimeout": execution_timeout_s * 1000}
        try:
            r = self.http.post(f"{self.base}/{endpoint_id}/run", json=body)
        except httpx.ConnectError as exc:  # TCP connect failed: nothing was sent
            return RunNotDelivered(f"connect error: {exc}", permanent=False)
        except httpx.HTTPError as exc:  # timeouts, resets after sending, protocol errors
            return RunUncertain(f"{type(exc).__name__}: {exc}")
        if r.status_code == 429:
            return RunNotDelivered("rate limited (429)", permanent=False)
        if 400 <= r.status_code < 500:
            return RunNotDelivered(f"/run rejected: {r.status_code} {r.text[:300]}", permanent=True)
        if r.status_code >= 500:
            return RunUncertain(f"/run returned {r.status_code}")
        try:
            job_id = r.json()["id"]
        except Exception:  # noqa: BLE001 - 2xx without a readable id: we cannot know what happened
            return RunUncertain(f"/run returned {r.status_code} without a job id")
        return RunAccepted(job_id)

    def status(self, endpoint_id: str, runpod_job_id: str) -> dict | None:
        """RunPod job view, or None if RunPod does not know the id (expired or never existed)."""
        r = self.http.get(f"{self.base}/{endpoint_id}/status/{runpod_job_id}")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise RunPodError(f"/status returned {r.status_code}")
        return r.json()

    def cancel(self, endpoint_id: str, runpod_job_id: str) -> None:
        r = self.http.post(f"{self.base}/{endpoint_id}/cancel/{runpod_job_id}")
        if r.status_code >= 400 and r.status_code != 404:
            raise RunPodError(f"/cancel returned {r.status_code}")
