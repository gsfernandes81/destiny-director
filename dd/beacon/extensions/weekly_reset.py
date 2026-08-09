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

import lightbulb as lb

from ..nav import ResetPages, make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2023, 7, 18, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = resolve_followable_channel("weekly_reset")

_pages = setup_nav_pages(
    loader,
    pages_cls=ResetPages,
    feed="weekly_reset",
    history_len=12,
    period=dt.timedelta(days=7),
    reference_date=REFERENCE_DATE,
)

weekly_reset_command_group = lb.Group("weekly", "Weekly Reset")
weekly_reset_command_group.register(
    make_navigator_command(
        _pages,
        name="reset",
        feed="weekly_reset",
        description="Find out about this weeks reset",
    )
)

loader.command(weekly_reset_command_group)

follow_control_command_maker("weekly_reset", "Weekly Reset auto posts")
