# ── Which Railway environment ──────────────────────────────────────────────────────
# Every railway target below reads TARGET_ENV. It defaults to dev; the word `prod`
# anywhere on the command line switches it to production:
#
#     make deploy-anchor          # dev
#     make prod deploy-anchor     # production
#     make prod cutover-copy      # production
#
# Prod is never the default and never implicit — you type the word, on the line, every
# time. That is deliberate: pushing to prod is the one action in this file that needs a
# human to have meant it, and a flag you can forget is not the same as a word you type.
#
# **TARGET_ENV, not RAILWAY_ENV** — do not rename it back. mysql-to-postgres drives its
# steps as sub-makes, passing the environment as a command-line assignment, and make
# exports command-line variables into the environment of every recipe it runs. So the
# `railway` process inherited a variable named RAILWAY_ENV that we invented, sitting in
# the namespace the CLI reads its own configuration from. `make dump-mysql` worked and
# `make mysql-to-postgres` failed with "Unauthorized" on the identical command line —
# Railway's API answers Not Authorized for an identifier it cannot resolve rather than
# Not Found, so it does not read as a configuration error at all. The variable is ours;
# it has no business wearing another tool's prefix.
TARGET_ENV ?= dev
ifneq (,$(filter prod,$(MAKECMDGOALS)))
  TARGET_ENV := production
endif

# A no-op goal, so `prod` can sit on the command line purely to be seen by the filter
# above. Silent (@:) because it does nothing and should not look like it did.
prod:
	@:

# Every railway invocation passes -e/-s explicitly rather than running `railway
# environment X` / `railway service Y` first. Those two mutate the CLI's *persistent*
# link, so a later bare `railway ...` in the same checkout would silently inherit
# whatever the last make target selected — including production. Flags do not leak.
# The flag goes AFTER the subcommand — `--environment` is a per-subcommand option, not
# a global one, and `railway --environment production up` is an arg-parse error.
TARGET_ENV_FLAG := --environment $(TARGET_ENV)

# The service whose variables the cutover targets borrow. anchor because it is the one
# with both the old MYSQL_URL and the new DATABASE_URL in scope.
RAILWAY_SERVICE ?= anchor

# `railway run` executes LOCALLY with the service's variables injected, so the database
# it reaches has to be reachable from here — i.e. the public TCP proxy in DATABASE_URL,
# not the platform-internal host in DATABASE_PRIVATE_URL. cfg.py prefers the private one
# (cfg.py:328), which does not resolve off-platform, so it is removed for these runs.
#
# `env -u`, NOT `env VAR=`: cfg falls back to DATABASE_URL only when the private var is
# *absent*. Set to the empty string it is still present, so `_getenv` returns "" without
# raising, the fallback never fires, and cfg lands on its Library-Mode placeholder —
# yielding a silently unusable "postgresql+psycopg://" rather than an error. Verified.
RAILWAY_RUN = railway run $(TARGET_ENV_FLAG) --service $(RAILWAY_SERVICE) -- \
	env -u DATABASE_PRIVATE_URL -u MYSQL_PRIVATE_URL

# ── Deploys ────────────────────────────────────────────────────────────────────────
# One recipe each for what used to be four near-identical targets, over one service
# list. These are **static** pattern rules (`targets: pattern: prereqs`), not plain
# pattern rules, for two reasons that turn out to be the same reason:
#
#   - A plain `deploy-%:` is an *implicit* rule, and make skips implicit-rule search
#     for a target it knows is phony. So `.PHONY: deploy-anchor` over a plain pattern
#     rule does not harden the target — it stops it working at all. A static pattern
#     rule is an explicit rule, so .PHONY and the recipe coexist.
#   - Naming the targets up front means a typo has no rule rather than matching one:
#     `make deploy-ancor` now says "No rule to make target", with no guard to write.
BOTS := beacon anchor

