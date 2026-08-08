# Raspberry Pi B+ image

This repo owns **the image**, not the deployment. Host provisioning, the compose
stack, and the trigger that actually deploys to the Raspberry Pi B+ ("two") all live in
the owner's separate **`infra`** repo now — this document only covers what this repo is
still responsible for: the one converged `Dockerfile`, its ARM build args, the
supervisord-as-PID-1 container, and the CI workflow that publishes the image to GHCR.
For host setup, the compose file, or how a deploy is triggered, see the `infra` repo —
deliberately not copied here, since a copy would drift the moment either repo changes.

## What this repo provides

- **One image definition for both deployment targets** — Railway (prod/dev, amd64) and
  the Pi B+ (`linux/arm/v6`). The repo-root `Dockerfile` builds both; there is no
  separate Pi `Containerfile` and no shell entrypoint — the container's PID 1 is
  **supervisord** (`supervisord.conf`), which picks the bot via `RAILWAY_SERVICE_NAME`
  and also runs a disarmed-by-default sshd (`sshd_config`) for getting a shell into a
  container whose bot has died. See `supervisord.conf`'s header for why a supervisor
  beats a shell `exec` here (bounded restarts; a container that outlives its bot).
- **Three build args are the entire difference** between the Railway image and the Pi
  one; their defaults are the Railway/amd64-correct values, so Railway needs no build
  configuration at all:

  ```sh
  docker build \
      --platform linux/arm/v6 \
      --build-arg BASE_IMAGE=docker.io/arm32v6/python:3.13-alpine3.23 \
      --build-arg UV_SYNC_GROUPS=--no-default-groups \
      --build-arg PURE_PYTHON=1 \
      -t dd-beacon:latest .
  ```

  `BASE_IMAGE` switches to the ARMv6 Alpine base; `UV_SYNC_GROUPS` drops the `speedups`
  group (`cryptography` has no ARMv6/musl wheel and would need a Rust toolchain);
  `PURE_PYTHON` forces the aiohttp family (aiohttp/multidict/yarl/frozenlist/propcache)
  to their pure-Python builds instead of compiling C extensions under emulation or on a
  700MHz core. See the Dockerfile's own header comment for the full rationale — it is
  the source of truth if this ever drifts from the summary above.
- **`.github/workflows/pi-image.yml`** builds the same `Dockerfile` for `linux/arm/v6`
  under QEMU emulation, with the same three build args, and pushes the result to GHCR
  (`ghcr.io/<owner>/<repo>`) — on push to the branches it watches and on manual
  dispatch. The job summary prints the exact tag and digest pushed; pin to the digest
  for anything meant to be reproducible, since branch-name tags move. GHCR packages
  default to *private* — an owner has to flip the package to public by hand once
  (Package settings → Danger Zone → Change visibility) before an unauthenticated
  `podman pull` from the Pi succeeds; no workflow permission can do this.

## Hardware / OS assumptions (why the image looks like this)

These constraints are hard-won and belong to the image, not to host provisioning, so
they stay here even though the host itself is documented in `infra`:

- **Raspberry Pi B+** (the original 512MB board) — ARMv6, no NEON, no hardware float in
  the same class as v7+. This is why every image built for this board targets
  `linux/arm/v6` specifically: `v7` or `v8` binaries raise **`SIGILL`** on this CPU, they
  don't just run slowly. If `beacon` exits instantly with no useful log line on the
  Pi, check the image's platform first.
- **Alpine, not Debian.** Every compiled prod dependency (cryptography, greenlet,
  orjson, aiohttp & family, ciso8601) publishes musllinux x86_64 wheels, so the amd64
  build compiles nothing despite musl — that's what makes one `Dockerfile` viable for
  both targets at all. On ARMv6/musl, no wheels exist for most of them, which is why
  `PURE_PYTHON` and dropping `speedups` matter there specifically.
- **`binutils` in the final image is not optional.** psycopg ships as its pure-Python
  distribution (see `pyproject.toml`), so it dlopens the system `libpq` at import time
  via `ctypes.util.find_library("pq")`. On Linux that tries `ldconfig -p` first — glibc
  has it, but **musl's `ldconfig` rejects `-p` outright** — and then falls back to
  asking `gcc` or `ld` where the library lives. With neither present, `find_library`
  returns `None` and psycopg dies at import with "libpq library not found", however
  installed libpq actually is. `binutils` (~10MB) supplies `ld`, the cheaper of the two
  fallbacks — and the **unversioned `libpq.so` symlink** in the Dockerfile is what
  `ld -lpq` actually resolves, since Alpine's `libpq` package ships only `libpq.so.5`.
  This was verified by running the image: without the symlink, beacon crash-loops
  before it ever reaches the database.
