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

from dd.hmessage import HMessage

from ...common import settings
from ..nav import make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2024, 1, 9, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = settings.get_followable_channel_sync("trials")

_pages = setup_nav_pages(
    loader,
    followable_channel=FOLLOWABLE_CHANNEL,
    history_len=12,
    period=dt.timedelta(days=7),
    reference_date=REFERENCE_DATE,
    suppress_content_autoembeds=False,
    no_data_message=HMessage(content="Trials is unavailable for this week."),
)

loader.command(
    make_navigator_command(
        _pages,
        name="trials",
        description="Find out about this weeks Trials weapon and map",
        allow_start_on_blank_page=True,
        display_date_offset=dt.timedelta(days=3),
    )
)

follow_control_command_maker(
    FOLLOWABLE_CHANNEL, "trials", "Trials", "Trials weekly auto posts"
)
