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
  ...) that needed a redeploy to change; they are now the same ``auto_post_settings``
  table rows, just not tied to a feed toggle — and this page is their ONLY writer: no
  env fallback and no seeding path exists behind it, so every value in the DB got there
  through the validation in :func:`_channel_problem` / :data:`_VALIDATORS`. A followable
  with an existing feed toggle above gets its channel field folded into that feed's
  group; the rest (beacon-only feeds with no toggle — twab, trials, weekly_reset,
  weekly_nightfall, free_games, emblems_and_cosmetics) get their own single-row group.

Scope is settings only — no "send now" / preview beyond the existing per-feed actions,
and no per-guild follow management (that is end-user ``/autopost <feed>`` territory,
stored as ``MirroredChannel`` rows). A missing toggle row reads as ``None``, which every
producer treats as *off*, so the page renders ``bool(get_enabled(slug))`` and lets
``set_enabled`` upsert on save. Authentication is handled centrally by the Discord-OAuth
middleware in ``web_auth.py`` (it protects every non-allowlisted route, so this module
needs no auth code).
"""

import asyncio
import html
import itertools
import logging
import re
import typing as t
from pathlib import Path

import aiohttp.web
import hikari as h
import lightbulb as lb
from toolbox.errors import CacheFailureError
from toolbox.members import calculate_permissions

from ...common import (
    cfg,
    feeds as dd_feeds,
    schemas,
    settings as dd_settings,
)
from .. import web
from ..autopost import registered_feeds

logger = logging.getLogger(__name__)

# No commands live here, but load_extensions_strict → load_extensions requires every
# extension module to expose a Loader, so define one.
loader = lb.Loader()

_PAGE_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "autopost_settings.html"
)
_TOGGLES_PLACEHOLDER = "<!--__TOGGLES__-->"

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
      as ``str(channel_id)`` (``"0"`` for "none configured" — an explicit clear stores
      "0" rather than NULL, so a cleared channel reads back as *dormant* rather than as
      "never set"; a followable's channel cannot be cleared at all, see
      :data:`_UNCLEARABLE_CHANNEL_SLUGS`). ``channel_scope`` picks which guild(s) the
      picker offers: ``"kyber"`` (where every followable posts) or ``"kyber_control"``
      (log/alerts channels, which could be in either).
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
    #: A general (non-feed) group's display title, set on the group's FIRST setting only
    #: (``sub=False``) — a feed group's own toggle row already names it, so a feed
    #: setting leaves this blank. Rendered above the group's rows rather than reusing
    #: that first setting's own ``label``, since the header names the *category*
    #: ("Branding"), not that one row ("Default accent colour").
    category: str = ""


# The seven global/ops rows, hand-written: they are not feeds, the catalog never
# mentions them, and each piece of their copy exists exactly once.
_GENERAL_SETTINGS: tuple[_Setting, ...] = (
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
)

# Per-feed page extras: the producer sub-options and image URLs that only some feeds
# have. Page-only copy with one consumer each, so it stays here rather than in the
# catalog — (sub-toggles, image-url rows), rendered around the channel row.
_FEED_EXTRA_ROWS: dict[str, tuple[tuple[_Setting, ...], tuple[_Setting, ...]]] = {
    "lost_sector": (
        (
            _Setting(
                "lost_sector_details",
                "Legendary weapon details",
                "Also list the featured legendary weapon rewards.",
                True,
            ),
        ),
        (
            _Setting(
                "lost_sector_image_url",
                "Default image URL",
                "Image shown at the bottom of each Lost Sector post. Leave blank for "
                "none.",
                True,
                "url",
            ),
        ),
    ),
    "xur": (
        (
            _Setting(
                "xur_default_image",
                "Use default image",
                "Fall back to a saved banner when no fresh image is available.",
                True,
            ),
        ),
        (
            _Setting(
                "xur_image_url",
                "Default image URL",
                "The saved banner used when 'Use default image' is on. Leave blank for "
                "none.",
                True,
                "url",
            ),
        ),
    ),
    "eververse": (
        (),
        (
            _Setting(
                "eververse_image_url",
                "Default image URL",
                "Banner shown at the bottom of each Eververse post. Leave blank for "
                "none.",
                True,
                "url",
            ),
        ),
    ),
}

# The two feeds that ship unconfigured, whose channel row says so. Page copy, two
# entries, pinned to catalog slugs by a test.
_DORMANT_NOTE_SLUGS = frozenset({"portal_ops", "iron_banner"})
_CHANNEL_DESC = "The Kyber channel this feed posts to."
_DORMANT_NOTE = (
    " Dormant until one is set; switch the feed off above to stop it after that."
)


def _feed_rows(feed: dd_feeds.Followable) -> tuple[_Setting, ...]:
    """One feed's settings rows, in display order.

    Shape follows from the feed's kind rather than from per-feed data: a feed anchor
    produces on a schedule leads with its produce toggle and hangs everything else off
    it; one whose content arrives another way is a single channel row carrying the
    feed's own name. Ordering is a rule — toggle, sub-toggles, channel, image URL — so
    _render_html's "a parent precedes its subs" precondition holds by construction
    rather than by careful hand-maintenance of a 218-line literal.
    """
    subs, urls = _FEED_EXTRA_ROWS.get(feed.slug, ((), ()))
    if not feed.has_toggle:
        # No produce toggle to lead with, so the channel row IS the group and wears the
        # feed's name; nothing else about these feeds is configurable here.
        return (
            _Setting(feed.channel_key, feed.display_name, feed.desc, False, "channel"),
        )
    desc = _CHANNEL_DESC + (_DORMANT_NOTE if feed.slug in _DORMANT_NOTE_SLUGS else "")
    return (
        _Setting(feed.slug, feed.display_name, feed.desc, False),
        *subs,
        _Setting(feed.channel_key, "Post to channel", desc, True, "channel"),
        *urls,
    )


# Ordered for display: the global rows, then each feed's group. Generated rather than
# written out, so the page cannot disagree with the catalog about which feeds exist or
# what they are called — the job the old hand-sync test used to do by watching.
_SETTINGS: tuple[_Setting, ...] = _GENERAL_SETTINGS + tuple(
    itertools.chain.from_iterable(_feed_rows(f) for f in dd_feeds.FOLLOWABLES)
)

# The slugs this page is allowed to write — a save request's keys are filtered against
# this so an unknown/forged key can never create a stray AutoPostSettings row. Split by
# kind so a save routes each to the right column and validation.
_TOGGLE_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "toggle")
_URL_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "url")
_COLOR_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "color")
_SELECT_OPTIONS = {s.slug: s.options for s in _SETTINGS if s.kind == "select"}
_CHANNEL_SLUGS = frozenset(s.slug for s in _SETTINGS if s.kind == "channel")
# Every channel setting by slug, so the save gate can enforce that setting's OWN
# eligibility rules (announce_only, channel_scope) rather than trusting the browser to
# have offered only eligible channels — see _channel_problem.
_CHANNEL_SETTINGS: dict[str, _Setting] = {
    s.slug: s for s in _SETTINGS if s.kind == "channel"
}
# A followable's post channel cannot be cleared: this page is the only writer, and a
# followable with no channel is a feed that silently produces nothing — every producer,
# mirror row and web action downstream then has to carry a "what if it's 0" branch. The
# log/alerts channels are NOT in this set: "unset" is a defined, useful state for them
# (the log is simply inert; alerts fall back to discovery), so they stay clearable.
# Note this bounds what can be *written*, not what can be read: a slug with no row at
# all still reads as 0/dormant on a fresh install, which resolve_followable_channel
# alerts on at boot.
_UNCLEARABLE_CHANNEL_SLUGS = frozenset(dd_settings.FOLLOWABLE_SLUGS.values())
_NO_CHANNEL_OPTION = '<option value="">— none configured —</option>'


# One validator per non-toggle kind (a toggle just needs bool(), handled inline in
# _handle_save): each takes the slug (for its error message) and the client's raw JSON
# value, and returns (value_to_store, error) — exactly one of the two is set. A blank
# url/color clears the setting (None -> stored as NULL, "use the default"); a blank
# channel stores "0" (explicitly dormant — see _Setting's "channel" kind docs on why
# that must NOT be NULL) unless the slug is unclearable, which rejects instead.
#
# JSON ``null`` never reaches a validator: it means "unchanged" (the page sends it for
# every field the operator didn't touch — see autopost_settings.js) and is skipped in
# _handle_save. That distinction is what keeps the unclearable rule from locking the
# page: an already-unset followable submits null, not "", so it isn't a *clear*.
def _validate_url(slug: str, raw: object) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, f"'{slug}' must be a string."
    trimmed = raw.strip()
    if trimmed and not trimmed.startswith(("http://", "https://")):
        return None, f"'{slug}' must be an http(s) URL."
    return trimmed or None, None


def _validate_color(slug: str, raw: object) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, f"'{slug}' must be a string."
    trimmed = raw.strip()
    if trimmed and not _HEX_COLOR_RE.match(trimmed):
        return None, f"'{slug}' must be a #RRGGBB colour."
    return trimmed or None, None


def _validate_select(slug: str, raw: object) -> tuple[str | None, str | None]:
    options = _SELECT_OPTIONS[slug]
    if raw not in options:
        return None, f"'{slug}' must be one of {', '.join(options)}."
    return t.cast(str, raw), None


def _validate_channel(slug: str, raw: object) -> tuple[str | None, str | None]:
    if isinstance(raw, str):
        raw = raw.strip()
    if raw == "":
        channel_id = 0
    else:
        try:
            channel_id = int(t.cast(str, raw))
        except (TypeError, ValueError):
            return None, f"'{slug}' must be a Discord channel id."
    if channel_id < 0:
        return None, f"'{slug}' must be a Discord channel id."
    if not channel_id and slug in _UNCLEARABLE_CHANNEL_SLUGS:
        return None, (
            f"'{slug}' needs a channel — a feed's post channel can't be cleared. "
            "Switch the feed off instead if it shouldn't post."
        )
    return str(channel_id), None


_ValueValidator = t.Callable[[str, object], tuple[str | None, str | None]]
_VALIDATORS: dict[str, _ValueValidator] = {
    **dict.fromkeys(_URL_SLUGS, _validate_url),
    **dict.fromkeys(_COLOR_SLUGS, _validate_color),
    **dict.fromkeys(_SELECT_OPTIONS, _validate_select),
    **dict.fromkeys(_CHANNEL_SLUGS, _validate_channel),
}


def _render_row(
    setting: _Setting,
    state: bool | str | None,
    *,
    flat: bool = False,
) -> str:
    """Render one settings row: label + description, then its control.

    ``flat`` overrides the indented ".sub" styling ``setting.sub`` would otherwise
    select, but keeps its dimmer background (via ``.flat-alt``). It's set by the caller
    for every row in a *categorised* general group (Branding, Logging & Alerts, ...):
    the category header is the group's only "parent" — a label, not a setting — so
    every row under it is a dark "detail" row, unlike a feed group, where the toggle row
    itself (e.g. ``lost_sector``) is a real light parent and only what follows
    (``lost_sector_details``) is dark. Without ``flat``, the setting that has to
    structurally start a category's group (``sub=False`` — something has to) would
    render with the same indent/smaller-name styling as a feed's actual parent toggle,
    implying a hierarchy among the category's settings that isn't there.
    """
    base_class = "row flat-alt" if flat else "row sub" if setting.sub else "row"

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

    def _row(kind_class: str, control_html: str, *, block: str = "") -> str:
        classes = f"{base_class} {kind_class}" if kind_class else base_class
        return f'<div class="{classes}">{block or _label_block()}{control_html}</div>'

    if setting.kind == "url":
        value = html.escape(state or "") if isinstance(state, str) else ""
        return _row(
            "urlrow",
            '<input type="url" class="urlfield" '
            f'data-slug="{html.escape(setting.slug)}"'
            f' value="{value}" placeholder="https://example.com/banner.png" />',
        )

    if setting.kind == "color":
        hex_value = (
            state if isinstance(state, str) and _HEX_COLOR_RE.match(state) else ""
        )
        # With no row saved, every producer is already painting dd.common.settings'
        # built-in default (a brand colour, not black) — so that is what an unset field
        # has to show, in the swatch and as the text field's placeholder. The text
        # field's *value* stays empty on purpose: blank is what stores NULL, and
        # pre-filling the hex would turn any later save into one that pins today's
        # default into the DB, losing "never set" for good.
        # Held to the same #RRGGBB shape a saved value is, since the swatch is a native
        # input[type=color]: a default the browser can't parse would silently render as
        # black there while the placeholder claimed otherwise.
        fallback = dd_settings.default_for(setting.slug) or ""
        if not _HEX_COLOR_RE.match(fallback):
            fallback = ""
        # input[type=color] cannot be blank, so it needs *some* value; black only when
        # there's no usable default to show either.
        swatch_value = hex_value or fallback or "#000000"
        placeholder = fallback or "#RRGGBB"
        return _row(
            "colorrow",
            '<div class="colorpicker">'
            '<input type="color" class="colorswatch" '
            f'data-for="{html.escape(setting.slug)}"'
            f' value="{html.escape(swatch_value)}" />'
            '<input type="text" class="colorfield no-focus-ring" '
            f'data-slug="{html.escape(setting.slug)}"'
            f' value="{html.escape(hex_value)}"'
            f' placeholder="{html.escape(placeholder)}"'
            ' maxlength="7" />'
            "</div>",
        )

    if setting.kind == "select":
        # Same fallback, for the same reason — and here it's load-bearing rather than
        # cosmetic: an HTML <select> always renders *some* option as selected, so
        # without this a slug with no row would show the first listed option (DEBUG)
        # while the bot applies the real default (ERROR).
        current = (
            state
            if isinstance(state, str) and state
            else dd_settings.default_for(setting.slug) or ""
        )
        opts = "".join(
            f'<option value="{html.escape(opt)}"'
            f"{' selected' if opt == current else ''}>{html.escape(opt)}</option>"
            for opt in setting.options
        )
        return _row(
            "selectrow",
            f'<select class="selectfield" data-slug="{html.escape(setting.slug)}">'
            f"{opts}"
            "</select>",
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
        # data-required drives the picker's clear affordance (autopost_settings.js): a
        # followable's channel can't be cleared, so don't offer an X that only produces
        # a rejected save. The empty option is still rendered while the field HAS no
        # value, because that is the honest state of an unconfigured feed and the page
        # has to be able to show it — the save gate is what refuses to write it back.
        required = setting.slug in _UNCLEARABLE_CHANNEL_SLUGS
        empty_opt = "" if required and channel_id else _NO_CHANNEL_OPTION
        return _row(
            "channelrow",
            f'<select class="channelfield" data-slug="{html.escape(setting.slug)}"'
            f' data-scope="{html.escape(setting.channel_scope)}"'
            f' data-required="{"true" if required else "false"}"'
            f' data-announce-only="{"true" if setting.announce_only else "false"}">'
            f"{empty_opt}"
            f"{current_opt}"
            "</select>",
        )

    checked = " checked" if state else ""
    return _row(
        "",
        '<label class="switch">'
        f'<input type="checkbox" class="no-focus-ring" '
        f'data-slug="{html.escape(setting.slug)}"{checked} />'
        '<span class="slider"></span>'
        "</label>",
        block=label_block,
    )


def _current_state(
    setting: _Setting, rows: dict[str, tuple[bool | None, str | None]]
) -> bool | str | None:
    """``setting``'s current value, from a bulk ``AutoPostSettings.get_all_rows()``
    fetch rather than one query per setting (``rows``, one dict for the whole page).

    The row IS the value for every kind, followable channels included: there is no
    env-var fallback behind them any more (see dd.common.settings' docstring), so this
    page and the producers reading through dd.common.settings resolve every setting
    from exactly the same place.
    """
    enabled, value = rows.get(setting.slug, (None, None))
    if setting.kind == "toggle":
        return bool(enabled)
    return value


def _wrap_group(
    entries: list[tuple[_Setting, bool | str | None]], category: str
) -> str:
    # Every row in a categorised group renders flat (no .sub indent, but still dark) —
    # see _render_row's `flat` docs: the category header is the group's only "parent",
    # so every row under it — including whichever one happens to structurally start the
    # list — is a dark "detail" row.
    flat = bool(category)
    rows = "".join(_render_row(setting, state, flat=flat) for setting, state in entries)
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
    rows = await schemas.AutoPostSettings.get_all_rows()
    for setting in _SETTINGS:
        state = _current_state(setting, rows)
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

# Everything a post might need in its destination channel: View to even open it, Send
# to post at all, Embed Links for CV2's link-preview/media-gallery components, and
# External Emoji since every post here resolves :shortcode: emoji that live on Kyber's
# server, not necessarily the destination's own.
_REQUIRED_CHANNEL_PERMS: tuple[tuple[h.Permissions, str], ...] = (
    (h.Permissions.VIEW_CHANNEL, "View Channel"),
    (h.Permissions.SEND_MESSAGES, "Send Messages"),
    (h.Permissions.EMBED_LINKS, "Embed Links"),
    (h.Permissions.USE_EXTERNAL_EMOJIS, "Use External Emoji"),
)


def _allowed_guild_ids(setting: _Setting) -> set[int]:
    """The guild(s) ``setting``'s picker offers — and therefore the only guilds a saved
    channel for it may live in. Mirrors autopost_settings.js's ``data-scope`` filter
    and _handle_channels' own guild list; see _channel_problem on why the server
    re-derives this rather than trusting the client's."""
    guild_ids = {cfg.kyber_discord_server_id}
    if setting.channel_scope == "kyber_control":
        guild_ids.add(cfg.control_discord_server_id)
    return {int(g) for g in guild_ids if g and g != -1}


async def _channel_problem(setting: _Setting, channel_id: int) -> str | None:
    """``None`` if ``channel_id`` is a channel ``setting`` may actually use — right
    guild, right type, and the bot can fully post there (view/send/embed/external-emoji)
    — otherwise a human-readable reason to reject the save for.

    Every rule the channel picker applies in the browser (autopost_settings.js's
    ``data-scope`` / ``data-announce-only`` filters, and _handle_channels' postable-type
    filter) is re-applied here, because this is the only place the value is actually
    written: the browser filter is a convenience for whoever is picking, not the
    enforcement point. A followable's channel must be an announcement channel or
    Discord's native "Follow Channel" cannot target it at all — the bot would post
    happily and every follower would silently receive nothing.

    Fails CLOSED — rejects the save — on anything short of a confirmed "yes, this
    channel is usable": the bot not started yet, an unresolvable permission-cache
    lookup, or an unexpected REST hiccup all reject, same as a confirmed missing
    permission does. A channel setting is worth being unable to save for a moment
    (retry once the bot's finished starting, or once its permission cache is warm)
    rather than risk accepting one silently unusable — this is the primary safety net,
    not just a courtesy check (a channel that goes bad *after* being saved still alerts
    rather than failing silently — see resolve_followable_channel/nav.py — but this is
    what stops a bad one going in to begin with).
    """
    # get_bot(), not require_bot(): this function owes its caller a REASON, and a
    # BotNotReady would leave the save's error path with no sentence to show. Fail
    # closed with the reason, as every other branch here does.
    bot = web.get_bot()
    if bot is None:
        return "the bot hasn't finished starting yet — try again in a moment."
    try:
        channel = await bot.rest.fetch_channel(channel_id)
    except (h.NotFoundError, h.ForbiddenError):
        return "the bot can't see that channel (deleted, or its access was revoked)."
    except Exception:
        logger.warning(
            "Channel permission check failed for %s", channel_id, exc_info=True
        )
        return (
            "couldn't confirm the bot's access to that channel right now — try again."
        )
    if not isinstance(channel, h.PermissibleGuildChannel):
        return "that channel doesn't support posting (not a text/announcement channel)."
    if channel.type not in _POSTABLE_CHANNEL_TYPES:
        return "that channel doesn't support posting (not a text/announcement channel)."
    if setting.announce_only and channel.type != h.ChannelType.GUILD_NEWS:
        return (
            "that's a text channel — this one has to be an announcement channel, or "
            "other servers can't follow it."
        )
    allowed_guilds = _allowed_guild_ids(setting)
    if allowed_guilds and int(channel.guild_id) not in allowed_guilds:
        return "that channel is in a server this setting can't post to."
    me = bot.get_me()
    if me is None:
        return "the bot's own identity isn't available yet — try again in a moment."
    try:
        member = await bot.rest.fetch_member(channel.guild_id, me.id)
        perms = calculate_permissions(member, channel)
    except CacheFailureError:
        return (
            "couldn't confirm the bot's permissions there yet (its cache isn't warm) "
            "— try again shortly."
        )
    except (h.NotFoundError, h.ForbiddenError):
        return "the bot isn't a member of that server."
    missing = [name for perm, name in _REQUIRED_CHANNEL_PERMS if not (perms & perm)]
    if not missing:
        return None
    return f"the bot is missing permissions there: {', '.join(missing)}."


async def _handle_channels(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """List postable channels in Kyber + the control server, for the channel pickers.

    One fetch covers every channel field on the page — autopost_settings.js filters by
    each field's ``data-scope`` (which guild(s)) and ``data-announce-only`` (whether a
    plain text channel is even eligible: a followable's post channel must be an
    announcement channel for Discord's native "Follow Channel" to work at all, unlike
    log_channel_id/alerts_channel_id, which the bot only ever sends to directly) —
    rather than one REST round trip per field. Those two filters keep the picker honest
    for whoever is using it; _channel_problem re-applies both server-side at save time,
    which is where they're actually enforced. Best-effort per guild: a guild the bot
    cannot currently see (not joined, or a permissions/REST blip) is simply omitted
    rather than failing the whole list — the affected pickers fall back to their
    current raw id (see autopost_settings.js).
    """
    # No degraded answer to give — an empty picker is indistinguishable from "this guild
    # has no postable channels" — so refuse, and let web's middleware say why.
    bot = web.require_bot()

    guild_ids = [
        g
        for g in {cfg.kyber_discord_server_id, cfg.control_discord_server_id}
        if g and g != -1
    ]
    # Both guilds' fetches are independent REST calls — run them concurrently rather
    # than one after another, since nothing here depends on the other's result.
    results = await asyncio.gather(
        *(bot.rest.fetch_guild_channels(guild_id) for guild_id in guild_ids),
        return_exceptions=True,
    )

    channels: list[dict[str, str | bool]] = []
    for guild_id, result in zip(guild_ids, results, strict=True):
        if isinstance(result, BaseException):
            logger.info(
                "Could not list channels for guild %s", guild_id, exc_info=result
            )
            continue
        for channel in result:
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

    # Validate every non-toggle field the operator actually CHANGED (one validator per
    # kind — see _VALIDATORS) so a bad value fails the whole save (before opening a
    # transaction) rather than persisting something a producer can't use.
    #
    # ``null`` is "unchanged", not "empty": the page sends it for every field it was
    # given and the operator didn't touch (see autopost_settings.js), so an untouched
    # field is never re-validated and never rewritten. Without that distinction, one
    # field that is invalid-but-untouched — or one already-unset followable, which the
    # unclearable rule rejects as a *clear* — would block every unrelated save on the
    # page.
    value_updates: dict[str, str | None] = {}
    for slug, validate in _VALIDATORS.items():
        if settings_in.get(slug) is None:
            continue
        value, error = validate(slug, settings_in[slug])
        if error:
            return aiohttp.web.json_response({"error": error}, status=400)
        value_updates[slug] = value

    # Confirm the channel is one this setting may actually use before persisting — a
    # channel id being syntactically valid says nothing about whether it's in the right
    # server, is the right type, or whether the bot can see it, has been kicked from
    # that server, or lacks Send/Embed/External-Emoji there. Skipped for "0" (clearing
    # a channel back to dormant needs no permission to do).
    for slug, setting in _CHANNEL_SETTINGS.items():
        channel_id = value_updates.get(slug)
        if not channel_id or channel_id == "0":
            continue
        problem = await _channel_problem(setting, int(channel_id))
        if problem:
            return aiohttp.web.json_response(
                {"error": f"Can't use that channel for '{slug}': {problem}"},
                status=400,
            )

    # Only known slugs are honoured; unknown keys are ignored (never trust the client's
    # key set to spawn rows). One transaction so a batch save is all-or-nothing.
    async with schemas.db_session() as session, session.begin():
        for slug, value in settings_in.items():
            # Same "null is unchanged" rule as the value fields above.
            if slug in _TOGGLE_SLUGS and value is not None:
                await schemas.AutoPostSettings.set_enabled(
                    slug, bool(value), session=session
                )
        for slug, value in value_updates.items():
            await schemas.AutoPostSettings.set_value(slug, value, session=session)

    # dd.common.settings caches every non-toggle row above (colors, urls, followable
    # channels, ...); refresh it now rather than waiting out the TTL (see that
    # module's docstring). A real refetch, not merely marking the cache stale, so a
    # *_sync reader (e.g. the one behind message-send channel routing) also sees the
    # new value immediately — those never check freshness, so they'd otherwise keep
    # serving the pre-save value until some other async getter triggered a refresh.
    await dd_settings.preload()

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
