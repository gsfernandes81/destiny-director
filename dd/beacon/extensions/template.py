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

"""Starting point for a new followable's beacon (reader) module.

**A feed's channel is never a module-level constant here.** It is a DB-backed setting
an operator edits on anchor's Autopost Settings page, so it can change while the bot is
running; a copy taken at import is stale from the moment it is taken.
``setup_nav_pages`` below is what resolves it — on ``StartedEvent``, and again whenever
it changes (see ``utils.reconcile_feed_channels``) — so nothing here names a channel.
The feed's ``slug`` is the only identity a module like this passes around; its display
name, ``/autopost`` subcommand name and channel key all come from its entry in
``dd.common.feeds``, which is where a new followable is declared before it gets a module
like this one.
"""

import datetime as dt
from typing import override

import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import settings
from ...common.utils import accumulate
from .. import utils
from ..nav import NavPages, make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker

loader = lb.Loader()

# Set IGNORE to False to enable the module
IGNORE = True

# The feed's catalog slug — its key in dd.common.feeds.FOLLOWABLES.
FEED = "xur"

# CODE FOR PAGES BELOW. CAN BE SAFELY REMOVED IF ONLY AUTOPOSTS ARE NEEDED

# Reference date and update period for the pages
REFERENCE_DATE = dt.datetime(2023, 7, 14, 17, tzinfo=dt.UTC)
UPDATE_PERIOD = dt.timedelta(days=7)


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
    # Registers the StartedEvent listener that builds the pages, and subscribes the
    # holder to channel changes. Never resolves a channel here: an unset or since-
    # deleted one is recorded on the holder (and paged for) rather than raised, so the
    # command below can answer for itself instead of exploding at whoever ran it.
    _pages = setup_nav_pages(
        loader,
        feed=FEED,
        pages_cls=Pages,
        reference_date=REFERENCE_DATE,
        period=UPDATE_PERIOD,
    )

    # CODE FOR PAGES ABOVE. CAN BE SAFELY REMOVED IF ONLY AUTOPOSTS ARE NEEDED

    # Reads _pages.pages at invoke time (never a captured copy) and gives the
    # "unavailable" answer when there are none. `feed` is the slug, not the command
    # name — a navigator's command is routinely not named after its feed.
    loader.command(
        make_navigator_command(
            _pages,
            name="xur",
            description="Find out what Xur has and where Xur is",
            feed=FEED,
        )
    )

    follow_control_command_maker(FEED, "Xûr auto posts")
