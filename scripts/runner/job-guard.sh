#!/bin/bash -p
# Installed on the self-hosted runner as ACTIONS_RUNNER_HOOK_JOB_STARTED. A non-zero exit fails
# the job before any of its steps run. Workflow files in a fork pull request can name any
# runner label, so the restriction has to live on the runner, not only in the workflows.
set -euo pipefail

REPO="aaronx3011/runpod-render"

deny() {
  echo "job refused by runner policy: $1" >&2
  exit 1
}

[ "${GITHUB_REPOSITORY:-}" = "$REPO" ] || deny "repository is not $REPO"
case "${GITHUB_EVENT_NAME:-}" in
  push | workflow_dispatch | schedule) ;;
  *) deny "event '${GITHUB_EVENT_NAME:-}' is not allowed" ;;
esac
[ "${GITHUB_REF:-}" = "refs/heads/main" ] || deny "ref is not main"
case "${GITHUB_WORKFLOW_REF:-}" in
  "$REPO"/.github/workflows/*@refs/heads/main) ;;
  *) deny "workflow file is not from main" ;;
esac
