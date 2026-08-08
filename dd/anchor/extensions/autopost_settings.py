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

"""Autopost + general settings page for the anchor web control panel.

A single owner-only page (linked from the control-panel homepage via
:func:`web.register_card`) that is now the sole editor for two things that used to be
separate:

- Every **global** autopost produce toggle — unchanged from before, one ``name`` row in
  :class:`~dd.common.schemas.AutoPostSettings` each. The scattered ``/<feed> auto``
  slash commands (plus ``/ls details`` and ``/xur default_image``) duplicated this and
  were removed 2026-08-04.
- Every setting :mod:`dd.common.settings` resolves — colors, the default link URL, the
  alert level, the log/alerts channels, ``disable_bad_channels``, and every followable's
  post channel. These used to be env vars (``EMBED_DEFAULT_COLOR``, ``FOLLOWABLES``,
  ...) that needed a redeploy to change; they are the same ``auto_post_settings`` table
  rows, just not tied to a feed toggle. A followable with an existing feed toggle above
  gets its channel field folded into that feed's group; the rest (beacon-only feeds with
  no toggle — twab, trials, weekly_reset, weekly_nightfall, free_games,
  emblems_and_cosmetics) get their own single-row group.

Scope is settings only — no "send now" / preview beyond the existing per-feed actions,
and no per-guild follow management (that is end-user ``/autopost <feed>`` territory,
stored as ``MirroredChannel`` rows). A missing toggle row reads as ``None``, which every
producer treats as *off*, so the page renders ``bool(get_enabled(slug))`` and lets
``set_enabled`` upsert on save. Authentication is handled centrally by the Discord-OAuth
middleware in ``web_auth.py`` (it protects every non-allowlisted route, so this module
needs no auth code).
"""

import html
import logging
import re
import typing as t
from pathlib import Path

import aiohttp.web
import hikari as h
import lightbulb as lb

from ...common import (
    cfg,
    schemas,
    settings as dd_settings,
)
from ...common.bot import CachedFetchBot
from .. import web
from ..autopost import registered_feeds

logger = logging.getLogger(__name__)

# No commands live here, but load_extensions_strict → load_extensions requires every
# extension module to expose a Loader, so define one (it also carries the StartedEvent
# listener that stashes the bot, below).
loader = lb.Loader()

_PAGE_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "autopost_settings.html"
)
_TOGGLES_PLACEHOLDER = "<!--__TOGGLES__-->"

# The live bot, stashed at StartedEvent so /autopost_settings/channels can list guild
# channels (the pattern every other route-owning extension here uses — see
# control_panel.py's identical stash).
_bot: CachedFetchBot | None = None


@loader.listener(h.StartedEvent)
async def _on_started(_event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED):
    global _bot
    _bot = bot


_ALERT_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class _Setting(t.NamedTuple):
    """One setting row: an ``AutoPostSettings`` toggle, or one of the general/followable
    settings :mod:`dd.common.settings` resolves.

    ``slug`` is the ``AutoPostSettings.name`` primary key; ``label`` is display copy;
    ``desc`` is a one-line explanation shown under the label; ``sub`` marks a row that
    shares its predecessor's group box (rendered indented) rather than starting a new
    one — e.g. ``lost_sector_details`` under ``lost_sector``. ``kind`` picks the control
    and which ``AutoPostSettings`` column backs it:

    - ``"toggle"`` — an on/off switch, the ``enabled`` column.
    - ``"url"`` — a text input, the ``value`` column (e.g. ``eververse_image_url``).
    - ``"color"`` — a colour swatch + hex text pair, ``value`` as ``"#RRGGBB"``.
    - ``"select"`` — a native dropdown over ``options``, ``value`` as the chosen string.
    - ``"channel"`` — a searchable channel picker (see autopost_settings.js), ``value``
      as ``str(channel_id)`` (``"0"`` for "none configured" — never NULL, unlike a
      cleared url/color row, so an explicit clear reads as *dormant* rather than falling
      through to the ``FOLLOWABLES`` env-var seed — see
      ``dd.common.settings.get_followable_channel``). ``channel_scope`` picks which
      guild(s) the picker offers: ``"kyber"`` (where every followable posts) or
      ``"kyber_control"`` (log/alerts channels, which could be in either).
    """

    slug: str
    label: str
    desc: str
    sub: bool
    kind: str = "toggle"
    options: tuple[str, ...] = ()
    channel_scope: str = "kyber"
    #: For "channel": restrict the picker to Discord announcement channels. True for
    #: every followable's post channel — a channel other servers *follow* (crossposting,
    #: see MirroredChannel) must be an announcement channel, a plain text channel cannot
    #: be followed at all — and the default, since that covers most "channel" settings.
    #: False for log_channel_id/alerts_channel_id: nothing follows them, the bot only
    #: sends there directly, so a plain text channel works fine too.
    announce_only: bool = True
    #: For "select": which option is pre-selected when no row is saved yet. Must match
    #: dd.common.settings' own default for this slug — otherwise the page shows the
    #: first listed option "selected" (an HTML <select> always has one) while the bot
    #: is actually using a different value, which is worse than showing nothing.
    default: str = ""
    #: A general (non-feed) group's display title, set on the group's FIRST setting only
    #: (``sub=False``) — a feed group's own toggle row already names it, so a feed
    #: setting leaves this blank. Rendered above the group's rows rather than reusing
    #: that first setting's own ``label``, since the header names the *category*
    #: ("Branding"), not that one row ("Default accent colour").
    category: str = ""


