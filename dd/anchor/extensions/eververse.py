import re
import typing as t
from collections import defaultdict

import aiocron
import aiohttp
import aiohttp.web
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import (
    components,
    feeds as dd_feeds,
    schemas,
    settings,
)
from ...common.bot import CachedFetchBot
from ...common.utils import fetch_emoji_dict
from ..autopost import Feed, register_feed
from . import (
    bungie_api as api,
    xur,
)

loader = lb.Loader()

#: Post-specific footer guide button(s); Support is appended by
#: ``components.footer_button_specs``.
EVERVERSE_GUIDES: tuple[tuple[str, str], ...] = (
    ("Eververse Guide", "https://kyber3000.com/Eververse"),
)

# Exotic ornaments carry the exotic they reskin only in their description, as
# "...change the appearance of <Exotic Name>." This pulls that name out so the
# daily-offerings line can show "for <Exotic>".
_ORNAMENT_TARGET_RE = re.compile(r"change the appearance of (.+?)\.")
# traitIds that mark an item as an ornament (vs. another exotic cosmetic like a
# ghost shell or ship, which we deliberately leave without a "for ..." suffix).
_ORNAMENT_TRAIT_IDS = frozenset({"item.ornament.weapon", "item.ornament.armor"})


def _rotator_hashes(manifest_table: dict[str, t.Any], prefix: str) -> list[int]:
    """Discover daily rotator vendor hashes from the manifest.

    These are the vendors whose ``vendorIdentifier`` starts with ``prefix`` — e.g.
    ``EVERVERSE_BRIGHT_DUST_ROTATOR`` or ``EVERVERSE_SILVER_ROTATOR``
    (``..._EXOTIC_GHOSTS`` and friends).

    The search runs in sqlite (``hashes_by_field_prefix``) rather than over
    ``.values()``: the two scalars this needs per vendor used to cost a full parse of
    ``DestinyVendorDefinition``, the fattest table in the manifest — ~385 MB RSS, and
    twice over in :func:`eververse_message_constructor`, where both lookups are live at
    once.
    """
    return manifest_table["DestinyVendorDefinition"].hashes_by_field_prefix(
        "vendorIdentifier", prefix
    )


def _exotic_ornament_target_name(
    item: api.DestinyItem, manifest_table: dict[str, t.Any]
) -> str | None:
    """Resolve the exotic an exotic ornament reskins, or ``None``.

    Only exotic ornaments (``traitIds`` containing ``item.ornament.weapon`` /
    ``item.ornament.armor``) carry a base item; their manifest description reads
    "...change the appearance of <Exotic>." Other exotic cosmetics (ghosts, ships,
    vehicles, emotes) and anything that doesn't match return ``None`` so no suffix
    is added.
    """
    manifest_entry = manifest_table["DestinyInventoryItemDefinition"].get(item.hash, {})

    trait_ids = manifest_entry.get("traitIds") or []
    if not _ORNAMENT_TRAIT_IDS.intersection(trait_ids):
        return None

    description = manifest_entry.get("displayProperties", {}).get("description", "")
    match = _ORNAMENT_TARGET_RE.search(description)
    return match.group(1) if match else None


# Class names that appear as ``item.class_`` on class-specific (armor ornament) items.
_CLASS_NAMES = ("Hunter", "Titan", "Warlock")


def _eververse_type_group(item: api.DestinyItem) -> tuple[int, str, str]:
    """Return ``(order, emoji_name, header)`` for an item's display group.

    Type-first grouping: armor ornaments first (one group, each line tagged with its
    class emoji), then weapon ornaments, ghosts, vehicles, and finally every other
    cosmetic under its own pluralised type name. ``emoji_name`` is "" when no server
    emoji fits the group; only the names verified present in the Kyber server are used.
    """
    if item.class_ in _CLASS_NAMES:  # class-specific armor ornament
        return (0, "armor", "Armor Ornaments")
    type_name = item.item_type_friendly_name or "Other"
    if "Weapon Ornament" in type_name:
        return (1, "weapon", "Weapon Ornaments")
    if "Ghost" in type_name:
        return (2, "ghost", "Ghosts")
    if type_name in ("Ship", "Vehicle", "Sparrow"):
        return (3, "sparrow", "Ships & Sparrows")
    if "Emote" in type_name:  # merge "Emote" + "Multiplayer Emote"
        return (4, "", "Emotes")
    return (4, "", f"{type_name}s")


