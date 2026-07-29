#!/bin/sh
# Preload jemalloc (installed in the Dockerfile) so freed heap is returned to the OS
# instead of being retained in glibc arenas — this workload is long-lived with bursty
# allocations (the Bungie manifest parse; the gateway cache), and Railway bills on
# memory-over-time. background_thread + short decay windows purge dirty/muzzy pages
# promptly without waiting for new allocation activity. Guarded on the arch-specific
# path existing so a missing lib never breaks startup. atlas is a static Go binary and
# is unaffected by LD_PRELOAD.
JEMALLOC="/usr/lib/$(uname -m)-linux-gnu/libjemalloc.so.2"
if [ -f "$JEMALLOC" ]; then
  export LD_PRELOAD="$JEMALLOC"
  export MALLOC_CONF="background_thread:true,dirty_decay_ms:10000,muzzy_decay_ms:10000"
fi

# If RAILWAY_SERVICE_NAME is beacon, then start beacon,
# otherwise if RAILWAY_SERVICE_NAME is anchor start anchor
# otherwise raise an error
if [ "$RAILWAY_SERVICE_NAME" = "beacon" ]; then
  atlas migrate apply -u ${MYSQL_URL} && python -OO -m dd.beacon
elif [ "$RAILWAY_SERVICE_NAME" = "anchor" ]; then
    atlas migrate apply -u ${MYSQL_URL} && python -OO -m dd.anchor
  else
    echo "Unknown service name: $RAILWAY_SERVICE_NAME"
    exit 1
fi
