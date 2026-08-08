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


import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import settings
from ...common.bot import CachedFetchBot
from .autoposts import follow_control_command_maker

loader = lb.Loader()

# Followable channel from which to pull messages for the command and autoposts
FOLLOWABLE_CHANNEL = settings.get_followable_channel_sync("free_games")

HELP_STRING = "See the current free games on The Epic Store, etc"

last_message_in_channel: h.PartialMessage
last_message_in_channel_id: int


async def refresh_message_for_command(bot: CachedFetchBot):
    global last_message_in_channel_id
    global last_message_in_channel

    channel = await bot.fetch_channel(FOLLOWABLE_CHANNEL)
    if not isinstance(channel, h.TextableChannel):
        raise TypeError("Free games followable channel is not textable")
    async for message in channel.fetch_history():
        last_message_in_channel = message
        last_message_in_channel_id = message.id
        break


@loader.listener(h.MessageCreateEvent)
async def on_message_create(event: h.MessageCreateEvent):
    global last_message_in_channel
    global last_message_in_channel_id

    if event.channel_id == FOLLOWABLE_CHANNEL:
        last_message_in_channel = event.message
        last_message_in_channel_id = event.message.id


@loader.listener(h.MessageUpdateEvent)
async def on_message_update(event: h.MessageUpdateEvent):
    global last_message_in_channel
    global last_message_in_channel_id

    if (
        event.channel_id == FOLLOWABLE_CHANNEL
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
        event.channel_id == FOLLOWABLE_CHANNEL
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
        await ctx.respond(
            **(HMessage.from_message(last_message_in_channel).to_message_kwargs())
        )


loader.command(slash_command_group)

follow_control_command_maker(
    FOLLOWABLE_CHANNEL, "free_games", "Free Games", HELP_STRING
)