$(addprefix deploy-,$(BOTS)): deploy-%:
	railway up $(TARGET_ENV_FLAG) --detach --service $*

# Removes the most recent deployment of a service. Explicit about both service and
# environment — it used to be a bare `railway down`, which acted on whatever the CLI
# happened to be linked to at the time.
$(addprefix remove-last-deploy-,$(BOTS)): remove-last-deploy-%:
	railway down $(TARGET_ENV_FLAG) --service $*

# Remote Pi dev container (docker-compose.dev.yml). dev-up builds the image with
# the uid/gid that OWN this clone so the bind-mounted /workspace stays writable,
# then starts it detached. We read the owner with `stat`, NOT `id -u`: when docker
# is run via sudo/root, `id -u` is 0 and the build then collides with the root
# account (`groupadd: GID '0' already exists`). The clone owner is the right uid
# whoever launches the build. dev-down stops it; dev-down-volumes also drops the
# named volumes (uv cache, claude/railway/gh config, postgres data) — use when the baked
# uid changed and the volumes must be recreated under the new owner. DEV_HOSTNAME
# sets the container's hostname to the docker host's name + `-dd-dev`, so Claude
# Code shows a stable, meaningful machine title instead of the random container ID
# (the suffix distinguishes the container from the host itself).
dev-up:
	HOST_UID=$$(stat -c '%u' .) HOST_GID=$$(stat -c '%g' .) DEV_HOSTNAME=$$(hostname)-dd-dev docker compose -f docker-compose.dev.yml up -d --build

# One command to stand the whole thing up: build + start the container, wait for it
# to be running, then walk through any logins that aren't done yet (git SSH, GitHub,
# Railway, Claude) interactively. Every login step is idempotent — already-signed-in
# services are skipped — so this is safe to re-run. Once Claude is logged in the
# entrypoint's background supervisor brings up `claude remote-control --spawn worktree`
# on its own (~10s), so there's nothing to exec by hand.
dev: dev-up
	@echo "Waiting for dd-dev to come up (up to 120s)..."
	@for i in $$(seq 1 120); do \
		docker exec dd-dev true 2>/dev/null && break; \
		[ $$i = 120 ] && { echo "ERROR: dd-dev did not become exec-able within 120s — check 'docker compose -f docker-compose.dev.yml logs dev'." >&2; exit 1; }; \
		sleep 1; \
	done
	@$(MAKE) dev-login

# Re-run the interactive login walkthrough against an already-running container.
dev-login:
	docker exec -it dd-dev bash /home/dev/login.sh

dev-down:
	docker compose -f docker-compose.dev.yml down

dev-down-volumes:
	docker compose -f docker-compose.dev.yml down -v

run-beacon-local: .env
	uv run python -OOm dd.beacon

run-anchor-local: .env
	uv run python -OOm dd.anchor

# --- Spin-up-to-test DEV bots (docker-run-devbot.sh) -------------------------------
# Unlike run-*-local (raw, uncapped, shared `kyber` DB), these run against an isolated
# `kyber_devbot` schema and are biased to be the first OOM victim under the dd-dev memory
# cap — so a throwaway test bot can't clobber the shared DB or OOM-kill a neighbour.
# `.devbot-logs/<bot>.{log,pid}` hold the background logs + process-group ids.

# Guard: refuse to launch unless dd-dev is memory-capped, so an OOM can only ever hit this
# container's cgroup (never a neighbour). memory.max=="max" means uncapped — apply the cap
# from the Pi HOST by recreating the container so the compose mem_limit takes effect.
_require-mem-cap:
	@[ "$$(cat /sys/fs/cgroup/memory.max 2>/dev/null)" != "max" ] || { \
		echo "REFUSING: dd-dev is not memory-capped (cgroup memory.max=max)." >&2; \
		echo "A runaway devbot could OOM-kill a neighbour container. Cap it on the Pi HOST first:" >&2; \
		echo "    make dev-up     # recreate dd-dev with the docker-compose.dev.yml mem_limit" >&2; \
		exit 1; }

