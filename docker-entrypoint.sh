#!/bin/sh
# jemalloc (installed in the Dockerfile) is preloaded for BOTH bots: its background-decay
# allocator returns freed heap to the OS, whereas glibc holds it in per-arena pools —
# measured in prod, plain glibc left anchor's freed startup/post heap resident (~430MB)
# vs jemalloc's ~360MB, and it drove beacon materially higher. Railway bills on
# memory-over-time, so retained heap is paid for continuously.
#
# anchor additionally caps narenas:2. jemalloc's default (4 * ncpu, large on a many-core
# Railway host) reserves per-arena metadata/caches that bloated the *small* anchor
# process for no concurrency benefit (anchor is single-event-loop + a couple of worker
# threads). beacon — the large, hot consumer that the arena parallelism actually helps,
# and which settled at ~230MB on the defaults — keeps the default arena count.
#
# The alembic pre-flight below runs under the same LD_PRELOAD; it is a short-lived
# process, so the allocator choice is immaterial there either way.
preload_jemalloc() {  # $1: extra MALLOC_CONF options prefix (may be empty)
  jemalloc="/usr/lib/$(uname -m)-linux-gnu/libjemalloc.so.2"
  if [ -f "$jemalloc" ]; then
    export LD_PRELOAD="$jemalloc"
    export MALLOC_CONF="${1}background_thread:true,dirty_decay_ms:10000,muzzy_decay_ms:10000"
  fi
}

# If RAILWAY_SERVICE_NAME is beacon, then start beacon,
# otherwise if RAILWAY_SERVICE_NAME is anchor start anchor
# otherwise raise an error
#
# Migrations run before either bot starts, and a failure aborts the boot (the `&&`).
# alembic takes the database URL from dd.common.cfg — i.e. the same DATABASE_PRIVATE_URL
# / DATABASE_URL the bot itself connects with — so nothing is passed on the command
# line; it only needs alembic.ini + migrations/, both copied to /app (see Dockerfile).
if [ "$RAILWAY_SERVICE_NAME" = "beacon" ]; then
  preload_jemalloc ""
  alembic upgrade head && python -OO -m dd.beacon
elif [ "$RAILWAY_SERVICE_NAME" = "anchor" ]; then
    preload_jemalloc "narenas:2,"
    alembic upgrade head && python -OO -m dd.anchor
  else
    echo "Unknown service name: $RAILWAY_SERVICE_NAME"
    exit 1
fi
