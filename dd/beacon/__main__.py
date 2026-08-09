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

"""Entry point for the beacon (main) Discord bot.

Run with ``python -OOm dd.beacon``. Wires up the hikari client and
lightbulb, loads the ``dd.beacon.extensions`` and starts the gateway.
"""

import asyncio
import logging

import hikari as h
import lightbulb as lb

import dd.beacon.extensions
import dd.beacon.extensions.user_commands
from dd.beacon.extensions.statistics import track_command_usage

from ..common import cfg, schemas, settings
from ..common.auth import owner_check_error_handler
from ..common.bot import CachedFetchBot, ServerEmojiEnabledBot
from ..common.db_migrations import run_migrations
from ..common.discord_logging import (
    aclose_discord_logging,
    install_command_error_reporting,
    install_discord_logging,
)
from ..common.emoji_store import AppEmojiStore
from ..common.extension_loader import load_extensions_strict
from ..common.lifecycle import apply_oom_score_adj, consume_exit_code
from . import utils as beacon_utils
from .mirror_worker import mirror_worker

# Before anything allocates in earnest, so the preference is in place for the whole
# process lifetime: raise this process's OOM-kill preference if one is configured. The
# container baseline stays at 0 so the kernel reaps a bot rather than supervisord.
apply_oom_score_adj(cfg.oom_score_adj)

# Live set of channel ids the gateway cache is allowed to keep (mirror sources +
# destinations). Populated from the DB before the gateway connects and refreshed
# periodically; held by reference by the scoped cache below. See
# dd.common.scoped_cache for why beacon scopes its channel cache at all.
_relevant_channel_ids: set[int] = set()


async def _refresh_relevant_channel_ids() -> None:
    """Refresh ``_relevant_channel_ids`` in place from the mirror config.

    Grows-then-shrinks (``|=`` then ``&=``) so a currently-relevant channel is never
    momentarily absent from the set while a ``GUILD_CREATE`` burst is being filtered.
    """
    ids = await schemas.MirroredChannel.fetch_all_channel_ids()
    # In-place (no rebind, so no ``global`` needed): add the current ids, then drop only
    # the ones no longer present — an id that is still relevant is never momentarily
    # absent while a GUILD_CREATE burst is being filtered.
    _relevant_channel_ids.update(ids)
    _relevant_channel_ids.difference_update(_relevant_channel_ids - ids)


bot = ServerEmojiEnabledBot(
    token=cfg.discord_token_beacon,
    intents=h.Intents.ALL_UNPRIVILEGED | h.Intents.MESSAGE_CONTENT,
    max_rate_limit=600,
    emoji_servers=[cfg.kyber_discord_server_id],
    # Beacon is in thousands of guilds but only reads a handful of channels. Trim the
    # gateway cache to what the code actually reads: keep guilds + roles (permission
    # checks resolve guild.get_roles()), the bot's own member only (only_my_member),
    # messages (bounded, mirror source reads) and channels — but scope the channel
    # cache to mirror sources/destinations via scoped_channel_ids. Everything else
    # (emojis, voice states, invites, stickers, threads, presences) is never read, so
    # it is dropped. All reads are fetch-through with a REST fallback (CachedFetchBot).
    cache_settings=h.impl.CacheSettings(
        components=(
            h.api.CacheComponents.GUILDS
            | h.api.CacheComponents.GUILD_CHANNELS
            | h.api.CacheComponents.ROLES
            | h.api.CacheComponents.MEMBERS
            | h.api.CacheComponents.MESSAGES
            | h.api.CacheComponents.ME
            | h.api.CacheComponents.DM_CHANNEL_IDS
        ),
        only_my_member=True,
        max_messages=100,
    ),
    scoped_channel_ids=_relevant_channel_ids,
)

client = lb.client_from_app(
    bot,
    cfg.test_env or (),  # Lightbulb enabled guilds
    hooks=[track_command_usage],  # client-wide command-usage counter
)

# Make the bot injectable as CachedFetchBot (its concrete subclass type) in
# addition to the hikari.GatewayBot registration lightbulb adds automatically.
client.di.registry_for(lb.di.Contexts.DEFAULT).register_value(CachedFetchBot, bot)
# Make the live command client injectable so the dynamic user-command system can
# reach it to (re)register commands at runtime.
client.di.registry_for(lb.di.Contexts.DEFAULT).register_value(lb.Client, client)
# The pure-lazy application-emoji store for Destiny item icons (uploaded on first use,
# LRU-evicted near the 2000/app cap). Self-warms on StartedEvent; injectable so posts
# can resolve an item to an inline ``<:emoji:id>`` from this bot's own app store.
client.di.registry_for(lb.di.Contexts.DEFAULT).register_value(
    AppEmojiStore, AppEmojiStore(bot)
)

# Render owner-gate rejections ephemerally, ahead of the catch-all alert reporter so
# they never page the alerts channel.
client.error_handler(owner_check_error_handler)