# Foreground, single bot (focused debugging).
run-beacon-devbot: .env _require-mem-cap
	./docker-run-devbot.sh beacon

run-anchor-devbot: .env _require-mem-cap
	./docker-run-devbot.sh anchor

# Background lifecycle for both bots. setsid detaches each into its own session so it
# survives this shell; $$! is the session leader == process-group id, recorded so
# devbot-down can signal the whole group (uv run's child python included).
devbot-up: .env _require-mem-cap
	@mkdir -p .devbot-logs
	@for b in beacon anchor; do \
		if [ -f .devbot-logs/$$b.pid ] && kill -0 -"$$(cat .devbot-logs/$$b.pid)" 2>/dev/null; then \
			echo "$$b devbot already running (pgid $$(cat .devbot-logs/$$b.pid))"; \
		else \
			setsid ./docker-run-devbot.sh $$b >.devbot-logs/$$b.log 2>&1 & \
			echo $$! >.devbot-logs/$$b.pid; \
			echo "started $$b devbot (pgid $$!) -> .devbot-logs/$$b.log"; \
		fi; \
	done

devbot-down:
	@for b in beacon anchor; do \
		if [ -f .devbot-logs/$$b.pid ]; then \
			pgid=$$(cat .devbot-logs/$$b.pid); \
			kill -TERM -"$$pgid" 2>/dev/null && echo "stopped $$b devbot (pgid $$pgid)" \
				|| echo "$$b devbot not running (pgid $$pgid)"; \
			rm -f .devbot-logs/$$b.pid; \
		else echo "$$b devbot: no pidfile"; fi; \
	done

devbot-logs:
	tail -n 50 -f .devbot-logs/beacon.log .devbot-logs/anchor.log

devbot-status:
	@for b in beacon anchor; do \
		if [ -f .devbot-logs/$$b.pid ] && kill -0 -"$$(cat .devbot-logs/$$b.pid)" 2>/dev/null; then \
			pgid=$$(cat .devbot-logs/$$b.pid); \
			mb=$$(ps -eo pgid=,rss= | awk -v g=$$pgid '$$1==g{s+=$$2} END{printf "%d", s/1024}'); \
			echo "$$b: UP   (pgid $$pgid, ~$${mb}MB rss, schema $${DEVBOT_SCHEMA:-kyber_devbot})"; \
		else echo "$$b: down"; fi; \
	done

destroy-schemas: .env
	uv run python -m dd.common.schemas --destroy-all

create-schemas: .env
	uv run python -m dd.common.schemas --create-all

# --- Alembic migrations (alembic.ini + migrations/) --------------------------------
# The URL is never passed on the command line: migrations/env.py reads it from
# dd.common.cfg, which needs the same populated .env as the bots — hence
# `uv run --env-file .env` on every target here. Autogenerate diffs the live database
# against dd.common.schemas.Base.metadata, so plan against a DB that IS at head.

# Write a new revision from the models-vs-database diff, then HAND-CHECK it: alembic's
# autogenerate is a starting point, not an oracle (it misses e.g. table/column renames,
# seeing them as drop+create). Message: `make migration-plan MSG="add foo table"`.
MSG ?= $(M)
migration-plan: .env
	@[ -n "$(MSG)" ] || { echo 'Set a message: make migration-plan MSG="add foo table"' >&2; exit 1; }
	uv run --env-file .env alembic revision --autogenerate -m "$(MSG)"

migration-apply: .env
	uv run --env-file .env alembic upgrade head

# Offline mode: print the SQL that `migration-apply` would run instead of running it.
migration-dry-run: .env
	uv run --env-file .env alembic upgrade head --sql

# Fails if the models have drifted from the migrations (i.e. an autogenerate here
# would produce a non-empty revision). Run after editing schemas.py.
migration-check: .env
	uv run --env-file .env alembic check

