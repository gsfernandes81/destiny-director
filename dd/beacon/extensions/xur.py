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

from ...common.components import build_container
from ..nav import make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2023, 7, 14, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = resolve_followable_channel("xur")

_pages = setup_nav_pages(
    loader,
    feed="xur",
    display_name="Xûr",
    history_len=12,
    period=dt.timedelta(days=7),
    reference_date=REFERENCE_DATE,
    cv2=True,
    no_data_message=HMessage(
        components=[
            build_container(
                [
                    "Xûr arrives at the Tower (Bazaar) every *Friday at reset* "
                    "(<t:1734109200:t>) and departs on *Tuesday at reset*."
                ]
            )
        ]
    ),
)

loader.command(
    make_navigator_command(
        _pages,
        name="xur",
        description="Find out what Xur has and where Xur is",
    )
)

follow_control_command_maker("xur", "Xur auto posts")
