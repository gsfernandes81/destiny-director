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

import asyncio as aio
import datetime as dt
import json
import logging
import pathlib
import re
import time
import typing as t

import aiocron
import aiohttp
import aiohttp.web
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import (
    cfg,
    feeds as dd_feeds,
    schemas,
    settings,
)
from ...common.bot import CachedFetchBot
from ...common.components import (
    build_container,
    finalize_cv2_post,
    footer_buttons_row,
    url_media_gallery,
)
from ...common.utils import accumulate, fetch_emoji_dict
from ...sector_accounting import xur as xur_support_data
from .. import utils
from ..autopost import Feed, register_feed
from . import bungie_api as api

logger = logging.getLogger(__name__)

loader = lb.Loader()

re_masterwork = re.compile("Tier [0-9]: ")
exotic_class_item_by_class = {
    "hunter": "Relativism",
    "titan": "Stoicism",
    "warlock": "Solipsism",
}
exotic_class_item_xur_strings_by_class = {
    "hunter": ":armor:  [**Relativism (Class Item)**](https://light.gg/db/items/3844826440)",
    "titan": ":armor:  [**Stoicism (Class Item)**](https://light.gg/db/items/3844826440)",
    "warlock": ":armor:  [**Solipsism (Class Item)**](https://light.gg/db/items/3844826440)",
}


def xur_departure_string(post_date_time: dt.datetime | None = None) -> str:
    # Find the closest Tuesday in the future and set the time
    # to 1700 UTC on that day
    if post_date_time is None:
        post_date_time = dt.datetime.now(tz=dt.UTC)

    # Find the next Tuesday
    days_ahead = 1 - post_date_time.weekday()
    days = days_ahead % 7
    post_date_time = post_date_time + dt.timedelta(days=days)

    # Set the time to 1700 UTC
    post_date_time = post_date_time.replace(hour=17, minute=0, second=0, microsecond=0)

    # Convert to unix time
    xur_unix_departure_time = int(post_date_time.timestamp())

    return f":time:  Xûr departs <t:{xur_unix_departure_time}:R>\n"


def xur_location_fragment(
    xur_location: str, xur_locations: xur_support_data.XurLocations
) -> str:
    location = xur_locations[xur_location]
    return f"## **__Location__**\n:location: **{str(location)}**\n"


# Committed seed document for the Xûr location map, shipped with the bot so a
# freshly-deployed bot (or a wiped row) serves data with no manual seed step. Mirrors
# ``dd/common/seed_data/world_activity`` (see ``dd.common.legacy_activities``).
_XUR_SEED_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "common"
    / "seed_data"
    / "xur_location.json"
)

# Last-known-good Xûr location map, served only if the DB store is unreachable so a
# transient DB blip never breaks the autopost. Mirrors
# ``dd.common.lost_sector._rotation_cache``.
_xur_locations_cache: dict[str, xur_support_data.XurLocations] = {}

_XUR_LOCATION = "xur_location"


def _load_seed_doc() -> dict | None:
    """The committed Xûr-location seed document, or ``None`` if absent."""
    try:
        raw = _XUR_SEED_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(raw)


async def _autoseed() -> dict | None:
    """Populate an absent ``xur_location`` row from the committed seed doc; return it.

    Self-seeds on first use so a freshly-deployed bot (or a wiped row) serves data with
    no manual seed step. Persisting is best-effort — the doc is returned for rendering
    even if the write fails."""
    doc = _load_seed_doc()
    if doc is None:
        return None
    try:
        await schemas.RotationData.set_data(_XUR_LOCATION, doc)
        logger.info("Auto-seeded xur_location from committed seed data")
    except Exception:
        logger.exception("Failed to persist xur_location auto-seed (serving in-memory)")
    return doc


async def load_xur_locations() -> xur_support_data.XurLocations:
    """Load the Xûr location map from the DB JSON store.

    Resolution order mirrors :func:`dd.common.legacy_activities.load_rotation`: the
    ``RotationData['xur_location']`` document (built via :meth:`XurLocations.from_json`)
    → an auto-seed from the committed seed doc on a clean absent read → the last-known-
    good cache. If even the seed is missing, degrades to an empty map — Xûr posts still
    render (``XurLocations.__getitem__`` falls back to the raw API location name).
    """
    db_ok = True
    try:
        doc = await schemas.RotationData.get_data(_XUR_LOCATION)
    except Exception:
        logger.exception("Failed to read xur_location data from the DB")
        doc, db_ok = None, False

    # Only auto-seed on a *clean* absent read — a transient DB error must not overwrite
    # a row that may exist, so it falls through to the cache instead.
    if doc is None and db_ok:
        doc = await _autoseed()

    if doc is not None:
        try:
            locations = xur_support_data.XurLocations.from_json(doc)
            _xur_locations_cache[_XUR_LOCATION] = locations
            return locations
        except Exception:
            logger.exception("Stored xur_location JSON is malformed")

    cached = _xur_locations_cache.get(_XUR_LOCATION)
    if cached is not None:
        return cached

    # No row, no seed, empty cache: an empty map still renders (raw API location names).
    return xur_support_data.XurLocations.from_json({})