# ── Backups ────────────────────────────────────────────────────────────────────────
# Back up a database to a timestamped ./kyber-<env>-<UTC>.sql, pulling the connection
# URL from the given Railway environment. Both run locally, so they need pg_dump /
# mysqldump installed here and the service reachable over its public TCP proxy.
#
# dump-db goes through $(RAILWAY_RUN) — i.e. the *bot* service — rather than naming the
# database service, because the Postgres services are not named the same in both
# environments: production's is `Postgres`, dev's is `Postgres-5iZg`. A hardcoded
# `--service Postgres` therefore worked on production and could never work on dev, which
# is the worst possible split for a target whose whole job is to be exercised on dev
# before it is trusted on production.
#
# Going through anchor removes that coupling: its DATABASE_URL is a
# ${{Postgres.DATABASE_PUBLIC_URL}} reference, which resolves to whichever service each
# environment has. It is not a new assumption either — cutover-schema already reads
# DATABASE_URL this way, and cutover-copy $$MYSQL_URL.
#
# dump-mysql does the opposite, and the note above it says why: the naming problem does
# not exist for `MySQL`, and reading the database service directly is immune to a stale
# copy on a bot. Symmetry between the two is not worth either failure.
dump-db:
	$(RAILWAY_RUN) bash -c 'pg_dump --no-owner --no-privileges "$$DATABASE_URL" > "kyber-$(TARGET_ENV)-$$(date -u +%Y%m%dT%H%M%SZ).sql"'

# The same, for the OLD MySQL database, until it is retired. `dump-db` above cannot
# stand in for this: it dumps DATABASE_URL, so before the cutover there is nothing to
# back up the database you are migrating *away from* — which is the one backup that
# matters, since mirrored_channel cannot be reconstructed from anything.
#
# Driven off the MYSQL_URL that `railway run` injects, exactly as the pg target uses
# DATABASE_URL. The one way these have to differ: mysqldump takes flags rather than
# a URL, so MYSQL_DUMP below splits it. sed's scripts are double-quoted so the recipe's
# own single quotes survive, and none of them contain a `$` to be expanded early.
#
# The password reaches mysqldump through MYSQL_PWD rather than --password, so it stays
# out of the local process list. A percent-encoded password would arrive undecoded, but
# that surfaces as an auth error at connect time, not as a bad dump.
# --single-transaction gives a consistent InnoDB snapshot without stopping a running
# bot; --no-tablespaces avoids needing the PROCESS privilege a managed user usually
# lacks.
MYSQL_DUMP = n=$$(echo "$$MYSQL_URL" | sed -e "s|^[a-z]*://||"); hp=$$(echo "$$n" | sed -e "s|^.*@||" -e "s|/.*||"); MYSQL_PWD=$$(echo "$$n" | sed -e "s|^[^:]*:||" -e "s|@.*||") mysqldump --host "$$(echo "$$hp" | cut -d: -f1)" --port "$$(echo "$$hp" | cut -d: -f2)" --user "$$(echo "$$n" | cut -d: -f1)" --single-transaction --quick --no-tablespaces "$$(echo "$$n" | sed -e "s|^.*/||" -e "s|?.*||")"

# Unlike dump-db, this runs under the **MySQL service**, not under anchor. The two are
# not inconsistent, they are each using the source that is correct for them:
#
#   - `MySQL` is the service's name in BOTH environments, so there is no name to keep in
#     sync — the reason dump-db cannot do this (production's is `Postgres`, dev's is
#     `Postgres-5iZg`).
#   - The database service's own MYSQL_URL is generated by Railway and is always current.
#     A bot's copy is only as good as its definition: a ${{MySQL.MYSQL_URL}} reference
#     re-resolves, but a flattened literal pins a host:port that Railway reassigns on
#     every redeploy of the database. dev's MySQL was redeployed six times in twelve
#     days, and a stale literal there surfaces as mysqldump error 2013, "Lost connection
#     at reading initial communication packet" — a connection error for what is really a
#     configuration one.
#
# anchor's DATABASE_URL is a ${{Postgres.DATABASE_PUBLIC_URL}} reference, so dump-db is
# not exposed to that; it re-resolves on every read.
dump-mysql:
	railway run $(TARGET_ENV_FLAG) --service MySQL -- \
		bash -c '$(MYSQL_DUMP) > "kyber-$(TARGET_ENV)-mysql-$$(date -u +%Y%m%dT%H%M%SZ).sql"'

