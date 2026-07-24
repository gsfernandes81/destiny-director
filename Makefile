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
	@echo "Waiting for dd-dev to come up..."
	@for i in $$(seq 1 30); do docker exec dd-dev true 2>/dev/null && break || sleep 1; done
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

destroy-schemas: .env
	uv run python -m dd.common.schemas --destroy-all

create-schemas: .env
	uv run python -m dd.common.schemas --create-all

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

test-unit: .env
	uv run --env-file .env python -m pytest -m "not integration"

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

check: lint typecheck test

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