def armor_stat_line_format(
    armor: api.DestinyArmor,
    simple_mode: bool = False,
    allowed_emoji_list: t.Iterable[str] = [],
) -> str:
    if simple_mode:
        return f"Σ {armor.stat_total}"
    stats = armor.stats
    stat_line = f"**Σ {armor.stat_total}**:"
    for stat_name, stat_value in stats.items():
        stat_name = str(stat_name).lower()
        if stat_name in allowed_emoji_list:
            stat_line += f" :{stat_name}: `{stat_value}`"
        else:
            stat_line += f" :{stat_name[:1].upper()}: `{stat_value}` "

    return stat_line


def costs_string_from_items(
    destiny_items: t.Iterable[api.DestinyItem],
    emoji_include_list: t.Iterable[str] = [],
    include_cost_text: bool = True,
) -> str:
    # Deduplicate cost structures by their sorted (currency, amount) items,
    # keeping one representative cost dict per distinct structure.
    unique_costs: dict[tuple[tuple[str, int], ...], dict[str, int]] = {
        tuple(sorted(destiny_item.costs.items())): destiny_item.costs
        for destiny_item in destiny_items
        if destiny_item.costs
    }

    if not unique_costs:
        return ""

    costs_line = "Cost:  " if include_cost_text else ""
    if len(unique_costs) == 1:
        (only_cost,) = unique_costs.values()
        for currency, amount in only_cost.items():
            emoji_name = api.likely_emoji_name(currency)
            if emoji_name not in emoji_include_list:
                costs_line = f"{costs_line}{currency} `x{amount}` "
            else:
                costs_line = f"{costs_line}:{emoji_name}: `x{amount}` "
    elif len(unique_costs) > 1:
        costs_line = "Costs vary per item"

    return costs_line


def exotic_armor_name_line(exotic_armor_piece: api.DestinyArmor) -> str:
    name = exotic_armor_piece.name
    armor_slot = exotic_armor_piece.bucket
    if armor_slot == "Leg":
        armor_slot = "Legs"
    return (
        ":armor:  "
        + f"[**{name} "
        + f"({armor_slot})**]"
        + f"({exotic_armor_piece.lightgg_url})"
    )


def exotic_armor_fragment(
    exotic_armor_pieces: list[api.DestinyArmor], allowed_emoji_list: t.Iterable[str]
) -> str:
    subfragments: list[str] = []

    # Group armor pieces by class, preserving first-seen class order.
    exotic_armor_pieces_by_class: dict[str, list[api.DestinyArmor]] = {}
    for armor_piece in exotic_armor_pieces:
        exotic_armor_pieces_by_class.setdefault(armor_piece.class_, []).append(
            armor_piece
        )

    for index, (class_, armor_pieces) in enumerate(
        exotic_armor_pieces_by_class.items()
    ):
        if index:
            # Add line break between classes
            subfragments.append("")
        subfragments.append(f"**{class_.capitalize()}**")
        for armor_piece in armor_pieces:
            subfragments.append(
                exotic_armor_name_line(armor_piece)
                + " "
                + armor_stat_line_format(
                    armor_piece, allowed_emoji_list=allowed_emoji_list, simple_mode=True
                )
            )
        if exotic_class_item_by_class[class_.lower()] not in [
            armor_piece.name for armor_piece in armor_pieces
        ]:
            subfragments.append(
                exotic_class_item_xur_strings_by_class[class_.lower()]
                + " *random rolls*"
            )

    return (
        "## **__Exotic Armor__**\n"
        + costs_string_from_items(exotic_armor_pieces, allowed_emoji_list)
        + "\n\n"
        + "\n".join(subfragments)
        + "\n"
    )


