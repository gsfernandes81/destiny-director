# Copyright © 2019-present gsfernandes81

# This file is part of "dd" henceforth referred to as "destiny-director".

# destiny-director is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.

# "destiny-director" is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License along with
# destiny-director. If not, see <https://www.gnu.org/licenses/>.

import json
import logging
import re
import typing as t
from os import getenv as __getenv

import hikari as h
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool, Pool

load_dotenv()
T = t.TypeVar("T")


@t.overload
def _getenv(key: str, default: int) -> int: ...


@t.overload
def _getenv(key: str, default: str) -> str: ...


@t.overload
def _getenv(key: str) -> str: ...


def _getenv(key: str, default: int | str | None = None) -> int | str:
    value = __getenv(key)

    if value is None:
        if default is None:
            raise ValueError(f"Environment variable '{key}' not found.")
        elif isinstance(default, int):
            return int(default)
        else:
            return default
    else:
        if isinstance(default, int):
            try:
                return int(value)
            except ValueError:
                raise ValueError(
                    f"Environment variable '{key}' must be an integer."
                ) from None

        return value


def _getbool(key: str, default: bool) -> bool:
    """Parse a boolean env var case-insensitively (``true``/``1``/``yes``/``on``).

    Replaces the ad-hoc ``_getenv(...) == "true"`` checks, which were inconsistent:
    case-sensitive for the old ``MYSQL_SSL`` (so ``MYSQL_SSL=True`` silently disabled
    SSL) and case-insensitive for ``DISABLE_BAD_CHANNELS``.
    """
    value = __getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"true", "1", "yes", "on"}


# The kernel's legal range for /proc/<pid>/oom_score_adj (see proc(5)). Kept here
# rather than in dd.common.lifecycle so the env parsing below and the helper that
# writes procfs share one definition.
OOM_SCORE_ADJ_MIN = -1000
OOM_SCORE_ADJ_MAX = 1000


def _getenv_oom_score_adj(key: str) -> int | None:
    """Parse an optional ``oom_score_adj``; ``None`` when unset or blank.

    Optional because it is a memory-pressure optimisation, not a dependency — but a
    *malformed* value is still a hard error here rather than garbage written to
    procfs later, which is the same fail-fast-at-import contract as the required
    vars above.
    """
    value = __getenv(key)
    if value is None or not value.strip():
        return None

    try:
        score_adj = int(value.strip())
    except ValueError:
        raise ValueError(
            f"Environment variable '{key}' must be an integer between "
            f"{OOM_SCORE_ADJ_MIN} and {OOM_SCORE_ADJ_MAX}."
        ) from None

    if not OOM_SCORE_ADJ_MIN <= score_adj <= OOM_SCORE_ADJ_MAX:
        raise ValueError(
            f"Environment variable '{key}' is {score_adj}, outside the kernel's "
            f"legal oom_score_adj range "
            f"({OOM_SCORE_ADJ_MIN}..{OOM_SCORE_ADJ_MAX})."
        )

    return score_adj


def _test_env(var_name: str) -> tuple[int, ...] | tuple[()]:
    test_env = _getenv(var_name, default="false")
    test_env = test_env.lower()
    test_env = (
        tuple(int(env.strip()) for env in test_env.split(","))
        if test_env != "false"
        else ()
    )
    return test_env


# Backend scheme (the part before "://", driver suffix stripped) -> the SQLAlchemy
# sync/async URL prefixes it maps to. Postgres runs on psycopg v3, whose SQLAlchemy
# dialect is dual sync/async, so both prefixes are the same one. The empty scheme is
# Library Mode's placeholder URL (see _db_urls). MySQL is deliberately absent — see
# the explicit error below.
_DB_SCHEMES: t.Mapping[str, tuple[str, str]] = {
    "": ("postgresql+psycopg", "postgresql+psycopg"),
    "postgres": ("postgresql+psycopg", "postgresql+psycopg"),
    "postgresql": ("postgresql+psycopg", "postgresql+psycopg"),
    "sqlite": ("sqlite", "sqlite+aiosqlite"),
}