- **The GHCR image is built under QEMU emulation, not natively.** `linux/arm/v6`'s
  emulated `uname -m` can report `armv7l` even though the image is genuinely armv6 (the
  base image and every apk package in it are true armv6) — so a CI build may resolve a
  different uv wheel/sdist path than a native build on the B+ itself would for the same
  dependency. This has not been observed to produce a wrong image, but a green
  `pi-image.yml` run is not by itself proof that a native on-device build would behave
  identically.

## RAM budget

The board's actual `MemTotal` is **486272 kB (~475 MB)** — **measured** on the device,
not the ~490MB figure this doc used to quote (that number was never measured on this
board).

Beacon's own footprint is derived, not measured on-device, because nothing has run the
image on the Pi yet:

- **Measured**: beacon on amd64 (Railway dev, 2-3 test guilds) uses **127-143 MB** RSS.
- **Inferred**: on 32-bit ARMv6, pointers halve and CPython typically lands 30-40% below
  the amd64 figure for the same workload, putting beacon at roughly **85-95 MB** on this
  board. This is inference from the amd64 measurement, not a measurement on ARMv6 —
  confirm with `podman stats` once the stack actually runs there.

The previous version of this table quoted 120-200MB for beacon, sourced from
prod-shaped assumptions at ~5000 guilds — roughly **2x** this box's actual load at 2-3
test guilds. The table below corrects that.

| Component                                      | Approx RSS      | Basis |
|-------------------------------------------------|----------------:|-------|
| Alpine base (kernel + OpenRC + sshd)            | ~30MB           | inferred |
| podman + crun (rootless runtime)                | ~10MB           | inferred |
| postgres (`shared_buffers=16MB` + backends)     | ~60-80MB        | inferred |
| beacon (hikari + lightbulb + asyncio)           | ~85-95MB        | inferred from amd64 measurement |
| supervisord (container PID 1)                   | ~3-5MB          | measured on amd64, inferred to ARMv6 |
| in-container sshd (always resident, see below)  | ~2-3MB          | inferred |
| **Subtotal**                                    | **~190-225MB**  | |
| zram swap cushion (host-provisioned, see infra) | up to 256MB compressed | |

That leaves real headroom under the measured ~475MB even before zram is touched — but
there isn't much to spare for a second bot or a heavier extension set; this image is
sized for one small test bot, not a scaled-down prod mirror.

`supervisord` running two programs was measured (on amd64) at **6192 kB RSS / 4623 kB
PSS / 4376 kB private**, against **6240 kB / 4709 kB / 4424 kB** for a bare
do-nothing CPython interpreter — i.e. it measured *fractionally smaller* than an empty
interpreter. Its own code and state are inside the noise floor: what you pay for is the
runtime, not the supervisor. Two things make the real cost lower still: supervisord and
the bot are the same interpreter from the same venv, so they share libpython's text
pages, and sshd is a small C daemon rather than a second interpreter. `sshd` **runs
whether or not it is armed** — an unset `SSH_AUTHORIZED_KEYS` empties its
`authorized_keys` file rather than stopping the daemon, because "don't start it" needs a
conditional supervisord does not have, and faking one makes the healthy default
deployment log a retry storm on every boot. See the comments in `supervisord.conf`.

## Getting a shell into the container

The container's PID 1 is **supervisord**, and it outlives the bot: if beacon fails to
start three times in a row it goes `FATAL` and supervisord keeps running, so the
container stays up and can still be inspected. That is what the in-container sshd is
for. It is **disarmed by default** — sshd runs, but its `authorized_keys` file is
written from `SSH_AUTHORIZED_KEYS` at every start, so with the variable unset the file
is empty and every login is refused (password auth is off). Arming it (setting that
variable, publishing port 2222, mounting a named volume for host keys so they survive a
container replacement) is host-side deploy config — see the `infra` repo.

Once in:

```sh
supervisorctl -c /etc/dd/supervisord.conf status      # what is running / FATAL
supervisorctl -c /etc/dd/supervisord.conf restart bot # bring the bot back by hand
supervisorctl -c /etc/dd/supervisord.conf tail -f bot
```

(`supervisorctl` and `alembic` are symlinked into `/usr/local/bin` so a login shell
finds them; for anything else in the venv, `export PATH=/app/.venv/bin:$PATH`.)

## Host deployment (owned by `infra`)

Everything about actually running this image on the Pi B+ — root provisioning
(podman/crun/subuid setup, the `dd` user, cgroups v2, zram, `gpu_mem`), the
`compose.yml` stack (Postgres + beacon, `.env`, volumes, port publishing), and how a
deploy gets triggered (pulling a tag/digest from GHCR, `podman-compose up -d`) — lives
in the owner's `infra` repo, not here. That is a deliberate split: this repo defines
what the image *is*, `infra` defines how it runs on a given host, and duplicating
`infra`'s content here would only give it a second copy to go stale. Consult `infra`
directly for that material.
