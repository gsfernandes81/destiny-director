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

"""The ``/feeds`` and ``/settings`` pages of the anchor web control panel.

Two owner-only pages (each a homepage row via :func:`web.register_card`), one module,
one save endpoint. They are the sole editor for two things that used to be separate:

- Every **global** produce toggle — one ``name`` row in
  :class:`~dd.common.schemas.AutoPostSettings` each. The scattered ``/<feed> auto``
  slash commands (plus ``/ls details`` and ``/xur default_image``) duplicated this and
  were removed 2026-08-04.
- Every setting :mod:`dd.common.settings` resolves — colors, the default link URL, the
  alert level, the alerts channel, ``disable_bad_channels``, and every followable's
  post channel. These used to be env vars (``EMBED_DEFAULT_COLOR``, ``FOLLOWABLES``,
  ...) that needed a redeploy to change; they are now the same ``auto_post_settings``
  table rows, just not tied to a feed toggle — and this module is their ONLY writer: no
  env fallback and no seeding path exists behind it, so every value in the DB got there
  through the validation in :func:`_channel_problem` / :data:`_VALIDATORS`.

**Why two pages and one endpoint.** The single page they replaced opened with eight
rows about colours and alert levels and then asked the reader to scroll past them to
reach any feed — two errands with nothing in common sharing one scroll. ``/feeds``
carries the twelve feed groups, ``/settings`` the eight general rows, and the old
``/autopost_settings`` 301s to ``/feeds`` for whoever has it bookmarked. The **save**
stays one route (:func:`_handle_save`) because the two pages' slug sets are disjoint and
validation is per-slug (:data:`_VALIDATORS`): splitting it would duplicate the
all-or-nothing transaction and the :func:`dd.common.settings.preload` refresh, and one
copy would eventually drift.

``/feeds`` groups its feeds three ways — produced on a schedule, written by a human on a
form, written by someone else entirely. See :func:`_feed_sections` for where the second
of those facts comes from and why it is not in the catalog.

Scope is settings only — no per-guild follow management (that is end-user
``/autopost <feed>`` territory, stored as ``MirroredChannel`` rows). A missing toggle
row reads as ``None``, which every producer treats as *off*, so the page renders
``bool(get_enabled(slug))`` and lets ``set_enabled`` upsert on save. Authentication is
handled centrally by the Discord-OAuth middleware in ``web_auth.py`` (it protects every
non-allowlisted route, so this module needs no auth code).
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
from ..hybrid_post_core import registered_specs
from . import feed_actions

logger = logging.getLogger(__name__)

# No commands live here, but load_extensions_strict → load_extensions requires every
# extension module to expose a Loader, so define one.
loader = lb.Loader()

_WEB_STATIC = Path(__file__).resolve().parent.parent / "web_static"
_FEEDS_HTML_PATH = _WEB_STATIC / "feeds.html"
_SETTINGS_HTML_PATH = _WEB_STATIC / "settings.html"
#: Both templates carry the same placeholder — each page substitutes its own groups.
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
      picker offers: ``"kyber"`` (the feed guild(s) — Kyber, or the ``TEST_ENV`` servers
      on a test deployment; see :func:`_feed_guild_ids`) or ``"kyber_control"`` (the
      alerts channel, which could be in either).
    """

    slug: str
    label: str
    desc: str
    #: Whether this row hangs off the one above it rather than heading its own group.
    #: Defaulted because the feed rows no longer set it by hand — ``_as_group`` stamps
    #: it, so a group cannot be built that opens with a sub. The general rows below
    #: still pass it explicitly.
    sub: bool = False
    kind: str = "toggle"
    options: tuple[str, ...] = ()
    channel_scope: str = "kyber"
    #: For "channel": restrict the picker to Discord announcement channels. True for
    #: every followable's post channel — a channel other servers *follow* (crossposting,
    #: see MirroredChannel) must be an announcement channel, a plain text channel cannot
    #: be followed at all — and the default, since that covers most "channel" settings.
    #: False for alerts_channel_id: nothing follows it, the bot only sends there
    #: directly, so a plain text channel works fine too.
    announce_only: bool = True
    #: A group's display title, set on its FIRST setting only (``sub=False``). Rendered
    #: above the group's rows rather than reusing that first setting's own ``label``,
    #: since the header names the *category* ("Branding"), not that one row ("Default
    #: accent colour"). Blank whenever a row already names the group for itself — a
    #: feed's produce toggle does, and so does the lone channel row of a feed that has
    #: only that; see :func:`_feed_rows` for when a feed earns a header instead.
    category: str = ""