# Ordered for display: each sub-row immediately follows its parent, and a parent always
# precedes its subs (required by _render_html's single-pass grouping). The first block
# (no feed toggle) is the general/ops settings; the rest mirror the feed-toggle rows
# that already existed, each gaining its followable channel (and, for two feeds, an
# image URL that used to be a separate env var).
_SETTINGS: tuple[_Setting, ...] = (
    # --- Branding: colours + the fallback link, all "how a post looks by default" ----
    _Setting(
        "embed_default_color",
        "Default accent colour",
        "The accent bar on nearly every embed and CV2 post, when nothing else sets "
        "one.",
        False,
        "color",
        category="Branding",
    ),
    _Setting(
        "embed_error_color",
        "Error accent colour",
        "Shown on error/failure messages (e.g. a permissions problem).",
        True,
        "color",
    ),
    _Setting(
        "default_url",
        "Default link URL",
        "Fallback link target for a post with no URL of its own (e.g. an image-only "
        "autoembed).",
        True,
        "url",
    ),
    # --- Logging & Alerts: the ops pipeline that forwards records to Discord ---------
    _Setting(
        "alert_min_level",
        "Alert level",
        "Minimum log severity forwarded to the alerts channel.",
        False,
        "select",
        options=_ALERT_LEVELS,
        default="ERROR",
        category="Logging & Alerts",
    ),
    _Setting(
        "disable_bad_channels",
        "Auto-disable unreachable mirrors",
        "Disable a legacy mirror destination once it stays unreachable past the grace "
        "window.",
        True,
    ),
    _Setting(
        "log_channel_id",
        "Log channel",
        "Where each mirror run's one-line result summary posts. Inert (nothing is "
        "sent) while unset.",
        True,
        "channel",
        channel_scope="kyber_control",
        announce_only=False,
    ),
    _Setting(
        "alerts_channel_id",
        "Alerts channel",
        "Where forwarded ERROR+ log records (and owner pings) post. Inert (nothing is "
        "sent) while unset.",
        True,
        "channel",
        channel_scope="kyber_control",
        announce_only=False,
    ),
    # --- Lost Sector ------------------------------------------------------------------
    _Setting(
        "lost_sector",
        "Lost Sector",
        "Today's Lost Sector — location, champions, and shields.",
        False,
    ),
    _Setting(
        "lost_sector_details",
        "Legendary weapon details",
        "Also list the featured legendary weapon rewards.",
        True,
    ),
    _Setting(
        "lost_sector_channel",
        "Post to channel",
        "The Kyber channel this feed posts to.",
        True,
        "channel",
    ),
    _Setting(
        "lost_sector_image_url",
        "Default image URL",
        "Image shown at the bottom of each Lost Sector post. Leave blank for none.",
        True,
        "url",
    ),
    # --- Xûr ----------------------------------------------------------------------
    _Setting("xur", "Xûr", "Xûr's weekend location and inventory.", False),
    _Setting(
        "xur_default_image",
        "Use default image",
        "Fall back to a saved banner when no fresh image is available.",
        True,
    ),
    _Setting(
        "xur_channel",
        "Post to channel",
        "The Kyber channel this feed posts to.",
        True,
        "channel",
    ),
    _Setting(
        "xur_image_url",
        "Default image URL",
        "The saved banner used when 'Use default image' is on. Leave blank for none.",
        True,
        "url",
    ),
    # --- Eververse ----------------------------------------------------------------
    _Setting(
        "eververse",
        "Eververse",
        "This week's Eververse featured items and Bright Dust.",
        False,
    ),
    _Setting(
        "eververse_image_url",
        "Default image URL",
        "Banner shown at the bottom of each Eververse post. Leave blank for none.",
        True,
        "url",
    ),
    _Setting(
        "eververse_channel",
        "Post to channel",
        "The Kyber channel this feed posts to.",
        True,
        "channel",
    ),
    # --- Ada-1 ----------------------------------------------------------------------
    _Setting("ada", "Ada-1", "Ada-1's weekly rotating shaders.", False),
    _Setting(
        "ada_channel",
        "Post to channel",
        "The Kyber channel this feed posts to.",
        True,
        "channel",
    ),
    # --- Portal Ops -------------------------------------------------------------------
    _Setting(
        "portal_ops",
        "Portal Ops",
        "Today's featured Portal Ops and their guaranteed rewards.",
        False,
    ),
    _Setting(
        "portal_ops_channel",
        "Post to channel",
        "The Kyber channel this feed posts to. Leave unset to keep this feed dormant.",
        True,
        "channel",
    ),
    # --- Iron Banner --------------------------------------------------------------
    _Setting(
        "iron_banner",
        "Iron Banner",
        "Iron Banner weeks — dates, game modes, bonus focus pool, and guide link.",
        False,
    ),
    _Setting(
        "iron_banner_channel",
        "Post to channel",
        "The Kyber channel this feed posts to. Leave unset to keep this feed dormant.",
        True,
        "channel",
    ),
    # --- Beacon-only feeds: no enable/disable toggle exists for these, so each is a
    # single-row group of just its channel (gated purely on whether one is set). -------
    _Setting(
        "twab_channel",
        "This Week At Bungie",
        "The Kyber channel TWAB posts follow from.",
        False,
        "channel",
    ),
    _Setting(
        "trials_channel",
        "Trials of Osiris",
        "The Kyber channel this feed posts to. Content is edited on the Trials form.",
        False,
        "channel",
    ),
    _Setting(
        "weekly_reset_channel",
        "Weekly Reset",
        "The Kyber channel this feed posts to. Content is edited on the Weekly Reset "
        "form.",
        False,
        "channel",
    ),
    _Setting(
        "weekly_nightfall_channel",
        "Weekly Nightfall",
        "The Kyber channel weekly nightfall posts follow from.",
        False,
        "channel",
    ),
    _Setting(
        "free_games_channel",
        "Free Games",
        "The Kyber channel free-games posts follow from.",
        False,
        "channel",
    ),
    _Setting(
        "emblems_and_cosmetics_channel",
        "Emblems & Cosmetics",
        "The Kyber channel emblems/cosmetics posts follow from.",
        False,
        "channel",
    ),
)

