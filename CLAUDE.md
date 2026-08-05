# Destiny Director — Project Rules

Two Discord bots (`hikari-lightbulb` v3) sharing one codebase under `dd/`:

- `dd.beacon` — main bot
- `dd.anchor` — secondary bot (larger than it sounds: web UI + Bungie API + CV2 posts)
- `dd.common` — shared config, DB schemas, bot classes, helpers
- `dd.hmessage`, `dd.sector_accounting` — shared domain code

Python 3.13, fully async (hikari / aiohttp / aiosqlite / psycopg), SQLAlchemy 2.0,
Alembic for migrations, deployed on Railway.

> **Two environments.** This repo is worked on both from the developer's **local box**
> (pyenv + Zed/VSCode) and by **Claude on the remote server** against this checkout.
> Rules below flag when something is local-box-only (e.g. editor config) vs. repo-wide
> (e.g. the committed ruff/ty config, which applies everywhere).

## Architecture map — read before writing code

Full detail lives in **`docs/architecture.md`**. **Before adding a command, touching the
DB layer, or building a message/embed, read it.** Quick orientation:

- **Commands live in per-bot `extensions/` subpackages** (`dd/beacon/extensions/`,
  `dd/anchor/extensions/`) — lightbulb **v3 class-based** style (`class X(lb.SlashCommand,
  …)` + `@lb.invoke`), *not* v2 decorators. Copy `dd/beacon/extensions/template.py`.
- **`dd.common` is large** — config (`cfg.py`), bot classes (`bot.py`), owner gating
  (`auth.py`), Components V2 (`components.py`), logging, lifecycle, DB (`schemas.py`).
- **`dd.anchor` is not just a "secondary bot"** — aiohttp web UI (`web.py`), Bungie
  OAuth/API client (`extensions/bungie_api/`), Components V2 post rendering
  (`cv2_*.py` — "CV2" is Discord Components V2, **not** OpenCV).
- **Reuse, don't reinvent:** `db_session()` / `@ensure_session` for DB; `HMessage`
  (`dd.hmessage`) for messages; `dd/beacon/nav.py` for paged messages; `cfg.py` for env.
- **Implicit namespace packages** — there is intentionally no `dd/__init__.py`,
  `dd/common/__init__.py`, or `dd/anchor/__init__.py`. Don't "fix" this by adding them.

## Package management — use uv

- Use **uv** only. Never use pip, pip-tools, poetry, or conda.
- Add runtime deps with `uv add <package>`; dev deps with `uv add --dev <package>`.
- `uv.lock` is committed — keep it in sync, never hand-edit `pyproject.toml`
  dependency lists.
- Dependency groups: `dev` (rope, pytest, pytest-asyncio, **ruff, ty, pre-commit,
  pytest-cov**) and `speedups`. Both are in `tool.uv.default-groups`, so `uv run ruff` /
  `uv run ty` work out of the box.
- Do not create virtualenvs by hand or install packages globally.
- The `Makefile`'s Python targets (`run-*-local`, `*-schemas`, `test`, `lint`, `format`,
  `typecheck`, `migration-*`) all use `uv run`. The `railway`/`dev-*` targets shell out
  to non-Python CLIs (Railway CLI, `docker compose`) as-is.

## Running & deploying

- Prefix execution with `uv run` — e.g. `uv run ruff check dd`. Don't invoke
  `python`/`pytest`/`ruff` bare.
- Run a bot locally: `uv run python -OOm dd.beacon` (or `dd.anchor`). Requires a
  populated `.env` (copy `.env-example`; **nearly all** vars are required — a few are
  optional, see its inline comments). `dd/common/cfg.py` validates required env **at
  import time**, so a missing var fails fast with `ValueError`.
- Deploy via `make deploy-beacon-dev` / `deploy-anchor-prod` etc. (Railway) or via
  Railway's plugin.
