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

"""General/autopost settings, DB-backed via
:class:`~dd.common.schemas.AutoPostSettings`.

Formerly a batch of env vars (``EMBED_DEFAULT_COLOR``, ``FOLLOWABLES``,
``LOG_CHANNEL_ID``, ...) that required a redeploy to change. They now live as rows in
the same ``auto_post_settings`` table the autopost toggles already use — the web
control panel's Autopost Settings page (``dd/anchor/extensions/autopost_settings.py``)
is the sole editor.

**Caching.** Every getter here is async and hits an in-process cache with a short TTL
(:data:`_TTL`), refilled with one bulk query rather than one round trip per setting —
these are read on nearly every embed built anywhere in either bot, so an uncached read
would put the DB on the hot path for basic command responses. A save on the settings
page calls :func:`invalidate` so the *saving* process picks up its own change
immediately; other processes (e.g. beacon, when the edit was made from anchor's web UI)
see it within one TTL window. That cross-process staleness window is the accepted cost
of not needing a redeploy to change these anymore.

**Followables and import-time reads.** Every other setting here is read at call time
inside an async function, so an ``await`` is all a consumer needs. Followable channel
ids are the one exception: a handful of beacon extensions key their command
registration and navigator-page setup on the channel id at **import** time
(module-level code, before any event loop the async getters could run on). For those,
:func:`preload` — called once by each bot's ``__main__`` on ``StartingEvent``, before
extensions import — warms the cache synchronously-readable via
:func:`get_followable_channel_sync`. Both entry points already did an async DB round
trip at that point (``schemas.wait_for_db`` / ``_refresh_relevant_channel_ids``), so
this is one more, not a new pattern.

A followable slug absent from the DB falls back to :data:`cfg.followables` (the old env
var, still read at import time by ``cfg.py``) rather than straight to 0 — that keeps
every existing deploy and test fixture working unchanged until someone actually edits
the settings page, at which point the DB row wins from then on.
"""

import asyncio
import logging
import os
import time
import typing as t

import hikari as h

from . import cfg, schemas

logger = logging.getLogger(__name__)

# How long a cached read is trusted before the next getter call triggers a refetch.
# Short enough that an edit on the settings page is felt quickly in the *same* process;
# see the module docstring for the cross-process case.
_TTL = 30.0

# (enabled_default, value_default) for every slug that isn't a followable channel.
# The colors are hardcoded brand defaults rather than black: #EC42A5 is the Kyber brand
# pink (shared.css's --accent — the same pink as every focus ring/button/toggle on this
# site), #ED4245 matches components.CV2_DANGER_COLOR (the app's one other "error" red),
# so an unconfigured install still looks branded/on-theme rather than defaulting to a
# literal black accent bar. Every other default is chosen to match its old env-var
# default exactly, so an unconfigured DB row behaves identically to the var having never
# been set.
_DEFAULTS: dict[str, tuple[bool | None, str | None]] = {
    "embed_default_color": (None, "#EC42A5"),
    "embed_error_color": (None, "#ED4245"),
    "default_url": (None, ""),
    "alert_min_level": (None, "ERROR"),
    "disable_bad_channels": (False, None),
    "log_channel_id": (None, "0"),
    "alerts_channel_id": (None, "0"),
    "lost_sector_image_url": (None, ""),
    "xur_image_url": (None, ""),
}

# feed slug (as used in the old FOLLOWABLES dict / AutoPostSettings toggle rows) -> the
# AutoPostSettings.name this module stores its channel id under. "_channel" is appended
# rather than reusing the feed's own toggle slug so a toggle's `enabled` column (on/off)
# and its channel id (where) stay independently readable/writable.
FOLLOWABLE_SLUGS: dict[str, str] = {
    "lost_sector": "lost_sector_channel",
    "xur": "xur_channel",
    "eververse": "eververse_channel",
    "ada": "ada_channel",
    "portal_ops": "portal_ops_channel",
    "iron_banner": "iron_banner_channel",
    "twab": "twab_channel",
    "trials": "trials_channel",
    "weekly_reset": "weekly_reset_channel",
    "weekly_nightfall": "weekly_nightfall_channel",
    "free_games": "free_games_channel",
    "emblems_and_cosmetics": "emblems_and_cosmetics_channel",
}

