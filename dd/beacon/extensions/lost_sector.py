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
import typing as t
from typing import override

import lightbulb as lb

from dd.hmessage import HMessage

from ...common.bot import ServerEmojiEnabledBot
from ...common.lost_sector import format_post, load_rotation
from ..nav import (
    NavPages,
    make_navigator_command,
    setup_nav_pages,
)
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2023, 7, 20, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = resolve_followable_channel("lost_sector")


class SectorMessages(NavPages):
    # preprocess_messages is inherited: this navigator is cv2=True, so the base method
    # converts any legacy embed pages in history to Components V2. Only lookahead is
    # overridden (it generates future pages, already CV2 via format_post).

    @override
    async def lookahead(self, after: dt.datetime) -> dict[dt.datetime, HMessage]:
        start_date = after
        bot = t.cast(ServerEmojiEnabledBot, self.bot)
        sector_on = await load_rotation(buffer=1)

        lookahead_dict = {}

        # ``after`` is index 1 (tomorrow); the navigator lets the user reach index
        # lookahead_len, so generate indices 1..lookahead_len (lookahead_len dates).
        # A short range left the last reachable forward page ("+lookahead_len days")
        # permanently empty ("No data here").
        for date in [start_date + self.period * n for n in range(self.lookahead_len)]:
            try:
                sectors = sector_on(date)
            except KeyError:
                # A KeyError will be raised if TBC is selected for the google sheet
                # In this case, we will just return a message saying that there
                # is no data
                lookahead_dict[date] = self.no_data_message
            else:
                lookahead_dict[date] = await format_post(
                    bot=bot,
                    sectors=sectors,
                    date=date,
                    emoji_dict=bot.emoji,
                )

        return lookahead_dict


_pages = setup_nav_pages(
    loader,
    pages_cls=SectorMessages,
    feed="lost_sector",
    history_len=14,
    lookahead_len=7,
    period=dt.timedelta(days=1),
    reference_date=REFERENCE_DATE,
    cv2=True,
)

ls_group = lb.Group("ls", "Find out about today's lost sector")
ls_group.register(
    make_navigator_command(
        _pages, name="today", description="Find out about today's lost sector"
    )
)

ls_group_2 = lb.Group("lost", "Find out about today's lost sector")
ls_group_2.register(
    make_navigator_command(
        _pages, name="sector", description="Find out about today's lost sector"
    )
)

loader.command(ls_group)
loader.command(ls_group_2)

follow_control_command_maker("lost_sector", "Lost Sector auto posts")
