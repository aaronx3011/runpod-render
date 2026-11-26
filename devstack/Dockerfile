# CPU-only image for the dev stack (fake RunPod, mock worker, smoke test). Not the GPU worker.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[dev]"
