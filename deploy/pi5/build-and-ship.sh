#!/bin/bash
# Unprivileged build+ship for the Pi B+ test bot. Run on the Pi 5, from the repo root,
# after deploy/pi5/root-setup.sh has run once (podman + subuid/subgid + linger):
#
#   deploy/pi5/build-and-ship.sh dd@pi-bplus.local
#
# or export PI_BPLUS_TARGET=dd@pi-bplus.local and omit the argument. Builds the
# repo-root Dockerfile for linux/arm/v6 -- native on this Pi 5's Cortex-A76, which runs
# 32-bit ARM userland directly, so no qemu/binfmt setup is needed -- and ships the
# resulting image to the B+ over SSH via `podman save | podman load` (never pushed to or
# pulled from a registry).
#
# There is one Dockerfile for both deployment targets now (the separate
# deploy/pi-bplus/Containerfile is gone). Its defaults are the Railway/amd64 ones, so
# the three --build-arg overrides below are what make it an ARMv6 image; see the
# Dockerfile's header for what each of them does.
set -euo pipefail

IMAGE="dd-beacon:latest"
CONTAINERFILE="Dockerfile"
TARGET="${1:-${PI_BPLUS_TARGET:-}}"

if [ -z "$TARGET" ]; then
    echo "Usage: $0 <user@pi-bplus-host>   (or set PI_BPLUS_TARGET)" >&2
    exit 1
fi

if [ ! -f "$CONTAINERFILE" ]; then
    echo "Run this from the repo root (expected to find $CONTAINERFILE here)." >&2
    exit 1
fi

echo "== building $IMAGE for linux/arm/v6"
podman build --platform linux/arm/v6 -f "$CONTAINERFILE" \
    --build-arg BASE_IMAGE=docker.io/arm32v6/python:3.13-alpine3.23 \
    --build-arg UV_SYNC_GROUPS=--no-default-groups \
    --build-arg PURE_PYTHON=1 \
    -t "$IMAGE" .

echo "== shipping $IMAGE to $TARGET"
podman save "$IMAGE" | ssh "$TARGET" podman load

echo "== done."
echo "   On $TARGET, start/refresh the stack with:"
echo "     cd /srv/dd && XDG_RUNTIME_DIR=/run/user/\$(id -u) podman-compose up -d"
