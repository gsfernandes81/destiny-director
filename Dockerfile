# syntax=docker/dockerfile:1
#
# One image definition for BOTH deployment targets — Railway (prod/dev, amd64) and the
# Raspberry Pi B+ test box (linux/arm/v6). There used to be a second, drifting copy at
# deploy/pi-bplus/Containerfile; it is gone. Alpine is now the base everywhere: every
# compiled prod dependency (cryptography, greenlet, orjson, aiohttp & family, ciso8601)
# publishes musllinux x86_64 wheels, so the amd64 build compiles nothing despite musl,
# and using one distro for both targets is what makes one file possible at all.
#
# The three remaining differences between the targets are architectural, and are the
# three build ARGs below. Their defaults are the prod-correct ones, so Railway needs no
# build configuration at all; the Pi passes all three:
#
#   docker build \
#     --platform linux/arm/v6 \
#     --build-arg BASE_IMAGE=docker.io/arm32v6/python:3.13-alpine3.23 \
#     --build-arg UV_SYNC_GROUPS=--no-default-groups \
#     --build-arg PURE_PYTHON=1 \
#     -t dd-beacon:latest .
#
# Everything else — the builder/final split, deps-below-code layer ordering, libpq for
# pure-Python psycopg, jemalloc, the non-root `dd` user — is identical on both.

# ARMv6 (the original B+'s instruction set; armv7/v8 binaries SIGILL on it) needs the
# arm32v6 base. Declared before the first FROM so both stages can use it.
ARG BASE_IMAGE=python:3.13-alpine3.23

# --- Builder: install deps + the project into a venv -----------------------
FROM ${BASE_IMAGE} AS builder

# uv's official distroless image (ghcr.io/astral-sh/uv) publishes no arm/v6 variant, so
# both targets install via the official shell installer instead — it autodetects the
# right musl build and, like pip's wheel hashes, verifies the download against a
# checksum embedded in the script. Pinned (not `latest`) so builds are reproducible.
ARG UV_VERSION=0.8.17
# Which dependency groups reach the image. Prod keeps `speedups` (hikari's C extras +
# cryptography, all musllinux-wheeled on amd64) and drops only `dev`. The Pi passes
# --no-default-groups to drop `speedups` too: cryptography has no ARMv6/musl wheel and
# would need a Rust toolchain to build, and hikari's speedups are a nicety, not a
# requirement, on a single-guild test bot.
ARG UV_SYNC_GROUPS=--no-dev
# Non-empty forces aiohttp & family to their pure-Python builds instead of compiling C
# extensions. Only bites when a package installs from sdist, i.e. on ARMv6/musl, where
# no wheels exist for them; on amd64 wheels are used and the flag never applies.
ARG PURE_PYTHON=

# gcc & co. are unconditional even though the amd64 build needs none of them: they are
# discarded with this stage, never reach the final image, and keeping them unconditional
# means a dependency that loses its musllinux wheel degrades to a slower build instead
# of a broken one. On ARMv6 they are load-bearing — with the aiohttp family forced pure
# below, greenlet (SQLAlchemy's asyncio bridge) is the one package still compiling from
# sdist there. psycopg needs no compiler either way: per pyproject.toml it is the
# pure-Python distribution, which dlopens libpq at runtime (installed in the final
# stage).
RUN apk add --no-cache curl gcc musl-dev python3-dev linux-headers \
    && curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/ \
    && apk del curl

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    AIOHTTP_NO_EXTENSIONS=${PURE_PYTHON} \
    MULTIDICT_NO_EXTENSIONS=${PURE_PYTHON} \
    YARL_NO_EXTENSIONS=${PURE_PYTHON} \
    FROZENLIST_NO_EXTENSIONS=${PURE_PYTHON} \
    PROPCACHE_NO_EXTENSIONS=${PURE_PYTHON}
WORKDIR /app
# Dependencies layer — cached on the pyproject/uv.lock hash. Only the manifests are
# copied in at this point, so editing anything under dd/ does NOT invalidate this layer;
# rebuilds reuse the cached (on ARMv6, slow and C-compiling) result.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen ${UV_SYNC_GROUPS} --no-install-project
# The project itself, installed non-editable into the venv. Copied in only now, after
# the expensive dependency sync above, so source edits invalidate only this cheap layer.
COPY dd ./dd
COPY README.md ./
RUN uv sync --frozen ${UV_SYNC_GROUPS} --no-editable

# --- Final: runtime image, no compilers ------------------------------------
FROM ${BASE_IMAGE} AS final

