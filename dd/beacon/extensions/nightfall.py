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
import logging
from typing import override

import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import settings
from ...common.utils import accumulate
from .. import utils
from ..nav import NavPages, make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2023, 7, 18, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = resolve_followable_channel("weekly_nightfall", "Weekly Nightfall")


class NightfallPages(NavPages):
    @override
    def preprocess_messages(self, messages: list[h.Message]) -> HMessage:
        if not messages:
            return self.no_data_message

        for m in messages:
            m.embeds = utils.filter_discord_autoembeds(m)

        try:
            msg_proto = (
                accumulate([HMessage.from_message(m) for m in messages])
                .merge_content_into_embed()
                .merge_attachements_into_embed(
                    default_url=settings.get_default_url_sync()
                )
            )
        except ValueError as e:
            e.add_note(
                "Issue while processing messages with ids:"
                + ", ".join(str(m.id) for m in messages)
            )
            logging.exception(e)
            msg_proto = HMessage(
                embeds=[
                    h.Embed(
                        title="Error",
                        color=settings.get_embed_error_color_sync(),
                        description="There was an issue processing the information "
                        "for this week.",
                    )
                ],
            )

        return msg_proto


_pages = setup_nav_pages(
    loader,
    pages_cls=NightfallPages,
    feed="weekly_nightfall",
    display_name="Weekly Nightfall",
    history_len=12,
    period=dt.timedelta(days=7),
    reference_date=REFERENCE_DATE,
)

loader.command(
    make_navigator_command(
        _pages,
        name="nightfall",
        description="Find out about this weeks nightfall",
    )
)

follow_control_command_maker(
    "weekly_nightfall",
    "nightfall",
    "Nightfall",
    "Nightfall weekly auto posts",
)