# ── Environment variables: snapshot and restore ────────────────────────────────────
# The cutover ends by deleting a dozen legacy variables, and Railway keeps no history.
# SHEETS_PRIVATE_KEY is a Google service-account key you would not get back; the Discord
# and Bungie secrets are no better. Take the snapshot before you delete anything.
#
# The file lands in the repo root as kyber-<env>-vars-<UTC>.json — a different name per
# environment, so a prod snapshot and a dev one cannot be confused for each other — and
# `kyber-*-vars-*.json` is gitignored alongside the .sql dumps. It holds live secrets in
# plaintext and is written 0600; treat it exactly like a database dump.
dump-vars:
	uv run python -m dd.common.railway_vars dump --environment $(TARGET_ENV)

# Put a snapshot back: `make restore-vars FILE=kyber-production-vars-….json`.
# Dry run by default, EXECUTE=1 to write, and it writes with --skip-deploys — a restore
# is a config repair, and redeploying is a separate decision.
#
# The snapshot records which environment it came from and the restore targets that one,
# so `make prod restore-vars FILE=<a dev snapshot>` refuses rather than quietly seeding
# production with dev's tokens. RAILWAY_*, DATABASE_* and MYSQL_* are never written back
# (platform-injected, or reference variables Railway hands us already flattened — see
# the module docstring).
restore-vars:
	@[ -n "$(FILE)" ] || { echo 'Set FILE: make restore-vars FILE=kyber-dev-vars-….json' >&2; exit 1; }
	uv run python -m dd.common.railway_vars restore --file "$(FILE)" \
		--environment $(TARGET_ENV) $(WRITE)

# Restore from a paste of Railway's web Raw Editor instead of a dump:
#   make prod restore-raw FILE=anchor-raw.txt SERVICE=anchor [EXECUTE=1]
#
# Worth preferring where it is available, because the Raw Editor is the *authoring*
# surface and so shows references unresolved. `railway variables` renders them, so a
# dump records ${{shared.SHEETS_PRIVATE_KEY}} as a flattened copy — and a large share of
# this project's variables are ${{shared.*}}, defined once at the environment level and
# referenced by both bots. The CLI cannot see that scope at all; it is per-service. Only
# this path puts a reference back as a reference.
# FILE is optional: with none, it prompts and reads the paste from the terminal, which
# is the natural shape when you are copying out of a browser — a file would exist only
# to be deleted afterwards. FILE=path still works for a saved capture.
restore-raw:
	@[ -n "$(SERVICE)" ] || { echo 'Set SERVICE: the raw editor is per-service' >&2; exit 1; }
	uv run python -m dd.common.railway_vars restore-raw --file "$(if $(FILE),$(FILE),-)" \
		--service "$(SERVICE)" --environment $(TARGET_ENV) $(WRITE)

