# dd-dev: the dev container on the Pi

Destiny Director is developed inside `dd-dev`, a long-lived container on `zero` (a
Raspberry Pi 5, `linux/arm64`). **Since 2026-10-27 the container is not described in this
repo**: it is one service in the infra repo's `dev/compose.yaml`, beside the other dev
containers and the hub that is the way in to all of them. Bring-up, lifecycle, getting in,
GitHub access, the Claude login and the secrets layout are infra's, documented once in
[`dev/README.md`](https://github.com/gsfernandes81/infra/blob/main/dev/README.md) in
gsfernandes81/infra — read it rather than a copy here. This page is what is dd-specific.

## What this repo still owns

| File | What it is |
|---|---|
| `Dockerfile.dev` | the image: a thin child of infra's `gsrpi-dev-base` adding the database clients, the Railway CLI and this project's venv. Infra builds it from this repo's `main` on GitHub and passes `BASE_TAG`, so the default in the file is only a fallback |
| `docker-child-init.dev.sh` | run by the base at every start: `uv sync --frozen` adds the editable project to the pre-built venv. Non-fatal — a failed sync warns and the container still comes up |
| `docker-login.dev.sh` | the login walkthrough, `/home/dev/login.sh` in the image — what `make login C=dd-dev` runs |
| `ssh_config.dev` | the baked defaults half of `~/.ssh/config` |
| `docker-run-devbot.sh` | the spin-up-to-test bot runner, below |

A change to any of them reaches the container when infra rebuilds it from `main`, so it
ships only once it is merged there.

## The layout, as it touches this repo

- **`/workspace` is a volume with its own clone of this repo**, over https — not a bind
  mount of a host clone. Push to share work; nothing on the host sees it.
- **`.env` lives in an env volume and is linked to `/workspace/.env`.** It is *not* the
  container's environment: dd reads it itself (`dd.common.cfg`'s `load_dotenv`, `uv run
  --env-file .env` in the Makefile). Infra's README has how the file gets there the first
  time (`docker cp` into `~/.env.d`, then `make restart C=dd-dev`). Beyond the app's own
  variables (`.env-example`) it holds:

  ```sh
  DATABASE_URL=postgresql://kyber:kyber@postgres:5432/kyber   # the dd-postgres sibling
  DATABASE_SSL=false                                          # it has no TLS
  RAILWAY_API_TOKEN=<railway account token>                   # see Railway CLI
  ```

- **GitHub is the hub's per-repo token**, for this repo only: git and `gh` use it with no
  login and no ssh key. `gh auth status` reports failure on it while every repo command
  works; `make login` checks the token instead. PRs made from the container appear as the
  App, commits as you.
- **Other ssh identities are read-only**, from `~/.dd-dev-secrets` on zero (mounted at
  `/run/dd-secrets`): the base copies each private key into `~/.ssh` and prepends that
  directory's `ssh_config.fleet` to `~/.ssh/config` at every start. The one this repo has
  a use for is the `shark` remote (geolocatingshark), which the token cannot reach.
- **The way in is `ssh dev`**, then dd-dev in the menu. Zed, scp and port-forwards to the
  container have no route at present.
- **Memory**: infra caps dd-dev (`mem_limit`), which is what the devbot targets check for.

## First run inside the container

```sh
make login C=dd-dev          # on zero, in ~/infra/dev: token, workspace, .env, Railway, Claude
```

then, in the container:

```sh
git -C /workspace remote -v              # origin, https — the fresh clone's only remote
uv sync --frozen                         # what child-init does; a no-op when current
make test                                # the unit + SQLite suite (needs .env)
make lint && make typecheck
```

The `shark` remote, only if an identity for it is in `~/.dd-dev-secrets` — a
`github.com-shark` Host block naming its key in `ssh_config.fleet`:

```sh
git remote add shark git@github.com-shark:geolocatingshark/destiny-director.git
```

> **Pushing to `shark/main` is a prod deploy** (CLAUDE.md). Never on your own initiative.

## Railway CLI

Baked into the image from the release tarball (the `@railway/cli` npm package 404s on
arm64), pinned by `RAILWAY_VERSION` in `Dockerfile.dev`. **The Makefile's railway targets
run it bare, and `.env` is not exported into the container**, so a `RAILWAY_API_TOKEN` in
`.env` does not reach them. Log in once instead — `railway login --browserless`, persisted
in the `dd-railway` volume (`make login C=dd-dev` offers it) — and check with
`railway whoami`. Then `make deploy-beacon` / `make deploy-anchor` deploy to Railway's dev
environment.

> **Prod deploys require explicit confirmation each time (see CLAUDE.md). Never deploy
> prod on your own initiative.**

## Postgres / migrations

`dd-postgres` (`postgres:17-alpine`, user/password/database `kyber`) starts with dd-dev and
answers as `postgres` inside it — the host the `DATABASE_URL` above names.

```sh
pg_isready -h postgres                                                   # is it up
make migration-apply                                                     # apply against DATABASE_URL
TEST_USE_POSTGRES=1 uv run --env-file .env python -m pytest -m integration  # DB suite on Postgres
```

Authoring a migration (`make migration-plan MSG="..."`) autogenerates a revision by diffing
`dd/common/schemas.py`'s models straight against that same database — no throwaway "dev"
database is needed. Always hand-check the generated revision; autogenerate is a starting
point, not an oracle.

## Dev bots (spin-up-to-test)

`make run-beacon-devbot` / `run-anchor-devbot` (foreground) and `make devbot-up` /
`devbot-down` / `devbot-status` / `devbot-logs` (background) run a bot inside dd-dev via
`docker-run-devbot.sh`, under `uv run --env-file .env` so `.env` reaches it. Each runs
against its own Postgres database (`DEVBOT_SCHEMA`, default `kyber_devbot`) on the same
server, created on first launch and migrated each time, so it cannot clobber the shared
`kyber` database or the integration tests. Each starts with a raised OOM score
(`DEVBOT_OOM_SCORE_ADJ`, default 800) so the kernel reaps it before the Claude session,
and the targets refuse to start while dd-dev is uncapped (`memory.max` reads `max`).
jemalloc is preloaded as in the deployed image; `DEVBOT_JEMALLOC=0` forces glibc for an
A/B RAM comparison.

## Claude Code

In the base image and logged in through `make login C=dd-dev`; by default the login is
shared across the dev containers (infra's switch). If Claude Code's Bash sandbox blocks
writes to `~/.cache/uv` (breaks uv/ruff/ty/pytest), relax the sandbox in-container — the
container is already an isolation boundary.