_cache: dict[str, tuple[bool | None, str | None]] = {}
_loaded_at: float = 0.0
# Single-flights a stale-cache refresh: without this, every coroutine that observes a
# stale cache in the same tick (several feeds' scheduled posts, several web requests,
# ...) independently calls preload() before any of them finishes updating _loaded_at,
# firing one redundant DB round trip per caller instead of sharing one.
_refresh_lock = asyncio.Lock()


def _is_fresh() -> bool:
    # _loaded_at (not _cache) is the "has this ever been loaded" signal: a fresh
    # install with zero configured settings rows legitimately has an empty _cache
    # after a real preload() — `if _cache` alone would read that as "never loaded"
    # and refetch on every single call, defeating the TTL cache entirely.
    return bool(_loaded_at) and time.monotonic() - _loaded_at < _TTL


async def _ensure_fresh() -> None:
    if _is_fresh():
        return
    async with _refresh_lock:
        # Re-check: another caller may have refreshed while this one waited for the
        # lock, in which case there's nothing left to do.
        if _is_fresh():
            return
        await preload()


async def preload() -> None:
    """Force a full reload from the DB.

    Called once by each bot's ``StartingEvent`` handler, before extensions import (see
    the module docstring on why that ordering matters for followables). Safe to call
    again later — a plain unconditional refresh, not just a cache-miss fill.
    """
    global _loaded_at
    rows = await schemas.AutoPostSettings.get_all_rows()
    _cache.clear()
    _cache.update(rows)
    _loaded_at = time.monotonic()


def invalidate() -> None:
    """Mark the cache stale so the next *async* read refetches.

    Sync — usable from a context that can't ``await``. That's also its limit: it
    doesn't touch ``_cache`` itself, so a ``*_sync`` getter (which reads ``_cache``
    directly, with no freshness check of its own — see e.g.
    :func:`get_followable_channel_sync`'s docstring) keeps serving whatever was cached
    until *some* async getter happens to run elsewhere in the process and trigger a
    real refresh. An async caller that wants the cache genuinely fresh on return
    (including for its own ``*_sync`` reads) should ``await`` :func:`preload` directly
    instead — see :func:`seed_followables_from_env` for the pattern.
    """
    global _loaded_at
    _loaded_at = 0.0


def _raw(slug: str) -> tuple[bool | None, str | None]:
    """The cached ``(enabled, value)`` pair for ``slug``, straight from ``_cache``.

    ``(None, None)`` if the slug has no row at all — never seen a save, or a fresh
    install. Doesn't apply :data:`_DEFAULTS` and doesn't refresh the cache; every caller
    here does both, since what counts as "unset" (and its default) differs between a
    toggle's ``enabled`` column and a url/id's ``value`` column.
    """
    return _cache.get(slug, (None, None))


async def _get_value(slug: str) -> str | None:
    """The live ``value`` column for ``slug`` (refreshing the cache first), or its
    default."""
    await _ensure_fresh()
    _enabled, value = _raw(slug)
    if value is not None:
        return value
    return _DEFAULTS.get(slug, (None, None))[1]


async def _get_enabled(slug: str) -> bool:
    """The live ``enabled`` column for ``slug`` (refreshing the cache first), or its
    default."""
    await _ensure_fresh()
    enabled, _value = _raw(slug)
    if enabled is not None:
        return enabled
    return bool(_DEFAULTS.get(slug, (False, None))[0])


def _parse_color(raw: str | None) -> h.Color:
    if not raw:
        return h.Color(0)
    try:
        return h.Color(int(raw.lstrip("#"), 16))
    except ValueError:
        logger.warning("Malformed stored color %r, falling back to black", raw)
        return h.Color(0)


async def get_embed_default_color() -> h.Color:
    return _parse_color(await _get_value("embed_default_color"))