def _with_sslmode(db_url: str) -> str:
    """Ensure a Postgres URL carries an explicit ``sslmode`` when TLS is wanted.

    Postgres expresses TLS as libpq's ``sslmode`` query parameter rather than a
    driver-side ``ssl.SSLContext`` (how the old asyncmy setup did it), so the knob
    lives in the URL. ``DATABASE_SSL`` defaults to true; an ``sslmode`` already in the
    URL always wins, and setting it false simply leaves libpq's own default (``prefer``)
    in place rather than forcing TLS off.
    """
    if not _getbool("DATABASE_SSL", True) or "sslmode=" in db_url:
        return db_url
    return db_url + ("&" if "?" in db_url else "?") + "sslmode=require"


def _db_urls(var_name: str, var_name_alternative: str) -> tuple[str, str]:
    """Sync and async SQLAlchemy URLs for the configured database.

    Accepts ``postgres://`` / ``postgresql://`` (mapped onto psycopg v3) and
    ``sqlite://`` (async side gets aiosqlite) — the latter covers the test suite and
    the small Raspberry Pi deployment.
    """
    try:
        db_url = _getenv(var_name)
    except ValueError:
        db_url = _getenv(var_name_alternative)

    if not db_url:
        # Added for compatiblity with Library Mode
        # db_url will only be none if the library mode environment
        # variable switch is enabled since _getenv(var_name_alternative)
        # would otherwise raise ValueError
        db_url = "://"

    scheme, _, remainder = db_url.partition("://")
    # Tolerate an already-qualified scheme ("postgresql+psycopg://…") by keeping only
    # the backend name; the driver is ours to pick.
    backend = scheme.split("+", 1)[0].strip().lower()

    if backend in {"mysql", "mariadb"}:
        raise ValueError(
            f"Environment variable '{var_name}'/'{var_name_alternative}' points at "
            f"MySQL ({db_url.split('://')[0]}://…), which is no longer supported. "
            "Use a postgresql:// or sqlite:// URL."
        )
    if backend not in _DB_SCHEMES:
        raise ValueError(
            f"Unsupported database scheme {backend!r} in "
            f"'{var_name}'/'{var_name_alternative}'; expected postgresql:// or "
            "sqlite://."
        )

    sync_prefix, async_prefix = _DB_SCHEMES[backend]
    if backend in {"", "sqlite"}:
        # SQLite has no sslmode, and the empty scheme is Library Mode's placeholder —
        # an engine is built from it but never connects, so leave it bare.
        return f"{sync_prefix}://{remainder}", f"{async_prefix}://{remainder}"

    return (
        _with_sslmode(f"{sync_prefix}://{remainder}"),
        _with_sslmode(f"{async_prefix}://{remainder}"),
    )


def _db_config(db_url_async: str) -> tuple[
    t.Mapping[str, bool | type[AsyncSession]],
    t.Mapping[str, bool],
    t.Mapping[str, t.Any],
    t.Mapping[str, int | str | bool | type[Pool]],
]:
    db_session_kwargs_sync: dict[str, t.Any] = {
        "expire_on_commit": False,
    }
    db_session_kwargs = db_session_kwargs_sync | {
        "class_": AsyncSession,
    }

    # TLS now rides in the URL (sslmode=…) rather than a driver-side ssl.SSLContext,
    # so what is left here is the session timezone.
    db_connect_args: dict[str, t.Any] = {}
    if not db_url_async.startswith("sqlite"):
        # Pin every psycopg session to UTC. The schema stores naive
        # TIMESTAMP WITHOUT TIME ZONE columns but the code writes a mix of naive-UTC
        # and tz-aware UTC datetimes into them, and Postgres converts an *aware* value
        # using the session's TimeZone — so on a server whose zone is not UTC (initdb
        # takes it from the OS, which on a Pi usually is a local zone) some rows would
        # silently land offset from the rest. MySQL's driver simply dropped the tzinfo,
        # which is why this never surfaced before.
        db_connect_args["options"] = "-c timezone=UTC"

    db_engine_args: dict[str, int | str | bool | type[Pool]] = {
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    }
    if not db_url_async.startswith("sqlite"):
        # Server/QueuePool knobs SQLite neither needs nor accepts. max_overflow is
        # finite (it was -1, i.e. unbounded, under MySQL) because the Pi's Postgres
        # runs with max_connections=25 — an unbounded overflow could eat every slot.
        db_engine_args["max_overflow"] = 10
        db_engine_args["isolation_level"] = "READ COMMITTED"
        db_engine_args["pool_use_lifo"] = True

    # Under pytest the engine is driven from many short-lived event loops
    # (every asyncio.run() in test setup/teardown opens a new loop). psycopg
    # connections are bound to their creating loop, so pooled connections that
    # outlive that loop blow up with "Event loop is closed" when the pool later
    # terminates them. NullPool closes each connection on return, within the
    # live loop, so nothing survives to a dead loop.
    if __getenv("PYTEST_VERSION") is not None:
        db_engine_args["poolclass"] = NullPool
        # max_overflow and pool_use_lifo are QueuePool-specific and rejected
        # by create_engine when combined with NullPool.
        db_engine_args.pop("max_overflow", None)
        db_engine_args.pop("pool_use_lifo", None)
    return db_session_kwargs, db_session_kwargs_sync, db_connect_args, db_engine_args


