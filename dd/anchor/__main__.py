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

"""Entry point for the anchor (secondary) Discord bot.

Run with ``python -OOm dd.anchor``. Wires up the hikari client and
lightbulb, loads the ``dd.anchor.extensions`` and starts the gateway.
"""

import hikari as h
import lightbulb as lb

import dd.anchor.extensions

from ..common import cfg, schemas, settings, utils
from ..common.auth import owner_check_error_handler, owner_only
from ..common.bot import CachedFetchBot
from ..common.discord_logging import (
    aclose_discord_logging,
    install_command_error_reporting,
    install_discord_logging,
)
from ..common.emoji_store import AppEmojiStore
from ..common.extension_loader import load_extensions_strict
from ..common.lifecycle import consume_exit_code
from . import web

bot = CachedFetchBot(
    token=cfg.discord_token_anchor,
    intents=h.Intents.ALL_UNPRIVILEGED,
)

# In lightbulb v3 the command client is a separate object from the hikari bot.
# The anchor bot is admin-only, so:
#  - default_enabled_guilds scopes every command (without its own ``guilds=``) to the
#    control guild (plus the test guild(s) in a test environment). ``guild_scope``
#    strips any guild id of 0 (lightbulb's global key) so the list never collapses
#    to a global (guild-0) registration. Context-menu commands and ``/post`` opt
#    back into Kyber explicitly via their own ``guilds=``.
#  - ``hooks=[owner_only]`` gates EVERY command to bot owners in the CHECKS step.
client = lb.client_from_app(
    bot,
    utils.guild_scope(*cfg.test_env, cfg.control_discord_server_id),
    hooks=[owner_only],
)

# Make the bot injectable as CachedFetchBot in addition to the hikari.GatewayBot
# registration lightbulb adds automatically.
client.di.registry_for(lb.di.Contexts.DEFAULT).register_value(CachedFetchBot, bot)
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
    # Before extensions import: a handful of them read a followable's channel id at
    # module level (see dd.common.settings' docstring), so the DB-backed settings cache
    # needs to be warm by the time load_extensions_strict imports them.
    await settings.preload()
    await load_extensions_strict(client, dd.anchor.extensions)
    await client.start()


@bot.listen(h.StartedEvent)
async def on_start_guild_count(_event: h.StartedEvent):
    bot.guild_count = len(await bot.rest.fetch_my_guilds())
    await utils.update_status(bot, bot.guild_count, bool(cfg.test_env))


@bot.listen(h.StartedEvent)
async def on_start_install_logging(_event: h.StartedEvent):
    await install_discord_logging(bot, bot_name="anchor")


@bot.listen(h.StartedEvent)
async def on_start_web_app(_event: h.StartedEvent):
    # One persistent HTTP server for the OAuth callback + rotation editor. Extensions
    # (loaded on StartingEvent) have already contributed their routes by now.
    #
    # Stash the bot BEFORE the first socket accept, not from a listener of its own: the
    # routes (and the auth middleware) reach it through web.get_bot()/require_bot(), and
    # StartedEvent listeners run concurrently — so a separate listener would leave a
    # window in which the server is up and the bot is not.
    web.stash_bot(bot)
    await web.start()


@bot.listen(h.StoppingEvent)
async def on_stopping_close_logging(_event: h.StoppingEvent):
    await aclose_discord_logging()


@bot.listen(h.StoppingEvent)
async def on_stopping_web_app(_event: h.StoppingEvent):
    await web.stop()


@bot.listen(h.GuildJoinEvent)
async def on_guild_add(_event: h.GuildJoinEvent):
    bot.guild_count += 1
    await utils.update_status(bot, bot.guild_count, bool(cfg.test_env))


@bot.listen(h.GuildLeaveEvent)
async def on_guild_rm(_event: h.GuildLeaveEvent):
    bot.guild_count -= 1
    await utils.update_status(bot, bot.guild_count, bool(cfg.test_env))


bot.run()
# Exit on the main thread with the code requested by a lifecycle command (0 if none).
# This is reliable where a SystemExit raised inside an interaction-callback task is not.
raise SystemExit(consume_exit_code())