# jemalloc: LD_PRELOADed for every process below. glibc/musl malloc retain freed arenas
# for this long-lived, bursty-allocation workload (the manifest parse; the gateway
# cache), inflating resident RAM (Railway bills memory-over-time). jemalloc returns freed
# pages to the OS via a background decay thread — measured in prod, plain system malloc
# left anchor's freed startup/post heap resident (~430MB) vs jemalloc's ~360MB.
#   NOTE: Alpine ships jemalloc for x86_64 AND armhf (package `jemalloc`, in main). The
#   old deploy/pi-bplus/Containerfile claimed Alpine has no jemalloc — that was wrong,
#   a confusion with Debian's package name (libjemalloc2).
# libpq: psycopg is the pure-Python distribution (see the builder stage), so it dlopens
# the system libpq at import time — without this the bot cannot start.
# binutils: the musl tax, and NOT optional. psycopg resolves libpq through
# ctypes.util.find_library("pq"), which on Linux tries `ldconfig -p` first — glibc has
# that, musl's ldconfig rejects `-p` outright — and then falls back to asking `gcc` or
# `ld` where the library lives. With neither present find_library returns None and
# psycopg dies at import with "libpq library not found", however installed libpq is.
# binutils (~10MB) supplies `ld`, which is the cheapest of the two fallbacks; the
# unversioned libpq.so symlink below is what `ld -lpq` resolves (Alpine's libpq package
# ships only libpq.so.5). This was verified by running the image: without it, beacon
# crash-loops before it ever reaches the database.
# openssh-server/-keygen: the break-glass sshd, see supervisord.conf. ~2MB.
# The jemalloc symlink gives the preload a stable name, so LD_PRELOAD below never has to
# encode a soname or an architecture. (Alpine's own path happens to be arch-stable,
# unlike Debian's multiarch /usr/lib/<triplet>/, but this keeps the soname in one place.)
RUN apk add --no-cache \
        binutils ca-certificates jemalloc libpq openssh-keygen openssh-server tzdata \
    && ln -s libpq.so.5 /usr/lib/libpq.so \
    && ln -s /usr/lib/libjemalloc.so.2 /usr/local/lib/libjemalloc.so

# LD_PRELOAD must come after the symlink exists: musl's loader *aborts* a process whose
# LD_PRELOAD cannot be resolved (glibc only warns), so an earlier ENV would break every
# subsequent RUN. MALLOC_CONF holds the settings shared by both bots; anchor additionally
# wants narenas:2 — see the note in supervisord.conf on where that now comes from.
ENV TZ=Etc/UTC \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    LD_PRELOAD=/usr/local/lib/libjemalloc.so \
    MALLOC_CONF=background_thread:true,dirty_decay_ms:10000,muzzy_decay_ms:10000

# Non-root, matching the `dd` convention used for the host user in
# deploy/pi-bplus/root-setup.sh (and defense in depth under rootless podman). The
# account keeps busybox adduser's `!` (no password) shadow entry: that is OpenSSH's
# locked-account marker on some platforms, but Alpine's build authenticates a public key
# against it happily — verified by flipping the field and logging in, rather than
# pre-emptively "fixing" it.
#   /app is owned by dd because anchor writes the downloaded Bungie manifest into
#   ./manifest at runtime; its *contents* stay root-owned and read-only.
#   /home/dd/.ssh-host holds the runtime-generated sshd host keys — never baked, see
#   supervisord.conf. `ssh-keygen -A -f <prefix>` appends etc/ssh/ to the prefix and does
#   NOT create it (it fails, and still exits 0), so the directory has to exist here.
#   Mount a NAMED volume at /home/dd/.ssh-host to persist the keys: a named volume is
#   seeded from the image, so etc/ssh comes with it; a bind mount would hide it.
#   /home/dd/.ssh receives the authorized_keys file written from SSH_AUTHORIZED_KEYS at
#   sshd start; 0700 because sshd's StrictModes insists on it.
#   /home/dd/run holds supervisord's pidfile and control socket.
RUN adduser -D -u 1000 dd \
    && mkdir -p /app /home/dd/.ssh-host/etc/ssh /home/dd/.ssh /home/dd/run \
    && chmod 700 /home/dd/.ssh \
    && chown -R dd:dd /app /home/dd/.ssh-host /home/dd/.ssh /home/dd/run

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# The migration environment: each bot runs `alembic upgrade head` from its own startup
# hook (dd/common/db_migrations.py), and alembic resolves `script_location` relative to
# alembic.ini, which is found relative to this WORKDIR.
COPY alembic.ini ./
COPY migrations ./migrations
# PID 1's config, and the break-glass sshd's. Both are read-only at runtime.
COPY supervisord.conf sshd_config /etc/dd/
# Reachable from an ssh login shell, whose PATH /etc/profile resets to one that excludes
# the venv (see sshd_config's SetEnv note). These two are what a break-glass session
# actually wants: `supervisorctl -c /etc/dd/supervisord.conf restart bot`, and alembic.
RUN ln -s /app/.venv/bin/supervisorctl /app/.venv/bin/alembic /usr/local/bin/
USER dd
# ARG only — do NOT promote to ENV. Baking an empty ENV would shadow Railway's
# runtime-injected RAILWAY_SERVICE_NAME and break beacon/anchor selection.
ARG RAILWAY_SERVICE_NAME
CMD ["supervisord", "-c", "/etc/dd/supervisord.conf"]
