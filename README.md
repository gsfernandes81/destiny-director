# Destiny Director

Setting up the dev environment:

1. Install [uv](https://docs.astral.sh/uv/) (the only supported package manager).
2. Run `uv sync` in the root of the git clone to create the virtualenv and install
   dependencies (Python 3.13, pinned in `.python-version`).
3. [Optional] Set up `.env` with the environment variables referenced in
   `dd/common/cfg.py` & `.env-example`.
4. [Optional] Run `uv run pre-commit install` to enable the lint/format/type-check
   hooks (`.pre-commit-config.yaml`) on each commit.

Quality gates (lint, type-check, tests) run via the `Makefile`:

```
make lint        # ruff check
make format      # ruff format + ruff check --fix
make typecheck   # ty check
make check       # lint + typecheck + test (the full gate)
```

Running a bot locally:

```
make run-beacon-local   # main bot
make run-anchor-local   # secondary bot
```

(Both run `uv run python -OOm dd.<bot>` and require a populated `.env`.)

Running tests locally:

```
make test
```

Running code locally with docker:

```
docker build -t dd .
docker run --env-file=.env -e RAILWAY_SERVICE_NAME=anchor dd
```

One `Dockerfile` builds the image for both deployment targets (Railway and the
Raspberry Pi B+); its defaults are the Railway/amd64 ones, and three build args switch
it to ARMv6 — see the file's header, or
[`docs/pi_bplus_image.md`](docs/pi_bplus_image.md). Inside the container PID 1 is
supervisord (`supervisord.conf`), which picks the bot from `RAILWAY_SERVICE_NAME` and
also runs a disarmed-by-default sshd for getting a shell into a container whose bot has
died (`sshd_config`).

Developing on a remote Raspberry Pi 5 in a Docker dev container (terminal-only, over the
Pi host's SSH — `make dev-claude` for a claude in an `abduco` session, `make dev-shell`
for a fish shell): see [`docs/pi_dev_setup.md`](docs/pi_dev_setup.md).

Deploying code to [railway](https://railway.app/):

Make sure you have the [railway cli](https://docs.railway.app/develop/cli) installed and
are logged in. Use these to deploy to the dev instance on railway:

```
make deploy-beacon-dev
make deploy-anchor-dev
```

**CAUTION** use these to deploy to the production instance on railway:

```
make deploy-beacon-prod
make deploy-anchor-prod
```
