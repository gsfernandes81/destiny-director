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

"""Ada-1's weekly shaders autopost (anchor / producer side).

Ada-1 rotates her shaders at the weekly Tuesday reset (17:00 UTC). This module fetches
those shaders from the Bungie vendor API and posts them into the Ada source channel as a
Components-V2 message (matching the Eververse post's look); the beacon side
(``dd/beacon/extensions/ada.py``) mirrors it to subscribed servers.

Shares the Xûr plumbing verbatim: :func:`xur.fetch_vendor_data` (vendor query) and
:func:`xur.api_to_discord_announcer` (placeholder → edit → crosspost loop).
"""

import datetime as dt
import typing as t

import aiocron
import aiohttp.web
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import components, schemas, settings
from ...common.bot import CachedFetchBot
from ...common.utils import fetch_emoji_dict
from ..autopost import Feed, register_feed
from . import (
    bungie_api as api,
    xur,
)

loader = lb.Loader()

# Item type (``itemTypeDisplayName``) that marks a sale item as a shader.
SHADER_TYPE_NAME = "Shader"

# Title links to Kyber's Ada-1 page, mirroring the Eververse post's linked title.
ADA_TITLE = "# [Ada-1's Weekly Shaders](https://kyber3000.com/Ada)"

#: Post-specific footer guide button(s); Support is appended by
#: ``components.footer_button_specs`` (the ADA_FOOTER note stays as body text).
ADA_GUIDES: tuple[tuple[str, str], ...] = (
    ("Ada-1 Guide", "https://kyber3000.com/Ada"),
)

# Ada rotates at the weekly reset: Tuesday 17:00 UTC.
ADA_RESET_WEEKDAY = 1  # Monday=0 … Tuesday=1
ADA_RESET_HOUR_UTC = 17

# Standing notice pinned to the bottom of every post, rendered as Discord subtext
# (``-#`` → small grey text). ``:information:`` is substituted for the server emoji.
ADA_FOOTER = (
    "-# :information: As of Monument of Triumph and Update 9.7.0, Ada-1 permanently "
    "sells legacy armor and continues to offer Shaders, Exotic Archetype focusing, "
    "along with repeatable bounties that reward Armor Synthesis Materials and XP."
)


def _shaders(sale_items: t.Iterable[api.DestinyItem]) -> list[api.DestinyItem]:
    """Ada's shaders, dropping her other wares (armour, synthesis materials, mods)."""
    return [
        item for item in sale_items if item.item_type_friendly_name == SHADER_TYPE_NAME
    ]


def next_ada_reset(now: dt.datetime | None = None) -> dt.datetime:
    """The next Tuesday 17:00 UTC strictly after ``now`` — when Ada's stock rotates."""
    if now is None:
        now = dt.datetime.now(tz=dt.UTC)
    days_ahead = (ADA_RESET_WEEKDAY - now.weekday()) % 7
    reset = (now + dt.timedelta(days=days_ahead)).replace(
        hour=ADA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if reset <= now:
        # ``now`` is on reset day at/after 17:00 UTC; the change is a week out.
        reset += dt.timedelta(days=7)
    return reset


def _inventory_changes_line(now: dt.datetime | None = None) -> str:
    """ "Inventory changes: <discord-timestamp>" for the next weekly reset."""
    unix = int(next_ada_reset(now).timestamp())
    return f"Inventory changes: <t:{unix}:f>"


def _by_name(item: api.DestinyItem) -> str:
    return item.name


def _ada_shader_line(item: api.DestinyItem) -> str:
    """One shader line: ``🎨 [**name**](lightgg_url)``."""
    return f"🎨 [**{item.name}**]({item.lightgg_url})"


def _render_shader_block(shaders: list[api.DestinyItem]) -> str:
    if not shaders:
        return "No shaders are available right now."
    return "\n".join(_ada_shader_line(item) for item in sorted(shaders, key=_by_name))


async def fetch_ada_data(
    webserver_runner: aiohttp.web.AppRunner,
) -> api.DestinyVendor:
    """Fetch Ada's wares. Shaders are class-agnostic, so a single character query
    returns them all — no need to query once per class."""
    return await xur.fetch_vendor_data(
        webserver_runner, vendor_hashes=api.ADA_VENDOR_HASH
    )


async def format_ada_vendor(
    vendor: api.DestinyVendor,
    bot: CachedFetchBot,
) -> HMessage:
    emoji_dict = await fetch_emoji_dict(bot)
    shaders = _shaders(vendor.sale_items)

    # Components V2 container with a linked title over a divider, matching Eververse
    # (raw :emoji: tokens — resolved on the assembled message below).
    container = h.impl.ContainerComponentBuilder(
        accent_color=await settings.get_embed_default_color()
    )
    container.add_text_display(ADA_TITLE + "\n" + _inventory_changes_line())
    container.add_separator(divider=True)
    container.add_text_display(_render_shader_block(shaders))
    container.add_separator(divider=True)
    container.add_text_display(ADA_FOOTER)
    container.add_component(components.footer_buttons_row(guides=ADA_GUIDES))

    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert
    # on overflow — the trailing footer is dropped first, then the shader block; the
    # title is kept whole).
    return await components.finalize_cv2_post(
        HMessage(components=[container]), emoji_dict, post_name="Ada"
    )


async def ada_message_constructor(bot: CachedFetchBot) -> HMessage:
    vendor = await fetch_ada_data(api.get_webserver_runner())
    return await format_ada_vendor(vendor, bot)


@loader.listener(h.StartedEvent)
async def on_start_schedule_autoposts(
    event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
):
    # Run every Tuesday at 17:00 UTC (Ada rotates at the weekly reset)
    @aiocron.crontab("0 17 * * TUE", start=True)
    # Use below crontab for testing to post every minute
    # @aiocron.crontab("* * * * *", start=True)
    async def autopost_ada():
        await xur.api_to_discord_announcer(
            bot,
            channel_id=await settings.get_followable_channel("ada"),
            check_enabled=True,
            enabled_check_coro=schemas.AutoPostSettings.get_ada_enabled,
            construct_message_coro=ada_message_constructor,
            cv2=True,
        )


# Contribute this feed's producer wiring to the web feed page (Preview / Send now).
register_feed(
    Feed(
        name="ada",
        channel_id=settings.get_followable_channel_sync("ada"),
        message_constructor_coro=ada_message_constructor,
        message_announcer_coro=xur.api_to_discord_announcer,
    )
)