# The slugs this page is allowed to write — a save request's keys are filtered against
# this so an unknown/forged key can never create a stray AutoPostSettings row. Split by
# kind so a save routes each to the right column and validation.
_TOGGLE_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "toggle")
_URL_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "url")
_COLOR_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "color")
_SELECT_OPTIONS = {s.slug: s.options for s in _SETTINGS if s.kind == "select"}
_CHANNEL_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "channel")


def _render_row(
    setting: _Setting,
    state: bool | str | None,
    *,
    flat: bool = False,
    alt: bool = False,
) -> str:
    """Render one settings row: label + description, then its control.

    ``flat`` overrides the indented/dimmer ".sub" styling ``setting.sub`` would
    otherwise select. It's set by the caller for every row in a *categorised* general
    group (Branding, Logging & Alerts, ...): every setting there is a peer under the
    category header, not a refinement of whichever setting happens to be first — unlike
    a feed group, where ``lost_sector_details`` genuinely IS a sub-setting of
    ``lost_sector``. Without it, the first setting in a category (which must have
    ``sub=False`` — something has to start the group) would render as if it were that
    category's "parent" row, visually singling it out for no reason other than being
    first in the list.

    ``alt`` (only meaningful when ``flat``) zebra-stripes every other row via
    ``.flat-alt`` — the same dimmer background ``.sub`` uses, without the indent or
    smaller name, so a categorised group keeps the two-tone rhythm of a feed group's
    parent/sub rows without implying any row there is a "child" of another.
    """
    base_class = "row"
    if flat:
        if alt:
            base_class = "row flat-alt"
    elif setting.sub:
        base_class = "row sub"

    def _label_block(actions_html: str = "") -> str:
        return (
            '<div class="text">'
            f'<div class="name">{html.escape(setting.label)}</div>'
            f'<div class="desc">{html.escape(setting.desc)}</div>'
            f"{actions_html}"
            "</div>"
        )

    # A top-level slug that names a registered feed gets its two actions inline — the
    # replacement for the old `/<feed> show` and `send` commands. They live here rather
    # than on a per-feed page: a feed has no state a page could show that this row does
    # not already, so a page would be a click in the way. The rendered post appears in
    # this page's modals (see autopost_settings.js), not in the row.
    actions = (
        '<div class="rowactions">'
        f'<button type="button" class="feedaction small" data-action="preview"'
        f' data-slug="{html.escape(setting.slug)}"'
        ' title="Builds the post exactly as the producer would right now, and shows it.'
        " Nothing is sent. The data comes from the live API, so this can take a few"
        ' seconds.">Preview</button>'
        f'<button type="button" class="feedaction small" data-action="send"'
        f' data-slug="{html.escape(setting.slug)}"'
        ' title="Posts to this feed&#39;s channel immediately, and (if publishing)'
        ' crossposts it so beacon mirrors it to every following server.">Send now'
        "</button>"
        "</div>"
    )
    if setting.sub or setting.slug not in registered_feeds():
        actions = ""
    label_block = _label_block(actions)

    if setting.kind == "url":
        value = html.escape(state or "") if isinstance(state, str) else ""
        return (
            f'<div class="{base_class} urlrow">'
            f"{_label_block()}"
            '<input type="url" class="urlfield" '
            f'data-slug="{html.escape(setting.slug)}"'
            f' value="{value}" placeholder="https://example.com/banner.png" />'
            "</div>"
        )

    if setting.kind == "color":
        hex_value = (
            state if isinstance(state, str) and _HEX_COLOR_RE.match(state) else ""
        )
        swatch_value = hex_value or "#000000"
        return (
            f'<div class="{base_class} colorrow">'
            f"{_label_block()}"
            '<div class="colorpicker">'
            '<input type="color" class="colorswatch" '
            f'data-for="{html.escape(setting.slug)}"'
            f' value="{html.escape(swatch_value)}" />'
            '<input type="text" class="colorfield no-focus-ring" '
            f'data-slug="{html.escape(setting.slug)}"'
            f' value="{html.escape(hex_value)}" placeholder="#RRGGBB"'
            ' maxlength="7" />'
            "</div>"
            "</div>"
        )

    if setting.kind == "select":
        current = state if isinstance(state, str) and state else setting.default
        opts = "".join(
            f'<option value="{html.escape(opt)}"'
            f"{' selected' if opt == current else ''}>{html.escape(opt)}</option>"
            for opt in setting.options
        )
        return (
            f'<div class="{base_class} selectrow">'
            f"{_label_block()}"
            f'<select class="selectfield" data-slug="{html.escape(setting.slug)}">'
            f"{opts}"
            "</select>"
            "</div>"
        )

    if setting.kind == "channel":
        channel_id = state if isinstance(state, str) and state not in ("", "0") else ""
        # A pre-JS/no-JS fallback option so the field still submits something sane —
        # autopost_settings.js replaces this with real channel names once its fetch of
        # /autopost_settings/channels resolves (see that file for why a raw id is kept
        # as a synthetic option if the live channel list doesn't contain it anymore).
        current_opt = (
            f'<option value="{html.escape(channel_id)}" selected>'
            f"{html.escape(channel_id)}</option>"
            if channel_id
            else ""
        )
        return (
            f'<div class="{base_class} channelrow">'
            f"{_label_block()}"
            f'<select class="channelfield" data-slug="{html.escape(setting.slug)}"'
            f' data-scope="{html.escape(setting.channel_scope)}"'
            f' data-announce-only="{"true" if setting.announce_only else "false"}">'
            '<option value="">— none configured —</option>'
            f"{current_opt}"
            "</select>"
            "</div>"
        )

    checked = " checked" if state else ""
    return (
        f'<div class="{base_class}">'
        f"{label_block}"
        '<label class="switch">'
        f'<input type="checkbox" class="no-focus-ring" '
        f'data-slug="{html.escape(setting.slug)}"{checked} />'
        '<span class="slider"></span>'
        "</label>"
        "</div>"
    )


