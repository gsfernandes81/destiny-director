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

import datetime as dt
from typing import override

import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import settings
from ...common.utils import accumulate
from .. import utils
from ..nav import NavigatorView, NavPages
from .autoposts import follow_control_command_maker

loader = lb.Loader()

# Set IGNORE to False to enable the module
IGNORE = True

# Followable channel from which to pull messages for the command and autoposts
FOLLOWABLE_CHANNEL = (
    123456789  # settings.get_followable_channel_sync(<"something-here">)
)

# CODE FOR PAGES BELOW. CAN BE SAFELY REMOVED IF ONLY AUTOPOSTS ARE NEEDED

# Reference date and update period for the pages
REFERENCE_DATE = dt.datetime(2023, 7, 14, 17, tzinfo=dt.UTC)
UPDATE_PERIOD = dt.timedelta(days=7)

pages: NavPages


class Pages(NavPages):
    @override
    def preprocess_messages(self, messages: list[h.Message]) -> HMessage:
        if not messages:
            return self.no_data_message

        for m in messages:
            m.embeds = utils.filter_discord_autoembeds(m)

        msg_proto = (
            accumulate([HMessage.from_message(m) for m in messages])
            .merge_content_into_embed()
            .merge_attachements_into_embed(default_url=settings.get_default_url_sync())
        )

        return msg_proto


if not IGNORE:

    @loader.listener(h.StartedEvent)
    async def on_start(event: h.StartedEvent):
        global pages
        pages = await Pages.from_channel(
            event.app,
            FOLLOWABLE_CHANNEL,
            reference_date=REFERENCE_DATE,
            period=UPDATE_PERIOD,
        )

    # CODE FOR PAGES ABOVE. CAN BE SAFELY REMOVED IF ONLY AUTOPOSTS ARE NEEDED

    class SlashCommand(
        lb.SlashCommand,
        name="xur",
        description="Find out what Xur has and where Xur is",
    ):
        @lb.invoke
        async def invoke(self, ctx: lb.Context):
            navigator = NavigatorView(pages=pages)
            await navigator.send(ctx)

    loader.command(SlashCommand)

    follow_control_command_maker(FOLLOWABLE_CHANNEL, "xur", "Xur", "Xur auto posts")
