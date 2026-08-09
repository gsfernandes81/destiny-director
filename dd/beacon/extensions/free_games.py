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


import logging

import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import settings
from ...common.bot import CachedFetchBot
from .. import utils
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

# Read once at import purely for the boot-time alert on an unconfigured feed; the
# listeners and the command below resolve the channel live (see _followable_channel).
FOLLOWABLE_CHANNEL = resolve_followable_channel("free_games")

HELP_STRING = "See the current free games on The Epic Store, etc"

# The most recent message in the followable channel, which /free games repeats. None
# until a StartedEvent has fetched one — an unset or unreachable channel leaves it that
# way, and `unavailable_reason` says which so the command can answer for itself instead
# of raising NameError at whoever ran it.
last_message_in_channel: h.PartialMessage | None = None
last_message_in_channel_id: int = 0
unavailable_reason: str | None = None


async def refresh_message_for_command(bot: CachedFetchBot):
    global last_message_in_channel_id
    global last_message_in_channel
    global unavailable_reason

    # alert_when_unset=False: this runs again whenever the repeated message is
    # deleted, and resolve_followable_channel already paged once at import for the
    # unset state — see open_feed_source. An unreachable channel does still alert.
    channel, reason = await utils.open_feed_source(
        "free_games", "Free Games", bot.fetch_channel, alert_when_unset=False
    )
    if channel is None:
        unavailable_reason = reason
        return
    if not isinstance(channel, h.TextableChannel):
        unavailable_reason = utils.FEED_UNREACHABLE
        logging.critical(
            "Free Games followable channel %s is not a textable channel — /free games "
            "will answer 'unavailable' until it's fixed on the Autopost Settings page.",
            channel.id,
        )
        return
    async for message in channel.fetch_history():
        last_message_in_channel = message
        last_message_in_channel_id = message.id
        unavailable_reason = None
        break


def _followable_channel() -> int:
    """The free-games channel as configured *now* (a cached read, no DB round trip).

    The listeners below fire on every message the bot can see, so they resolve here
    rather than closing over the import-time id: a channel set or changed on the
    settings page otherwise wouldn't be watched until the next restart.
    """
    return settings.get_followable_channel_sync("free_games")


@loader.listener(h.MessageCreateEvent)
async def on_message_create(event: h.MessageCreateEvent):
    global last_message_in_channel
    global last_message_in_channel_id

    if event.channel_id == _followable_channel():
        last_message_in_channel = event.message
        last_message_in_channel_id = event.message.id


@loader.listener(h.MessageUpdateEvent)
async def on_message_update(event: h.MessageUpdateEvent):
    global last_message_in_channel
    global last_message_in_channel_id

    if (
        event.channel_id == _followable_channel()
        and event.message.id == last_message_in_channel_id
    ):
        last_message_in_channel = event.message
        last_message_in_channel_id = event.message.id


@loader.listener(h.MessageDeleteEvent)
async def on_message_delete(
    event: h.MessageDeleteEvent, bot: CachedFetchBot = lb.di.INJECTED
):
    global last_message_in_channel
    global last_message_in_channel_id
    if (
        event.channel_id == _followable_channel()
        and event.message_id == last_message_in_channel_id
    ):
        await refresh_message_for_command(bot)


@loader.listener(h.StartedEvent)
async def on_start(event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED):
    await refresh_message_for_command(bot)


slash_command_group = lb.Group(
    "free", "See the current free games on The Epic Store, etc"
)


@slash_command_group.register
class FreeGames(lb.SlashCommand, name="games", description=HELP_STRING):
    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        if last_message_in_channel is None:
            await ctx.respond(
                await utils.feed_unavailable_embed(
                    "Free Games",
                    unavailable_reason or "the bot is still starting up",
                )
            )
            return
        await ctx.respond(
            **(HMessage.from_message(last_message_in_channel).to_message_kwargs())
        )


loader.command(slash_command_group)

follow_control_command_maker("free_games", HELP_STRING)