# The eight global/ops rows, hand-written: they are not feeds, the catalog never
# mentions them, and each piece of their copy exists exactly once.
_GENERAL_SETTINGS: tuple[_Setting, ...] = (
    # --- Branding: colours + the fallback link, all "how a post looks by default" ----
    _Setting(
        "embed_default_color",
        "Default accent colour",
        "The accent bar down the side of nearly every post, when nothing else sets "
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
        "embed_warning_color",
        "Warning accent colour",
        "Shown on WARNING alerts forwarded to the alerts channel.",
        True,
        "color",
    ),
    _Setting(
        "embed_critical_color",
        "Critical accent colour",
        "Shown on CRITICAL alerts — the ones that also ping the owners.",
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
    # --- Alerts: the ops pipeline that forwards records to Discord -------------------
    _Setting(
        "alert_min_level",
        "Alert level",
        "Minimum log severity forwarded to the alerts channel.",
        False,
        "select",
        options=_ALERT_LEVELS,
        category="Alerts",
    ),
    _Setting(
        "disable_bad_channels",
        "Stop sending to unreachable servers",
        "After a destination stays unreachable past the grace window, stop trying it.",
        True,
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
    " Nothing posts until a channel is picked. If it shouldn't post at all, switch the "
    "feed off above."
)


def _as_group(rows: tuple[_Setting, ...], category: str = "") -> tuple[_Setting, ...]:
    """Stamp a group: the first row heads it, everything after hangs off it.

    ``_render_groups`` groups in a single pass on ``sub``, so "a parent precedes its
    subs" has to hold or a row lands silently in the group above. Stamping it here makes
    that structural — no caller writes a ``sub`` flag, so no caller can write a group
    that opens with one.
    """
    head, *rest = rows
    return (
        head._replace(sub=False, category=category),
        *(row._replace(sub=True) for row in rest),
    )


def _feed_rows(feed: dd_feeds.Followable) -> tuple[_Setting, ...]:
    """One feed's settings rows, in display order.

    One sequence, not two shapes: the produce toggle is simply the first row when the
    feed has one. There is no early return to forget a row in — the previous version
    had one, and it silently dropped any extras belonging to a feed without a toggle.

    What heads the group follows from what the group contains:

    * a feed anchor produces on a schedule leads with its produce toggle, which names
      it, and everything else indents beneath;
    * a feed with no toggle and nothing but a channel *is* that one row, which wears the
      feed's name — a header above a single row would only be a label above a label;
    * a feed with no toggle and more than one row earns a header, the same way Branding
      and Logging & Alerts do. Without it the channel row would have to be both the
      feed's name and a control, which demotes every sibling row beneath a heading that
      is really a peer.

    Ordering is a rule — toggle, sub-toggles, channel, image URL — rather than per-feed
    data, which is what let the twelve groups be generated from the catalog at all.
    """
    subs, urls = _FEED_EXTRA_ROWS.get(feed.slug, ((), ()))

    if feed.has_toggle:
        channel_desc = _CHANNEL_DESC + (
            _DORMANT_NOTE if feed.slug in _DORMANT_NOTE_SLUGS else ""
        )
        return _as_group(
            (
                _Setting(feed.slug, feed.display_name, feed.desc),
                *subs,
                _Setting(
                    feed.channel_key, "Post to channel", channel_desc, kind="channel"
                ),
                *urls,
            )
        )

    if subs or urls:
        # The feed's name heads the group, so the channel row is an ordinary setting
        # again — and keeps the feed's own description, which the header cannot carry.
        return _as_group(
            (
                _Setting(
                    feed.channel_key, "Post to channel", feed.desc, kind="channel"
                ),
                *subs,
                *urls,
            ),
            category=feed.display_name,
        )

    # Nothing but a channel — reached only when subs and urls are both empty, so there
    # is nothing here to forget. The row is the group and wears the feed's name.
    return _as_group(
        (_Setting(feed.channel_key, feed.display_name, feed.desc, kind="channel"),)
    )


# Every feed's rows, in catalog order. Generated rather than written out, so the page
# cannot disagree with the catalog about which feeds exist or what they are called — the
# job the old hand-sync test used to do by watching.
_FEED_SETTINGS: tuple[_Setting, ...] = tuple(
    itertools.chain.from_iterable(_feed_rows(f) for f in dd_feeds.FOLLOWABLES)
)

# Every row this module owns, across both pages. The two pages render disjoint halves of
# it, but the SAVE is one endpoint over the whole set (see the module docstring), so the
# slug sets below — what a save is allowed to write, and how each slug is validated —
# are derived from the union rather than per page.
_SETTINGS: tuple[_Setting, ...] = _GENERAL_SETTINGS + _FEED_SETTINGS

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
# all still reads as 0/dormant on a fresh install, which beacon's dormancy sweep
# (``dd.beacon.utils.sweep_dormant_feeds``) pages for until one is picked here.
_UNCLEARABLE_CHANNEL_SLUGS = frozenset(dd_settings.FOLLOWABLE_SLUGS.values())
# What an unset channel field says — the same words the /send row's destination field
# uses, which is why they live in feed_actions rather than here. Rendered italic/muted
# by settings_page.css, and reused as the picker's placeholder (autopost_settings.js).
_NO_CHANNEL_OPTION = f'<option value="">{feed_actions.NO_CHANNEL_LABEL}</option>'


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
    actions: str = "",
) -> str:
    """Render one settings row: label + description, then its control.

    ``flat`` overrides the indented ".sub" styling ``setting.sub`` would otherwise
    select. It's set by the caller for every row in a *categorised* general group
    (Branding, Alerts, ...): the category header is the group's only "parent" — a label,
    not a setting — so every row under it is a peer, unlike a feed card, where the
    toggle row itself (e.g. ``lost_sector``) is a real parent and only what follows
    (``lost_sector_details``) hangs off it. Without ``flat``, the setting that has to
    structurally start a category's group (``sub=False`` — something has to) would
    render with the same indent/smaller-name styling as a feed's actual parent toggle,
    implying a hierarchy among the category's settings that isn't there.

    ``actions`` is the caller's Preview/Send block (see
    :func:`dd.anchor.extensions.feed_actions.actions_html`), passed
    in rather than derived here: whether Send is available depends on the feed's
    *channel*, which lives in a different row of the same card and so is only known one
    level up.
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
    entries: list[tuple[_Setting, bool | str | None]],
    category: str,
    *,
    footer: str = "",
    head_actions: str = "",
) -> str:
    """One card: an optional category header, the rows, and an optional trailing link.

    ``head_actions`` lands on the FIRST row only — a feed's Preview/Send pair belongs to
    the feed, and the first row is the one that names it.
    """
    # Every row in a categorised group renders flat (no .sub indent) — see _render_row's
    # `flat` docs: the category header is the group's only "parent", so every row under
    # it, including whichever one happens to structurally start the list, is a peer.
    flat = bool(category)
    rows = "".join(
        _render_row(
            setting, state, flat=flat, actions=head_actions if index == 0 else ""
        )
        for index, (setting, state) in enumerate(entries)
    )
    header = (
        f'<div class="groupheader">{html.escape(category)}</div>' if category else ""
    )
    return f'<div class="group">{header}{rows}{footer}</div>'


def _render_groups(
    settings: t.Iterable[_Setting], rows: dict[str, tuple[bool | None, str | None]]
) -> str:
    """Group ``settings`` into ``.group`` boxes and render them.

    A top-level setting (``sub`` is False) and every sub-setting that follows it share
    one box, so a feed and its content/channel/url sub-rows read as one category. A
    parent always precedes its subs (``_as_group`` makes that structural), so a single
    pass groups them. A general group's ``category`` (set on its first setting) renders
    as an explicit header — a feed group needs none, since its own toggle row already
    names it. Rows aren't rendered until their whole group is collected (see
    ``_wrap_group``), since whether a row renders flat depends on the group's category,
    which isn't known until the group's first (``sub=False``) setting is reached.
    """
    groups: list[str] = []
    current: list[tuple[_Setting, bool | str | None]] = []
    current_category = ""
    for setting in settings:
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
    return "".join(groups)


# --- /feeds: the three sections ------------------------------------------------------


class _FeedSection(t.NamedTuple):
    """One heading on the feeds page, and the feeds under it."""

    heading: str
    #: One line under the heading saying what these feeds have in common. It answers
    #: "why is this feed in this pile", which is the only question a heading of three
    #: words leaves open.
    blurb: str
    feeds: tuple[dd_feeds.Followable, ...]


_SECTION_COPY: tuple[tuple[str, str], ...] = (
    (
        "Posted on a schedule",
        "The bot builds these itself and posts them on a timer. Preview one to see "
        "what it would post right now, or send it early.",
    ),
    (
        "Written by you",
        "You write these on a form and press publish yourself — nothing goes out until "
        "you send it, so there is no switch here.",
    ),
    (
        "Posted by someone else",
        "Somebody else writes these straight into the channel. The bot only needs to "
        "know where to read them from.",
    ),
)


def _feed_sections() -> tuple[_FeedSection, ...]:
    """The twelve feeds, split three ways, in catalog order within each section.

    Two of the three splits come off the catalog: a feed with a produce toggle is one
    anchor runs on a schedule (``Followable.has_toggle``), and everything else is not.

    The third does not, and cannot. "Written by you" is Trials and Weekly Reset, which
    :class:`~dd.common.feeds.FeedKind` calls ``UNSCHEDULED`` alongside This Week At
    Bungie and Free Games — the catalog has no field that tells them apart, and adding
    one would be storing an anchor fact in a module beacon imports too. What actually
    separates them is that anchor has a **web form** wired to each: a
    :class:`~dd.anchor.hybrid_post_core.HybridPostSpec` registered at import time, which
    also carries the ``form_path`` this section's rows link to. So the grouping and the
    link come from the same registration; a producer that appears cannot be listed
    without its form, and one that is removed takes its row's promise with it.

    Read at request time, never cached: the registry fills as extensions load, and this
    module has no ordering relationship with the two producers that populate it.

    An empty section is dropped rather than rendered as a heading over nothing — which
    is also the honest render if the producers somehow did not load.
    """
    written_by_hand = set(registered_specs())
    scheduled: list[dd_feeds.Followable] = []
    written: list[dd_feeds.Followable] = []
    elsewhere: list[dd_feeds.Followable] = []
    for feed in dd_feeds.FOLLOWABLES:
        if feed.has_toggle:
            scheduled.append(feed)
        elif feed.slug in written_by_hand:
            written.append(feed)
        else:
            elsewhere.append(feed)
    return tuple(
        _FeedSection(heading, blurb, tuple(feeds))
        for (heading, blurb), feeds in zip(
            _SECTION_COPY, (scheduled, written, elsewhere), strict=True
        )
        if feeds
    )


def _form_link(form_path: str) -> str:
    """The "Open the form →" row closing a *Written by you* feed's group.

    Inside the group box rather than beside the heading: the section holds two feeds
    with two different forms, so a link at section level could only be ambiguous.
    """
    return (
        f'<a class="row formlink" href="{html.escape(form_path)}">'
        '<span class="name">Open the form</span>'
        '<span class="go" aria-hidden="true">→</span>'
        "</a>"
    )


def _render_feed_section(
    section: _FeedSection, rows: dict[str, tuple[bool | None, str | None]]
) -> str:
    specs = registered_specs()
    groups: list[str] = []
    for feed in section.feeds:
        entries = [(s, _current_state(s, rows)) for s in _feed_rows(feed)]
        spec = specs.get(feed.slug)
        # "0" is an explicit clear, None a slug that was never written — both mean the
        # feed has nowhere to post, which is what Send's availability turns on.
        channel = rows.get(feed.channel_key, (None, None))[1]
        actions = (
            feed_actions.actions_html(
                feed.slug,
                label=feed.display_name,
                channel_set=channel not in (None, "", "0"),
                # The picker is a row further down this very card.
                fix_channel_here=True,
            )
            if feed.has_toggle
            else None
        )
        groups.append(
            _wrap_group(
                entries,
                entries[0][0].category,
                footer=_form_link(spec.form_path) if spec else "",
                # Buttons then reason, stacked: a row here is a block of copy with its
                # controls in it, so the sentence simply follows them.
                head_actions=actions.buttons + actions.notes if actions else "",
            )
        )
    return (
        '<section class="section">'
        f"<h2>{html.escape(section.heading)}</h2>"
        f'<p class="sectiondesc">{html.escape(section.blurb)}</p>'
        f'<div class="groups">{"".join(groups)}</div>'
        "</section>"
    )


async def _render_feeds_html() -> str:
    """Render ``/feeds``: the twelve feed groups, under three section headings."""
    rows = await schemas.AutoPostSettings.get_all_rows()
    sections = "".join(_render_feed_section(s, rows) for s in _feed_sections())
    shell = _FEEDS_HTML_PATH.read_text(encoding="utf-8").replace(
        _TOGGLES_PLACEHOLDER, sections
    )
    # The preview/send dialogs are shared with /send, so the page carries a placeholder
    # for them rather than its own copy of the markup.
    return feed_actions.splice_modals(shell)


async def _render_settings_html() -> str:
    """Render ``/settings``: the general rows, as their two categorised groups."""
    rows = await schemas.AutoPostSettings.get_all_rows()
    return _SETTINGS_HTML_PATH.read_text(encoding="utf-8").replace(
        _TOGGLES_PLACEHOLDER, _render_groups(_GENERAL_SETTINGS, rows)
    )


async def _handle_feeds(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # Auth is enforced by the web_auth middleware; this just renders the page.
    return aiohttp.web.Response(
        text=await _render_feeds_html(), content_type="text/html"
    )


async def _handle_settings(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=await _render_settings_html(), content_type="text/html"
    )


async def _handle_legacy_redirect(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """``/autopost_settings`` → ``/feeds``, permanently.

    A 301 rather than a deletion because this page has had one URL for its whole life
    and the operator's bookmarks and muscle memory both point at it. It is the feed half
    that inherits the address: that is what the page was mostly used for, and what the
    name referred to.
    """
    raise aiohttp.web.HTTPMovedPermanently("/feeds")


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


def _feed_guild_ids() -> set[int]:
    """The guild(s) standing in for Kyber — where a feed's own post channel may live.

    A test deployment (``TEST_ENV`` set, which is what dev runs as) offers its OWN
    servers instead. Otherwise every picker on the dev control panel lists the live
    Kyber server's channels, and the one mis-save that invites points a dev feed at a
    real Kyber channel. ``TEST_ENV`` is already the "these are my servers" list every
    guild-scoped command registers against, so it answers this too rather than being a
    second variable to keep in sync with it.

    The control server is NOT replaced: it is the operator's own either way, and a
    ``kyber_control``-scoped setting (the log/alerts channels) may legitimately sit
    there in both environments. On dev it is in ``TEST_ENV`` anyway.
    """
    guild_ids = set(cfg.test_env) or {cfg.kyber_discord_server_id}
    return {int(g) for g in guild_ids if g and g != -1}


def allowed_guild_ids(*, include_control_server: bool = False) -> set[int]:
    """The guild(s) a channel field's picker offers — and therefore the only guilds a
    saved channel for it may live in. Mirrors autopost_settings.js's ``data-scope``
    filter and _handle_channels' own guild list; see :func:`check_channel` on why the
    server re-derives this rather than trusting the client's.

    ``include_control_server`` is the ``"kyber_control"`` scope (the alerts channel,
    which may legitimately sit in the control server). Public because the CV2 builder's
    web-originated "custom post" flow validates its target against exactly the
    non-control scope this returns by default — one definition of "where may this bot
    post", not two that can drift.
    """
    guild_ids = _feed_guild_ids()
    control_id = int(cfg.control_discord_server_id)
    if include_control_server and control_id and control_id != -1:
        guild_ids = guild_ids | {control_id}
    return guild_ids


class ChannelCheck(t.NamedTuple):
    """What :func:`check_channel` concluded about a channel.

    ``problem`` is ``None`` exactly when the channel is usable, and otherwise the
    human-readable reason to refuse. ``channel`` is the fetched channel on success, so a
    caller that needs a fact off it (the CV2 mint route needs its ``guild_id``) doesn't
    pay a second REST round-trip — and, more to the point, records the guild the
    *server* resolved rather than one the client also sent.
    """

    problem: str | None
    channel: h.PermissibleGuildChannel | None = None


async def check_channel(
    channel_id: int, *, announce_only: bool, allowed_guild_ids: set[int]
) -> ChannelCheck:
    """Whether ``channel_id`` is a channel the bot may actually post to — right guild,
    right type, and the bot can fully post there (view/send/embed/external-emoji).

    Takes the two facts a caller cares about rather than a :class:`_Setting`, so the
    web surfaces that have no setting row — the CV2 builder's "custom post" mint route —
    can vet a target through this same function instead of a stand-in setting.
    :func:`_channel_problem` is the settings-page shaped wrapper over it.

    Every rule the channel picker applies in the browser (autopost_settings.js's
    ``data-scope`` / ``data-announce-only`` filters, and _handle_channels' postable-type
    filter) is re-applied here, because this is the only place the value is actually
    used: the browser filter is a convenience for whoever is picking, not the
    enforcement point. With ``announce_only`` the channel must additionally be an
    announcement channel — a followable's channel must be, or Discord's native "Follow
    Channel" cannot target it at all and the bot would post happily while every follower
    silently received nothing.

    Fails CLOSED — rejects — on anything short of a confirmed "yes, this channel is
    usable": the bot not started yet, an unresolvable permission-cache lookup, or an
    unexpected REST hiccup all reject, same as a confirmed missing permission does. A
    channel is worth being unable to use for a moment (retry once the bot's finished
    starting, or once its permission cache is warm) rather than risk accepting one
    silently unusable — this is the primary safety net, not just a courtesy check (a
    channel that goes bad *after* being saved still alerts rather than failing silently
    — see ``dd.beacon.utils.open_feed_source`` — but this is what stops a bad one going
    in to begin with).
    """
    # get_bot(), not require_bot(): this function owes its caller a REASON, and a
    # BotNotReady would leave the save's error path with no sentence to show. Fail
    # closed with the reason, as every other branch here does.
    bot = web.get_bot()
    if bot is None:
        return ChannelCheck(
            "the bot hasn't finished starting yet — try again in a moment."
        )
    try:
        channel = await bot.rest.fetch_channel(channel_id)
    except (h.NotFoundError, h.ForbiddenError):
        return ChannelCheck(
            "the bot can't see that channel (deleted, or its access was revoked)."
        )
    except Exception:
        logger.warning(
            "Channel permission check failed for %s", channel_id, exc_info=True
        )
        return ChannelCheck(
            "couldn't confirm the bot's access to that channel right now — try again."
        )
    if not isinstance(channel, h.PermissibleGuildChannel):
        return ChannelCheck(
            "that channel doesn't support posting (not a text/announcement channel)."
        )
    if channel.type not in _POSTABLE_CHANNEL_TYPES:
        return ChannelCheck(
            "that channel doesn't support posting (not a text/announcement channel)."
        )
    if announce_only and channel.type != h.ChannelType.GUILD_NEWS:
        return ChannelCheck(
            "that's a text channel — this one has to be an announcement channel, or "
            "other servers can't follow it."
        )
    if allowed_guild_ids and int(channel.guild_id) not in allowed_guild_ids:
        return ChannelCheck("that channel is in a server this setting can't post to.")
    me = bot.get_me()
    if me is None:
        return ChannelCheck(
            "the bot's own identity isn't available yet — try again in a moment."
        )
    try:
        member = await bot.rest.fetch_member(channel.guild_id, me.id)
        perms = calculate_permissions(member, channel)
    except CacheFailureError:
        return ChannelCheck(
            "couldn't confirm the bot's permissions there yet (its cache isn't warm) "
            "— try again shortly."
        )
    except (h.NotFoundError, h.ForbiddenError):
        return ChannelCheck("the bot isn't a member of that server.")
    missing = [name for perm, name in _REQUIRED_CHANNEL_PERMS if not (perms & perm)]
    if not missing:
        return ChannelCheck(None, channel)
    return ChannelCheck(f"the bot is missing permissions there: {', '.join(missing)}.")


async def _channel_problem(setting: _Setting, channel_id: int) -> str | None:
    """``None`` if ``channel_id`` is a channel ``setting`` may actually use, otherwise a
    human-readable reason to reject the save for.

    A thin :class:`_Setting`-shaped wrapper over :func:`check_channel`, which holds the
    rules and the sentences — ``setting`` only ever contributed these two facts.
    """
    return (
        await check_channel(
            channel_id,
            announce_only=setting.announce_only,
            allowed_guild_ids=allowed_guild_ids(
                include_control_server=setting.channel_scope == "kyber_control"
            ),
        )
    ).problem


async def _handle_channels(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """List postable channels in the feed guild(s) + the control server, for the picker.

    "Feed guild(s)" is Kyber in production and the ``TEST_ENV`` servers on a test
    deployment — see :func:`_feed_guild_ids`.

    One fetch covers every channel field on the page — autopost_settings.js filters by
    each field's ``data-scope`` (which guild(s)) and ``data-announce-only`` (whether a
    plain text channel is even eligible: a followable's post channel must be an
    announcement channel for Discord's native "Follow Channel" to work at all, unlike
    alerts_channel_id, which the bot only ever sends to directly) —
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

    feed_guild_ids = _feed_guild_ids()
    control_id = int(cfg.control_discord_server_id)
    # Sorted so the response (and the tests reading it) has one stable order; a set's
    # iteration order is not one.
    guild_ids = sorted(
        feed_guild_ids | ({control_id} if control_id and control_id != -1 else set())
    )
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
            # A list, not one id: TEST_ENV may name several servers (see
            # _feed_guild_ids), and the client filters each picker against them all.
            "feedGuildIds": [str(g) for g in sorted(feed_guild_ids)],
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
    """Add the feeds/settings routes to the shared persistent app.

    Two pages, but one channel list and one save: both are shared machinery rather than
    either page's own, so they keep the neutral ``/autopost_settings/`` prefix the pages
    themselves have given up. Renaming them under one page's URL would make the other
    page's fetches read as though they were reaching across.
    """
    app.router.add_get("/feeds", _handle_feeds)
    app.router.add_get("/settings", _handle_settings)
    app.router.add_get("/autopost_settings", _handle_legacy_redirect)
    app.router.add_get("/autopost_settings/channels", _handle_channels)
    app.router.add_post("/autopost_settings/save", _handle_save)


web.register_routes(register_autopost_settings_routes)
# First two rows of "Set up and admin": what posts where is asked far more often than
# what colour it comes out, and both are asked more often than the Bungie login (30).
web.register_card(
    web.Card(
        "Feeds",
        "What each feed posts to, whether it posts at all, and a way to send one now.",
        "/feeds",
        web.CardGroup.ADMIN,
        10,
    )
)
web.register_card(
    web.Card(
        "Appearance & alerts",
        "Default colours and links for every post, and where problems get reported.",
        "/settings",
        web.CardGroup.ADMIN,
        20,
    )
)
