#!/bin/sh
# Rootless test-bot entrypoint (beacon only -- this box doesn't run anchor). Mirrors
# the prod docker-entrypoint.sh's ordering/failure behavior: migrations run before the
# bot starts, and a migration failure aborts the boot.
#
# One deliberate addition: a short retry loop around `alembic upgrade head`.
# compose.yml's `depends_on: [postgres]` is start-order-only under podman-compose (no
# health-gated wait -- see the comment there), so on a cold `podman-compose up -d` this
# container can start before Postgres has finished initdb, especially on SD-card I/O at
# 700MHz. dd.beacon itself already tolerates a not-yet-ready DB via
# dd.common.schemas.wait_for_db() (see dd/beacon/__main__.py) once it's running --
# this loop just extends the same courtesy to the one-shot migration step.
#
# No jemalloc preload here (unlike prod): Alpine's musl-libc repos don't ship
# libjemalloc2, and at this box's scale (one small bot, ~512MB total) the allocator
# choice doesn't move the needle the way it does under Railway's larger, billed-by-
# memory-over-time workload.
set -u

ATTEMPTS="${MIGRATION_ATTEMPTS:-12}"
DELAY="${MIGRATION_RETRY_DELAY:-5}"

i=1
while [ "$i" -le "$ATTEMPTS" ]; do
    if alembic upgrade head; then
        exec python -OO -m dd.beacon
    fi
    echo "alembic upgrade head failed (attempt $i/$ATTEMPTS), retrying in ${DELAY}s..." >&2
    i=$((i + 1))
    sleep "$DELAY"
done

echo "alembic upgrade head did not succeed after $ATTEMPTS attempts, giving up" >&2
exit 1