- **Never call the Railway connector's agent** (`mcp__Railway__railway-agent`). Use the
  direct tools only — `get-status`, `list-deployments`, `get-logs`, `get-service-config`,
  `list-variables`, `redeploy`, `update-service`. The agent takes its own actions to
  reach a goal, and they are not always the ones asked for: told to *remove* a running
  deployment it *restarted* the service instead, bouncing a live bot nobody asked to
  bounce. An agent that improvises is the wrong tool against infrastructure.
- Consequence, so it isn't rediscovered: **deploying a specific commit is then not
  reachable from here.** `redeploy` rebuilds the *previous snapshot*, not the branch tip
  — it reported SUCCESS while running day-old code, and only a missing extension in the
  logs gave it away. Pushing to the watched branch does not reliably auto-trigger either.
  So deploys are the owner's to drive; the agent's commit-SHA deploy is not a substitute.
  Always verify **which commit** a deployment built (`list-deployments` reports
  `commitHash`) rather than trusting its status.
- **Never deploy to prod on your own initiative.** The ONLY condition under which you may
  deploy to prod is when you have **explicitly asked the user whether to deploy and they
  confirmed** in that exchange. Pushing to `shark/main` triggers a Railway auto-deploy to
  prod, so that push counts as a prod deploy and requires the same explicit confirmation.

## Testing

- **pytest** with **pytest-asyncio** (`asyncio_mode = "strict"`).
- Tests live **inside each package** as `tests/` subdirs, e.g. `dd/beacon/tests/test_*.py`
  — not a single root `tests/` dir. Follow that convention.
- **Run via `make test`** (= `uv run --env-file .env python -m pytest -m "not discord"`),
  not bare `uv run python -m pytest` — the latter skips `.env`, so `cfg.py`'s import-time
  validation raises a cryptic `ValueError: Environment variable '…' not found.`
- Three markers: `integration` (DB layer; SQLite by default, `TEST_USE_POSTGRES=1` for
  Postgres), `discord` (hits live Discord, needs a real token), and `browser` (drives a real
  Chromium via Playwright). The safe default suite is `-m "not discord"`. Also:
  `make test-unit`, `make test-integration`, `make test-browser`, `make coverage`.
- **Browser tests** cover the CV2 builder's drag layer against the no-server fixture
  `dd/anchor/web_static/tests/builder_harness.html`. Playwright is a dev dep, but the
  browser is a separate ~150MB download `uv sync` does not fetch, so they **skip** until
  you run `uv run playwright install chromium` (CI installs it, so they run there every
  push). Assert behaviour only — never appearance, or restyling breaks them.
- **JS unit tests** (`make test-js`, `node --test`, no bundler) live in
  `dd/*/web_static/tests/`. `make check` runs them alongside the Python suite.

## Linting, formatting & type checking

- **ruff** does linting + formatting; **ty** is the type checker. Config for both is
  **committed** (`[tool.ruff]` in `pyproject.toml`, plus `ty.toml`) — so it applies
  everywhere (`uv run`, CI, pre-commit), not just in an editor.
- ruff: line length **88**, double quotes; isort `combine-as-imports` and
  `force-wrap-aliases` on; lint rule set `E`, `F`, `W`, `I`, `UP`, `B`, `SIM`
  (pycodestyle, pyflakes, isort, pyupgrade, bugbear, simplify).
- Run `make lint` (`ruff check dd`), `make format` (`ruff format` + `ruff check --fix`),
  `make typecheck` (`ty check dd`). `make check` = lint + typecheck + test (the local
  mirror of CI).
- ruff removes **unused imports** (F401 fails CI, and the developer's editor strips them
  on save). When you add an import, add its usage in the **same edit**.
- ty: prefer fixing types over suppressing. When ty genuinely can't model a pattern
  (attrs-generated `__init__`, the SQLAlchemy Core/ORM query layer, dict subclasses),
  suppress it in **`ty.toml` overrides with an explanatory comment** — see the existing
  per-file blocks. Avoid bare
  inline `# type: ignore`; if you must, include the error code.

## CI

