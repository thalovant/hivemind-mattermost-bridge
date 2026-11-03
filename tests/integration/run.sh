#!/usr/bin/env bash
# Run the bridge image against a real Mattermost server (mattermost-preview,
# with its own database) and a real HiveMind hub. See README.md here.
#
#   tests/integration/run.sh [image]
#
# Without an image argument the image is built from this checkout first.
# CONTAINER_ENGINE   podman or docker (default: whichever is installed);
#                    may carry flags, e.g. "podman --cgroup-manager=cgroupfs"
# PYTHON             a Python with this package and its e2e extra installed
#                    (default: .venv/bin/python, else python3)
# MATTERMOST_IMAGE   default docker.io/mattermost/mattermost-preview:latest
# MATTERMOST_PORT    host port for the server (default 18065)
# KEEP_MATTERMOST=1  leave the server running afterwards
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
if [ -n "${CONTAINER_ENGINE:-}" ]; then
  read -r -a engine <<< "$CONTAINER_ENGINE"
elif command -v podman > /dev/null; then
  engine=(podman)
else
  engine=(docker)
fi
export CONTAINER_ENGINE="${engine[*]}"
python="${PYTHON:-}"
if [ -z "$python" ]; then
  if [ -x "$repo/.venv/bin/python" ]; then python="$repo/.venv/bin/python"; else python=python3; fi
fi
mattermost_image="${MATTERMOST_IMAGE:-docker.io/mattermost/mattermost-preview:latest}"
port="${MATTERMOST_PORT:-18065}"
image="${1:-}"
if [ -z "$image" ]; then
  image="localhost/hivemind-mattermost-bridge:integration"
  echo "== building $image"
  "${engine[@]}" build -q -t "$image" "$repo" > /dev/null
fi

name="mm-integration-$$"
cleanup() {
  if [ "${KEEP_MATTERMOST:-0}" = 1 ]; then
    echo "== Mattermost left running: $name on http://127.0.0.1:$port"
  else
    "${engine[@]}" rm -f "$name" > /dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "== starting $mattermost_image on 127.0.0.1:$port"
"${engine[@]}" run -d --name "$name" -p "127.0.0.1:$port:8065" \
  -e MM_SERVICESETTINGS_SITEURL="http://127.0.0.1:$port" \
  -e MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION=true \
  -e MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS=true \
  -e MM_RATELIMITSETTINGS_ENABLE=false \
  "$mattermost_image" > /dev/null
version="$("${engine[@]}" image inspect "$mattermost_image" --format '{{.Id}}' 2> /dev/null | cut -c1-19 || true)"
echo "== server image ${version:-unknown}; bridge image $image"

"$python" "$here/real_mattermost.py" --image "$image" --mattermost "http://127.0.0.1:$port"
