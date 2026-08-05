# syntax=docker/dockerfile:1

# --- Builder: install deps + the project into a venv -----------------------
FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# build-essential is here for the handful of deps with no arm64 wheel, which uv
# then builds from sdist; it is discarded with this stage and never reaches the
# final image. psycopg needs no compiler — we install the pure-Python
# distribution, which loads libpq at runtime (see the final stage).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Dependencies layer — cached on the pyproject/uv.lock hash. --no-dev drops the
# dev group but keeps speedups (a default group), matching prod today.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# The project itself, installed non-editable into the venv.
COPY dd ./dd
COPY README.md ./
RUN uv sync --frozen --no-dev --no-editable

# --- Final: runtime image, no compilers ------------------------------------
FROM python:3.13-slim-bookworm AS final
ENV TZ=Etc/UTC \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"
# libjemalloc2: preloaded in docker-entrypoint.sh. glibc malloc retains freed arenas
# for this long-lived, bursty-allocation workload (the manifest parse; the gateway
# cache), inflating resident RAM (Railway bills memory-over-time). jemalloc returns
# freed pages to the OS via a background decay thread — see the entrypoint.
# libpq5: psycopg is installed as the pure-Python distribution (no ARMv6/musl binary
# wheels exist), so it dlopens the system libpq at import time — without this the bot
# cannot even start.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata ca-certificates libjemalloc2 libpq5 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# The migration environment: docker-entrypoint.sh runs `alembic upgrade head` before
# either bot starts, and alembic resolves `script_location` relative to alembic.ini.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./
# ARG only — do NOT promote to ENV. Baking an empty ENV would shadow Railway's
# runtime-injected RAILWAY_SERVICE_NAME and break beacon/anchor selection.
ARG RAILWAY_SERVICE_NAME
CMD ["sh", "docker-entrypoint.sh"]
