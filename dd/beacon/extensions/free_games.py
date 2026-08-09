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

from ...common import (
    feeds as dd_feeds,
    settings,
)
from ...common.bot import CachedFetchBot
from .. import utils
from .autoposts import follow_control_command_maker

loader = lb.Loader()


HELP_STRING = "See the current free games on The Epic Store, etc"

# The most recent message in the followable channel, which /free games repeats. None
# until a StartedEvent has fetched one — an unset or unreachable channel leaves it that
# way, and `unavailable_reason` says which so the command can answer for itself instead
# of raising NameError at whoever ran it.
last_message_in_channel: h.PartialMessage | None = None
last_message_in_channel_id: int = 0
unavailable_reason: str | None = None
#: The channel ``last_message_in_channel`` was read out of, 0 when there is none. What
#: the reconciler compares against the configured id — see ``utils.watch_feed_channel``.
last_message_channel_id: int = 0


def _forget_cached_message() -> None:
    """Stop serving whatever was cached, because the source is no longer usable.

    Zeroing only ``last_message_channel_id`` was not enough: the command gates solely
    on ``last_message_in_channel``, so a feed moved to a channel beacon cannot open
    went on repeating the *old* channel's post while ``unavailable_reason`` said the
    channel was unreachable — the same "looks right, and is not" failure the moved-to-
    an-empty-channel branch below exists to prevent, one state along.
    """
    global last_message_in_channel, last_message_in_channel_id, last_message_channel_id
    last_message_in_channel = None
    last_message_in_channel_id = 0
    last_message_channel_id = 0


async def refresh_message_for_command(bot: CachedFetchBot, *, alert: bool = True):
    global last_message_in_channel_id
    global last_message_in_channel
    global last_message_channel_id
    global unavailable_reason

    # An unreachable channel alerts, except on a reconciler retry of one already known
    # bad (``alert``). An *unset* one no longer alerts from here at all — that is
    # sweep_dormant_feeds' to report, once, for every reader of the feed at a time.
    channel, reason = await utils.open_feed_source(
        "free_games", bot.fetch_channel, alert_when_unreachable=alert
    )
    if channel is None:
        unavailable_reason = reason
        _forget_cached_message()
        return
    if not isinstance(channel, h.TextableChannel):
        unavailable_reason = utils.FEED_UNREACHABLE
        _forget_cached_message()
        if alert:
            logging.critical(
                "Free Games followable channel %s is not a textable channel — /free "
                "games will answer 'unavailable' until it's fixed on the Autopost "
                "Settings page.",
                channel.id,
            )
        return
    moved = bool(last_message_channel_id) and int(channel.id) != last_message_channel_id
    # Recorded whether or not the history read below yields anything: it is what the
    # reconciler compares against, and an empty (but perfectly reachable) channel must
    # not have its history re-read on every tick.
    last_message_channel_id = int(channel.id)

    newest: h.PartialMessage | None = None
    async for message in channel.fetch_history():
        newest = message
        break

    if newest is not None:
        last_message_in_channel = newest
        last_message_in_channel_id = newest.id
        unavailable_reason = None
    elif moved:
        # An empty channel we have just been pointed at. Drop what was cached from the
        # old one, or /free games would go on repeating a post out of a channel it no
        # longer reads — worse than saying it has nothing, because it looks right.
        # Only on a move: a same-channel refresh (the repeated message was deleted)
        # keeps what it had, which is how that path has always behaved.
        last_message_in_channel = None
        last_message_in_channel_id = 0
        unavailable_reason = utils.FEED_EMPTY


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


#: Set on StartedEvent, so the reconciler below has something to fetch with.
_bot: CachedFetchBot | None = None


@loader.listener(h.StartedEvent)
async def on_start(event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED):
    global _bot
    _bot = bot
    await refresh_message_for_command(bot)


#: The channel the last open was attempted against; -1 = never. Only used to decide
#: whether to alert — see _on_channel_change.
_last_attempt: int = -1


async def _on_channel_change(channel_id: int) -> None:
    """Re-read the history when the feed is pointed at a different channel.

    The listeners above already watch whichever channel is configured *now*, but they
    only ever bring news of a *new* message — so moving the feed to a channel that
    already has posts in it left ``/free games`` repeating the old channel's message
    (or answering "unavailable") until somebody restarted the bot.

    Alerting is keyed on the channel, not on whether the feed was previously healthy,
    which is the rule ``nav.NavPagesHolder.reopen`` follows and the one the reconcile
    tests pin: a still-broken channel retried on every tick must stay quiet, but moving
    to a *different* broken channel is new information and has to reach the owners.
    Keying it on the previous state got the first half right and the second wrong —
    and the settings page validates a channel with *anchor's* bot, so a channel beacon
    cannot read passes validation and lands here looking fine.
    """
    global _last_attempt
    if _bot is None:
        return  # not started yet; on_start does the first read
    alert = channel_id != _last_attempt
    _last_attempt = channel_id
    await refresh_message_for_command(_bot, alert=alert)


utils.watch_feed_channel(
    "free_games", lambda: last_message_channel_id, _on_channel_change
)


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
                    dd_feeds.FEEDS["free_games"].display_name,
                    unavailable_reason or "the bot is still starting up",
                )
            )
            return
        await ctx.respond(
            **(HMessage.from_message(last_message_in_channel).to_message_kwargs())
        )


loader.command(slash_command_group)

follow_control_command_maker("free_games", HELP_STRING)