# Surface any otherwise-unhandled command failure to the alerts channel, labelled
# with the command that failed.
install_command_error_reporting(client)


@bot.listen(h.StartingEvent)
async def on_starting_event(_event: h.StartingEvent):
    await schemas.wait_for_db()
    # Bring the schema to head before anything reads a table. Ordered here, in
    # straight-line Python, rather than by a shell entrypoint — supervisord (PID 1 in
    # the container) has no dependency mechanism to sequence a migration step ahead of
    # the bot. Fatal on failure; see dd/common/db_migrations.py.
    await run_migrations()
    # Warm the scoped-cache channel set from the DB *before* the gateway connects, so
    # the initial GUILD_CREATE burst is filtered against a populated set (an empty set
    # would refuse every channel until the first refresh).
    await _refresh_relevant_channel_ids()
    # Also before extensions import: a handful of them read a followable's channel id
    # at module level (see dd.common.settings' docstring), so the DB-backed settings
    # cache needs to be warm by the time load_extensions_strict imports them.
    await settings.preload()
    await load_extensions_strict(client, dd.beacon.extensions)
    await dd.beacon.extensions.user_commands.resync_user_commands(client, sync=False)
    await client.start()


@bot.listen(h.StartedEvent)
async def on_start_refresh_relevant_channels(_event: h.StartedEvent):
    # Keep the scoped-cache channel set current with the mirror config so channels for
    # mirrors added at runtime are cached on the next gateway reconnect. Cache misses in
    # between are harmless (fetch-through REST fallback), so a coarse interval is fine.
    async def _loop(interval: int = 300):
        while True:
            await asyncio.sleep(interval)
            try:
                await _refresh_relevant_channel_ids()
            except Exception:
                logging.exception("Failed to refresh scoped-cache channel set")

    _ = asyncio.create_task(_loop())


@bot.listen(h.StartedEvent)
async def on_start_reconcile_feed_channels(_event: h.StartedEvent):
    # A followable's channel is a setting edited on anchor's Autopost Settings page, so
    # it can change while this bot is running. The readers that hold something *built*
    # from that channel — the nav pages, free games' cached message — used to be built
    # once here and never revisited, so picking or moving a channel did nothing until
    # somebody restarted beacon, with nothing on the page to say so.
    #
    # Polled rather than pushed: the two bots share only the database, so there is no
    # event to listen for. A tick over an unchanged config does no work beyond one
    # cached settings read (see utils.reconcile_feed_channels), and rebuilding reads
    # channel history, so it only ever happens on an actual change. The interval is the
    # worst-case delay between saving a channel and beacon serving it.
    #
    # The dormancy sweep rides the same tick — it reads the same cached settings, and
    # being periodic rather than one-shot is what lets it notice a feed *leaving* the
    # dormant state now that a channel can be picked while the bots are up.
    async def _sweep():
        try:
            await beacon_utils.sweep_dormant_feeds()
        except Exception:
            logging.exception("Failed to sweep for dormant feeds")

    async def _loop(interval: int = 60):
        # Swept before the first sleep, so a bot that boots with a feed unconfigured
        # says so now rather than a tick later. The reconciler is not: the readers it
        # compares against are still being built by their own StartedEvent listeners.
        await _sweep()
        while True:
            await asyncio.sleep(interval)
            await _sweep()
            try:
                await beacon_utils.reconcile_feed_channels()
            except Exception:
                logging.exception("Failed to reconcile feed source channels")

    _ = asyncio.create_task(_loop())


@bot.listen(h.StartedEvent)
async def on_start_install_logging(_event: h.StartedEvent):
    await install_discord_logging(bot, bot_name="beacon")


@bot.listen(h.StoppingEvent)
async def on_stopping_event(_event: h.StoppingEvent):
    # Drain the mirror worker *before* disposing the DB engine (its in-flight flush
    # needs a live engine): stop() lets the current batch finish and flush so already-
    # sent rows are recorded and never re-sent on restart, force-cancelling if the drain
    # stalls (see MirrorWorker.stop). StoppingEvent fires on a clean shutdown and on
    # SIGINT/SIGTERM (hikari's signal handlers, enabled below) — i.e. on a Railway
    # redeploy — so this drain runs on every graceful exit; only a SIGKILL skips it.
    await mirror_worker.stop()
    await aclose_discord_logging()
    await schemas.db_engine.dispose()


# enable_signal_handlers=True (hikari's main-thread default, made explicit) installs the
# SIGINT/SIGTERM handlers that trigger a clean shutdown → StoppingEvent → the drain.
bot.run(enable_signal_handlers=True)
# Exit on the main thread with the code requested by a lifecycle command (0 if none).
# This is reliable where a SystemExit raised inside an interaction-callback task is not.
raise SystemExit(consume_exit_code())
