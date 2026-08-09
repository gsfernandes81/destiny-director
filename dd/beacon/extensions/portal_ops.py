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

"""Beacon-side Portal Ops follow command + daily navigator.

Mirrors the anchor's Portal Ops autopost (see ``dd/anchor/extensions/portal_ops.py``)
into per-guild channels and exposes a ``/portal ops`` navigator over the mirrored
channel history. The post is daily (period = 1 day, daily reset anchor).

Both commands register whether or not a channel has been set: a command that silently
stops existing reads to a user as a broken bot, and Discord serves the stale command
list anyway. With no channel they answer "unavailable" and page us instead — see
``dd.beacon.utils.feed_unavailable_embed``.
"""

import datetime as dt

import lightbulb as lb

from ...common import feeds as dd_feeds
from ..nav import ResetPages, make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

# Daily reset anchor (Tue/any day 17:00 UTC); the period is one day.
REFERENCE_DATE = dt.datetime(2025, 7, 15, 17, tzinfo=dt.UTC)

# Import-time read purely for the boot-time alert on an unconfigured feed; every
# command below resolves the channel live when it runs.
FOLLOWABLE_CHANNEL = resolve_followable_channel("portal_ops")

_pages = setup_nav_pages(
    loader,
    pages_cls=ResetPages,
    feed="portal_ops",
    history_len=14,
    period=dt.timedelta(days=1),
    reference_date=REFERENCE_DATE,
    cv2=True,
)

portal_command_group = lb.Group("portal", "Destiny 2 Portal Ops")
portal_command_group.register(
    make_navigator_command(
        _pages,
        name="ops",
        display_name=dd_feeds.FEEDS["portal_ops"].display_name,
        description="Find out about today's featured Portal ops",
    )
)

loader.command(portal_command_group)

follow_control_command_maker("portal_ops", "Portal Ops auto posts")