def _group_eververse_offerings(
    items: list[api.DestinyItem],
) -> list[tuple[str, str, list[api.DestinyItem]]]:
    """Bucket items into ordered ``(emoji_name, header, items)`` display groups.

    Groups are ordered by :func:`_eververse_type_group`'s rank then header; each
    group's items are sorted by name."""
    groups: defaultdict[tuple[int, str, str], list[api.DestinyItem]] = defaultdict(list)
    for item in items:
        groups[_eververse_type_group(item)].append(item)
    return [
        (emoji, header, sorted(groups[key], key=lambda i: i.name))
        for key in sorted(groups, key=lambda k: (k[0], k[2]))
        for _order, emoji, header in (key,)
    ]


# Item types in the "Ships & Sparrows" group, mapped to their inline label (sparrows
# are the "Vehicle" item type in the manifest).
_SHIP_SPARROW_LABEL = {"Ship": "Ship", "Vehicle": "Sparrow", "Sparrow": "Sparrow"}


def _eververse_line(
    item: api.DestinyItem,
    manifest_table: dict[str, t.Any] | None,
    currency: str = "Bright Dust",
) -> str:
    """One rendered offering line: ``• [name](url) — cost (… target) · subtype``.

    Every line starts with the item name for a uniform look; costs are bare numbers
    (the section header notes the currency). Armor ornaments put their class emoji
    inside the parens before the exotic they reskin — ``(:titan: Hallowfire Heart)`` —
    or the class emoji alone when no target resolves; weapon ornaments show just the
    exotic; ships/sparrows get a Ship/Sparrow subtype label."""
    line = f"• [**{item.name}**]({item.lightgg_url}) — {item.costs.get(currency, 0)}"
    target = (
        _exotic_ornament_target_name(item, manifest_table)
        if item.is_exotic and manifest_table is not None
        else None
    )
    if item.class_ in _CLASS_NAMES:  # armor ornament
        class_emoji = f":{item.class_.lower()}:"
        line += f" ({class_emoji} {target})" if target else f" ({class_emoji})"
    elif target:  # weapon ornament
        line += f" ({target})"
    subtype = _SHIP_SPARROW_LABEL.get(item.item_type_friendly_name)
    if subtype:
        line += f" · {subtype}"
    return line


async def _fetch_daily_rotator_offerings(
    webserver_runner: aiohttp.web.AppRunner,
    *,
    rotator_prefix: str,
    currency: str,
) -> tuple[list[api.DestinyItem], dict[str, t.Any]]:
    """Fetch the deduped items across all active rotator vendors matching
    ``rotator_prefix`` whose cost includes ``currency``.

    Returns the items (deduped by item hash) plus the manifest table, which the
    renderer needs for the exotic-ornament base-item lookup. Inactive rotators
    (``VendorNotFound``) are skipped so the post still succeeds.
    """
    access_token = await api.refresh_api_tokens(webserver_runner)

    async with aiohttp.ClientSession() as session:
        memberships = await api.client.fetch_memberships(session, access_token)
        membership = api.DestinyMembership.from_api_response(memberships)
        profile = await api.client.fetch_profile(
            session,
            access_token,
            membership.membership_type,
            membership.membership_id,
        )
        # Armor ornaments are class-specific: a vendor only returns the queried
        # character's class's ornaments, so the rotators must be queried once per
        # class to surface every class's offerings. Class-agnostic cosmetics (ghosts,
        # ships, shaders, weapon ornaments, …) come back identically for each and
        # dedupe by item hash below. Each item's ``class_`` is set from the manifest.
        character_ids = [
            membership.parse_character_id(profile, class_)
            for class_ in ("Hunter", "Titan", "Warlock")
        ]

    manifest_table = await api._build_manifest_dict(
        await api._get_latest_manifest(schemas.BungieCredentials.api_key)
    )
    rotator_hashes = _rotator_hashes(manifest_table, rotator_prefix)

    cost_match = currency.lower()
    items: dict[int, api.DestinyItem] = {}  # dedupe by item hash across classes
    for character_id in character_ids:
        for vendor_hash in rotator_hashes:
            try:
                response = await api.client.fetch_vendor(
                    access_token=access_token,
                    membership_type=membership.membership_type,
                    membership_id=membership.membership_id,
                    character_id=character_id,
                    vendor_hash=vendor_hash,
                )
            except api.VendorNotFound:
                # Rotator is not currently active; skip it.
                continue

            vendor = api.DestinyVendor.from_vendors_api_response(
                response=response, manifest_table=manifest_table
            )
            for sale_item in vendor.sale_items:
                if cost_match in str(sale_item.costs).lower():
                    items.setdefault(sale_item.hash, sale_item)

    return list(items.values()), manifest_table