- `.github/workflows/ci.yml` runs on every push/PR: `uv run ruff check dd` →
  `uv run ty check dd` → `pytest -m "not discord" --cov` (SQLite, dummy env vars). Run
  `make check` locally to catch failures before pushing.

## Git & workflow

- **Commit messages: conventional commits** — `type(scope): summary`. Types: `feat`,
  `fix`, `refactor`, `chore`, `docs`. Common scopes: `anchor`, `beacon`, `dev`,
  `user_commands`, `deploy`, `rotation`.
- **Branches/remotes:** `dev` is the integration branch — feature/worktree branches merge
  there first. Two remotes: `origin` (gsfernandes81) and `shark` (geolocatingshark).
  Pushing to `shark/main` is a **prod deploy** — see the confirmation rule above.
- **Name worktrees & branches descriptively.** A worktree branch must describe the
  work (`mirror-auto-disable-gate`, `fix/bungie-oauth-refresh`), not carry an opaque
  harness hash (`worktree-bridge-cse_01DZBPw5r3bqtJbbXQE4WgRQ`). If you land on a
  hash-name branch, rename it before your first commit (`git branch -m <name>`) using
  the conventional-commit scopes (`anchor`, `beacon`, `rotation`, …). Keep the name
  as-is only if the worktree came from a pre-existing, already-descriptive branch.
- **plans/:** Stores deferred plans. When a plan is executed completely, ALWAYS remove
  it from this directory. Prompt the user in case the plan was partially executed.

## Database & migrations

- SQLAlchemy schemas in `dd/common/schemas.py` (it doubles as the DDL source and a
  management CLI). Migrations via **Alembic** (`alembic.ini`, `migrations/` at repo
  root): `make migration-plan MSG="..."` autogenerates a revision from the live
  models-vs-database diff (hand-check it — autogenerate is a starting point, not an
  oracle, e.g. it sees a rename as drop+create), `make migration-apply` to apply,
  `make migration-dry-run` to print the SQL without running it, `make migration-check`
  to fail if the models have drifted from the migrations. `make create-schemas` /
  `destroy-schemas` manage tables directly.
- **`migration-plan`/`migration-apply` need no separate dev/scratch database** — unlike
  Atlas, Alembic autogenerates by diffing the models against the target database
  directly (`DATABASE_PRIVATE_URL`/`DATABASE_URL`, read via `dd.common.cfg` in
  `migrations/env.py`), so the same `.env`-driven connection works identically on the
  local box and inside the dev container.
- **`make destroy-schemas` / `--destroy-all` refuse a non-local DB** unless
  `ALLOW_REMOTE_SCHEMA_DESTROY=1` (`schemas.py`). Never bypass it — there was a real
  dev-DB-wipe incident (`plans/dev_db_auto_wipe_investigation.md`).

## Dev environment (local box only)

- The editor is **Zed** on the developer's box (LSPs: `ty` + `ruff`; `pyright`/`pylsp`
  disabled). Zed's `settings.json` and any `.vscode/` config are **local-box only** — they
  aren't part of the server checkout, so a server-side agent can ignore them. The ruff/ty
  *rules* themselves are committed (see above) and apply everywhere.
- On the pyenv box `.python-version` pins 3.13; the repo-authoritative pin is
  `requires-python = "~=3.13.0"` in `pyproject.toml`.
- The dev **container** is managed with `make dev-up` / `dev-down` (not bare
  `docker compose up`) so its uid/gid matches the host bind mount — otherwise writes to
  `/workspace` fail on non-1000-uid hosts (e.g. the Pi).

## Project layout & config

- All metadata in `pyproject.toml` (PEP 621); build backend is **hatchling**. No
  `setup.py`, `setup.cfg`, or `requirements.txt`.

## Conventions

- Async/await throughout — don't introduce blocking I/O in coroutine paths.
- Keep new code matching the surrounding style (naming, comment density, idioms).
- Secrets live in `.env` (gitignored) — never commit them.
