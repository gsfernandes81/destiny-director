#!/usr/bin/env bash
# dd-dev's own start-up, baked into the image at /home/dev/child-init.sh and RUN by the
# base image's entrypoint (gsrpi-dev-base, infra's dev/Dockerfile.base) — its child-init
# seam. The base runs it after it has cloned or pulled /workspace and linked .env in, and
# before it starts sshd, so a lockfile that pull moved is the one installed from, and no
# session has arrived yet to meet a half-installed venv.
#
# WHAT THE BASE DOES, SO THIS DOES NOT. Since 2026-10-27 dd-dev is a service in infra's
# dev/compose.yaml, and everything about getting in and reaching GitHub is the base's:
#   /workspace     a volume of its own, cloned over https with the hub's GitHub token —
#                  not a bind mount of a host clone
#   .env           kept in the env volume (~/.env.d) and linked to /workspace/.env
#   ssh identities copied out of DEV_SECRETS_DIR (/run/dd-secrets, read-only — the host's
#                  ~/.dd-dev-secrets), with its ssh_config.fleet prepended to ~/.ssh/config
# What is left is this repo's own: the venv, and a line about the identities.
#
# NON-FATAL AT THE OTHER END. A non-zero exit here is printed by the entrypoint as a
# warning and the container still comes up — deliberately, because a container you can
# get into and fix beats one that refused to start over its dependencies. So this script
# reports failure honestly rather than swallowing it: the old `uv sync … || true` hid a
# broken sync behind a container that looked fine until the first import failed.
set -u

rc=0
secrets="${DEV_SECRETS_DIR:-/run/dd-secrets}"

# ── ssh identities: reported, never written ─────────────────────────────────
# The mount is read-only and this container cannot change it; identities are added on the
# HOST, in ~/.dd-dev-secrets. origin needs none of them — git and gh reach this repo with
# the hub's token — so absence is not a fault, only a remote the token cannot reach is.
if [ -f "$secrets/ssh_config.fleet" ]; then
    echo "ssh identities: $secrets/ssh_config.fleet, prepended by the base at start"
elif [ -f "$secrets/config" ]; then
    # The pre-base name, carried over if a clone's .dev-ssh/ still had it. The base reads
    # only ssh_config.fleet, so these identities are not in force.
    echo "LEGACY NAME: $secrets/config is not read — the base reads ssh_config.fleet."
    echo "    On zero: mv ~/.dd-dev-secrets/config ~/.dd-dev-secrets/ssh_config.fleet"
else
    echo "no ssh identities in $secrets — origin needs none (the hub's token); other"
    echo "    remotes over ssh need a key and ssh_config.fleet in ~/.dd-dev-secrets on zero."
fi

# ── the venv ────────────────────────────────────────────────────────────────
# The dependency set is baked into /home/dev/venv at build time with
# --no-install-project; this adds the editable project itself, which cannot happen at
# build time because the source is not in the image — /workspace is a volume, cloned at
# first start. It runs on every start because the venv is in the image and not on a
# volume, so a rebuild is a fresh one.
#
# Gated on .git, not pyproject.toml: the image's own copies of pyproject.toml and uv.lock
# seed a new workspace volume, so they are there even when the clone has not happened yet.
#
# --frozen: install exactly what uv.lock says and never resolve. A start-up that quietly
# re-resolved dependencies would make the container's environment a function of when it
# was last restarted.
if [ -d /workspace/.git ] && [ -f /workspace/pyproject.toml ]; then
    if uv sync --frozen; then
        echo "venv synced from uv.lock"
    else
        rc=1
        echo "uv sync FAILED — /home/dev/venv is stale or incomplete."
        echo "    The container is still up: open a shell and run 'uv sync --frozen' to see why."
    fi
else
    echo "no checkout in /workspace yet — venv left as built. The clone waits for the hub's"
    echo "    token; 'make login C=dd-dev' in ~/infra/dev on zero clones it."
fi

exit "$rc"