async def fetch_daily_bright_dust_offerings(
    webserver_runner: aiohttp.web.AppRunner,
) -> tuple[list[api.DestinyItem], dict[str, t.Any]]:
    """Fetch the deduped daily bright-dust rotator items + the manifest table."""
    return await _fetch_daily_rotator_offerings(
        webserver_runner,
        rotator_prefix=api.EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX,
        currency="Bright Dust",
    )


async def fetch_daily_silver_offerings(
    webserver_runner: aiohttp.web.AppRunner,
) -> tuple[list[api.DestinyItem], dict[str, t.Any]]:
    """Fetch the deduped featured daily Silver rotator items + the manifest table.

    Items the bot account already owns are reported at 0 Silver by the vendor; drop
    them so the post never shows a misleading "— 0".
    """
    items, manifest_table = await _fetch_daily_rotator_offerings(
        webserver_runner,
        rotator_prefix=api.EVERVERSE_SILVER_ROTATOR_PREFIX,
        currency="Silver",
    )
    items = [item for item in items if item.costs.get("Silver", 0) > 0]
    return items, manifest_table


async def eververse_message_constructor(bot: CachedFetchBot) -> HMessage:
    classes = ["Hunter", "Titan", "Warlock"]
    sale_items_dict: dict[str, set[api.DestinyItem]] = defaultdict(set)
    eververse_data: api.DestinyVendor | None = None
    for class_ in classes:
        eververse_data = await xur.fetch_vendor_data(
            api.get_webserver_runner(),
            vendor_hashes=3361454721,
            character_class=class_,
        )
        for sale_item in eververse_data.sale_items:
            sale_items_dict[class_].add(sale_item)

    if eververse_data is None:
        raise RuntimeError("No eververse vendor data was fetched")

    # Eververse offers some items to only one class and some to every class.
    # Split the per-class sets into class-specific items (present in exactly one
    # class's set) and common items (present in all three) so each group can be
    # labelled with its class and rendered once.
    hunter_sale_items = sale_items_dict["Hunter"] - (
        sale_items_dict["Titan"] | sale_items_dict["Warlock"]
    )
    titan_sale_items = sale_items_dict["Titan"] - (
        sale_items_dict["Hunter"] | sale_items_dict["Warlock"]
    )
    warlock_sale_items = sale_items_dict["Warlock"] - (
        sale_items_dict["Hunter"] | sale_items_dict["Titan"]
    )
    common_sale_items = (
        sale_items_dict["Hunter"]
        & sale_items_dict["Titan"]
        & sale_items_dict["Warlock"]
    )

    for class_, class_specific_sale_items in zip(
        classes,
        [hunter_sale_items, titan_sale_items, warlock_sale_items],
        strict=True,
    ):
        for sale_item in class_specific_sale_items:
            sale_item.class_ = class_

    eververse_data.sale_items = list(
        hunter_sale_items | titan_sale_items | warlock_sale_items | common_sale_items
    )

    daily_items, daily_manifest_table = await fetch_daily_bright_dust_offerings(
        api.get_webserver_runner()
    )
    silver_items, _ = await fetch_daily_silver_offerings(api.get_webserver_runner())

    return await format_eververse_vendor(
        eververse_data,
        bot,
        daily_items=daily_items,
        silver_items=silver_items,
        manifest_table=daily_manifest_table,
    )