# Check dev and production are shaped the same, so a rehearsal on dev predicts
# production rather than merely resembling it. Reads only — nothing is written, and it
# needs no .env, no database and no `railway run`; `railway variables --json` per
# service is the whole input.
#
#     make vars-check
#
# Two axes. Across environments (dev/anchor vs production/anchor, and the same for
# beacon): a name on one side and not the other, or a value that differs where the name
# is not environment-scoped. Credentials, guild ids, channel ids and the database URLs
# are allowed to differ — flagging those would be noise. DATABASE_SSL,
# RUN_MIGRATIONS_ON_STARTUP and OOM_SCORE_ADJ are deliberately not exempt: those
# differing is exactly what makes a dev rehearsal stop predicting prod.
#
# Across services (anchor vs beacon within one environment): no value may differ at all
# — the two bots share a database, a guild and a set of channels — and a name on one bot
# only is a finding unless it is one of the handful that belongs to a single bot (the
# gateway tokens, and anchor's web-UI settings). This axis is here because it is not
# hypothetical: DEFAULT_URL and DISABLE_BAD_CHANNELS are set on beacon and not on
# anchor, and `cutover-settings` runs under anchor.
#
# Exit 1 on any difference, so it works as a gate. Names only — values are secrets and
# are never printed. It cannot see a ${{shared.X}} that was flattened to a literal on
# one side: `railway variables` renders references, and only the web Raw Editor does
# not (the same limitation `restore-raw` exists for).
#
# ENVS/SERVICES override the pairs; `prod` has no meaning here, since the target spans
# both environments by definition.
ENVS ?= dev,production
SERVICES ?= anchor,beacon

vars-check:
	uv run python -m dd.common.railway_vars compare \
		--environments $(ENVS) --services $(SERVICES)

# ── Cutover: MySQL → Postgres, env → database ──────────────────────────────────────
# The runbook's window, one target per step. Every one runs under `railway run`, so
# none of them needs a local .env, a hand-pasted URL, or a variable exported into your
# shell — the deployment's own variables are the only source, which is also what stops
# you pointing a step at the wrong environment by accident. Say `prod` to aim them at
# production; without it they act on dev, where you should rehearse first.
#
# They are ordered, and each is safe to re-run:
#
#     make prod cutover-backup     # 1. dump the old MySQL
#     make prod cutover-schema     # 2. create the Postgres schema (alembic)
#     make prod cutover-copy       # 3. copy the rows  (dry run; EXECUTE=1 to write)
#     make prod cutover-settings   # 4. load the env vars into the settings rows
#     make prod cutover-verify     # 5. read back what landed
#
# or `make prod mysql-to-postgres` for all of them in order.

# EXECUTE=1 is the write gate on the two destructive-ish steps, matching the repo's
# existing ALLOW_REMOTE_SCHEMA_DESTROY convention: the dangerous thing needs a word.
#
# Compared with `filter`, not tested for emptiness. Make's `$(if)` asks whether a
# variable is NON-EMPTY, not whether it is true — so `EXECUTE=0`, `EXECUTE=no` and
# `EXECUTE=false` all used to arm the gate and write to whatever environment was named.
# Only the exact string `1` counts now; every other spelling, including a typo, is off,
# and off is the direction a mistake should fall.
EXECUTE ?=
OVERWRITE ?=
TRUNCATE ?=
WRITE = $(if $(filter 1,$(EXECUTE)),--execute,)
FORCE = $(if $(filter 1,$(OVERWRITE)),--overwrite,)

# TRUNCATE empties the destination tables before copying. Doubly gated — it needs its
# own `1` *and* EXECUTE=1 — because on its own it reads like a modifier and it is not:
# it is the only flag in this file that destroys data already in Postgres. Requiring
# both also stops it being armed on a dry run, where it would issue the DELETEs and
# then roll them back: nothing lost, but a confusing thing to have happened.
EMPTY_FIRST = $(if $(and $(filter 1,$(EXECUTE)),$(filter 1,$(TRUNCATE))),--truncate,)

cutover-backup: dump-mysql

# Alembic reads its URL from dd.common.cfg, so the recipe passes no URL — the point of
# running it under `railway run`. Idempotent: against a database already at head it is
# a no-op, which is also what each bot does at boot.
cutover-schema:
	$(RAILWAY_RUN) uv run alembic upgrade head

