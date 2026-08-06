# Handoff: finish the Pi B+ ("two") test-bot deployment from the cloud

> For the agent picking this up in a NEW cloud session (started in the environment
> whose network allowlist includes `*.gsrpi.uk`). Per `plans/` convention: delete this
> file once the deployment is done; prompt the owner if only partially done.

## State of `slim-stack` (all pushed to origin)

The branch is complete and verified (`make check` green: ruff, ty, 1354 pytest,
203 JS). It contains, in order:

1. **MySQL/asyncmy fully removed → PostgreSQL via pure-Python psycopg.** Env vars are
   now `DATABASE_PRIVATE_URL`/`DATABASE_URL`/`DATABASE_SSL`. SQLite stays for tests.
   Two real Postgres-only bugs were found and fixed during validation (psycopg
   `rowcount=-1` on insert-ignore; tz-aware timestamps shifting on non-UTC servers —
   both have regression tests; CI's Postgres lane runs with `TZ: America/New_York`).
2. **Atlas → Alembic.** Baseline revision catalogue-diffed identical to
   `create_all`; boot advisory lock prevents beacon/anchor `upgrade head` races.
   Targets: `make migration-plan/-apply/-dry-run/-check`.
3. **Dev container + all docs on the new stack** (postgres:17-alpine sibling, rewritten
   `docker-run-devbot.sh`, CLAUDE.md/docs sweep).
4. **Pi B+ deployment assets**: `deploy/pi-bplus/{root-setup.sh,compose.yml}`,
   `deploy/pi5/{root-setup.sh,build-and-ship.sh}`, and the full runbook
   `docs/pi_bplus_setup.md` — READ THE RUNBOOK FIRST. (The Pi's own Containerfile and
   entrypoint.sh are gone: one repo-root `Dockerfile` now builds both targets, and PID 1
   is supervisord — see `supervisord.conf`.)
5. **Pure-Python slimming**: `regex`/`dateparser` replaced by stdlib `re` +
   `python-dateutil`; aiohttp-family forced pure via `*_NO_EXTENSIONS=1` in the
   image build; `greenlet` is the sole remaining sdist compile. Deps-below-code
   layer ordering enforced in both images.

Known non-blocker: `ruff format --check` flags 13 files with formatting drift that
pre-dates this branch. Leave it; it belongs in a separate `make format` commit on dev.

## What remains: deploy to "two" and verify

Target: **original Pi B+**, Alpine 3.23 sys install, reachable ONLY via Cloudflare
tunnel — `gavin@ssh-two.gsrpi.uk`, SSH over `cloudflared access ssh`. The owner
reports no Cloudflare Access policy on the hostname; SSH key auth is the only gate.
The owner's goal is a **cloud → SSH → B+ pipeline with no Pi 5 in the loop**.

### Step 0 — connectivity (do this first; everything else depends on it)

1. **Generate a fresh ed25519 keypair** in your session (the previous session's
   temporary key died with its container) and give the owner the PUBLIC key to append
   to `authorized_keys` on two (for `gavin`, and/or the `dd` user if root-setup.sh
   created it). Never commit the private key.
2. Install cloudflared (GitHub releases, linux-amd64) and use an ssh_config
   `ProxyCommand cloudflared access ssh --hostname %h` for Host `ssh-two.gsrpi.uk`.
3. The session egress proxy (HTTPS_PROXY) allowlists domains. `*.gsrpi.uk` should now
   be allowed in this environment — if you get a bare `Forbidden` on CONNECT, check
   `curl -sS "$HTTPS_PROXY/__agentproxy/status"` (recentRelayFailures shows policy
   denials) before blaming Cloudflare. Also unknown: whether cloudflared's WebSocket
   dial honors HTTPS_PROXY — if it bypasses the proxy and times out, try its own
   proxy flags or wrap the CONNECT yourself.

### Step 1 — host prep on two (owner may have already run it)

`deploy/pi-bplus/root-setup.sh` must have been run as root + reboot (check:
`podman info` works as the unprivileged user, cgroups v2, overlay storage — see
runbook's verification checklist). If not run, the owner must run it; it embeds the
OLD session's pubkey, so have the owner override/append the new one.

### Step 2 — image build without a Pi 5 (two options)

- **In-cloud build (preferred for the no-Pi-5 goal)**: the cloud session VM has
  Docker + 16GB RAM. Enable binfmt/qemu for arm/v6 and build
  the repo-root `Dockerfile` with `--platform linux/arm/v6` and the three Pi build args
  (see the runbook's Step 2), then ship:
  `docker save | ssh ssh-two.gsrpi.uk podman load` (or gzip over scp if the pipe is
  fragile through the tunnel). Emulated builds are slow but only `greenlet` compiles.
- **On-device build**: clone the repo on two and `podman build` locally — feasible
  since the pure-Python change (expect ~30+ min on the 700MHz core).

### Step 3 — deploy + verify

Follow `docs/pi_bplus_setup.md` Step 3 onward: `/srv/dd/compose.yml`, `/srv/dd/.env`
(the owner must supply `DISCORD_TOKEN_BEACON`, `TEST_ENV`, `POSTGRES_PASSWORD` — never
ask them to paste secrets into chat if avoidable; have them write the file on two
directly, then you fill in the non-secret vars), `podman-compose up -d`, then the
runbook's verification checklist. Watch first-startup: alembic baseline apply, then
gateway connect. RAM budget table is in the runbook.

### Afterward

- Prod cutover (Railway Postgres + pgloader + env swap) is OWNER-DRIVEN — never
  deploy to prod without an explicit confirmed ask (CLAUDE.md rule).
- Delete this file when the deployment is verified; prompt the owner if partial.

## Owner's working preferences (carry these forward)

- Orchestrate with **Opus and Sonnet subagents extensively, run sequentially** —
  Opus for code-heavy/risky phases, Sonnet for docs/config/mechanical phases. The
  main (Fable) loop coordinates, reviews reports, and talks to the owner; it does
  not do bulk implementation itself (conserves Fable credits).
- Conventional commits; push to `origin/slim-stack`; no PR unless asked.
- The owner runs root/sudo steps themselves from reviewed scripts; agent SSH access
  is unprivileged only.