def _render_offering_groups(
    groups: list[tuple[str, str, list[api.DestinyItem]]],
    manifest_table: dict[str, t.Any] | None,
    currency: str,
) -> str:
    """Render the type-grouped offering lines for one currency pool as markdown."""
    blocks: list[str] = []
    for emoji_name, header, items in groups:
        header_prefix = f":{emoji_name}: " if emoji_name else ""
        lines = [f"{header_prefix}**{header}**"]
        lines += [_eververse_line(item, manifest_table, currency) for item in items]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


async def format_eververse_vendor(
    vendor: api.DestinyVendor,
    bot: CachedFetchBot,
    daily_items: list[api.DestinyItem] | None = None,
    silver_items: list[api.DestinyItem] | None = None,
    manifest_table: dict[str, t.Any] | None = None,
) -> HMessage:
    emoji_dict = await fetch_emoji_dict(bot)
    daily_items = daily_items or []

    # Merge the weekly "This Week at Eververse" items with the daily rotator items
    # into one Bright-Dust pool (deduped by item hash), excluding DokiDoki bundles,
    # then present grouped by item type (see _group_eververse_offerings).
    pool: dict[int, api.DestinyItem] = {}
    for sale_item in [*vendor.sale_items, *daily_items]:
        if "bright dust" not in str(sale_item.costs).lower():
            continue
        # Manually exclude DokiDoki Bundles returned from the API (remove once the
        # API stops returning them).
        if sale_item.name.startswith("Doki Doki Destiny "):
            continue
        pool.setdefault(sale_item.hash, sale_item)

    bright_dust_body = (
        _render_offering_groups(
            _group_eververse_offerings(list(pool.values())),
            manifest_table,
            "Bright Dust",
        )
        or "No Bright Dust offerings are available right now."
    )
    silver_groups = _group_eververse_offerings(silver_items or [])

    # Components V2: a container with one text display per block (raw :emoji: tokens),
    # with separators giving visual space between each section's heading and contents.
    container = h.impl.ContainerComponentBuilder(
        accent_color=await settings.get_embed_default_color()
    )
    container.add_text_display(
        "# :eververse: [Today 𝘢𝘵 Eververse](https://kyber3000.com/Eververse)"
    )
    container.add_separator(divider=True)
    container.add_text_display("## :bright_dust: Bright Dust Offerings")
    container.add_separator(spacing=h.SpacingType.SMALL, divider=False)
    container.add_text_display(bright_dust_body)

    if silver_groups:
        container.add_separator(divider=True)
        container.add_text_display("## :silver: Silver Offerings")
        container.add_separator(spacing=h.SpacingType.SMALL, divider=False)
        container.add_text_display(
            _render_offering_groups(silver_groups, manifest_table, "Silver")
        )

    container.add_separator(divider=True)
    # Optional default banner, edited from the autopost settings webpage. A blank/unset
    # URL means "no image"; a non-empty one is appended just above the footer buttons.
    default_image_url = await schemas.AutoPostSettings.get_eververse_image_url()
    if default_image_url:
        container.add_component(components.url_media_gallery(default_image_url))
    container.add_component(components.footer_buttons_row(guides=EVERVERSE_GUIDES))

    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert
    # on overflow — an oversized day trims Silver first, then drops the tail).
    return await components.finalize_cv2_post(
        HMessage(components=[container]),
        emoji_dict,
        post_name=dd_feeds.FEEDS["eververse"].display_name,
    )


@loader.listener(h.StartedEvent)
async def on_start_schedule_autoposts(
    event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
):
    # Run daily at 17:00 UTC (the post carries the daily bright-dust + silver rotators)
    @aiocron.crontab("0 17 * * *", start=True)
    # Use below crontab for testing to post every minute
    # @aiocron.crontab("* * * * *", start=True)
    async def autopost_eververse():
        await xur.api_to_discord_announcer(
            bot,
            channel_id=await settings.get_followable_channel("eververse"),
            check_enabled=True,
            enabled_check_coro=schemas.AutoPostSettings.get_eververse_enabled,
            construct_message_coro=eververse_message_constructor,
            cv2=True,
        )


# Contribute this feed's producer wiring to the web feed page (Preview / Send now).
register_feed(
    Feed(
        name="eververse",
        message_constructor_coro=eververse_message_constructor,
        message_announcer_coro=xur.api_to_discord_announcer,
    )
)
