#!/usr/bin/env bash
# Launch a beacon/anchor DEV bot inside the dd-dev container for spin-up-to-test work.
# Two isolation guarantees, so a throwaway test run can never harm anything else:
#
#   * DB isolation — the bot runs against $DEVBOT_SCHEMA (default kyber_devbot), a
#     separate Postgres DATABASE on the same server, NOT the shared `kyber` database, so
#     `make create/destroy-schemas` and the integration tests can't clobber it (and
#     vice-versa). The database is created + owned by the app user on the first launch
#     (needs the postgres sibling's superuser — see below).
#   * OOM isolation — relies on the dd-dev container memory cap (docker-compose.dev.yml
#     mem_limit) to confine any OOM kill to THIS container's cgroup; `choom` then biases
#     the bot to be the FIRST victim within it, ahead of the Claude session. The make
#     targets refuse to launch unless the cap is in place (see `_require-mem-cap`).
#
# No jemalloc in the dev image (prod-only), so RAM won't mirror prod exactly — fine for
# functional spin-up-to-test, not for a RAM-measurement instance.
#
# Usage: ./docker-run-devbot.sh <beacon|anchor>   (normally via `make run-*-devbot` /
# `make devbot-up`). Env vars are ambient in dd-dev via the compose `env_file: [.env]`.
set -euo pipefail

bot="${1:-}"
case "$bot" in
  beacon | anchor) ;;
  *) echo "usage: $0 <beacon|anchor>" >&2; exit 2 ;;
esac

: "${DATABASE_URL:?DATABASE_URL not set — run inside dd-dev where env_file loads .env}"
schema="${DEVBOT_SCHEMA:-kyber_devbot}"   # a Postgres DATABASE name, not a MySQL-style schema

# The bot connects with the SAME creds/host as DATABASE_URL, only the database swapped —
# the most faithful mirror of prod's (non-root) connection, just against a separate
# database.
devbot_url="${DATABASE_URL%/*}/${schema}"

# Host/port/user/password for the admin provisioning connection, parsed from
# DATABASE_URL so a non-default host still works. Port defaults to 5432 when the URL
# omits it. The compose `postgres` sibling's POSTGRES_USER is already a superuser (the
# official postgres image grants that to whichever user POSTGRES_USER names), so by
# default we just reuse the same creds as DATABASE_URL for provisioning rather than a
# separate root account — override with DEVBOT_DB_ADMIN_USER / DEVBOT_DB_ADMIN_PASSWORD
# if the compose file is ever changed to a non-superuser app role.
userinfo="${DATABASE_URL#*//}"; userinfo="${userinfo%%@*}"
app_user="${userinfo%%:*}"
app_password="${userinfo#*:}"
hostport="${DATABASE_URL#*@}"; hostport="${hostport%%/*}"
db_host="${hostport%%:*}"; db_port="${hostport##*:}"
[ "$db_port" = "$db_host" ] && db_port=5432

# Provision the database as the admin role (mirrors docker-entrypoint.dev.sh's old
# atlas_dev step — Alembic needs no throwaway "dev" database, so that step is gone
# entirely; this is purely the devbot's own isolated database). Fatal on failure —
# unlike a best-effort step, the bot can't run without its database — but bounded-retry
# first so a not-yet-ready Postgres doesn't spuriously fail the launch.
echo "devbot: provisioning database '$schema' on ${db_host}:${db_port} (owner -> ${app_user})"
DEVBOT_SCHEMA="$schema" \
DEVBOT_APP_USER="$app_user" \
DEVBOT_DB_HOST="$db_host" \
DEVBOT_DB_PORT="$db_port" \
DEVBOT_DB_ADMIN_USER="${DEVBOT_DB_ADMIN_USER:-$app_user}" \
DEVBOT_DB_ADMIN_PASSWORD="${DEVBOT_DB_ADMIN_PASSWORD:-$app_password}" \
"${VIRTUAL_ENV:-/home/dev/venv}/bin/python" - <<'PY'
import os
import time

