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

from ..nav import make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2023, 7, 18, 17, tzinfo=dt.UTC)

EVERVERSE = resolve_followable_channel("eververse")

_pages = setup_nav_pages(
    loader,
    feed="eververse",
    history_len=14,
    period=dt.timedelta(days=1),
    reference_date=REFERENCE_DATE,
    # The eververse autopost is a Components V2 message, so its navigator renders CV2
    # pages (and a CV2 no-data page — every page must share the IS_COMPONENTS_V2 flag).
    cv2=True,
)

loader.command(
    make_navigator_command(
        _pages,
        name="eververse",
        description="Find out about the eververse items",
    )
)

follow_control_command_maker("eververse", "Eververse auto posts")