async def _current_state(setting: _Setting, session: t.Any) -> bool | str | None:
    if setting.kind == "toggle":
        return bool(
            await schemas.AutoPostSettings.get_enabled(setting.slug, session=session)
        )
    return await schemas.AutoPostSettings.get_value(setting.slug, session=session)


def _wrap_group(
    entries: list[tuple[_Setting, bool | str | None]], category: str
) -> str:
    # Every row in a categorised group renders flat (no .sub indent/dim) — see
    # _render_row's `flat` docs: within a category, the first setting is not that
    # category's "parent", just whichever one happens to start the list. Every other
    # one alternates onto .flat-alt (`alt`) so the group keeps a two-tone background.
    flat = bool(category)
    rows = "".join(
        _render_row(setting, state, flat=flat, alt=flat and bool(idx % 2))
        for idx, (setting, state) in enumerate(entries)
    )
    header = (
        f'<div class="groupheader">{html.escape(category)}</div>' if category else ""
    )
    return f'<div class="group">{header}{rows}</div>'


async def _render_html() -> str:
    """Render the settings page with the current DB state substituted in.

    A top-level setting (``sub`` is False) and every sub-setting that follows it share
    one ``.group`` box, so a feed and its content/channel/url sub-rows read as one
    category. A parent always precedes its subs in ``_SETTINGS``, so a single pass
    groups them. A general (non-feed) group's ``category`` (set on its first setting)
    renders as an explicit header — a feed group needs none, since its own toggle row
    already names it. Rows aren't rendered until their whole group is collected (see
    ``_wrap_group``), since whether a row renders flat depends on the group's category,
    which isn't known until the group's first (``sub=False``) setting is reached.
    """
    groups: list[str] = []
    current: list[tuple[_Setting, bool | str | None]] = []
    current_category = ""
    async with schemas.db_session() as session:
        for setting in _SETTINGS:
            state = await _current_state(setting, session)
            if setting.sub:
                current.append((setting, state))
            else:
                if current:
                    groups.append(_wrap_group(current, current_category))
                current = [(setting, state)]
                current_category = setting.category
        if current:
            groups.append(_wrap_group(current, current_category))
    return _PAGE_HTML_PATH.read_text(encoding="utf-8").replace(
        _TOGGLES_PLACEHOLDER, "".join(groups)
    )


