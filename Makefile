deploy-beacon-dev:
	railway environment dev
	railway service beacon
	railway up -d

deploy-anchor-dev:
	railway environment dev
	railway service anchor
	railway up -d

deploy-beacon-prod:
	railway environment production
	railway service beacon
	railway up -d

deploy-anchor-prod:
	railway environment production
	railway service anchor
	railway up -d

remove-last-deploy:
	railway down

# Remote Pi dev container (docker-compose.dev.yml). dev-up builds the image with
# the uid/gid that OWN this clone so the bind-mounted /workspace stays writable,
# then starts it detached. We read the owner with `stat`, NOT `id -u`: when docker
# is run via sudo/root, `id -u` is 0 and the build then collides with the root
# account (`groupadd: GID '0' already exists`). The clone owner is the right uid
# whoever launches the build. dev-down stops it; dev-down-volumes also drops the
# named volumes (uv cache, claude/railway/gh config, mysql data) — use when the baked
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

# One-time bridge for retiring FOLLOWABLES/cfg.followables: backfill any followable
# channel DB row that doesn't exist yet from the current FOLLOWABLES env var.
# Idempotent (never overwrites an existing row) — see
# dd.common.settings.seed_followables_from_env's docstring for the full cutover plan.
seed-followables: .env
	uv run python -m dd.common.schemas --seed-followables

# Same bridge as seed-followables, for every OTHER setting dd.common.settings replaced
# (colors, default url, alert level, disable_bad_channels, log/alerts channel, the two
# image urls) — their env vars are no longer even read by cfg.py, so this is the only
# way an already-configured deploy's values survive the migration. Idempotent — see
# dd.common.settings.seed_settings_from_env's docstring.
seed-settings: .env
	uv run python -m dd.common.schemas --seed-settings

# Render the SQLAlchemy models to DDL (.atlas/desired.sql, gitignored), then let
# Atlas diff it against migrations/ and write a new migration if they differ. The
# DDL is generated here rather than via Atlas's `external_schema` provider so the
# community Atlas binary in the dev container can run it too (see atlas.hcl). Set
# ATLAS_DEV_URL (dev container does) to use the sibling MySQL scratch schema
# instead of an ephemeral docker:// dev database.
atlas-migration-plan: .env
	mkdir -p .atlas
	uv run python dd/common/schemas.py --print-ddl > .atlas/desired.sql
	atlas migrate diff --env sqlalchemy

atlas-migration-dry-run:
	@echo "atlas migrate apply -u <MYSQL_URL> --dry-run"
	atlas migrate apply -u ${MYSQL_URL} --dry-run

atlas-migration-apply:
	@echo "atlas migrate apply -u <MYSQL_URL>"
	atlas migrate apply -u ${MYSQL_URL}

# Back up a DB to a timestamped ./kyber-<env>-<UTC>.sql via mysqldump, pulling the MySQL
# service's connection vars from the given Railway environment. Runs locally, so it needs
# mysqldump installed and the MySQL service reachable (public TCP proxy).
dump-prod-db:
	railway run -e production -s MySQL bash -c 'mysqldump -h "$$MYSQLHOST" -P "$$MYSQLPORT" -u "$$MYSQLUSER" -p"$$MYSQLPASSWORD" --skip-ssl-verify-server-cert --single-transaction --quick --no-tablespaces "$$MYSQLDATABASE" > "kyber-prod-$$(date -u +%Y%m%dT%H%M%SZ).sql"'

dump-dev-db:
	railway run -e dev -s MySQL bash -c 'mysqldump -h "$$MYSQLHOST" -P "$$MYSQLPORT" -u "$$MYSQLUSER" -p"$$MYSQLPASSWORD" --skip-ssl-verify-server-cert --single-transaction --quick --no-tablespaces "$$MYSQLDATABASE" > "kyber-dev-$$(date -u +%Y%m%dT%H%M%SZ).sql"'

lint:
	uv run ruff check dd

format:
	uv run ruff format dd
	uv run ruff check --fix dd

typecheck:
	uv run ty check dd

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

check: lint typecheck test test-js

.env:
	@echo "Please create a .env file with all variables as per beacon.cfg"
	@echo "and .env-example to be able to run this locally. Note that all"
	@echo "variables are required and the example values are not valid but"
	@echo "are there to show the approximate format of values."
	@exit 1

install-termux-deps:
	@echo "If the specific python version for this project is not available"
	@echo "and cannot be upgraded, then consider using the TUR to find it:"
	@echo "https://github.com/termux-user-repository/tur"
	pkg install python uv