import psycopg

schema = os.environ["DEVBOT_SCHEMA"]
owner = os.environ["DEVBOT_APP_USER"]


def main() -> None:
    last = None
    for _ in range(15):
        try:
            conn = psycopg.connect(
                host=os.environ["DEVBOT_DB_HOST"],
                port=int(os.environ["DEVBOT_DB_PORT"]),
                user=os.environ["DEVBOT_DB_ADMIN_USER"],
                password=os.environ["DEVBOT_DB_ADMIN_PASSWORD"],
                dbname="postgres",  # maintenance DB — always present, needed to CREATE DATABASE
                autocommit=True,  # CREATE DATABASE cannot run inside a transaction block
            )
        except Exception as exc:  # noqa: BLE001 — retry only while Postgres isn't ready yet
            last = exc
            time.sleep(2)
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (schema,))
                if cur.fetchone() is None:
                    # The database name can't be a bind parameter — identifier-quote it
                    # instead (it's ours, not user input). The sibling bot may create it
                    # concurrently — tolerate that race.
                    try:
                        cur.execute(f'CREATE DATABASE "{schema}" OWNER "{owner}"')
                    except psycopg.errors.DuplicateDatabase:
                        pass
            return
        finally:
            conn.close()
    raise SystemExit(f"could not connect to provision `{schema}` — is postgres up? ({last})")


main()
PY

# Apply migrations to the devbot database (mirrors the prod entrypoint's pre-flight).
# alembic.ini + migrations/ are resolved relative to cwd, which make runs at the repo
# root. Override BOTH url vars so cfg.py selects the devbot database regardless of which
# it prefers (it prefers DATABASE_PRIVATE_URL when set).
echo "devbot: applying migrations to '$schema'"
DATABASE_URL="$devbot_url" DATABASE_PRIVATE_URL="$devbot_url" uv run alembic upgrade head

# Same override for the bot process itself. choom biases the bot up the OOM-kill list so
# it, not the Claude session, is reaped first if the container cap is ever hit.
export DATABASE_URL="$devbot_url" DATABASE_PRIVATE_URL="$devbot_url"

# Mirror prod's allocator (docker-entrypoint.sh preload_jemalloc). glibc retains freed
# arenas, jemalloc returns them to the OS via a background decay thread. anchor caps
# narenas:2 (its small, single-loop process gained nothing from the default 4*ncpu and
# paid the per-arena overhead); beacon keeps the default. Needs libjemalloc2 in the image
# (Dockerfile.dev) — absent until the container is rebuilt, in which case we fall back to
# glibc, same as before. Set DEVBOT_JEMALLOC=0 to force glibc for an A/B RAM comparison.
# Keep MALLOC_CONF in sync with docker-entrypoint.sh.
jemalloc="/usr/lib/$(uname -m)-linux-gnu/libjemalloc.so.2"
if [ "${DEVBOT_JEMALLOC:-1}" = "0" ]; then
  echo "devbot: jemalloc disabled via DEVBOT_JEMALLOC=0 (glibc malloc)"
elif [ -f "$jemalloc" ]; then
  export LD_PRELOAD="$jemalloc"
  malloc_prefix=""; [ "$bot" = "anchor" ] && malloc_prefix="narenas:2,"
  export MALLOC_CONF="${malloc_prefix}background_thread:true,dirty_decay_ms:10000,muzzy_decay_ms:10000"
  echo "devbot: jemalloc preloaded (MALLOC_CONF=$MALLOC_CONF)"
else
  echo "devbot: jemalloc not in image (glibc malloc) — rebuild dd-dev (make dev-up) to enable"
fi

oom_adj="${DEVBOT_OOM_SCORE_ADJ:-800}"
echo "devbot: starting $bot against '$schema' (oom_score_adj=$oom_adj)"
# `--` stops choom parsing python's -OOm flags as its own options.
exec choom -n "$oom_adj" -- uv run python -OOm "dd.$bot"
