#!/bin/sh
# Build (or rebuild) the bot-machine image from a release tree.
#
#   deploy/build_machine_image.sh [RELEASE_ROOT]   (default: the tree this
#                                                   script is in)
#
# Idempotent and safe to run on every release: docker's layer cache keeps the
# apt/Chrome layers, so a rebuild after a harness update only re-copies /app.
# The machines backend (isolation/machines.py) notices the new image id and
# recreates each machine on its next spawn, home volume kept — that is how a
# tenant's machines pick up new in-machine code.
#
# Exit 0 on success. Exit 3 when no container engine is available — callers
# on the process backend, or a laptop without docker, treat that as "nothing
# to do", not as a failure. Any other non-zero exit is a real build failure.
set -eu

case "${1:-}" in
  -h|--help)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
image="${HARNESS_MACHINE_IMAGE:-agent-harness-machine}"
engine="${HARNESS_CONTAINER_ENGINE:-}"
if [ -z "$engine" ]; then
  if command -v docker >/dev/null 2>&1; then engine=docker
  elif command -v podman >/dev/null 2>&1; then engine=podman
  else
    echo "build_machine_image: no docker/podman on PATH; skipping" >&2
    exit 3
  fi
fi
if [ ! -f "$root/deploy/Dockerfile.machine" ]; then
  echo "build_machine_image: $root/deploy/Dockerfile.machine not found" >&2
  exit 2
fi
if ! "$engine" info >/dev/null 2>&1; then
  echo "build_machine_image: $engine daemon not reachable; skipping" >&2
  exit 3
fi
echo "build_machine_image: building $image from $root" >&2
exec "$engine" build -t "$image" -f "$root/deploy/Dockerfile.machine" "$root"