def weapon_line_format(
    weapon: api.DestinyItem,
    include_weapon_type: bool,
    # Either a list of perk indices or a callable that returns a list of perk indices
    # based on a list of perks
    include_perks: list[int] | t.Callable[[list[str]], list[int]],
    include_lightgg_link: bool,
    emoji_include_list: t.Iterable[str] = ["weapon"],
    default_emoji: str = "weapon",
) -> str:
    weapon_line = weapon.name

    if include_weapon_type:
        weapon_line += f" ({weapon.item_type_friendly_name})"

    weapon_line = f"**{weapon_line}**"

    if include_lightgg_link:
        weapon_line = f"[{weapon_line}]({weapon.lightgg_url})"

    if emoji_include_list:
        if weapon.expected_emoji_name in emoji_include_list:
            weapon_line = f":{weapon.expected_emoji_name}: {weapon_line}"
        else:
            weapon_line = f":{default_emoji}: {weapon_line}"

    if include_perks:
        _include_perks: t.Any = include_perks
        perk_indices: list[int] = t.cast(
            list[int],
            _include_perks(weapon.perks)
            if callable(_include_perks)
            else _include_perks,
        )
        perks: list[str] = []
        for perk_index in perk_indices:
            if perk_index >= len(weapon.perks):
                continue

            perk_options = weapon.perks[perk_index]
            if isinstance(perk_options, tuple):
                perks.append(" / ".join(perk_options))
            else:
                perks.append(perk_options)

        weapon_line += ": " + ", ".join(perks)

    return weapon_line


def exotic_weapons_fragment(
    exotic_weapons: list[api.DestinyItem],
    emoji_include_list: t.Iterable[str],
) -> str:
    exotic_weapons_fragment_ = "## **__Exotic Weapons__**\n"

    exotic_weapons_fragment_ += (
        costs_string_from_items(exotic_weapons, emoji_include_list) + "\n\n"
    )

    for exotic_weapon in exotic_weapons:
        exotic_weapons_fragment_ += (
            weapon_line_format(
                exotic_weapon,
                include_weapon_type=exotic_weapon.name != "Hawkmoon",
                include_perks=[2] if exotic_weapon.name == "Hawkmoon" else [],
                include_lightgg_link=True,
                emoji_include_list=emoji_include_list,
            )
            + "\n"
        )
    return exotic_weapons_fragment_


def exotic_catalysts_fragment(
    exotic_catalysts: list[api.DestinyItem], emoji_include_list: t.Iterable[str]
) -> str:
    exotic_catalysts_fragment_ = "## **__Exotic Catalysts__**\n"

    exotic_catalysts_fragment_ += (
        costs_string_from_items(exotic_catalysts, emoji_include_list) + "\n\n"
    )

    for exotic_catalyst in exotic_catalysts:
        exotic_catalysts_fragment_ += (
            weapon_line_format(
                exotic_catalyst,
                include_weapon_type=False,
                include_perks=[],
                include_lightgg_link=True,
                emoji_include_list=emoji_include_list,
                default_emoji="exotic_catalyst",
            )
            + "\n"
        )
    return exotic_catalysts_fragment_


def last_two_active_perk_columns(perks: list[str]) -> list[int]:
    # NOTE: ``perks`` is typed ``t.List[str]`` to match ``DestinyItem.perks`` but at
    # runtime each element is itself a sequence of perk option strings (a "column").
    perks_to_return: list[int] = []
    for i, perk_column in enumerate(t.cast(list[t.Sequence[str]], perks)):
        if len(perk_column) == 0:
            continue
        elif len(perk_column) > 2:
            perks_to_return.append(i)
            continue
        else:
            perk = perk_column[0]
            if not (
                "shader" in perk.lower()
                or "tracker" in perk.lower()
                or re_masterwork.search(perk)
            ):
                perks_to_return.append(i)

    return perks_to_return[-2:]


def legendary_weapons_fragment(
    legendary_weapons: list[api.DestinyItem],
    emoji_include_list: t.Iterable[str],
    include_title: str = "## **__Legendary Weapons__**",
) -> str:
    subfragments = []
    if include_title:
        subfragments.append(include_title)

    subfragments.append(costs_string_from_items(legendary_weapons, emoji_include_list))
    subfragments.append("")

    for weapon in legendary_weapons:
        subfragments.append(
            weapon_line_format(
                weapon,
                include_weapon_type=False,
                include_perks=last_two_active_perk_columns,
                include_lightgg_link=True,
                emoji_include_list=emoji_include_list,
            )
        )

    return "\n".join(subfragments) + "\n"


def legendary_armor_sets_fragment(
    legendary_armor: list[api.DestinyArmor],
    emoji_include_list: t.Iterable[str],
    include_title: str = "## **__Legendary Armor Sets__**",
) -> str:
    subfragments: list[str] = []
    if include_title:
        subfragments.append(include_title)

    subfragments.append(costs_string_from_items(legendary_armor, emoji_include_list))
    subfragments.append("")

    armor_set_names = {armor.collectible_set_name for armor in legendary_armor}

    for armor_set_name in sorted(armor_set_names):
        subfragments.append(f":armor: **{armor_set_name}**")

    return "\n".join(subfragments) + "\n"


