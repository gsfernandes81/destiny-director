#!/bin/sh
# Unprivileged on-device build for the Pi B+ test bot. Run on the B+ itself, as the
# unprivileged $PI_USER (default `dd`), from a clone of this repo:
#
#   deploy/pi-bplus/build-local.sh
#
# This is the no-Pi-5 path: there is no cross-build host in this setup, so the image is
# built natively on the B+'s single 700MHz ARM1176 core. That's only feasible at all
# because the dependency-install layer now compiles just one package from sdist
# (`greenlet`, SQLAlchemy's asyncio C bridge -- `regex` and `dateparser` were dropped
# for pure-Python `re`/`python-dateutil`, and aiohttp/multidict/yarl/frozenlist/
# propcache are forced to their pure-Python builds via `*_NO_EXTENSIONS=1`). Even so,
# expect roughly 30+ minutes end to end -- this is not a fast box.
#
# #!/bin/sh + `set -eu`, not bash: Alpine's `sys` install only ships busybox ash, so
# this script is POSIX-only throughout (no arrays, no `[[`, no `local`).
set -eu

CONTAINERFILE="deploy/pi-bplus/Containerfile"
IMAGE="dd-beacon:latest"

if [ ! -f "$CONTAINERFILE" ]; then
    echo "Run this from the repo root (expected to find $CONTAINERFILE here)." >&2
    exit 1
fi

# root-setup.sh only exports XDG_RUNTIME_DIR from $PI_USER's .profile, which a
# non-interactive `ssh host 'cmd'` invocation never sources (no login shell, no tty) --
# so a script launched that way would otherwise fail with podman's usual rootless
# "could not get runtime directory" error. Set it ourselves if it's missing so this
# script also works run non-interactively, not just from an interactive SSH session.
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

# --platform linux/arm/v6 is redundant on this host (the build is already native
# ARMv6) but passed anyway for two reasons: it makes the intent unmissable in the build
# log, and it guards against someone copy-pasting this script onto a v7/v8 board, where
# it would otherwise silently produce a binary that SIGILLs on the real B+.
#
# No build-parallelism cap is set here, deliberately. The obvious one (MAKEFLAGS=-j1)
# would be theatre twice over: env set out here does not cross into the build
# container, and greenlet -- the one package still compiling from sdist -- builds
# through setuptools as a single translation unit, so there is no parallel `cc` fan-out
# on this board to cap in the first place.
echo "== building $IMAGE for linux/arm/v6 (native, ~30+ min on this CPU)"
podman build --platform linux/arm/v6 -f "$CONTAINERFILE" -t "$IMAGE" .

echo "== done."
echo "   Start/refresh the stack with:"
echo "     cd /srv/dd && podman-compose up -d --force-recreate beacon"
echo "   (--force-recreate is required: podman-compose up -d alone will not recreate a"
echo "   running container against a same-tagged image it already has, so a rebuild"
echo "   here would otherwise silently leave the old container running.)"
