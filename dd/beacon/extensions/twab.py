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

from ...common import cfg, settings
from ...common.utils import accumulate
from .. import utils
from ..nav import NavPages, make_navigator_command, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2025, 3, 20, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = resolve_followable_channel("twab")


class TWIDPages(NavPages):
    @override
    def preprocess_messages(self, messages: list[h.Message]) -> HMessage:
        if not messages:
            return self.no_data_message
        msg: HMessage = accumulate([HMessage.from_message(m) for m in messages])
        msg.embeds = utils.filter_discord_autoembeds(msg)

        urls = cfg.url_regex.findall(msg.content)

        autoembeds_from_discord = list(
            filter(lambda embed: embed.url in urls, msg.embeds)
        )
        image_autoembeds_from_discord = list(
            filter(
                lambda embed: any(
                    [
                        (embed.url or "").lower().endswith(extension)
                        for extension in cfg.IMAGE_EXTENSIONS_LIST
                    ]
                ),
                autoembeds_from_discord,
            )
        )
        non_image_autoembeds_from_discord = list(
            filter(
                lambda embed: embed not in image_autoembeds_from_discord,
                autoembeds_from_discord,
            )
        )

        msg.embeds = list(
            filter(
                lambda embed: embed not in non_image_autoembeds_from_discord, msg.embeds
            )
        )

        msg.merge_content_into_embed(0)

        for embed in list(image_autoembeds_from_discord):
            msg.merge_url_as_image_into_embed(
                embed.url, 0, default_url=settings.get_default_url_sync()
            )

        msg.remove_all_embed_thumbnails()
        msg.embeds = list(filter(lambda embed: embed.description, msg.embeds))

        # If filtering left nothing renderable, fall back to the "No data here!" embed
        # rather than an empty message.
        if not msg.embeds:
            return self.no_data_message

        return msg


_pages = setup_nav_pages(
    loader,
    pages_cls=TWIDPages,
    feed="twab",
    history_len=4,
    period=dt.timedelta(days=7),
    reference_date=REFERENCE_DATE,
)

_TWID_DESCRIPTION = "Find out about This Week In Destiny (formerly the TWAB)"

loader.command(
    make_navigator_command(_pages, name="twid", description=_TWID_DESCRIPTION)
)
loader.command(
    make_navigator_command(_pages, name="twab", description=_TWID_DESCRIPTION)
)

follow_control_command_maker("twab", "This Week In Destiny weekly auto posts")