XUR_FOOTER = "\n\nHave a great weekend! :gscheer:"

#: Post-specific footer guide button(s); Support is appended by
#: ``footer_button_specs``. The "View More" page that was a markdown link in the footer.
XUR_GUIDES: tuple[tuple[str, str], ...] = (
    ("Xûr Guide", "https://kyber3000.com/D2-Xur"),
)


async def format_xur_vendor(
    vendor: api.DestinyVendor,
    bot: CachedFetchBot,
) -> HMessage:
    xur_locations = await load_xur_locations()

    emoji_dict = await fetch_emoji_dict(bot)

    header = "# [XÛR'S LOOT](https://kyber3000.com/D2-Xur)\n\n"
    header += xur_departure_string()
    header += xur_location_fragment(vendor.location or "", xur_locations)

    exotic_armor_pieces: list[api.DestinyArmor] = [
        t.cast(api.DestinyArmor, item)
        for item in vendor.sale_items
        if item.is_exotic and item.is_armor
    ]
    exotic_armor_pieces.sort(key=lambda x: x.class_)
    body = exotic_armor_fragment(
        exotic_armor_pieces,
        allowed_emoji_list=emoji_dict.keys(),
    )
    body += exotic_weapons_fragment(
        [item for item in vendor.sale_items if item.is_exotic and item.is_weapon],
        emoji_include_list=emoji_dict.keys(),
    )
    body += exotic_catalysts_fragment(
        [item for item in vendor.sale_items if item.is_exotic and item.is_catalyst],
        emoji_include_list=emoji_dict.keys(),
    )
    body += legendary_armor_sets_fragment(
        [
            t.cast(api.DestinyArmor, item)
            for item in vendor.sale_items
            if item.is_legendary and item.is_armor
        ],
        emoji_include_list=emoji_dict.keys(),
    )
    body += legendary_weapons_fragment(
        [item for item in vendor.sale_items if item.is_weapon and item.is_legendary],
        emoji_include_list=emoji_dict.keys(),
    )

    # Components V2: the whole post is one text display, with the optional default
    # image as a trailing full-width media gallery (mirroring the old set_image).
    container = h.impl.ContainerComponentBuilder(
        accent_color=await settings.get_embed_default_color()
    )
    container.add_text_display(header + body + XUR_FOOTER)

    if await schemas.AutoPostSettings.get_xur_default_image_enabled():
        # A blank/unconfigured URL must not reach url_media_gallery — Discord rejects a
        # media item with an empty url, which would fail the ENTIRE post, not just drop
        # the image (unlike Lost Sector's image, which is skipped when unset).
        default_image_url = await settings.get_xur_image_url()
        if default_image_url:
            container.add_component(url_media_gallery(default_image_url))
    container.add_component(footer_buttons_row(guides=XUR_GUIDES))

    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert
    # on overflow, measured on the final rendered length).
    return await finalize_cv2_post(
        HMessage(components=[container]),
        emoji_dict,
        post_name=dd_feeds.FEEDS["xur"].display_name,
    )


async def fetch_vendor_data(
    webserver_runner: aiohttp.web.AppRunner,
    vendor_hashes: list[int] | int,
    character_class: str = "Hunter",
) -> api.DestinyVendor:
    if isinstance(vendor_hashes, int):
        vendor_hashes = [vendor_hashes]

    access_token = await api.refresh_api_tokens(webserver_runner)

    async with aiohttp.ClientSession() as session:
        destiny_membership = await api.DestinyMembership.from_api(session, access_token)
        character_id = await destiny_membership.get_character_id(
            session, access_token, character_class
        )

    manifest_table = await api._build_manifest_dict(
        await api._get_latest_manifest(schemas.BungieCredentials.api_key)
    )
    vendor: api.DestinyVendor = accumulate(
        [
            await api.DestinyVendor.request_from_api(
                destiny_membership=destiny_membership,
                character_id=character_id,
                access_token=access_token,
                manifest_table=manifest_table,
                vendor_hash=vendor_hash,
            )
            for vendor_hash in vendor_hashes
        ]
    )

    return vendor


async def fetch_xur_data(webserver_runner: aiohttp.web.AppRunner) -> api.DestinyVendor:
    xur = await fetch_vendor_data(
        webserver_runner, [api.XUR_VENDOR_HASH, api.XUR_STRANGE_GEAR_VENDOR_HASH]
    )
    return xur


