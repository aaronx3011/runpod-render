#!/usr/bin/env bash
# Weekly cleanup of this project's build cache only. The machine is shared, so this never prunes
# the default builder, other builders, containers, images or volumes.
set -euo pipefail

BUILDER="runpod-render" # must match BUILDER in .github/workflows/worker-image.yml
ROOT="${HOME:?}/.cache/runpod-render"
CACHE="$ROOT/buildx"
MAX_CACHE_GB="${MAX_CACHE_GB:-40}"
MAX_IDLE_DAYS="${MAX_IDLE_DAYS:-14}"

mkdir -p "$ROOT"
exec 9>"$ROOT/lock"
flock -n 9 || {
  echo "a build is running; skipping"
  exit 0
}

# The workflow removes its builder after each build; one is left only if a job was killed.
if docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  docker buildx prune --builder "$BUILDER" --all --force || true
  docker buildx rm "$BUILDER"
fi

rm -rf "${CACHE:?}.new"
if [ -d "$CACHE" ]; then
  size_kb=$(du -sk "$CACHE" | cut -f1)
  if [ "$size_kb" -gt $((MAX_CACHE_GB * 1000000)) ] ||
    [ -n "$(find "$CACHE" -maxdepth 0 -mtime +"$MAX_IDLE_DAYS")" ]; then
    echo "removing local build cache ($((size_kb / 1000000)) GB)"
    rm -rf "${CACHE:?}"
  fi
fi
