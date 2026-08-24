# Remote Raspberry Pi 5 dev environment

Develop Destiny Director inside a long-lived Docker container on a Raspberry Pi 5
(`linux/arm64`). The primary workflow is terminal-based: `ssh` into the Pi host →
`docker exec` into the container → run `claude` / git / make. An in-container sshd
(port 2222) additionally lets **Zed remote directly into `/workspace`** (see
[Zed remote / SSH access](#zed-remote--ssh-access)). The container bakes the toolchain
(uv + Node/Claude Code + Railway CLI + GitHub CLI + make); the repo is bind-mounted, so edits
on the host clone and inside the container are the same files.

Files: `Dockerfile.dev`, `docker-child-init.dev.sh`, `docker-compose.dev.yml`,
`ssh_config.dev`, `sshd_config.dev.d/`, `docker-login.dev.sh`,
`docker-rc-supervisor.dev.sh`. Git identity keys live in a gitignored `.dev-ssh/` dir
that rides along with the clone.

## The image is a thin child of a shared base

Since **2026-08-24** `Dockerfile.dev` does not build a dev environment — it pulls one.
Everything this container has in common with its three siblings on the same Pi
(`infra-dev`, `or3-dev`, `ds-dev`) lives in one image, `gsrpi-dev-base`, whose source is
`dev/Dockerfile.base` in the [infra](https://github.com/gsfernandes81/infra) repo and
which is built by CI and published **publicly** to
`ghcr.io/gsfernandes81/gsrpi-dev-base`. From there come python 3.13-slim, Node + Claude
Code, `gh`, screen, **abduco**, fish, the ssh client and server, cloudflared, the `dev`
user, the dotfiles, and the entrypoint that starts everything. What is left in this repo
is the database clients, the Railway CLI, this project's venv, and the repo-specific
files listed above.

- **The tag is pinned** (`ARG BASE_TAG` at the top of `Dockerfile.dev`), never `latest`,
  so a base rebuild cannot change this container until that line moves. Which base a
  running container is on is a label: `docker inspect dd-dev --format '{{index
  .Config.Labels "uk.gsrpi.base-tag"}}'`.
- **No build order and no cross-repo checkout**: the `FROM` pulls. The infra repo is
  needed on this host only for the offline fallback — `make base` there builds the same
  image under the same name, and docker prefers a local image over a pull.
- **What this image adds at start** is `docker-child-init.dev.sh`, run by the base's
  entrypoint after it pulls the clone and before it starts sshd: the `.dev-ssh` git
  identities, and `uv sync --frozen` to add the editable project to the pre-built venv.
  It is non-fatal — a failed sync warns and the container still comes up, so you can ssh
  in and look.
- **Two files parameterise the base rather than fork it**: `ssh_config.dev` (the baked
  half of `~/.ssh/config`, used when `.dev-ssh/ssh_config.fleet` is absent) and
  `sshd_config.dev.d/10-authorized-keys.conf` (points sshd at the host account's
  `authorized_keys`, as it has always been served here). The rest is `DEV_*` environment
  in `docker-compose.dev.yml`.

## Prerequisites (assumed already done on the Pi)

- Docker + `docker compose` v2 and `git` installed.
- The Pi's own SSH server is enabled and your laptop can `ssh <pi-user>@<pi-ip>`.

## One-time bootstrap

```sh
# 1. Clone (HTTPS is fine for the read-only bootstrap) and check out dev.
git clone https://github.com/gsfernandes81/destiny-director.git
cd destiny-director
git checkout dev

# 2. Create the git identity keys in the gitignored .dev-ssh/ dir.
mkdir -p .dev-ssh && chmod 700 .dev-ssh
ssh-keygen -t ed25519 -f .dev-ssh/id_ed25519_personal -N ""   # -> gsfernandes81
ssh-keygen -t ed25519 -f .dev-ssh/id_ed25519_shark    -N ""   # -> geolocatingshark
chmod 600 .dev-ssh/id_ed25519_personal .dev-ssh/id_ed25519_shark
```

Create `.dev-ssh/ssh_config.fleet`. The base image **prepends** it to the baked defaults
to make `~/.ssh/config` at every start, before it pulls the clone — which is why it has
that name and why nothing in this repo symlinks the file:

```
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_personal
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
Host github.com-shark
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_shark
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

The `IdentityFile` paths are `~/.ssh/…` and not `/workspace/.dev-ssh/…`: the base copies
every `.dev-ssh/id_*` into `~/.ssh` at mode 600 on each start, and a copy it owns cannot
be refused for the bind mount's modes.

> **Renaming from the old layout.** This file used to be `.dev-ssh/config`, symlinked
> over `~/.ssh/config` by the entrypoint. If your clone still has that name, rename it —
> `mv .dev-ssh/config .dev-ssh/ssh_config.fleet` — and switch the paths as above. Until
> you do, `docker-child-init.dev.sh` copies it into place each start so nothing breaks,
> says so loudly, and the start-up `git pull` has no identity to offer and reports itself
> as offline whatever the truth is. The symlink is gone on purpose: the base rewrites
> `~/.ssh/config` with a redirection at every start, and a redirection follows a symlink
> — the second boot of a container would have truncated your `.dev-ssh/config` and filled
> it with the baked defaults.

Register each **public** key with its GitHub account (Settings → SSH keys):
`cat .dev-ssh/id_ed25519_personal.pub` → gsfernandes81, `…_shark.pub` → geolocatingshark.

```sh
# 3. Put the dev .env at the repo root (bind-mounted -> visible in-container).
#    Simplest: scp it from your existing dev box. It must contain every var the
#    bots read at import (Discord tokens, etc.), plus:
#      DATABASE_URL=postgresql://kyber:kyber@postgres:5432/kyber
#      RAILWAY_API_TOKEN=<railway account token>   # Railway -> Account -> Tokens
#    (.env is required even for unit tests — make test uses --env-file .env and
#     dd/common/cfg.py reads env at import.)

# 4. Build + start (dev container + postgres) AND log in to everything in one command.
#    `make dev` builds/starts the container, then runs an interactive walkthrough
#    that signs you in to whatever isn't done yet — git SSH, GitHub, Railway, Claude.
#    Each step is idempotent (already-authed services are skipped), so it's safe to
#    re-run. Run it on the Pi host, in the clone:
make dev

# 5. Enter the container over the Pi host's sshd (fish is the interactive shell).
ssh -t <pi-user>@<pi-ip> 'docker exec -it dd-dev fish'
```

`make dev` = `make dev-up` (build + start) + `make dev-login` (the login walkthrough).
Use `make dev-up` alone if you only want to (re)build without touching logins, and
`make dev-login` alone to re-run just the login steps against a running container.
Step 2's manual key generation is optional — the walkthrough offers to generate a git
SSH key for you (into `.dev-ssh/`) and upload it via `gh` if nothing authenticates yet.

Optional laptop `~/.ssh/config` alias so `ssh dd` drops you straight in:

```
Host dd
  HostName <pi-ip>
  User <pi-user>
  RequestTTY yes
  RemoteCommand docker exec -it dd-dev fish
```

**uid note:** the container's `dev` user is built in the BASE image, not here — the
published one bakes uid/gid **1001**, so bind-mounted files (source + `.dev-ssh/` keys)
line up with a Pi account at that uid. `make dev-up` no longer passes `USER_UID`/
`USER_GID`, because this image no longer creates the account. If your Pi user's uid
differs, build the base locally instead: `cd ~/infra/dev && make base` reads its own
clone's owner and tags the result under the same ghcr name, and the next `make dev-up`
here picks it up without touching this repo. Changing the uid means the named volumes
must be recreated under the new owner — `make dev-down-volumes`.

### What else the base changed, and what this repo put back

Three behaviours moved with the base image. Two are put back here because the conversion
is meant to change what the image is *built from*, not what it does:

- **`AllowTcpForwarding`** — the old `sshd_config.dev` had no such line, so OpenSSH's
  default (yes) applied and `ssh -p 2222 -L 5432:dd-postgres:5432 dev@<pi>` worked. The
  base turns forwarding off, correctly for itself. `sshd_config.dev.d/20-forwarding.conf`
  turns it back on here.
- **The `dev` account's login shell** — the base makes it fish; this image puts it back to
  bash (`usermod` in `Dockerfile.dev`), because sshd runs the login shell from
  `/etc/passwd`, so it is what every `ssh <pi> '<cmd>'` and every Zed remote bootstrap
  command executes under, and this repo's tooling assumes a POSIX shell there. `docker
  exec -it dd-dev fish` is unchanged.
- **`ClientAliveInterval 30` / `ClientAliveCountMax 3`** — kept, not overridden. The base
  drops a session after 90s of *unanswered probes*, which a live client answers; what it
  actually removes is wedged sessions, and abduco (also new, from the base) is what makes
  losing one free.

## First run inside the container (`/workspace`, user `dev`)

```sh
# Git remotes + identity (keys are wired into ~/.ssh at start by child-init.sh).
git remote set-url origin git@github.com:gsfernandes81/destiny-director.git
git remote add shark git@github.com-shark:geolocatingshark/destiny-director.git
git config user.name  "gsfernandes81"
git config user.email "<your git email>"
ssh -T git@github.com; ssh -T git@github.com-shark   # both should greet their user

# Prove the toolchain.
uv sync                                  # editable project into /home/dev/venv
make test                                # DB-free unit suite (SQLite)
uv run ruff check dd && uv run ty check dd
```

## Claude Code

Node 22 + `@anthropic-ai/claude-code` are baked in; `~/.claude` is a persistent volume,
so login survives rebuilds. `make dev` / `make dev-login` handle sign-in for you
(`claude auth login`), but you can also do it directly:

```sh
claude auth login     # prints a URL — open on your laptop, paste the code back
claude auth status    # verify
```

**Claude Remote Control starts automatically.** The base image's entrypoint starts this
repo's supervisor (`docker-rc-supervisor.dev.sh`, baked in at
`/home/dev/rc-supervisor.sh`) whenever `DEV_REMOTE_CONTROL=1` — it is set in
`docker-compose.dev.yml` — and that supervisor launches `claude remote-control --spawn
worktree` as soon as you're signed in (it polls auth every ~10s), so you can drive this
container's sessions from [claude.ai/code](https://claude.ai/code) or the Claude mobile
app with nothing to type. `--spawn worktree` gives each on-demand session its own git
worktree, and `--no-create-session-in-dir` means an unused daemon sits at a true **0/32**
(no phantom cwd session).

**Since the base landed, the supervisor runs in the BACKGROUND and sshd is the foreground
process** — the reverse of the arrangement before it, because the door is what the
container's lifetime should equal: a wedged supervisor must not take away the ssh you
would fix it from. So `docker logs dd-dev` shows sshd and the start-up lines, not the
Claude stream, and `~/.local/share/remote-control.log` is where to read the supervisor.
That file gets a cleaned view:
escape sequences stripped, repeated TUI repaints collapsed to one copy (with a
`(suppressed N repeated TUI line(s))` note so nothing looks quieter than it was), and
the file rotated at `RC_LOG_MAX_BYTES` (default 5MiB) keeping one `.1` generation.
Teeing the raw stream used to grow that file ~5MB/day of cursor-control noise.

*Why a supervisor and not just restart-on-crash:* Claude Code's remote-control server
has a known class of upstream hangs where the **process stays alive but wedges** and
stops accepting new sessions (anthropics/claude-code#51267, #40416, #37321). A
restart-on-exit loop can't recover that, so the supervisor also **health-recycles** a
wedged daemon — but only when it is free to: it restarts the daemon **only at a literal
0/32 sessions**, never while a session is live (even an idle one), because killing a
live session forces a painful remote recovery. Concretely: a freshly started daemon is
left alone until it has served ≥1 session; once it has been used and then drops to 0
sessions, it gets `RC_IDLE_RECYCLE_SECS` (default 300s) of continuous idle and is then
recycled once. So you always return to a fresh, unwedged daemon after an idle gap, but
an untouched one is never churned. Tunables (env): `RC_POLL_SECS`,
`RC_IDLE_RECYCLE_SECS`, `RC_PERMISSION_MODE` (default keeps prompts on),
`RC_LOG_MAX_BYTES`. A daemon that
wedges *mid-session* is deliberately left until that session ends — end the stuck
session from claude.ai/code (or `docker exec`) and the idle recycle takes it from there.

If Claude Code's Bash sandbox blocks writes to `~/.cache/uv` (breaks uv/ruff/ty/pytest),
relax the sandbox in-container — the container is already an isolation boundary.

## Railway CLI

The Railway CLI is baked into the image (installed from the release tarball — the
`@railway/cli` npm package 404s on arm64). With `RAILWAY_API_TOKEN` in `.env` the container is already
authenticated; verify with `railway whoami`. (Alternative: `railway login --browserless`,
persisted via the `dd-railway` volume.) `make deploy-beacon-dev` / `deploy-anchor-dev`
then run from inside the container.

> **Prod deploys require explicit confirmation each time (see CLAUDE.md). Never deploy
> prod on your own initiative.**

## GitHub CLI

`gh` is baked into the image (installed from the release tarball, same as the Railway CLI).
`GH_CONFIG_DIR` points at `~/.config/gh`, a persistent `dd-gh` volume, so login survives
rebuilds. Authenticate once with the device flow:

```sh
gh auth login   # choose GitHub.com → HTTPS → "Login with a web browser"; open the URL on
                # your laptop, paste the one-time code back
gh auth status  # verify
```

This slim image has no secret keyring, so `gh` stores the token in
`~/.config/gh/hosts.yml` inside the volume. Once authenticated you can read code scanning
alerts and other API data, e.g.
`gh api repos/gsfernandes81/destiny-director/code-scanning/alerts`. (Alternative: set
`GH_TOKEN` in `.env` to a PAT instead of running `gh auth login`.)

## Postgres / migrations (integration scope)

```sh
docker compose -f docker-compose.dev.yml up -d postgres   # if not already up
make migration-apply                                       # apply against DATABASE_URL
TEST_USE_POSTGRES=1 uv run --env-file .env python -m pytest -m integration  # DB suite on Postgres
```

Applying migrations needs only the running `postgres` service. Authoring new migrations
(`make migration-plan MSG="..."`) autogenerates a revision by diffing
`dd/common/schemas.py`'s models straight against that same live database — no throwaway
"dev" database or Docker socket needed (that was an Atlas-specific requirement this repo
no longer has). Always hand-check the generated revision; autogenerate is a starting
point, not an oracle. `postgres:17-alpine` is much lighter than `mysql:8` was; still fine
to leave it stopped when not integration-testing.

## Editing

Terminal-only by default: edit via Claude Code or a terminal editor (`vim`/`nano`/`helix`,
`apt`-installable in-container) inside `docker exec`. No editor is installed on the Pi host.

### Zed remote / SSH access

The container also runs an in-container sshd (port 2222) so **Zed can remote directly into
`/workspace`** (reversing the original terminal-only, no-sshd/no-ports decision). It runs as
the non-root `dev` user, key-only — so only `dev` can log in, preserving the `docker exec`
model and uid-1000 file ownership. Authorized keys are **not hardcoded**: the compose file
bind-mounts the Pi host user's `.ssh` directory (from `DEV_SSH_AUTHORIZED_KEYS` in `.env`,
e.g. `/home/<pi-user>/.ssh/`) read-only, and sshd reads `authorized_keys` from it — so it
authorizes the same keys that already log into the Pi host. Host `2222` publishes the
container's sshd; the host key is persisted in the `dd-ssh-host` volume so Zed's
`known_hosts` stays stable across `make dev-down && make dev-up`.

To use it:

1. Ensure `DEV_SSH_AUTHORIZED_KEYS` is set in `.env` (Pi host user's `.ssh` dir) and that
   its `authorized_keys` lists the public key you'll connect with (Zed's key).
2. `make dev-up` rebuilds the image (with `openssh-server`) and starts sshd on 2222.
3. Locally: `ssh -p 2222 dev@<pi-ip>` should log in as `dev`. For access off-LAN, point a
   **Cloudflare tunnel** at the Pi host's TCP port 2222 (configured from the Cloudflare
   dashboard — out of repo scope; key-based SSH only, pair with Cloudflare Access as you
   see fit).
4. In Zed, add an SSH remote to that host as user `dev` with the matching private key and
   open `/workspace`. Zed uploads its server and connects; SSH sessions inherit the app env
   (`.env` vars + the venv on `PATH`) via `~/.ssh/environment`, so tools resolve as they do
   under `docker exec`.

The old `docker exec -it dd-dev fish` path still works unchanged.