async def xur_message_constructor(bot: CachedFetchBot) -> HMessage:
    xur = await fetch_xur_data(api.get_webserver_runner())
    return await format_xur_vendor(xur, bot)


async def api_to_discord_announcer(
    bot: CachedFetchBot,
    channel_id: int,
    construct_message_coro: t.Callable[..., t.Awaitable[HMessage]],
    check_enabled: bool = False,
    enabled_check_coro: t.Callable[[], t.Awaitable[bool | None]] | None = None,
    publish_message: bool = True,
    cv2: bool = False,
):
    # Bail out before posting anything if the autopost is disabled (or never
    # enabled — ``get_enabled`` returns None). This is the ONLY enabled-check: it
    # runs before the placeholder is posted, so a disabled autopost leaks nothing.
    # Re-checking mid-loop would be worse than useless — by then the placeholder is
    # already up, so a mid-run ``return`` would orphan it (the original bug). Once
    # we've committed to posting, we always finish the placeholder→edit→crosspost.
    if check_enabled and (enabled_check_coro is None or not await enabled_check_coro()):
        return

    # Match the placeholder's type to the final post (CV2 vs embed) so the edit loop
    # below never has to toggle IS_COMPONENTS_V2 (which Discord forbids on edit).
    if cv2:
        hmessage = HMessage(
            components=[build_container(["Waiting for data from the API…"])]
        )
    else:
        hmessage = HMessage(
            embeds=[
                h.Embed(
                    description="Waiting for data from the API...",
                    color=await settings.get_embed_default_color(),
                )
            ]
        )
    msg = await utils.send_message(
        bot,
        hmessage,
        channel_id=channel_id,
        crosspost=False,
        deduplicate=True,
    )

    # ``retries`` / ``started`` live outside the loop so backoff actually grows and
    # a long stall escalates exactly once (a reset-each-iteration counter would
    # never back off and would log every spin).
    retries = 0
    started = time.monotonic()
    alerted = False
    while True:
        try:
            await api.check_bungie_api_online(raise_exception=True)

            hmessage = await construct_message_coro(bot=bot)
        except Exception as e:
            # Log the first failure with a traceback; keep retries quiet (the alert
            # handler dedups, and a sustained stall escalates below).
            if retries == 0:
                logger.exception(e)
            else:
                logger.debug(
                    "Announcer retry %d for channel %d: %r", retries, channel_id, e
                )
            retries += 1
            if not alerted and time.monotonic() - started > int(
                cfg.announcer_offline_alert_after
            ):
                alerted = True
                logger.critical(
                    "Autopost for channel %d stalled for >%ds (still retrying): %r",
                    channel_id,
                    int(cfg.announcer_offline_alert_after),
                    e,
                )
            await aio.sleep(2 ** min(retries, 8))
        else:
            break

    retries = 0
    started = time.monotonic()
    alerted = False
    while True:
        try:
            await msg.edit(**hmessage.to_message_kwargs())
        except Exception as e:
            if retries == 0:
                logger.exception(e)
            else:
                logger.debug(
                    "Announcer edit retry %d for channel %d: %r",
                    retries,
                    channel_id,
                    e,
                )
            retries += 1
            if not alerted and time.monotonic() - started > int(
                cfg.announcer_offline_alert_after
            ):
                alerted = True
                logger.critical(
                    "Autopost edit for channel %d stalled for >%ds (retrying): %r",
                    channel_id,
                    int(cfg.announcer_offline_alert_after),
                    e,
                )
            await aio.sleep(min(2**retries, 300))
        else:
            break
    if publish_message:
        # Wait 5 seconds before crossposting to allow time between the edit
        # and the crosspost events to avoid a race condition type error where
        # the pre-edit message is crossposted for mirrors
        await aio.sleep(5)
        await utils.crosspost_message_with_retries(bot, channel_id, msg.id)


@loader.listener(h.StartedEvent)
async def on_start_schedule_autoposts(
    event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
):
    # Run every Friday at 17:00 UTC
    @aiocron.crontab("0 17 * * FRI", start=True)
    # Use below crontab for testing to post every minute
    # @aiocron.crontab("* * * * *", start=True)
    async def autopost_xur():
        await api_to_discord_announcer(
            bot,
            channel_id=await settings.get_followable_channel("xur"),
            check_enabled=True,
            enabled_check_coro=schemas.AutoPostSettings.get_xur_enabled,
            construct_message_coro=xur_message_constructor,
            cv2=True,
        )


# Contribute this feed's producer wiring to the web feed page (Preview / Send now).
register_feed(
    Feed(
        name="xur",
        message_constructor_coro=xur_message_constructor,
        message_announcer_coro=api_to_discord_announcer,
    )
)
