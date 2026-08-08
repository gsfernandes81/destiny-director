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

from ..nav import NO_DATA_HERE_EMBED, NavigatorView, setup_nav_pages
from .autoposts import follow_control_command_maker, resolve_followable_channel

loader = lb.Loader()

REFERENCE_DATE = dt.datetime(2023, 7, 14, 17, tzinfo=dt.UTC)

FOLLOWABLE_CHANNEL = resolve_followable_channel("ada", "Ada")

SINGLE_PAGE_MODE = True

_pages = setup_nav_pages(
    loader,
    followable_channel=FOLLOWABLE_CHANNEL,
    history_len=12,
    period=dt.timedelta(days=7),
    reference_date=REFERENCE_DATE,
    suppress_content_autoembeds=False,
)


class AdaCommand(
    lb.SlashCommand, name="ada", description="Find out about ada's weekly items"
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        pages = _pages.pages
        if pages is None:
            raise RuntimeError("Ada pages not yet initialised")

        if not SINGLE_PAGE_MODE:
            navigator = NavigatorView(pages=pages, timeout=60)
            await navigator.send(ctx)
            return

        await ctx.defer()
        page_no = 0
        while True:
            try:
                page = pages[page_no]
            except IndexError:
                return await ctx.respond(NO_DATA_HERE_EMBED)
            except Exception:
                page_no -= 1
                continue

            if page.embeds and page.embeds[0] == NO_DATA_HERE_EMBED:
                page_no -= 1
                continue
            else:
                return await ctx.respond(**page.to_message_kwargs())


loader.command(AdaCommand)

follow_control_command_maker(
    FOLLOWABLE_CHANNEL, "ada", "Ada", "Ada's weekly item auto posts"
)