######### loglevel config #########

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname).1s %(name)s | %(message)s",
)
###### Environment variables ######

# Discord environment config
test_env = _test_env("TEST_ENV")
discord_token_anchor = _getenv("DISCORD_TOKEN_ANCHOR", default="")
discord_token_beacon = _getenv("DISCORD_TOKEN_BEACON", default="")
disable_bad_channels = _getbool("DISABLE_BAD_CHANNELS", False)

# Discord control server config
control_discord_server_id = int(_getenv("CONTROL_DISCORD_SERVER_ID", "-1"))
control_discord_role_id = _getenv("CONTROL_DISCORD_ROLE_ID", "-1")
kyber_discord_server_id = _getenv("KYBER_DISCORD_SERVER_ID", default=-1)
log_channel = _getenv("LOG_CHANNEL_ID", default=0)
alerts_channel = _getenv("ALERTS_CHANNEL_ID", default=0)


# Discord constants
embed_default_color = h.Color(int(_getenv("EMBED_DEFAULT_COLOR", "0"), 16))
embed_error_color = h.Color(int(_getenv("EMBED_ERROR_COLOR", "0"), 16))
followables: dict[str, int] = json.loads(_getenv("FOLLOWABLES", "{}"), parse_int=int)
default_url = _getenv("DEFAULT_URL", "")
# Seconds a paginator waits for interaction before timing out. Baked in — prod ran
# NAVIGATOR_TIMEOUT=900, never per-deploy overridden.
navigator_timeout = 900

# Discord logging / alerting config (see dd/common/discord_logging.py)
# Minimum log level forwarded to the alerts channel.
alert_min_level = _getenv("ALERT_MIN_LEVEL", "ERROR")
# Seconds the consumer waits collecting records before flushing a batch (lets
# duplicate records within the window collapse into a single alert).
# The knobs below have baked-in sensible defaults and are never overridden
# per-deploy, so they are plain constants (not env-backed) to keep the env contract
# small. Re-introduce env-backing only if a per-deploy override is ever needed.
alert_flush_interval = 5
# Max queued records before new ones are dropped (back-pressure guard).
alert_queue_maxsize = 1000
# Rolling window (seconds) and occurrence threshold for the "error storm"
# escalation, plus a debounce so a sustained storm doesn't re-ping every flush.
alert_freq_window = 300
alert_freq_threshold = 10
alert_escalation_debounce = 600
# Auto-disable is driven by a separate low-load reachability sweep, not delivery
# failures. A legacy mirror pair whose destination stays unreachable / unsendable for
# at least this many hours (continuous, measured from unreachable_since) is disabled.
# The sweep itself runs every mirror_reachability_sweep_hours.
mirror_unreachable_grace_hours = 48
mirror_reachability_sweep_hours = 6
# Mirror fan-out tuning. These are baked-in defaults (not env-backed) to keep the
# env contract small; re-introduce env-backing if a per-deploy override is ever
# needed. mirror_max_concurrency caps in-flight delivery coroutines; mirror_rate_per_sec
# is the global token-bucket rate shared across all runs (kept below Discord's ~50/s
# global REST budget to leave headroom for interactive commands). The retry window is
# the randomised delay (seconds) before a transient failure is retried.
mirror_max_concurrency = 8
mirror_rate_per_sec = 30.0
mirror_retry_min = 180
mirror_retry_max = 300
# Delivery-ledger worker knobs. The single worker picks up to mirror_pick_batch_size due
# rows per pass; mirror_poll_interval is the lazy backstop it sleeps when no gateway
# nudge arrives. The per-op attempt caps are 3 for a send, 2 for an edit/delete.
# Ledger rows are pruned once older than mirror_retention_days (bar the latest delivered
# message per destination channel, kept as an anchor).
mirror_pick_batch_size = 50
mirror_poll_interval = 45
mirror_send_max_attempts = 3
mirror_edit_max_attempts = 2
mirror_retention_days = 14
# Seconds an autopost announcer may stall (API offline / edit failing) before a
# single critical alert fires for that run.
announcer_offline_alert_after = 900
# Accent colours for alert severities (hex, like the other embed colours).
embed_warning_color = h.Color(0xF1C40F)
embed_critical_color = h.Color(0x992D22)

