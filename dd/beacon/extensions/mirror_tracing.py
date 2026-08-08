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

"""Tracks non-legacy mirror relationships as Discord crossposts arrive.

Listens for crossposted messages whose source is a followable channel and
records the implied source -> destination mirror in the database so the mirror
module can replay edits and deletes to it.
"""

import asyncio as aio
import logging
from collections import defaultdict
from types import TracebackType
from typing import override

import hikari as h
import lightbulb as lb

from ...common import cfg, settings
from ...common.schemas import MirroredChannel

loader = lb.Loader()


class TimedSemaphore(aio.Semaphore):
    """Semaphore that allows no more than ``value`` acquisitions per ``period`` seconds.

    Bounds this module's DB writes + API calls to stay well within Discord rate limits.
    (The mirror fan-out itself uses the token-bucket rate limiter in ``mirror_core``,
    not this; this lives here because tracing is its only user.)
    """

    def __init__(self, value: int = 30, period: int = 1):
        super().__init__(value)
        self.period = period

    async def arelease(self) -> None:
        """Delay release until the period has passed."""
        await aio.sleep(self.period)
        super().release()

    @override
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.arelease()


# Tracing is non-critical, so keep the database loading and api
# ratelimit consumption to a minimum
speed_limit = TimedSemaphore(value=1, period=1)

followable_servers_list = (cfg.kyber_discord_server_id, cfg.control_discord_server_id)
non_legacy_mirrors = defaultdict(list)


def forget_traced_mirror(src_ch_id: int, dest_ch_id: int) -> None:
    """Drop a traced ``src -> dest`` mirror from the in-memory cache.

    Called when a non-legacy mirror is disabled/removed so ``non_legacy_mirrors``
    does not (a) grow without bound and (b) go stale relative to the DB — the
    ``message_tracer`` gate (``channel_id in non_legacy_mirrors``) would otherwise
    keep re-adding a mirror a user just removed. The source key is dropped once its
    dest list empties.
    """
    dests = non_legacy_mirrors.get(src_ch_id)
    if not dests:
        return
    while dest_ch_id in dests:
        dests.remove(dest_ch_id)
    if not dests:
        del non_legacy_mirrors[src_ch_id]


@loader.listener(h.MessageCreateEvent)
async def message_tracer(event: h.MessageCreateEvent):
    if not event.message.flags.all(h.MessageFlag.IS_CROSSPOST):
        # Not a crosspost
        return

    if not (
        event.message.message_reference
        and event.message.message_reference.guild_id in followable_servers_list
        and event.message.message_reference.channel_id in non_legacy_mirrors
    ):
        # Not a crosspost from our servers
        return

    src_ch_id = event.message.message_reference.channel_id
    dest_ch_id = event.message.channel_id
    dest_guild_id = event.message.guild_id

    if dest_ch_id in non_legacy_mirrors[src_ch_id]:
        # Non-Legacy mirror already being tracked
        return

    try:
        async with speed_limit:
            if dest_guild_id is None:
                # Crossposts always originate in a guild; skip if somehow absent.
                return
            await MirroredChannel.add_mirror(
                src_ch_id,
                dest_ch_id,
                dest_guild_id,
                legacy=False,
                enabled=True,
            )
        non_legacy_mirrors[src_ch_id].append(dest_ch_id)
    except Exception as e:
        e.add_note("Error adding traced mirror")
        logging.exception(e)


@loader.listener(h.StartedEvent)
async def on_start(event: h.StartedEvent):
    global non_legacy_mirrors

    retry_count = 0

    for followable in (await settings.get_followables()).values():
        while True:
            try:
                non_legacy_mirrors[followable] = await MirroredChannel.fetch_dests(
                    followable, legacy=False, enabled=True
                )
            except Exception as e:
                e.add_note("Error fetching non-legacy mirrors for tracing")
                logging.exception(e)
                retry_count += 1
                await aio.sleep(2**retry_count)
            else:
                break
