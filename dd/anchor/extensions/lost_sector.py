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

import aiocron
import hikari as h
import lightbulb as lb

from ...common import cfg, schemas, settings
from ...common.bot import CachedFetchBot
from ...common.components import cv2_error, cv2_notice, cv2_success, respond_cv2
from ...common.lost_sector import format_post
from ...common.utils import guild_scope
from ..autopost import Feed, discord_announcer, register_feed

logger = logging.getLogger(__name__)

loader = lb.Loader()


class LsUpdate(
    lb.MessageCommand,
    name="ls_update",
    description="Update a lost sector post",
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        """Correct a mistake in the lost sector announcement"""
        msg_to_update: h.Message = self.target

        if not await schemas.AutoPostSettings.get_lost_sector_enabled():
            await respond_cv2(
                ctx, cv2_error("Please enable autoposts before using this command.")
            )
            return

        logger.info("Correcting posts")

        initial = await ctx.respond(
            components=[cv2_notice("Updating post now…")],
            flags=h.MessageFlag.IS_COMPONENTS_V2,
            ephemeral=True,
        )

        message = await format_post(bot=bot)
        await msg_to_update.edit(**message.to_message_kwargs())
        await ctx.edit_response(initial, components=[cv2_success("Post updated")])


@loader.listener(h.StartedEvent)
async def on_start_schedule_autoposts(
    event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
):
    # Run every day at 17:00 UTC
    @aiocron.crontab("0 17 * * *", start=True)
    # Use below crontab for testing to post every minute
    # @aiocron.crontab("* * * * *", start=True)
    async def autopost_ls():
        await discord_announcer(
            bot,
            channel_id=await settings.get_followable_channel("lost_sector"),
            check_enabled=True,
            enabled_check_coro=schemas.AutoPostSettings.get_lost_sector_enabled,
            construct_message_coro=format_post,
        )


# The ls_update context-menu command appears in the Kyber server in addition to the
# client default (control + test_env).
loader.command(
    LsUpdate,
    guilds=guild_scope(
        *cfg.test_env,
        cfg.control_discord_server_id,
        cfg.kyber_discord_server_id,
    ),
)


# Contribute this feed's producer wiring to the web feed page (Preview / Send now).
# Keyed on the followable name — the old "ls" abbreviation was a command-name artefact.
register_feed(
    Feed(
        name="lost_sector",
        message_constructor_coro=format_post,
        message_announcer_coro=discord_announcer,
    )
)
