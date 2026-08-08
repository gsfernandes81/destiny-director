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

"""Beacon read commands for the legacy world-activity rotations.

One top-level command per destination (``/neomuna``, ``/moon``, ``/dares``, …). Each
destination renders in one of three modes:

- **single** (``rahool``, ``pale_heart``, ``kepler``): short cycles, so a single
  non-paginated post (distortion-style) lists the current + upcoming rotation.
- **week-daily** (``neomuna``, ``moon``): a *weekly* navigator; each page shows the
  week's weekly activities once plus a per-day breakdown of the daily activities.
- **navigator** (everything else): a date-navigable navigator (one page per day for a
  daily destination, per week for a weekly one).

Each page is a CV2 container of divider-separated markdown sections
(:func:`dd.common.components.build_container`).
"""

import datetime as dt
import typing as t

import hikari as h
import lightbulb as lb

from ...common import components
from ...common.bot import ServerEmojiEnabledBot
from ...common.components import cv2_error, respond_cv2
from ...common.legacy_activities import (
    SINGLE_DESTINATIONS as _SINGLE,
    WEEK_DAILY_DESTINATIONS as _WEEK_DAILY,
    load_rotation,
    period_starts,
    render_dares_sections,
    render_date_sections,
    render_upcoming_sections,
    render_week_sections,
    reset_week_start,
)
from ...common.rotation_schema import LEGACY_DESTINATIONS
from ...sector_accounting.legacy_activities import LegacyRotation

loader = lb.Loader()

# _SINGLE / _WEEK_DAILY (the render-mode partitions) now live in
# dd.common.legacy_activities so the anchor preview wall shares them; imported above.

# How many pages / rows each mode shows.
_DAILY_PAGE_COUNT = 14  # navigator, daily destination (two weeks)
_WEEKLY_PAGE_COUNT = 8  # navigator, weekly destination (two months)
_WEEK_DAILY_PAGE_COUNT = 8  # week-daily navigator (two months of weeks)
_SINGLE_DAILY_ROWS = 7  # single mode, daily destination (upcoming rows)
_SINGLE_WEEKLY_ROWS = 5  # single mode, weekly destination (upcoming rows)


def _page(sections: list[str]) -> components.Page:
    """A Paginator page factory building a **fresh** container each call.

    Fresh per call by contract: the Paginator injects its nav row into the returned
    container on every render, so a reused builder would pile up rows on revisits.
    """
    return lambda secs=sections: [components.build_container(secs)]


async def build_pages(
    destination_key: str,
    rotation: LegacyRotation,
    emoji_dict: dict[str, h.Emoji],
    *,
    now: dt.datetime,
) -> list[components.Page]:
    """A navigator's forward window of per-date pages (reset-aligned day or week)."""
    count = _DAILY_PAGE_COUNT if rotation.step.days == 1 else _WEEKLY_PAGE_COUNT
    pages: list[components.Page] = []
    for date in period_starts(rotation, now, count):
        if destination_key == "dares":
            sections = render_dares_sections(
                rotation(date), date, emoji_dict=emoji_dict, links=rotation.item_links
            )
        else:
            sections = render_date_sections(
                destination_key,
                rotation(date),
                date,
                emoji_dict=emoji_dict,
                links=rotation.item_links,
            )
        pages.append(_page(sections))
    return pages


async def build_week_pages(
    destination_key: str,
    rotation: LegacyRotation,
    emoji_dict: dict[str, h.Emoji],
    *,
    now: dt.datetime,
) -> list[components.Page]:
    """A weekly navigator (page 1 = the current reset week) with a daily breakdown."""
    week0 = reset_week_start(rotation, now)
    pages: list[components.Page] = []
    for offset in range(_WEEK_DAILY_PAGE_COUNT):
        week_start = week0 + dt.timedelta(days=7 * offset)
        sections = render_week_sections(
            destination_key,
            rotation,
            week_start,
            emoji_dict=emoji_dict,
            links=rotation.item_links,
        )
        pages.append(_page(sections))
    return pages


def make_legacy_command(
    destination_key: str, name: str, description: str
) -> type[lb.SlashCommand]:
    """Build an (unregistered) top-level ``/<destination>`` command."""

    class _LegacyCommand(lb.SlashCommand, name=name, description=description):
        @lb.invoke
        async def invoke(self, ctx: lb.Context) -> None:
            bot = t.cast(ServerEmojiEnabledBot, ctx.client.app)

            try:
                rotation = await load_rotation(destination_key)
            except Exception:
                await respond_cv2(
                    ctx,
                    cv2_error(
                        "No data yet",
                        f"The {name} rotation hasn't been set up yet.",
                    ),
                    ephemeral=True,
                )
                return

            emoji = bot.emoji
            now = dt.datetime.now(tz=dt.UTC)

            if destination_key in _SINGLE:
                rows = (
                    _SINGLE_WEEKLY_ROWS
                    if rotation.step.days == 7
                    else _SINGLE_DAILY_ROWS
                )
                # +1: the aligned window's first entry is the *current* period.
                dates = period_starts(rotation, now, rows + 1)
                sections = render_upcoming_sections(
                    destination_key,
                    rotation,
                    dates,
                    emoji_dict=emoji,
                    links=rotation.item_links,
                )
                await ctx.respond(
                    components=[components.build_container(sections)],
                    flags=h.MessageFlag.IS_COMPONENTS_V2,
                )
                return

            if destination_key in _WEEK_DAILY:
                pages = await build_week_pages(
                    destination_key, rotation, emoji, now=now
                )
            else:
                pages = await build_pages(destination_key, rotation, emoji, now=now)
            await components.Paginator(pages).send(ctx)

    return _LegacyCommand


for _key, (_title, _activities) in LEGACY_DESTINATIONS.items():
    loader.command(
        make_legacy_command(
            _key,
            name=_key.replace("_", "-"),
            description=f"{_title} world-activity rotation",
        )
    )
