# Raspberry Pi B+ test-bot deployment

Run a real (test-guild) copy of `dd.beacon` + its own Postgres on an **original
Raspberry Pi B+** (BCM2835, single ARM1176JZF-S core @ 700MHz, ARMv6, 512MB RAM) — a
low-stakes, always-on box for exercising the bot outside of Railway. Images are built
on a Raspberry Pi 5 and shipped to the B+ over SSH; nothing is pulled from or pushed to
a registry.

Files: `deploy/pi-bplus/root-setup.sh`, `deploy/pi-bplus/Containerfile`,
`deploy/pi-bplus/entrypoint.sh`, `deploy/pi-bplus/compose.yml`,
`deploy/pi5/root-setup.sh`, `deploy/pi5/build-and-ship.sh`.

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
- **Raspberry Pi 5**, used only as a build host — its Cortex-A76 executes 32-bit ARM
  userland natively, so `podman build --platform linux/arm/v6` runs without qemu/binfmt
  emulation.
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

## Step 2 — build and ship from the Pi 5

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

This builds `deploy/pi-bplus/Containerfile` for `linux/arm/v6`, tags it
`dd-beacon:latest`, and pipes `podman save` straight into `ssh ... podman load` on the
B+ — no registry involved. It takes a `user@host` argument (or reads
`PI_BPLUS_TARGET` from the environment).

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
# Postgres (consumed by the postgres service's env_file)
POSTGRES_PASSWORD=<pick something>

# Points the bot at the compose-network Postgres. dd/<PASSWORD>/kyber must match
# POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB in compose.yml above.
DATABASE_URL=postgresql://dd:<same password as POSTGRES_PASSWORD>@postgres:5432/kyber
DATABASE_SSL=false   # in-network traffic between two containers on the same host; no TLS to negotiate

# Discord
DISCORD_TOKEN_BEACON=<test bot token>
TEST_ENV=<test-guild id>[,<second test-guild id>...]

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
up a freshly-shipped image without rebooting: `podman-compose up -d` alone does *not*
recreate a running container against a same-tagged image it already has, so after
`build-and-ship.sh` ships an update run `podman-compose up -d --force-recreate
beacon`.)

## Verification checklist

- `podman info` — under **Storage**, `graphDriverName` should read `overlay`, not
  `vfs` or `fuse-overlayfs`. `vfs` works but is slow and burns disk on every layer copy
  (real cost on an SD card); `fuse-overlayfs` is podman's rootless fallback when the
  kernel/host doesn't support rootless native overlay — Alpine 3.23's kernel + `crun`
  combo should give native overlay, but confirm rather than assume on a board this old.
  Also check **OCI Runtime** is `crun` (installed by `root-setup.sh`; `runc` doesn't
  ship for `armv6` in the same way) and **cgroup Manager**/**cgroupVersion** show `v2`
  (`root-setup.sh` step 4).
- `cd /srv/dd && podman-compose logs -f` — `postgres` should reach `database system is
  ready to accept connections`, then `beacon` should run its `alembic upgrade head`
  (look for `entrypoint.sh`'s retry line if it races a cold Postgres start — one or two
  retries on first boot is expected, not a bug) and then hikari's gateway-connected log
  line.
- **Slow startup is normal.** A cold start (`podman-compose up -d` right after boot)
  can take a couple of minutes end to end on this CPU: Postgres's first `initdb`, then
  `alembic upgrade head` running every migration from scratch, then hikari's own
  gateway handshake. Don't take 60-90s of silence as a hang.
- `podman ps` — both containers `Up`; `podman stats --no-stream` — sanity-check RSS
  against the budget below.

## Expected RAM budget

512MB total, of which usable Linux RAM after `gpu_mem=16` is roughly the standard
~490MB. Rough steady-state budget (idle test bot, one small guild):

| Component                          | Approx RSS |
|-------------------------------------|-----------:|
| Alpine base (kernel + OpenRC + sshd) |     ~30MB |
| podman + crun (rootless runtime)     |     ~10MB |
| postgres (`shared_buffers=16MB` + backends) | ~60-80MB |
| beacon (hikari + lightbulb + asyncio)| ~120-200MB |
| **Subtotal**                         | **~220-320MB** |
| zram swap cushion                    | up to 256MB compressed |

That leaves real headroom under 490MB even before zram is touched, but there isn't
much to spare for a second bot or a heavier extension set — this box is sized for one
small test bot, not a scaled-down prod mirror.

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