async def get_embed_error_color() -> h.Color:
    return _parse_color(await _get_value("embed_error_color"))


def _get_value_sync(slug: str) -> str | None:
    """Sync counterpart to :func:`_get_value`: no :func:`_ensure_fresh`, so it only ever
    sees whatever is already cached (warm after :func:`preload`, stale/empty otherwise —
    see :func:`get_embed_default_color_sync`'s docstring for why that's the deal for a
    sync reader). Same default fallback as :func:`_get_value` otherwise.
    """
    _enabled, value = _raw(slug)
    if value is not None:
        return value
    return _DEFAULTS.get(slug, (None, None))[1]


def get_embed_default_color_sync() -> h.Color:
    """Sync counterpart to :func:`get_embed_default_color`.

    For the rare low-level building block (``components.build_container``) called from
    far too many sync call sites, across both bots and their tests, to make async —
    unlike every other setting here, which is read from inside an already-async
    function. Reflects whatever is cached (warm after :func:`preload`, refreshed
    passively whenever any async getter runs elsewhere in the process); a cold cache
    (e.g. a unit test that never touches ``settings``) reads the same hardcoded default
    the pre-migration env var had.
    """
    return _parse_color(_get_value_sync("embed_default_color"))


def get_embed_error_color_sync() -> h.Color:
    """Sync counterpart to :func:`get_embed_error_color`.

    Same rationale as :func:`get_embed_default_color_sync`: ``dd/beacon/nav.py`` and
    its ``preprocess_messages`` overrides (twab, nightfall, template) are a sync
    pipeline (``NavPages`` compares its "no data" sentinel embed by equality, which
    needs it built once at import time — see ``NO_DATA_HERE_EMBED`` — so the pipeline
    around it stays sync rather than splitting one override async and the rest not).
    """
    return _parse_color(_get_value_sync("embed_error_color"))


async def get_default_url() -> str:
    return await _get_value("default_url") or ""


def get_default_url_sync() -> str:
    """Sync counterpart to :func:`get_default_url`, for the same nav.py pipeline."""
    return _get_value_sync("default_url") or ""


async def get_lost_sector_image_url() -> str:
    return await _get_value("lost_sector_image_url") or ""


async def get_xur_image_url() -> str:
    return await _get_value("xur_image_url") or ""


async def get_alert_min_level() -> str:
    return await _get_value("alert_min_level") or "ERROR"


async def get_disable_bad_channels() -> bool:
    return await _get_enabled("disable_bad_channels")


async def get_log_channel_id() -> int:
    return int(await _get_value("log_channel_id") or 0)


async def get_alerts_channel_id() -> int:
    return int(await _get_value("alerts_channel_id") or 0)


async def get_followable_channel(feed: str) -> int:
    """The channel id ``feed`` posts to, 0 if dormant/unset. Async — for call-time
    reads."""
    slug = FOLLOWABLE_SLUGS.get(feed)
    if slug is None:
        return 0
    raw = await _get_value(slug)
    if raw is not None and raw != "":
        return int(raw)
    return int(cfg.followables.get(feed, 0))


async def get_followables() -> dict[str, int]:
    """Every followable's channel id, keyed by feed slug — the old
    ``cfg.followables``."""
    return {feed: await get_followable_channel(feed) for feed in FOLLOWABLE_SLUGS}


def get_followable_channel_sync(feed: str) -> int:
    """Import-time counterpart to :func:`get_followable_channel`.

    Only reflects the DB once :func:`preload` has run (both ``__main__`` entry points
    do this before their extensions package imports). Until then — e.g. under pytest,
    which imports extensions directly — falls back straight to :data:`cfg.followables`,
    exactly matching the pre-migration behaviour those import-time reads always had.

    An unknown ``feed`` (not in :data:`FOLLOWABLE_SLUGS`) is 0, same as the async
    version — there's no DB column to check, and no reason to trust a stale
    ``cfg.followables`` entry for a feed this module doesn't otherwise recognise.
    """
    slug = FOLLOWABLE_SLUGS.get(feed)
    if slug is None:
        return 0
    default = cfg.followables.get(feed, 0)
    _enabled, value = _raw(slug)
    if value is not None and value != "":
        return int(value)
    return default