async def _handle_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # Auth is enforced by the web_auth middleware; this just renders the page.
    return aiohttp.web.Response(text=await _render_html(), content_type="text/html")


# Channel types a post can actually go to (text + announcement); voice/category/forum/
# etc. are not valid autopost/log/alert destinations.
_POSTABLE_CHANNEL_TYPES = (h.ChannelType.GUILD_TEXT, h.ChannelType.GUILD_NEWS)


async def _handle_channels(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """List postable channels in Kyber + the control server, for the channel pickers.

    One fetch covers every channel field on the page — autopost_settings.js filters by
    each field's ``data-scope`` (which guild(s)) and ``data-announce-only`` (whether a
    plain text channel is even eligible: a followable's post channel must be an
    announcement channel for Discord's native "Follow Channel" to work at all, unlike
    log_channel_id/alerts_channel_id, which the bot only ever sends to directly) —
    rather than one REST round trip per field. Best-effort per guild: a guild the bot
    cannot currently see (not joined, or a permissions/REST blip) is simply omitted
    rather than failing the whole list — the affected pickers fall back to their
    current raw id (see autopost_settings.js).
    """
    if _bot is None:
        return aiohttp.web.json_response(
            {"error": "Bot is still starting."}, status=503
        )

    guild_ids = {cfg.kyber_discord_server_id, cfg.control_discord_server_id}
    channels: list[dict[str, str | bool]] = []
    for guild_id in guild_ids:
        if not guild_id or guild_id == -1:
            continue
        try:
            guild_channels = await _bot.rest.fetch_guild_channels(guild_id)
        except Exception:
            logger.info("Could not list channels for guild %s", guild_id, exc_info=True)
            continue
        for channel in guild_channels:
            if channel.type not in _POSTABLE_CHANNEL_TYPES:
                continue
            channels.append(
                {
                    "id": str(channel.id),
                    "name": "#" + channel.name if channel.name else str(channel.id),
                    "guildId": str(guild_id),
                    "announce": channel.type == h.ChannelType.GUILD_NEWS,
                }
            )
    channels.sort(key=lambda c: str(c["name"]).casefold())
    return aiohttp.web.json_response(
        {
            "channels": channels,
            "kyberGuildId": str(cfg.kyber_discord_server_id),
            "controlGuildId": str(cfg.control_discord_server_id),
        }
    )


async def _handle_save(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # The middleware already enforced auth + Origin (CSRF); mirror weekly_reset's save.
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "Malformed body."}, status=400)

    settings_in = payload.get("settings")
    if not isinstance(settings_in, dict):
        return aiohttp.web.json_response(
            {"error": "Expected a 'settings' object."}, status=400
        )

    # Validate every non-toggle field up front so a bad value fails the whole save
    # (before opening a transaction) rather than persisting something a producer can't
    # use. A blank url/color field clears the setting (stored as NULL — "use the
    # default"); a blank channel field stores "0" (explicitly dormant — see _Setting's
    # "channel" kind docs on why that must NOT be NULL).
    url_values: dict[str, str | None] = {}
    for slug in _URL_SLUGS:
        if slug not in settings_in:
            continue
        raw = settings_in[slug]
        if not isinstance(raw, str):
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be a string."}, status=400
            )
        trimmed = raw.strip()
        if trimmed and not trimmed.startswith(("http://", "https://")):
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be an http(s) URL."}, status=400
            )
        url_values[slug] = trimmed or None

    color_values: dict[str, str | None] = {}
    for slug in _COLOR_SLUGS:
        if slug not in settings_in:
            continue
        raw = settings_in[slug]
        if not isinstance(raw, str):
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be a string."}, status=400
            )
        trimmed = raw.strip()
        if trimmed and not _HEX_COLOR_RE.match(trimmed):
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be a #RRGGBB colour."}, status=400
            )
        color_values[slug] = trimmed or None

    select_values: dict[str, str] = {}
    for slug, options in _SELECT_OPTIONS.items():
        if slug not in settings_in:
            continue
        raw = settings_in[slug]
        if raw not in options:
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be one of {', '.join(options)}."}, status=400
            )
        select_values[slug] = raw

    channel_values: dict[str, str] = {}
    for slug in _CHANNEL_SLUGS:
        if slug not in settings_in:
            continue
        raw = settings_in[slug]
        if isinstance(raw, str):
            raw = raw.strip()
        if raw in ("", None):
            channel_values[slug] = "0"
            continue
        try:
            channel_id = int(raw)
        except (TypeError, ValueError):
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be a Discord channel id."}, status=400
            )
        if channel_id < 0:
            return aiohttp.web.json_response(
                {"error": f"'{slug}' must be a Discord channel id."}, status=400
            )
        channel_values[slug] = str(channel_id)

    # Only known slugs are honoured; unknown keys are ignored (never trust the client's
    # key set to spawn rows). One transaction so a batch save is all-or-nothing.
    async with schemas.db_session() as session, session.begin():
        for slug, value in settings_in.items():
            if slug in _TOGGLE_SLUGS:
                await schemas.AutoPostSettings.set_enabled(
                    slug, bool(value), session=session
                )
        for slug, url in url_values.items():
            await schemas.AutoPostSettings.set_value(slug, url, session=session)
        for slug, color in color_values.items():
            await schemas.AutoPostSettings.set_value(slug, color, session=session)
        for slug, choice in select_values.items():
            await schemas.AutoPostSettings.set_value(slug, choice, session=session)
        for slug, channel_id in channel_values.items():
            await schemas.AutoPostSettings.set_value(slug, channel_id, session=session)

    # dd.common.settings caches every non-toggle row above (colors, urls, followable
    # channels, ...); drop the cache so this process picks the change up immediately
    # rather than waiting out the TTL (see that module's docstring).
    dd_settings.invalidate()

    return aiohttp.web.json_response({"ok": True})


def register_autopost_settings_routes(app: aiohttp.web.Application) -> None:
    """Add the autopost-settings routes to the shared persistent app."""
    app.router.add_get("/autopost_settings", _handle_get)
    app.router.add_get("/autopost_settings/channels", _handle_channels)
    app.router.add_post("/autopost_settings/save", _handle_save)


web.register_routes(register_autopost_settings_routes)
web.register_card(
    web.Card(
        "Autopost Settings",
        "Toggle which feeds anchor posts, and general bot settings",
        "/autopost_settings",
    )
)