# Dry run by default: prints a per-table report of source rows, destination-before,
# copied and destination-after, and writes nothing. Read it, then EXECUTE=1.
# --source is the old database, taken from the same injected environment.
#
# Note what the dry run does NOT prove: its "dest after" column is computed as
# before+source rather than measured, so the per-table OK flag is true by construction.
# It tells you the row counts, the credentials and the schema are good. Only the real
# run measures whether the copy reconciled.
#
# The copy is ONE transaction, so a failure rolls the whole thing back and leaves the
# destination empty — re-run freely. The exception is a run that completes but does not
# reconcile: it exits 1 having *committed*, and says so. That is what TRUNCATE=1 is for,
# and the only thing:
#
#     make prod cutover-copy EXECUTE=1 TRUNCATE=1
#
# Without it a second EXECUTE=1 run refuses a non-empty destination rather than doubling
# every row whose primary key is generated. That refusal is the safe default; TRUNCATE
# is how you say "I have read the report and I want to start over".
cutover-copy:
	$(RAILWAY_RUN) sh -c 'uv run python -m dd.common.db_transfer --source "$$MYSQL_URL" $(WRITE) $(EMPTY_FIRST)'

# Loads FOLLOWABLES, the alerts channel and level, the colours, the default URL, the
# bad-channel switch and the two image URLs into auto_post_settings — the step that
# used to be "type twelve channels into a web form". Dry run by default, same as above.
# Never overwrites a row that already holds a value (OVERWRITE=1 if you mean to).
cutover-settings:
	$(RAILWAY_RUN) uv run python -m dd.common.settings_import $(WRITE) $(FORCE)

# Read the settings back out of the new database, as the bots will see them: the import
# in dry-run mode, which prints every setting's stored value beside the env's. After a
# successful cutover-settings every row should read `unchanged` — anything else is a
# setting that did not land, and it names which.
cutover-verify:
	$(RAILWAY_RUN) uv run python -m dd.common.settings_import

# All of it, in order. Without EXECUTE=1 this is a full rehearsal: it takes the backup,
# creates the schema, then dry-runs the copy and the settings import so you can read
# both reports before committing to anything.
#
# "Rehearsal" is precise, not a synonym for read-only: the backup is taken for real and
# the schema is migrated for real (`alembic upgrade head` is idempotent, and the copy
# cannot be dry-run without it). What EXECUTE=1 gates is the two steps that move data —
# the row copy and the settings write — and the closing line says exactly that rather
# than claiming nothing happened.
#
# There is no confirmation prompt. The gate is EXECUTE=1 plus the word `prod`, both of
# which have to be typed on the same line — and a rehearsal against dev is a different
# command from the real thing by exactly one word, which is the property worth having.
mysql-to-postgres:
	@echo "==> environment: $(TARGET_ENV)$(if $(WRITE), (EXECUTE=1 — this writes), (rehearsal))"
	@$(MAKE) --no-print-directory TARGET_ENV=$(TARGET_ENV) cutover-backup
	@$(MAKE) --no-print-directory TARGET_ENV=$(TARGET_ENV) cutover-schema
	@$(MAKE) --no-print-directory TARGET_ENV=$(TARGET_ENV) EXECUTE=$(EXECUTE) TRUNCATE=$(TRUNCATE) cutover-copy
	@$(MAKE) --no-print-directory TARGET_ENV=$(TARGET_ENV) EXECUTE=$(EXECUTE) OVERWRITE=$(OVERWRITE) cutover-settings
	@$(if $(WRITE),$(MAKE) --no-print-directory TARGET_ENV=$(TARGET_ENV) cutover-verify,:)
	@echo "==> done. $(if $(WRITE),Rows copied and settings written; check cutover-verify above.,Backup taken and the schema is at head on $(TARGET_ENV); NO rows were copied and NO settings were written. Re-run with EXECUTE=1.)"

# conftest.py is named alongside `dd` in all three: it is the one Python file outside
# the package tree (repo-root, where pytest's session DB fixture and its non-local-DB
# wipe guard live), so a bare `dd` path would leave it unlinted and untyped.
lint:
	uv run ruff check dd conftest.py

