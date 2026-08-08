"""Shared rendering of the Lost Sector post used by both bots."""

import datetime as dt
import logging
import typing as t

import hikari as h

from dd.hmessage import HMessage

from ..common import components, schemas, settings
from ..common.utils import fetch_emoji_dict
from ..sector_accounting import sector_accounting
from .utils import follow_link_single_step, space

_elements = ["solar", "void", "arc", "stasis", "strand"]

# Last-known-good rotation, keyed by post type. Populated on every successful load and
# served only if the DB store is unreachable, so a transient DB blip never breaks an
# autopost.
_rotation_cache: dict[str, sector_accounting.Rotation] = {}

_LOST_SECTOR = "lost_sector"


async def load_rotation(buffer: int = 5) -> sector_accounting.Rotation:
    """Load the ``lost_sector`` rotation from the DB JSON store.

    Resolution order: the ``RotationData['lost_sector']`` document (built via
    :meth:`Rotation.from_json`) → the last-known-good cache. The DB is consulted every
    call (it is cheap and lets editor saves take effect immediately); the cache exists
    only for the total-outage case. Unlike Xûr, an absent row cannot degrade gracefully
    (there is no schedule to render), so this raises if the DB row is missing/malformed
    and nothing was ever cached.
    """
    try:
        doc = await schemas.RotationData.get_data(_LOST_SECTOR)
    except Exception:
        logging.exception("Failed to read lost_sector rotation from the DB")
        doc = None

    if doc is not None:
        try:
            rotation = sector_accounting.Rotation.from_json(doc, buffer=buffer)
            _rotation_cache[_LOST_SECTOR] = rotation
            return rotation
        except Exception:
            logging.exception("Stored lost_sector rotation JSON is malformed")

    cached = _rotation_cache.get(_LOST_SECTOR)
    if cached is not None:
        return cached
    raise RuntimeError(
        "No lost_sector rotation available (no usable DB row and empty cache)"
    )


def _elements_to_emoji(elements: str):
    elements = elements.lower()
    present_elements = []
    for element in _elements:
        if element in elements:
            present_elements.append(f":{element}:")
    return present_elements


# Static header/footer of the Lost Sector post (raw :emoji: tokens). Split out so both
# ``format_post`` (the live CV2 post) and ``build_body`` (the preview wall) share one
# layout.
_HEADER = (
    "**Destiny 2**\n"
    "## [World Lost Sectors](https://kyber3000.com/LS)\n"
    "\n"
    "Changes daily at <t:1753894800:t> local time.\n"
    "\n"
)
_FOOTER = (
    "Rewards:\n"
    ":enhancement_core: Enhancement Core\n"
    ":exotic_engram: Exotic Engram (If-Solo)\n"
    ":legendary_weap: Legendary Weapon (If-Solo)\n"
)

#: Post-specific footer guide button(s); Support is appended by
#: ``components.footer_button_specs``. The "more details" page that was a markdown link.
GUIDES: tuple[tuple[str, str], ...] = (("More Details", "https://lostsectortoday.com"),)


def build_body(sectors: list[sector_accounting.Sector], details_enabled: bool) -> str:
    """The Lost Sector post body markdown (raw ``:emoji:`` tokens) for a day's sectors.

    The same header + per-sector lines + footer ``format_post`` renders into the CV2
    text display, factored out so the web preview wall can show any day's post from the
    same body the live post uses, rather than duplicating the layout.
    ``details_enabled`` mirrors the ``AutoPostSettings`` champions/shields toggle.
    """
    body = ""
    for sector in sectors:
        body += f":LS: **[{sector.name}]({sector.shortlink_gfx})**\n"
        if details_enabled:
            body += format_data(sector)
    if not details_enabled:
        body += "\n"
    return _HEADER + body + _FOOTER


def format_data(sector: sector_accounting.Sector) -> str:
    expert_data = sector.expert_data
    master_data = sector.master_data

    champs_string = space.figure.join(
        ["Champions:"]
        + [
            f":{champ}:"
            for champ in set(expert_data.champions_list + master_data.champions_list)
        ]
    )
    shields_string = space.figure.join(
        ["Shields:"]
        + _elements_to_emoji(str(expert_data.shields_list + master_data.shields_list))
    )

    return "\n".join([champs_string, shields_string]) + "\n\n"


async def format_post(
    bot: h.GatewayBot | None = None,
    sectors: list[sector_accounting.Sector] | None = None,
    date: dt.datetime | None = None,
    emoji_dict: dict[str, h.Emoji] | None = None,
) -> HMessage:
    """Format a lost sector announcement message

    Args:
        bot (h.GatewayBot | None, optional): The bot instance. Must be specified if
        emoji_dict is not.

        sector (sector_accounting.Sector | None, optional): The sector to announce.
        Fetches today's sector if not specified

        secondary_image (h.Attachment | None, optional): The secondary image to embed.
        Defaults to None.

        secondary_embed_title (str | None, optional): The title of the secondary embed.
        Defaults to "".

        secondary_embed_description (str | None, optional): The description of the
        secondary embed. Defaults to "".

        date (dt.datetime | None, optional): The date to mention in the post announce.
        Defaults to None.

        emoji_dict (t.Dict[str, h.Emoji] | None, optional): The emoji dictionary must
        be specified if the bot is not specified.
    """

    if emoji_dict is None:
        if bot is None:
            raise ValueError("bot must be provided if emoji_dict is not")
        emoji_dict = t.cast(dict[str, h.Emoji], await fetch_emoji_dict(bot))

    if sectors is None:
        rotation = await load_rotation(buffer=5)
        sectors = rotation(date)

    # Follow the hyperlink to have the newest image embedded — but only if there is a
    # link at all: the image url is a DB setting now and may legitimately be blank.
    # follow_link_single_step must never be handed "" — aiohttp raises InvalidURL from
    # inside the helper's own retry loop, where it is caught as the ClientError subclass
    # it is, so a blank setting costs an ERROR alert per attempt plus the retry sleeps
    # (~2s) on every single post build, and never surfaces to the caller.
    ls_image_url_setting = await settings.get_lost_sector_image_url()
    ls_image_url = (
        await follow_link_single_step(ls_image_url_setting)
        if ls_image_url_setting
        else None
    )

    ls_extra_details_enabled = (
        await schemas.AutoPostSettings.get_lost_sector_details_enabled()
    )

    # Components V2: the former embed's title + description become one text display and
    # the trailing image a full-width media gallery (mirroring the old set_image). The
    # markdown (build_body) is unchanged so the post looks the same as the old embed did
    # — and is shared with the web preview wall.
    container = h.impl.ContainerComponentBuilder(
        accent_color=await settings.get_embed_default_color()
    )
    container.add_text_display(build_body(sectors, bool(ls_extra_details_enabled)))
    if ls_image_url:
        # URL-referenced (Discord fetches it) rather than uploaded — the image can be
        # large, which would 413 on upload and re-download from the host on every send.
        container.add_component(components.url_media_gallery(ls_image_url))
    container.add_component(components.footer_buttons_row(guides=GUIDES))

    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert
    # on overflow, measured on the final rendered length).
    return await components.finalize_cv2_post(
        HMessage(components=[container]), emoji_dict, post_name="Lost Sector"
    )


async def format_sector(sector: sector_accounting.Sector) -> str:
    """Formats a Sector object into an embed."""
    return f":LS: **[{sector.name}]({sector.shortlink_gfx})**\n"