def get_followables_sync() -> dict[str, int]:
    """Sync counterpart to :func:`get_followables` — every feed's channel id.

    Same deal as :func:`get_followable_channel_sync`: for sync call sites (a handful of
    formatting/lookup helpers, e.g. ``dd.common.utils.followable_name``) rather than a
    genuine import-time constraint. Prefer the async :func:`get_followables` when the
    call site is (or can be) async.
    """
    return {feed: get_followable_channel_sync(feed) for feed in FOLLOWABLE_SLUGS}


def followable_slugs() -> t.Iterable[str]:
    """Every feed slug this module resolves a channel id for."""
    return FOLLOWABLE_SLUGS.keys()


def followable_name(*, id: int, followables: dict[str, int] | None = None) -> str | int:
    """The configured feed slug for a followable channel id, or the id itself.

    Formerly ``dd.common.utils.followable_name`` reading ``cfg.followables`` directly;
    moved here so it reflects DB overrides too. Sync (log-line / status-display call
    sites), via :func:`get_followables_sync`.

    ``followables`` lets a caller resolving names in a loop (e.g. the mirror log's run
    list) pass one pre-built dict from a single :func:`get_followables_sync` call,
    instead of this rebuilding — and re-resolving every followable's channel id — on
    every single lookup.
    """
    if followables is None:
        followables = get_followables_sync()
    return next(
        (feed for feed, channel_id in followables.items() if channel_id == id),
        id,
    )


async def seed_followables_from_env() -> dict[str, int]:
    """One-time migration: backfill any followable-channel DB row that doesn't exist yet
    from :data:`cfg.followables` (the ``FOLLOWABLES`` env var).

    Idempotent — never touches a slug that already has a row, whether from a previous
    run of this or an edit on the Autopost Settings page since, so it is safe to run
    more than once (e.g. once per environment) and safe to run after the settings page
    has already been used.

    This is the bridge for retiring ``FOLLOWABLES``/``cfg.followables`` for good: run it
    once against each environment's DB (``uv run python -m dd.common.schemas
    --seed-followables``, or ``make seed-followables``), confirm every followable's
    channel on the Autopost Settings page matches what ``FOLLOWABLES`` has today, *then*
    remove the env var, delete ``cfg.followables``, and delete the ``cfg.followables``
    fallback in :func:`get_followable_channel` / :func:`get_followable_channel_sync`
    (search this module for ``cfg.followables`` — every remaining reference is that
    fallback, and becomes dead code once every environment has been seeded and the var
    is gone). Returns the slugs actually written, keyed by feed (empty if every
    followable already had a row — the common case after the first run anywhere).
    """
    written: dict[str, int] = {}
    for feed, channel_id in cfg.followables.items():
        slug = FOLLOWABLE_SLUGS.get(feed)
        if slug is None or not channel_id:
            continue
        existing = await schemas.AutoPostSettings.get_value(slug)
        if existing is not None:
            continue
        await schemas.AutoPostSettings.set_value(slug, str(int(channel_id)))
        written[feed] = int(channel_id)
    if written:
        # preload(), not invalidate(): this runs in an async context, so there's no
        # reason to leave the cache merely *marked* stale (which only the next async
        # getter would notice — a *_sync getter reads _cache directly and wouldn't see
        # this until something else happens to trigger a refresh). Refresh now.
        await preload()
    return written