format:
	uv run ruff format dd conftest.py
	uv run ruff check --fix dd conftest.py

# What CI runs: reports drift, writes nothing. Part of `check` so the local mirror of
# CI catches a formatting failure before a push does.
format-check:
	uv run ruff format --check dd conftest.py

typecheck:
	uv run ty check dd conftest.py

test: .env
	uv run --env-file .env python -m pytest -m "not discord"

# The web_static JS unit tests (node --test, no bundler, no browser). Currently the CV2
# builder's pure node model — the client mirror of dd/anchor/cv2_nodes.py, which is worth
# testing directly because the builder UI renders straight out of it. Separate from
# `test` because it needs node rather than the Python env; `check` runs both.
test-js:
	node --test "dd/*/web_static/tests/*.test.js"

test-unit: .env
	uv run --env-file .env python -m pytest -m "not integration"

# The CV2 builder's drag layer, driven in a real Chromium against
# dd/anchor/web_static/tests/builder_harness.html (a no-server fixture). Included in
# `test` too, but skips there unless the browser is installed — Playwright ships as a
# dev dep, the ~150MB browser does not:
#     uv run playwright install chromium
test-browser: .env
	uv run --env-file .env python -m pytest -m browser -v

coverage: .env
	uv run --env-file .env python -m pytest -m "not discord" --cov=dd --cov-report=term-missing

# All live Discord integration tests (marker `discord`). Opt-in: these hit Discord
# and need a real bot token, so they're excluded from `test`/`coverage`/`check`.
# The bot token comes from .env (DISCORD_TOKEN_BEACON) via --env-file.
test-integration: .env
	uv run --env-file .env python -m pytest -m discord -v

# Just the mirror integration tests (a subset of `test-integration`). Each run
# reuses the dedicated test guild and isolates by sweeping its test channels.
test-mirror-integration: .env
	uv run --env-file .env python -m pytest \
		dd/beacon/tests/test_mirror_integration.py -v

# Every test, including the live Discord integration tests (no marker filter).
# Needs a real bot token in .env (DISCORD_TOKEN_BEACON), same as
# `test-integration`. Use this for a full run before a release.
test-all: .env
	uv run --env-file .env python -m pytest -v

check: lint format-check typecheck test test-js

.env:
	@echo "Please create a .env file with all variables as per beacon.cfg"
	@echo "and .env-example to be able to run this locally. Note that all"
	@echo "variables are required and the example values are not valid but"
	@echo "are there to show the approximate format of values."
	@exit 1

# Every target here names an action, not a file it produces — except `.env`, which IS a
# real file and is the guard that fails when it is missing. Without this a stray file
# named `check` or `test` in the repo root would silently make those targets no-ops.
#
# The per-bot targets are expanded from $(BOTS) rather than written out: `%` is literal
# in .PHONY, so a `.PHONY: deploy-%` declares a target called "deploy-%" and leaves the
# real ones unprotected. They are static pattern rules for the same reason — see the
# note on the deploy block for why a *plain* pattern rule cannot be made phony at all.
.PHONY: prod $(addprefix deploy-,$(BOTS)) $(addprefix remove-last-deploy-,$(BOTS)) \
	dev dev-up dev-login dev-down \
	dev-down-volumes run-beacon-local run-anchor-local _require-mem-cap \
	run-beacon-devbot run-anchor-devbot devbot-up devbot-down devbot-logs \
	devbot-status destroy-schemas create-schemas migration-plan migration-apply \
	migration-dry-run migration-check dump-db dump-mysql dump-vars restore-vars restore-raw \
	vars-check cutover-backup \
	cutover-schema cutover-copy cutover-settings cutover-verify mysql-to-postgres \
	lint format format-check typecheck test test-js test-unit test-browser \
	coverage test-integration test-mirror-integration test-all check
