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

Guarded on the ``portal_ops`` followable channel id: if it is not configured in this
environment's FOLLOWABLES the module loads cleanly and registers nothing (the bot is
unaffected) until the channel is set.
"""

import datetime as dt
import logging

import lightbulb as lb

from ...common import settings
from ..nav import ResetPages, make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker

logger = logging.getLogger(__name__)

loader = lb.Loader()

# Daily reset anchor (Tue/any day 17:00 UTC); the period is one day.
REFERENCE_DATE = dt.datetime(2025, 7, 15, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = settings.get_followable_channel_sync("portal_ops")

if not FOLLOWABLE_CHANNEL:
    # No followable channel configured (absent, or the 0 placeholder) → load cleanly
    # and register nothing. The navigator + follow command come online once
    # 'portal_ops' is set to a real channel id in FOLLOWABLES.
    logger.info(
        "Portal Ops command is dormant: no 'portal_ops' entry in FOLLOWABLES. "
        "Add the followable channel id to enable it."
    )
else:
    # Narrowed non-None channel id for the helpers below.
    _followable_channel: int = FOLLOWABLE_CHANNEL

    _pages = setup_nav_pages(
        loader,
        pages_cls=ResetPages,
        followable_channel=_followable_channel,
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
            description="Find out about today's featured Portal ops",
        )
    )

    loader.command(portal_command_group)

    follow_control_command_maker(
        _followable_channel,
        "portal_ops",
        "Portal ops",
        "Portal ops auto posts",
    )