# Database URLs. DATABASE_PRIVATE_URL wins over DATABASE_URL — Railway injects both
# and the private one stays on the internal network.
db_url, db_url_async = _db_urls("DATABASE_PRIVATE_URL", "DATABASE_URL")

# Whether each bot runs `alembic upgrade head` itself on StartingEvent (see
# dd/common/db_migrations.py). Defaults ON: the process model has no dependency
# ordering to lean on (supervisord spawns its programs simultaneously), so the
# migration is sequenced by straight-line Python inside the bot instead of by a shell
# entrypoint. Set false to run a bot against a database somebody else migrates —
# local dev against a shared DB, or a container where a one-shot job does it.
run_migrations_on_startup = _getbool("RUN_MIGRATIONS_ON_STARTUP", True)

# Optional Linux OOM-kill preference this process raises itself to at startup (see
# dd.common.lifecycle.apply_oom_score_adj). Unset -> left at whatever the container
# baseline is. Named without the DEVBOT_ prefix of the dev-runner knob it mirrors
# (DEVBOT_OOM_SCORE_ADJ, applied externally by choom in docker-run-devbot.sh) because
# this one is read by the app itself, in every environment.
oom_score_adj = _getenv_oom_score_adj("OOM_SCORE_ADJ")

# Static Images / Resources
lost_sector_gif_url = _getenv("LOST_SECTOR_GIF_URL")
xur_image_url = _getenv("XUR_IMAGE_URL")

# Bungie credentials
bungie_api_key = _getenv("BUNGIE_API_KEY", "")
bungie_client_id = _getenv("BUNGIE_CLIENT_ID", "")
bungie_client_secret = _getenv("BUNGIE_CLIENT_SECRET", "")

# Discord OAuth for the anchor web UI (see dd/anchor/extensions/web_auth.py). All these
# default to "" and are intentionally NOT part of import-time required-var validation:
# they are only needed by the anchor web surface, so leaving them unset must not break
# local dev or the beacon process. The client id is the bot's application id; the secret
# comes from the Developer Portal's OAuth2 tab (NOT the bot token).
discord_oauth_client_id = _getenv("DISCORD_OAUTH_CLIENT_ID", "")
discord_oauth_client_secret = _getenv("DISCORD_OAUTH_CLIENT_SECRET", "")
# Dev-only auth bypass: a Discord user id treated as an authenticated owner for the web
# UI. Honored ONLY when TEST_ENV is set AND there is no public base URL (see the
# middleware's triple gate), so it is inert on any internet-facing deploy — including
# dev, which also sets TEST_ENV — even if this leaks into that config.
dev_auth_user_id = _getenv("DEV_AUTH_USER_ID", "")


port = _getenv("PORT", 8080)


def _public_base_url() -> str:
    """Public origin (scheme + host) the anchor web app is reachable at.

    Prefers an explicit ``PUBLIC_BASE_URL`` (e.g. behind a custom domain); otherwise
    derives ``https://<RAILWAY_PUBLIC_DOMAIN>`` from Railway's injected domain. Empty
    when neither is set (e.g. local dev without a tunnel) — the rotation editor command
    surfaces that as a clear error rather than minting an unreachable link.
    """
    explicit = __getenv("PUBLIC_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    railway_domain = __getenv("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain:
        return "https://" + railway_domain.rstrip("/")
    return ""


public_base_url = _public_base_url()
#### Environment variables end ####

###################################

####### Configs & constants #######

(
    db_session_kwargs,
    db_session_kwargs_sync,
    db_connect_args,
    db_engine_args,
) = _db_config(db_url_async)

url_regex = re.compile(
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)
IMAGE_EXTENSIONS_LIST = [
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tiff",
    ".tif",
    ".heif",
    ".heifs",
    ".heic",
    ".heics",
    ".webp",
]

##### Configs & constants end #####
