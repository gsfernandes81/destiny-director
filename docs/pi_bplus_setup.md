# Raspberry Pi B+ test-bot deployment

Run a real (test-guild) copy of `dd.beacon` + its own Postgres on an **original
Raspberry Pi B+** (BCM2835, single ARM1176JZF-S core @ 700MHz, ARMv6, 512MB RAM) — a
low-stakes, always-on box for exercising the bot outside of Railway. No Pi 5 (or any
other build host) is *required* for this setup: images can be built directly on the B+
itself, unprivileged as `dd`, from a clone of this repo — nothing pulled from or pushed
to a registry, nothing shipped over SSH. Two faster alternatives exist for whoever wants
them: a Pi 5 cross-build path (see
[Optional: Pi 5 cross-build](#optional-faster-alternative--build-on-a-pi-5)), and a
prebuilt image that `.github/workflows/pi-image.yml` now publishes to GHCR on every push
to `dev`/`slim-stack` (see
[Fastest: pull the prebuilt image](#fastest-pull-the-prebuilt-image-from-ghcr)) — but
building on the B+ itself remains the primary/assumed path documented step-by-step
below.

Files: `deploy/pi-bplus/root-setup.sh`, `deploy/pi-bplus/compose.yml`,
`deploy/pi5/root-setup.sh`, `deploy/pi5/build-and-ship.sh`,
`.github/workflows/pi-image.yml` (builds + publishes the prebuilt image, see below), and
— shared with Railway — the repo-root `Dockerfile`, `supervisord.conf` and
`sshd_config`.

There is **one image definition for both deployment targets**. The separate
`deploy/pi-bplus/Containerfile` is gone, and so are the shell entrypoints
(`docker-entrypoint.sh`, `deploy/pi-bplus/entrypoint.sh`) and `build-local.sh`: the
container's PID 1 is now **supervisord**, configured by `supervisord.conf`. What used to
differ between the two targets is now three build args on the one Dockerfile, whose
defaults are the Railway/amd64 ones — see [Step 2](#step-2--build-the-image-on-the-b-primary-path).

## Hardware / OS assumptions

- **Raspberry Pi B+** (the original 512MB board, not a B+ variant of a later Pi) —
  ARMv6, no NEON, no hardware float in the same class as v7+. This is why every image
  here targets `linux/arm/v6` specifically: v7 or v8 binaries raise `SIGILL` on this
  CPU, they don't just run slowly.
- **Alpine Linux 3.23, `sys` (disk) install** — installed to the SD card as a normal
  read-write root filesystem, *not* Alpine's `diskless` boot mode. Diskless mode keeps
  the whole running system in a RAM-backed overlay (synced to disk only via `lbu
  commit`), which is the wrong trade for this box: a 512MB machine can't spare the RAM
  to hold its own OS image *and* Postgres's buffers *and* the bot process, and an
  unsynced diskless system loses Postgres's data directory outright on power loss —
  worse than the SD-endurance trade-off `sys` mode makes (see
  [Caveats](#constraints--caveats) below).
- **No Pi 5 (or any other build host) in this setup.** Images are built directly on the
  B+, natively, as ARMv6 — which is only feasible because the dependency-install layer
  now compiles just one package from sdist (`greenlet`, SQLAlchemy's asyncio C bridge —
  `regex` and `dateparser` were dropped in favor of pure-Python `re`/`python-dateutil`,
  and aiohttp/multidict/yarl/frozenlist/propcache are forced to their pure-Python
  builds via `*_NO_EXTENSIONS=1`). With only that one small C build left, an on-device
  build is slow but no longer impractical — expect roughly 30+ minutes on the single
  700MHz ARM1176 core (see [Step 2](#step-2--build-the-image-on-the-b-primary-path)).
  A Raspberry Pi 5 — whose Cortex-A76 executes 32-bit ARM userland natively, so
  `podman build --platform linux/arm/v6` runs without qemu/binfmt emulation — remains
  available as a strictly optional, faster cross-build path for anyone who has one; it
  is not assumed or required by anything else in this document.
- Rootless **podman** + **podman-compose** throughout on the B+ (no Docker, no root
  containers) — see `deploy/pi-bplus/root-setup.sh` for why (no systemd/logind on
  Alpine, no cgroup delegation for rootless user slices under OpenRC).

## Step 1 — root setup on the B+

As root on the B+, once:

```sh
sh deploy/pi-bplus/root-setup.sh
reboot
```

It (idempotently) does everything that needs root, so nothing after this step does:

1. Enables the `community` apk repo and installs `podman`, `crun`, `passt`,
   `shadow-uidmap`, `podman-compose`, `zram-init`.
2. Creates the `dd` user and authorizes the deploy SSH key.
3. Grants `dd` a subuid/subgid range (rootless podman's user-namespace mapping).
4. Switches cgroups to unified (v2) mode and enables the `cgroups` service.
5. Creates `/run/user/<uid>` for `dd` and writes `/etc/local.d/podman-user.start`, which
   at boot recreates that runtime dir and — if `/srv/dd/compose.yml` exists (Step 3) —
   runs `podman-compose up -d` as `dd`. `XDG_RUNTIME_DIR` is also exported from `dd`'s
   `.profile` for interactive SSH sessions.
6. Creates `/srv/dd`, owned by `dd` — this is where `compose.yml` and `.env` go
   (Step 3).
7. Sets up a `zram` swap device (`ZRAM_SIZE`, default 256M) — compressed-RAM swap
   cushions transient memory pressure without hitting the SD card.
8. Caps GPU memory split to 16MB (`gpu_mem=16` in `/boot/usercfg.txt` /
   `/boot/config.txt`) — this box is headless, so give the extra RAM back to Linux.
9. Adds `noatime` to the root filesystem's `fstab` entry, if it can do so safely
   (fewer SD writes).

**Reboot after this step** — the cgroup mode, `gpu_mem`, and `noatime` changes all need
it. Everything from here on runs unprivileged as `dd` over SSH.

## Step 2 — build the image on the B+ (primary path)

`root-setup.sh` (Step 1) does not install `git` — it only installs what the running
stack needs (`podman`, `crun`, `passt`, `shadow-uidmap`, `podman-compose`, `zram-init`),
not what building from a repo clone needs. Add it once, as root, before cloning:

```sh
apk add git
```

(This is a deliberate omission from `root-setup.sh` rather than an oversight — adding
it there would mean re-running that script, and the whole point of Step 1 is that you
only run it once. Installing `git` by hand here is one extra command against re-running
a root script for a one-line change.)

Then, as `dd` (everything from here on is unprivileged), clone the repo onto the B+ and
build:

```sh
git clone <this-repo-url> ~/destiny-director
cd ~/destiny-director
podman build --platform linux/arm/v6 \
    --build-arg BASE_IMAGE=docker.io/arm32v6/python:3.13-alpine3.23 \
    --build-arg UV_SYNC_GROUPS=--no-default-groups \
    --build-arg PURE_PYTHON=1 \
    -t dd-beacon:latest .
```

That builds the repo-root `Dockerfile` for `linux/arm/v6` natively (no qemu, no
cross-build host, nothing shipped over SSH) and tags it `dd-beacon:latest`. The three
build args are the whole difference between this image and the Railway one: the ARMv6
base (v7/v8 binaries `SIGILL` on this CPU), dropping the `speedups` group (cryptography
has no ARMv6/musl wheel and would need a Rust toolchain), and forcing the aiohttp family
to its pure-Python builds. `--platform linux/arm/v6` is redundant on the B+ itself (the
build is already native) but is passed anyway so the intent is unmissable in the build
log and so copy-pasting this onto a v7/v8 board cannot silently produce an image that
`SIGILL`s on the real B+.

If `XDG_RUNTIME_DIR` is unset (a non-interactive `ssh host '...'` never sources `dd`'s
`.profile`), export it first — `export XDG_RUNTIME_DIR=/run/user/$(id -u)` — or podman
fails with its rootless "could not get runtime directory" error.

**Expect roughly 30+ minutes** on the B+'s single 700MHz ARM1176 core — the bulk of
that is compiling `greenlet` from sdist; see the hardware-assumptions note above for
why that's the only C extension left in the build. This is a one-off wait per image,
not a normal iteration loop, so plan builds around it rather than expecting quick
feedback.

For every rebuild after a code change, from `~/destiny-director` as `dd`:

```sh
git pull
podman build --platform linux/arm/v6 \
    --build-arg BASE_IMAGE=docker.io/arm32v6/python:3.13-alpine3.23 \
    --build-arg UV_SYNC_GROUPS=--no-default-groups \
    --build-arg PURE_PYTHON=1 \
    -t dd-beacon:latest .
cd /srv/dd && podman-compose up -d --force-recreate beacon
```

(`--force-recreate` is required — see the `podman-compose up -d` note in Step 3 below
for why plain `up -d` won't pick up a freshly-built same-tagged image.)

Because the image builds directly on this host, its build layers accumulate on the SD
card across repeated rebuilds — there's no separate build host to absorb that churn.
Periodically run `podman image prune` (dangling layers) or, more aggressively,
`podman system prune` (unused images/containers/build cache) as `dd` to reclaim space;
how often depends on how frequently you're rebuilding, but it's worth checking `df -h`
after a run of several rebuilds in a short span.

### Optional faster alternative — build on a Pi 5

If a Raspberry Pi 5 is available, it cross-builds the same image far faster than the
B+'s single 700MHz core can build it natively, and ships it over SSH instead of
building in place. This is entirely optional — nothing else in this document depends
on it, and the B+ never needs a Pi 5 to be usable.

On the Pi 5, once:

```sh
sudo bash deploy/pi5/root-setup.sh
```

This installs `podman` + `uidmap` + `slirp4netns`, authorizes the deploy SSH key for
the build user, grants it a subuid/subgid range, and enables user lingering (so the
build user's podman keeps working across logout). No reboot needed.

Then, from the repo root on the Pi 5, for every build (and every rebuild after a code
change):

```sh
deploy/pi5/build-and-ship.sh dd@<pi-bplus-hostname-or-ip>
```

This builds the repo-root `Dockerfile` for `linux/arm/v6` (with the same three build
args as above), tags it
`dd-beacon:latest`, and pipes `podman save` straight into `ssh ... podman load` on the
B+ — no registry involved. It takes a `user@host` argument (or reads
`PI_BPLUS_TARGET` from the environment). With this path, the B+ never needs its own
repo clone or `git` install — the image arrives pre-built.

### Fastest: pull the prebuilt image from GHCR

No build hardware needed at all: `.github/workflows/pi-image.yml` builds this same
`Dockerfile` for `linux/arm/v6` under QEMU emulation (with the same three build args as
Step 2 above) on every push to `dev`/`slim-stack` and on manual dispatch, and pushes the
result to GHCR — a normal GitHub Actions runner does in a few minutes what takes 30+ on
the B+'s own core. On the B+, as `dd`:

```sh
podman pull ghcr.io/gsfernandes81/destiny-director:sha-<full commit sha>
```

The workflow's job summary for a given run prints the exact tag and digest it pushed —
pin to that digest for anything you want to be able to reproduce later; the moving
`dev`/`slim-stack` branch tags are there for convenience, not reproducibility.

**One-time manual step, not something this workflow can do**: GHCR packages default to
*private*, and package visibility is a setting on the package itself in GitHub's UI, not
a workflow permission — an unauthenticated `podman pull` from the B+ will 403 until an
owner makes the `destiny-director` package public (on the package's GitHub page:
Package settings → Danger Zone → Change visibility). Do this once, after the workflow's
first successful push has created the package.

This path does not update `deploy/pi-bplus/compose.yml`, which still references the
locally-built/loaded `localhost/dd-beacon:latest` tag — pointing it at a pulled
`ghcr.io/...` tag instead is a real change (the image reference, and probably how
updates get pulled going forward) and is left for whoever next touches host-side
deployment config, rather than made as a one-line edit here.

## Step 3 — deploy config + bring the stack up

Copy `deploy/pi-bplus/compose.yml` to `/srv/dd/compose.yml` on the B+ (`scp` or a copy
+ paste over the SSH session both work — it's a static file, not something
`root-setup.sh` templates):

```sh
scp deploy/pi-bplus/compose.yml dd@<pi-bplus>:/srv/dd/compose.yml
```

Then create `/srv/dd/.env` on the B+ directly (never commit it — same rule as the
repo's own `.env`). It's read by **both** compose services via `env_file:`, so it holds
the Postgres password *and* the bot's own config, `.env-example`-style. At minimum:

```sh
# Which bot this container runs. supervisord's only program command is
# `python -OO -m dd.$RAILWAY_SERVICE_NAME`, so this is REQUIRED on the Pi — Railway
# injects it automatically, nothing here does. supervisord refuses to start at all if it
# is unset, so a missing value fails loudly and immediately rather than subtly.
RAILWAY_SERVICE_NAME=beacon

# Postgres (consumed by the postgres service's env_file)
POSTGRES_PASSWORD=<pick something>

# Points the bot at the compose-network Postgres. dd/<PASSWORD>/kyber must match
# POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB in compose.yml above.
DATABASE_URL=postgresql://dd:<same password as POSTGRES_PASSWORD>@postgres:5432/kyber
DATABASE_SSL=false   # in-network traffic between two containers on the same host; no TLS to negotiate

# Discord
DISCORD_TOKEN_BEACON=<test bot token>
TEST_ENV=<test-guild id>[,<second test-guild id>...]

# Optional: arms the in-container sshd (see "Getting a shell into the container"
# below). One public key; unset means sshd runs but authorizes nobody.
# SSH_AUTHORIZED_KEYS=ssh-ed25519 AAAA... you@yourbox

# ...plus every other var dd.common.cfg validates at import time — copy the rest of
# .env-example's beacon-relevant vars (CONTROL_DISCORD_SERVER_ID, FOLLOWABLES,
# EMBED_DEFAULT_COLOR, etc.). cfg.py fails fast with a ValueError naming the first
# missing one, which is the fastest way to find what you missed.
```

Bring it up:

```sh
cd /srv/dd
XDG_RUNTIME_DIR=/run/user/$(id -u) podman-compose up -d
```

(root-setup.sh's boot-time script does this automatically after a reboot once
`compose.yml` exists — this manual command is for the first bring-up, and for picking
up a freshly-built image without rebooting: `podman-compose up -d` alone does *not*
recreate a running container against a same-tagged image it already has, so after
the `podman build` above (or `build-and-ship.sh`, on the Pi 5 path) produces a new
image, run
`podman-compose up -d --force-recreate beacon` — this is exactly the last line of
Step 2's rebuild loop above.)

## Verification checklist

- `podman info` — under **Storage**, `graphDriverName` should read `overlay`, not
  `vfs` or `fuse-overlayfs`. `vfs` works but is slow and burns disk on every layer copy
  (real cost on an SD card); `fuse-overlayfs` is podman's rootless fallback when the
  kernel/host doesn't support rootless native overlay — Alpine 3.23's kernel + `crun`
  combo should give native overlay, but confirm rather than assume on a board this old.
  Also check **OCI Runtime** is `crun` (installed by `root-setup.sh`; `runc` doesn't
  ship for `armv6` in the same way) and **cgroup Manager**/**cgroupVersion** show `v2`
  (`root-setup.sh` step 4).
- `cd /srv/dd && podman-compose logs -f` — first `supervisord started with pid 1`, then
  `spawned: 'bot'`, then `postgres` reaching `database system is ready to accept
  connections`, then beacon's own `alembic upgrade head` and hikari's gateway-connected
  log line. A cold start racing `initdb` is handled inside the bot now
  (`dd.common.schemas.wait_for_db` runs before the migration, both in beacon's
  `StartingEvent` hook), not by an entrypoint retry loop — so the bot simply waits
  rather than exiting and being restarted.
- `sshd` losing one round to `sshd-keygen` on the very first boot (`sshd: no hostkeys
  available -- exiting`, then a respawn a second later) is expected and self-healing —
  supervisord has no ordering mechanism, and its retry loop is what supplies it.
- **Slow startup is normal.** A cold start (`podman-compose up -d` right after boot)
  can take a couple of minutes end to end on this CPU: Postgres's first `initdb`, then
  `alembic upgrade head` running every migration from scratch, then hikari's own
  gateway handshake. Don't take 60-90s of silence as a hang.
- `podman ps` — both containers `Up`; `podman stats --no-stream` — sanity-check RSS
  against the budget below.

## Getting a shell into the container

The container's PID 1 is **supervisord** (`supervisord.conf`, shared with Railway), and
it outlives the bot: if beacon fails to start three times in a row it goes `FATAL` and
supervisord keeps running, so the container stays up and can still be inspected. That is
what the in-container sshd is for.

It is **disarmed by default**: sshd runs, but its authorized_keys file is written from
`SSH_AUTHORIZED_KEYS` at every start, so with the variable unset the file is empty and
every login is refused (password auth is off). To arm it, put one public key in
`/srv/dd/.env` and recreate the container:

```sh
# in /srv/dd/.env
SSH_AUTHORIZED_KEYS=ssh-ed25519 AAAA... you@yourbox
```

sshd listens on **2222** (it runs as the unprivileged `dd` user, so it cannot use 22),
inside the container's network namespace. `compose.yml` does not publish that port —
publish it, or reach it with `podman exec`, depending on how much you trust the network
the B+ is on.

Two things `compose.yml` should carry for this to be pleasant (it is host-side
deployment config, so it is edited on the B+ / in whatever repo owns it, not here):

* a **named volume mounted at `/home/dd/.ssh-host`**. Host keys are generated at
  runtime, never baked into the image — a baked host key would be the same private key
  in every deployment — so without a volume they are regenerated on every container
  replacement and your client warns about a changed host key each time. It must be a
  *named* volume, not a bind mount: the image pre-creates `.ssh-host/etc/ssh`, and a
  named volume is seeded from the image while a bind mount would hide it.
* a **port publish for 2222**, if you want to ssh in from off-box.

Once in:

```sh
supervisorctl -c /etc/dd/supervisord.conf status      # what is running / FATAL
supervisorctl -c /etc/dd/supervisord.conf restart bot # bring the bot back by hand
supervisorctl -c /etc/dd/supervisord.conf tail -f bot
```

(`supervisorctl` and `alembic` are symlinked into `/usr/local/bin` so a login shell
finds them; for anything else in the venv, `export PATH=/app/.venv/bin:$PATH`.)

## Expected RAM budget

512MB total, of which usable Linux RAM after `gpu_mem=16` is roughly the standard
~490MB. Rough steady-state budget (idle test bot, one small guild):

| Component                          | Approx RSS |
|-------------------------------------|-----------:|
| Alpine base (kernel + OpenRC + sshd) |     ~30MB |
| podman + crun (rootless runtime)     |     ~10MB |
| postgres (`shared_buffers=16MB` + backends) | ~60-80MB |
| beacon (hikari + lightbulb + asyncio)| ~120-200MB |
| supervisord (container PID 1)        |    ~3-5MB |
| in-container sshd (always resident; see below) |    ~2-3MB |
| **Subtotal**                         | **~225-330MB** |
| zram swap cushion                    | up to 256MB compressed |

That leaves real headroom under 490MB even before zram is touched, but there isn't
much to spare for a second bot or a heavier extension set — this box is sized for one
small test bot, not a scaled-down prod mirror.

### On supervisord's and sshd's share of that

Measured rather than estimated, because on a 512MB board a supervisor that "just adds
a few MB" is worth checking rather than assuming. `supervisord` running two programs
was **6192 kB RSS / 4623 kB PSS / 4376 kB private**, against **6240 kB / 4709 kB /
4424 kB** for a bare do-nothing CPython interpreter — i.e. it measured *fractionally
smaller* than an empty interpreter. Its own code and state are inside the noise floor:
what you pay for is the runtime, not for supervisor.

Two things make the real cost lower still than the ~6MB standalone figure: supervisord
and the bot are the same interpreter from the same venv, so they share libpython's text
pages; and sshd is a small C daemon rather than a second interpreter. Note that sshd
**runs whether or not it is armed** — an unset `SSH_AUTHORIZED_KEYS` empties its
authorized_keys file rather than stopping the daemon, because the alternative
(not starting it) needs a conditional supervisord does not have, and faking one makes
the healthy default deployment log a retry storm on every boot. See the comments in
`supervisord.conf`. The table's ~3-5MB reflects the 32-bit ARMv6 build, where pointers
halve and CPython typically lands 30-40% below the amd64 figures quoted above — that
part is inference from the amd64 measurement, not a measurement on this board, so
confirm it with `podman stats` once the stack is actually running.

The trade runs in the box's favour regardless: those few MB buy *bounded* restarts,
which is what prevents the genuinely expensive failure mode — an unbounded crash-loop
where the container restarts forever, re-running `alembic upgrade head` against the SD
card on every cycle.

## Constraints / caveats

- **Images must be built for `linux/arm/v6`.** `linux/arm/v7` or `arm64` binaries
  `SIGILL` immediately on this CPU rather than degrading gracefully — if `beacon`
  exits instantly with no useful log line, check the image's platform first.
- **No `--memory` / `mem_limit` anywhere in `compose.yml`, deliberately.** Rootless
  podman on Alpine/OpenRC has no cgroup delegation for user slices (no systemd/logind),
  so a rootless container here cannot have a `memory.max` set at all — see
  `deploy/pi-bplus/root-setup.sh`'s header comment. `oom_score_adj: 800` on `beacon`
  is the closest available substitute: it doesn't cap memory, it just tells the
  kernel's OOM killer to reap `beacon` before `postgres` or the host if the box does
  run out of RAM.
- **`synchronous_commit=off`** trades a small durability window (the last few committed
  transactions can be lost on a *hard power loss*, not on a process crash or container
  restart) for far less fsync-driven latency on SD-card storage. Acceptable for a test
  bot's data; do not carry this setting into anything that needs prod-grade durability.
- **SD card endurance.** `noatime` (root-setup.sh), `wal_compression=on` and a small
  `shared_buffers` (compose.yml) all reduce write volume, but this remains a consumer
  SD card doing database writes continuously — plan to re-flash/replace it
  periodically, and don't be surprised if it's the first thing to fail on this box.
- **`docker.io/arm32v6/postgres:17-alpine`** was confirmed present on Docker Hub as of
  Aug 2026 (the alpine variants are the ones built for arm32v6; the Debian-based tags
  are not). If a future major drops the arch, check
  `https://hub.docker.com/r/arm32v6/postgres/tags` for the nearest alpine tag.
- **The GHCR image is built under QEMU emulation, not natively.** `linux/arm/v6`'s
  emulated `uname -m` can read `armv7l` even though the image is genuinely armv6 (base
  image and every apk package in it), so a CI build may resolve a different uv
  wheel/sdist than a native build on the B+ would for the same dependency. This has not
  been observed to produce a wrong image, but it means a green `pi-image.yml` run is not
  by itself proof that the on-device build (Step 2) would behave identically. The GHCR
  package must also be made public by hand once (see
  [Fastest: pull the prebuilt image](#fastest-pull-the-prebuilt-image-from-ghcr)) —
  that's a GitHub package setting, not a workflow permission, so no workflow change
  can do it.
