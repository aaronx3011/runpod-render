#!/usr/bin/env bash
# Install the GitHub Actions runner for this repository on the build machine, as a systemd
# *user* service of the current login user (no sudo needed). Run it on that machine:
#
#   RUNNER_TOKEN=... scripts/runner/install-runner.sh
#
# The registration token is short-lived; generate it when you need it, from a machine where
# `gh` is logged in as a repository admin, and never store it:
#
#   gh api -X POST repos/aaronx3011/runpod-render/actions/runners/registration-token --jq .token
#
# Needs job-guard.sh and prune.sh next to this script.
set -euo pipefail

REPO="${REPO:-aaronx3011/runpod-render}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-runpod-render}"
RUNNER_NAME="${RUNNER_NAME:-runpod-render-builder}" # public in job logs: no hostnames
RUNNER_LABELS="self-hosted,linux,x64,lesbian"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
TOKEN="${RUNNER_TOKEN:-${1:-}}"
SERVICE="actions-runner-runpod-render"
UNIT_DIR="$HOME/.config/systemd/user"
HERE="$(cd "$(dirname "$0")" && pwd)"

die() {
  echo "error: $*" >&2
  exit 1
}

[ "$(id -u)" -ne 0 ] || die "run as the runner's login user, not root"
[ "$(uname -sm)" = "Linux x86_64" ] || die "expects Linux x86_64"
for cmd in curl tar sha256sum python3 flock docker systemctl loginctl; do
  command -v "$cmd" >/dev/null || die "missing command: $cmd"
done
for file in job-guard.sh prune.sh; do
  [ -f "$HERE/$file" ] || die "missing $HERE/$file"
done
docker info >/dev/null 2>&1 || die "this user cannot use docker"
docker buildx version >/dev/null || die "docker buildx is not installed"

for dir in "$(docker info --format '{{.DockerRootDir}}')" "$HOME"; do
  free=$(df -P -B1 "$dir" | awk 'NR==2 {print $4}')
  echo "free for $dir: $((free / 1000000000)) GB"
  [ "$free" -ge $((MIN_FREE_GB * 1000000000)) ] || die "less than ${MIN_FREE_GB} GB free for $dir"
done

[ "$(loginctl show-user "$USER" --property=Linger --value)" = "yes" ] ||
  die "lingering is off; run: sudo loginctl enable-linger $USER"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

if [ ! -f .runner ]; then
  [ -n "$TOKEN" ] || die "set RUNNER_TOKEN (see the header of this script)"

  # Latest release, verified against the digest GitHub publishes for the asset.
  read -r url digest < <(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | python3 -c '
import json, re, sys
for a in json.load(sys.stdin)["assets"]:
    if re.fullmatch(r"actions-runner-linux-x64-[0-9.]+\.tar\.gz", a["name"]):
        print(a["browser_download_url"], (a.get("digest") or "").removeprefix("sha256:"))
')
  [ -n "${url:-}" ] && [ -n "${digest:-}" ] || die "could not find the runner download and its digest"
  curl -fsSL -o runner.tar.gz "$url"
  echo "$digest  runner.tar.gz" | sha256sum -c -
  tar -xzf runner.tar.gz
  rm -f ./runner.tar.gz

  ./config.sh --unattended --replace \
    --url "https://github.com/$REPO" \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work _work
fi

install -m 0755 "$HERE/job-guard.sh" "$HERE/prune.sh" "$RUNNER_DIR/"
touch .env
sed -i '/^ACTIONS_RUNNER_HOOK_JOB_STARTED=/d' .env
echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=$RUNNER_DIR/job-guard.sh" >>.env
install -m 0755 bin/runsvc.sh runsvc.sh

mkdir -p "$UNIT_DIR"
cat >"$UNIT_DIR/$SERVICE.service" <<UNIT
[Unit]
Description=GitHub Actions runner for $REPO
After=network-online.target

[Service]
WorkingDirectory=$RUNNER_DIR
ExecStart=$RUNNER_DIR/runsvc.sh
Restart=always
RestartSec=10
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min

[Install]
WantedBy=default.target
UNIT

cat >"$UNIT_DIR/$SERVICE-prune.service" <<UNIT
[Unit]
Description=Prune the runpod-render build cache

[Service]
Type=oneshot
ExecStart=$RUNNER_DIR/prune.sh
UNIT

cat >"$UNIT_DIR/$SERVICE-prune.timer" <<UNIT
[Unit]
Description=Weekly prune of the runpod-render build cache

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable "$SERVICE.service"
systemctl --user restart "$SERVICE.service"
systemctl --user enable --now "$SERVICE-prune.timer"
systemctl --user --no-pager status "$SERVICE.service" || true

units="${UNIT_DIR:?}/${SERVICE:?}"
cat <<DONE

Installed in $RUNNER_DIR (labels: $RUNNER_LABELS).
Logs:  journalctl --user -u $SERVICE -f

To uninstall:
  systemctl --user disable --now $SERVICE.service $SERVICE-prune.timer
  rm "${units:?}.service" "${units:?}-prune.service" "${units:?}-prune.timer"
  systemctl --user daemon-reload
  cd "$RUNNER_DIR" && ./config.sh remove --token <removal token>
  rm -rf "${RUNNER_DIR:?}" "${HOME:?}/.cache/runpod-render"
Removal token (from the admin machine):
  gh api -X POST repos/$REPO/actions/runners/remove-token --jq .token
DONE