# The env vars every non-followable setting here used to read, before cfg.py dropped
# them in the same migration that added this module (see the module docstring). Unlike
# followables, there's no cfg.<attr> fallback left to read for these — cfg.py no longer
# parses them at all — so seed_settings_from_env below reads os.environ directly.
_VALUE_ENV_VARS: dict[str, str] = {
    "default_url": "DEFAULT_URL",
    "alert_min_level": "ALERT_MIN_LEVEL",
    "lost_sector_image_url": "LOST_SECTOR_GIF_URL",
    "xur_image_url": "XUR_IMAGE_URL",
}
_COLOR_ENV_VARS: dict[str, str] = {
    "embed_default_color": "EMBED_DEFAULT_COLOR",
    "embed_error_color": "EMBED_ERROR_COLOR",
}
_CHANNEL_ID_ENV_VARS: dict[str, str] = {
    "log_channel_id": "LOG_CHANNEL_ID",
    "alerts_channel_id": "ALERTS_CHANNEL_ID",
}


async def seed_settings_from_env() -> dict[str, str]:
    """One-time migration: backfill every general (non-followable) setting that has no
    DB row yet, straight from the same env vars ``cfg.py`` used to read before this
    module existed — ``EMBED_DEFAULT_COLOR``/``ERROR_COLOR``, ``DEFAULT_URL``,
    ``ALERT_MIN_LEVEL``, ``DISABLE_BAD_CHANNELS``, ``LOG_CHANNEL_ID``,
    ``ALERTS_CHANNEL_ID``, ``LOST_SECTOR_GIF_URL``, ``XUR_IMAGE_URL``.

    Unlike :func:`seed_followables_from_env`, ``cfg.py`` no longer parses or exposes
    any of these, so this reads ``os.environ`` directly and replicates ``cfg.py``'s old
    parsing exactly: a hex color (``0x``-prefix optional), a case-insensitive bool
    (``true``/``1``/``yes``/``on``), a plain int/string otherwise. A var that was never
    set in this environment is simply skipped — the hardcoded :data:`_DEFAULTS` already
    matches its old env-var default, so an unconfigured row behaves the same either
    way. A var present but unparseable (a non-hex color, a non-integer channel id) is
    logged and skipped rather than raised, so one bad value can't abort the whole
    backfill.

    Idempotent — never overwrites an existing row, whether from a previous run of this
    or an edit on the Autopost Settings page since. Run once per environment (``make
    seed-settings``, alongside ``make seed-followables``) before removing these env
    vars for good — see :func:`seed_followables_from_env`'s docstring for the full
    cutover shape, which applies here too. Returns the slugs actually written, keyed by
    slug (empty if every setting already had a row — the common case after the first
    run anywhere).
    """
    written: dict[str, str] = {}

    async def _seed_value(slug: str, raw: str | None) -> None:
        if raw is None:
            return
        existing = await schemas.AutoPostSettings.get_value(slug)
        if existing is not None:
            return
        await schemas.AutoPostSettings.set_value(slug, raw)
        written[slug] = raw

    for slug, env_key in _VALUE_ENV_VARS.items():
        await _seed_value(slug, os.environ.get(env_key))

    for slug, env_key in _COLOR_ENV_VARS.items():
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        try:
            color_int = int(raw, 16)
        except ValueError:
            logger.warning("%s=%r is not a hex colour, skipping seed", env_key, raw)
            continue
        await _seed_value(slug, f"#{color_int:06X}")

    for slug, env_key in _CHANNEL_ID_ENV_VARS.items():
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        try:
            channel_id = int(raw)
        except ValueError:
            logger.warning("%s=%r is not an integer, skipping seed", env_key, raw)
            continue
        await _seed_value(slug, str(channel_id))

    disable_bad_channels_raw = os.environ.get("DISABLE_BAD_CHANNELS")
    if disable_bad_channels_raw is not None:
        existing_enabled = await schemas.AutoPostSettings.get_enabled(
            "disable_bad_channels"
        )
        if existing_enabled is None:
            value = disable_bad_channels_raw.strip().lower() in {
                "true",
                "1",
                "yes",
                "on",
            }
            await schemas.AutoPostSettings.set_enabled("disable_bad_channels", value)
            written["disable_bad_channels"] = str(value)

    if written:
        # See seed_followables_from_env's identical comment: preload() over
        # invalidate() so this (async) process's cache — including its *_sync readers
        # — is actually fresh on return, not just marked stale for later.
        await preload()
    return written
