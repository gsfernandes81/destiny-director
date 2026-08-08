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

"""Bot-agnostic, miru-free interactive components.

This module provides a :class:`Paginator` built directly on lightbulb v3's
``lightbulb.components.Menu`` plus Discord Components V2 (CV2) builders, with no
dependency on miru. It is kept general enough to eventually replace
``dd.beacon.nav`` but currently focuses on the needs of the ``/help`` command.

CV2 composition (confirmed via spike):
    - A page is a factory producing a list of hikari component builders, sent with
      the ``hikari.MessageFlag.IS_COMPONENTS_V2`` flag. The help layout uses a single
      :class:`hikari.impl.ContainerComponentBuilder` per page (text displays +
      separators) with the navigation buttons rendered inside it via an action row.
    - ``lightbulb.components.Menu`` is used purely as the ``custom_id`` -> callback
      router: its interaction handler matches on ``interaction.custom_id`` against
      the buttons added to the menu, independent of *where* those buttons are
      rendered. We therefore add the nav buttons to the menu (to register the
      handlers) and render visually-identical buttons (same custom ids) inside the
      CV2 container.
    - On every press/timeout we rebuild the whole container from scratch with the
      buttons reflecting the current page index / disabled state, then edit the
      message in place.

A classic-embed fallback mode is also supported: pass ``h.Embed`` pages and the
paginator sends/edits them as embeds driven by the exact same menu buttons.
"""

import contextlib
import logging
import typing as t
import uuid

import hikari as h
import lightbulb as lb
from lightbulb import components as lbc

from . import cfg, settings

if t.TYPE_CHECKING:
    from dd.hmessage.message import HMessage

# Unicode reverse (◀) / play (▶) triangles used as the prev/next button emoji.
# Defined as module-level constants so they are not constructed in function-argument
# defaults (ruff B008).
PREV_PAGE_EMOJI = chr(9664)
NEXT_PAGE_EMOJI = chr(9654)

# --- standard post footer buttons -------------------------------------------------
# Every autopost ends with the same link-button row: any post-specific "guide" links,
# then a Support button. Kept here as the single source of truth so the live CV2 post
# (footer_buttons_row) and the web preview (PostSpec.buttons, built from
# footer_button_specs) can never drift.
KOFI_URL = "https://ko-fi.com/Kyber3000"


def footer_button_specs(
    *, guides: t.Sequence[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """The ``(label, url)`` specs for a post's footer button row.

    ``guides`` are the post-specific links (0-4 of them — e.g. a Lost Sector guide +
    details page), followed by the standard Support button. This is the one source both
    the live CV2 row and the preview render from. Labels must be plain text (no
    ``:emoji:`` tokens — button labels aren't emoji-substituted).
    """
    if len(guides) > 4:
        raise ValueError("at most 4 guide buttons (Discord caps an action row at 5)")
    return [
        *[(str(label), str(url)) for label, url in guides],
        ("Support Us", KOFI_URL),
    ]


def link_button_row(
    specs: t.Sequence[tuple[str, str]],
) -> h.impl.MessageActionRowBuilder:
    """An action row of link buttons from ``(label, url)`` specs."""
    row = h.impl.MessageActionRowBuilder()
    for label, url in specs:
        row.add_component(h.impl.LinkButtonBuilder(url=url, label=label))
    return row


def footer_buttons_row(
    *, guides: t.Sequence[tuple[str, str]] = ()
) -> h.impl.MessageActionRowBuilder:
    """The standard footer link-button row (see :func:`footer_button_specs`)."""
    return link_button_row(footer_button_specs(guides=guides))


_PREV_CUSTOM_ID = "dd_paginator:prev"
_INDICATOR_CUSTOM_ID = "dd_paginator:indicator"
_NEXT_CUSTOM_ID = "dd_paginator:next"

# A CV2 page is a *factory* that builds a fresh ordered list of component builders
# each time it is rendered. A factory (rather than a prebuilt list) is used so the
# nav buttons can be (re)injected into a freshly-built container on every press
# without mutating shared builder objects across renders. A fallback page is a
# single embed.
Cv2PageFactory = t.Callable[[], list[h.api.ComponentBuilder]]
Page = Cv2PageFactory | h.Embed


# ---------------------------------------------------------------------------
# CV2 text limits (one source of truth, shared by autoposts and the navigator)
# ---------------------------------------------------------------------------
#
# Discord hard-caps a Components V2 message's total displayable text at 4000 code units,
# and counts in UTF-16 (an astral-plane glyph counts as 2). CV2_TEXT_BUDGET is the safe
# target we truncate to — a margin below the limit for the counting difference plus the
# converter's markdown/emoji overhead.
CV2_TEXT_LIMIT = 4000
CV2_TEXT_BUDGET = 3900
CV2_TRUNCATION_NOTE = "\n\n-# … (truncated)"


def cv2_utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units — the unit Discord counts CV2 text in."""
    return len(text.encode("utf-16-le")) // 2


def cv2_text_length(components: t.Iterable[h.api.ComponentBuilder]) -> int:
    """Total displayable CV2 text across component builders, in UTF-16 units.

    Recurses into containers and sections so every nested ``TextDisplay`` is counted.
    """
    total = 0
    for comp in components:
        if isinstance(comp, h.impl.TextDisplayComponentBuilder):
            total += cv2_utf16_len(comp.content or "")
        else:
            children = getattr(comp, "components", None)
            if children:
                total += cv2_text_length(children)
    return total


def cap_cv2_text(text: str, *, budget: int = CV2_TEXT_BUDGET) -> str:
    """Truncate ``text`` to ``budget`` UTF-16 units, appending a ``… (truncated)`` note.

    Counts and slices in UTF-16 (Discord's unit) so the result genuinely fits the cap,
    dropping a trailing lone surrogate rather than splitting a code point.
    """
    encoded = text.encode("utf-16-le")
    if len(encoded) // 2 <= budget:
        return text
    note_len = cv2_utf16_len(CV2_TRUNCATION_NOTE)
    if budget < note_len:
        # No room for the note without exceeding budget — hard-slice to the budget so
        # the result still honours the cap (``* 2`` keeps a code-unit boundary;
        # errors="ignore" drops a lone surrogate if the cut landed mid-pair).
        return encoded[: max(budget, 0) * 2].decode("utf-16-le", errors="ignore")
    keep = budget - note_len
    # ``keep * 2`` is a code-unit boundary; errors="ignore" drops a lone surrogate if
    # the cut landed mid-pair.
    cut = encoded[: keep * 2].decode("utf-16-le", errors="ignore")
    return cut.rstrip() + CV2_TRUNCATION_NOTE


def fit_cv2_components(
    components: t.Sequence[h.api.ComponentBuilder],
    *,
    budget: int = CV2_TEXT_BUDGET,
) -> list[h.api.ComponentBuilder]:
    """Return ``components`` guaranteed to hold at most ``budget`` UTF-16 text units.

    The single enforcement point for the CV2 text cap on an *assembled* message: a page
    built from heterogeneous parts — native CV2 containers rebuilt from a source post,
    embeds converted in-memory, several messages accumulated into one bin — is passed
    through this so Discord never rejects it for length, no matter where the text came
    from. Builders are immutable, so an over-budget page is rebuilt with its text
    displays trimmed front-to-back (earliest text kept whole, the tail trimmed then
    dropped); a text display trimmed to nothing is removed, and a section left with no
    text is dropped. Non-text components (media galleries, separators, buttons) are
    preserved untouched. Under budget (the common case) the input is returned as-is.
    """
    if cv2_text_length(components) <= budget:
        return list(components)

    remaining = budget

    def rebuild(comp: h.api.ComponentBuilder) -> h.api.ComponentBuilder | None:
        nonlocal remaining
        if isinstance(comp, h.impl.TextDisplayComponentBuilder):
            content = comp.content or ""
            if remaining <= 0 or not content:
                return None
            length = cv2_utf16_len(content)
            if length <= remaining:
                remaining -= length
                return comp
            trimmed = cap_cv2_text(content, budget=remaining)
            remaining = 0
            return text_display(trimmed) if trimmed else None
        if isinstance(comp, h.impl.ContainerComponentBuilder):
            container = h.impl.ContainerComponentBuilder(
                accent_color=comp.accent_color, spoiler=comp.is_spoiler
            )
            for child in comp.components:
                rebuilt = rebuild(child)
                if rebuilt is not None:
                    # rebuild() preserves each child's concrete type (or trims text to a
                    # new TextDisplay); all are valid container children. ty only sees
                    # the broad recursion return type, so cast.
                    container.add_component(t.cast(t.Any, rebuilt))
            return container
        if isinstance(comp, h.impl.SectionComponentBuilder):
            texts = [
                t.cast(h.impl.TextDisplayComponentBuilder, c)
                for c in map(rebuild, comp.components)
                if c is not None
            ]
            if not texts:
                return None  # a section requires at least one text display
            section = h.impl.SectionComponentBuilder(accessory=comp.accessory)
            for text in texts:
                section.add_component(text)
            return section
        # No text to trim (separator, media gallery, action row, file) — reuse as-is.
        return comp

    return [c for c in map(rebuild, components) if c is not None]


async def _alert_cv2_overflow(post_name: str, message: str) -> None:
    """Raise a CRITICAL (owner-pinging) alert that a CV2 autopost overflowed the cap.

    Escalates via the ``level`` flag so a single occurrence pings the owners; the
    escalated alert renders as a clean notice (no traceback) rather than an error.
    """
    # Local import avoids a components <-> utils import cycle.
    from .utils import discord_error_logger

    await discord_error_logger(
        ValueError(message), operation=f"{post_name} autopost", level=logging.CRITICAL
    )


async def guard_cv2_post_text(
    text: str, *, post_name: str, budget: int = CV2_TEXT_BUDGET
) -> str:
    """Truncate an autopost body to the CV2 budget; raise a CRITICAL alert on overflow.

    A Components V2 autopost that outgrows the 4000-char cap is otherwise silently
    rejected by Discord (dropped post). When ``text`` exceeds ``budget`` this pings the
    owners (so the truncation / near-miss is caught) and returns the capped text.
    """
    length = cv2_utf16_len(text)
    if length <= budget:
        return text
    await _alert_cv2_overflow(
        post_name,
        f"{post_name} CV2 post is {length} UTF-16 units (over the {budget} budget) "
        "— truncated, content lost",
    )
    return cap_cv2_text(text, budget=budget)


async def guard_cv2_post_sections(
    header: str,
    body: str,
    footer: str,
    *,
    post_name: str,
    budget: int = CV2_TEXT_BUDGET,
) -> str:
    """Assemble ``header + body + footer``, truncating only ``body`` on overflow.

    The fixed header/footer budget is reserved so those always survive a body-heavy
    day; only the variable ``body`` is routed through :func:`guard_cv2_post_text`
    (truncate + CRITICAL alert). All three parts must already be emoji-substituted —
    the budget is measured on the final rendered text.
    """
    reserve = cv2_utf16_len(header) + cv2_utf16_len(footer)
    body = await guard_cv2_post_text(
        body, post_name=post_name, budget=max(budget - reserve, 0)
    )
    return header + body + footer


async def warn_cv2_post_over_limit(
    components: t.Iterable[h.api.ComponentBuilder], *, post_name: str
) -> None:
    """Backstop for a built autopost: CRITICAL-alert if its total CV2 text would exceed
    Discord's hard limit (and thus be rejected). Catches multi-text-display posts that
    never route through :func:`guard_cv2_post_text`. Alert-only — it does not mutate the
    components, so callers that can overflow must truncate their variable body first.
    """
    length = cv2_text_length(components)
    if length <= CV2_TEXT_LIMIT:
        return
    await _alert_cv2_overflow(
        post_name,
        f"{post_name} CV2 post is {length} UTF-16 units, over Discord's "
        f"{CV2_TEXT_LIMIT} cap — Discord will reject it",
    )


async def guard_cv2_hmessage(
    hmsg: "HMessage", *, post_name: str, budget: int = CV2_TEXT_BUDGET
) -> "HMessage":
    """Fit a built ``HMessage``'s CV2 text to ``budget`` in place; alert on overflow.

    The single CV2 length guard for an assembled message: it caps the text in place via
    :meth:`HMessage.fit_cv2_text` (a naive front-to-back trim — a body-heavy post may
    lose its footer) and, on overflow, raises a CRITICAL owner alert so the truncation
    is surfaced. Under budget it is untouched. Call *after* emoji substitution, so the
    measured length is the final rendered length."""
    length = hmsg.fit_cv2_text(budget)
    if length > budget:
        await _alert_cv2_overflow(
            post_name,
            f"{post_name} CV2 post is {length} UTF-16 units (over the {budget} "
            "budget) — truncated, content lost",
        )
    return hmsg


async def finalize_cv2_post(
    hmsg: "HMessage", emoji_dict: dict[str, h.Emoji], *, post_name: str
) -> "HMessage":
    """Resolve guild ``:emoji:`` then cap CV2 text — the shared tail of every CV2 post.

    Every CV2 autopost builds its container(s) with raw ``:name:`` tokens and ends here:
    substitute the guild emoji across the assembled message, then guard it with
    :func:`guard_cv2_hmessage` (which measures the final rendered length)."""
    # Local import avoids a components<->utils module-load cycle.
    from .utils import substitute_guild_emoji

    substitute_guild_emoji(hmsg, emoji_dict)
    return await guard_cv2_hmessage(hmsg, post_name=post_name)


# ---------------------------------------------------------------------------
# CV2 layout helpers
# ---------------------------------------------------------------------------


def text_display(content: str) -> h.impl.TextDisplayComponentBuilder:
    """Build a CV2 text display component."""
    return h.impl.TextDisplayComponentBuilder(content=content)


def separator(*, divider: bool = True) -> h.impl.SeparatorComponentBuilder:
    """Build a CV2 separator component."""
    return h.impl.SeparatorComponentBuilder(divider=divider)


def nav_buttons_row(
    *,
    page_index: int,
    page_count: int,
    all_disabled: bool = False,
    prev_id: str = _PREV_CUSTOM_ID,
    next_id: str = _NEXT_CUSTOM_ID,
) -> list[h.api.InteractiveButtonBuilder]:
    """Build the prev / indicator / next interactive buttons for an action row.

    ``prev_id`` / ``next_id`` must match the ids the :class:`Paginator` registers on its
    menu — the caller passes per-instance ids so a press on this paginator's message
    routes only to its own menu, not to another live paginator matching a shared id.

    Args:
        page_index: Zero-based index of the currently displayed page.
        page_count: Total number of pages.
        all_disabled: If ``True`` every button is disabled (used on timeout).
    """
    prev_disabled = all_disabled or page_index <= 0
    next_disabled = all_disabled or page_index >= page_count - 1
    return [
        h.impl.InteractiveButtonBuilder(
            style=h.ButtonStyle.SECONDARY,
            custom_id=prev_id,
            emoji=PREV_PAGE_EMOJI,
            is_disabled=prev_disabled,
        ),
        h.impl.InteractiveButtonBuilder(
            style=h.ButtonStyle.SECONDARY,
            custom_id=_INDICATOR_CUSTOM_ID,
            label=f"{page_index + 1}/{page_count}",
            is_disabled=True,
        ),
        h.impl.InteractiveButtonBuilder(
            style=h.ButtonStyle.SECONDARY,
            custom_id=next_id,
            emoji=NEXT_PAGE_EMOJI,
            is_disabled=next_disabled,
        ),
    ]


def build_container(
    text_sections: t.Sequence[str],
    *,
    accent_color: h.Color | None = None,
) -> h.impl.ContainerComponentBuilder:
    """Build a CV2 container with one text display per section, separated by dividers.

    Args:
        text_sections: Markdown blocks; each becomes its own text display with a
            divider separator in between.
        accent_color: The container accent colour (defaults to the
            ``embed_default_color`` Autopost Setting, see :mod:`dd.common.settings`).
    """
    container = h.impl.ContainerComponentBuilder(
        accent_color=accent_color
        if accent_color is not None
        else settings.get_embed_default_color_sync()
    )
    for i, section in enumerate(text_sections):
        if i:
            container.add_separator(divider=True)
        container.add_text_display(section)
    return container


# --- one-off status responses (errors / successes / notices) ----------------------
#
# One source of truth for the accent colours and shape of the short CV2 responses a
# command shows its invoker, replacing the per-file `_ERROR_COLOR` / `_WARN_COLOR` /
# `_SUCCESS_COLOR` / ad-hoc `h.Embed(...)` stylings that had drifted apart across
# extensions. Every user-facing status message routes through these so errors,
# successes and notices look identical everywhere. Values match the previously
# scattered constants (danger/success = posts.py; warning/neutral = controller.py).
CV2_DANGER_COLOR = h.Color(0xED4245)
CV2_SUCCESS_COLOR = h.Color(0x57F287)
CV2_WARNING_COLOR = h.Color(0xFEE75C)
CV2_NEUTRAL_COLOR = h.Color(0x5865F2)


def cv2_error(title: str, body: str = "") -> h.impl.ContainerComponentBuilder:
    """A CV2 error response: a bold ``⚠️ title`` line plus optional body, red accent.

    Title and body share one text display (no divider between them) so short errors
    stay compact. Send with ``flags=h.MessageFlag.IS_COMPONENTS_V2``.
    """
    text = f"⚠️ **{title}**"
    if body:
        text += f"\n{body}"
    return build_container([text], accent_color=CV2_DANGER_COLOR)


def cv2_success(body: str) -> h.impl.ContainerComponentBuilder:
    """A CV2 success response: ``✅ body``, green accent."""
    return build_container([f"✅ {body}"], accent_color=CV2_SUCCESS_COLOR)


def cv2_notice(body: str) -> h.impl.ContainerComponentBuilder:
    """A CV2 neutral notice/progress/confirmation response, blurple accent.

    Use for "Doing X…" progress lines and plain confirmations that are neither an
    error nor a success. When a progress notice is later edited into its result, keep
    the result CV2 too — Discord forbids toggling ``IS_COMPONENTS_V2`` on an edit.
    """
    return build_container([body], accent_color=CV2_NEUTRAL_COLOR)


async def respond_cv2(
    ctx: lb.Context,
    container: h.impl.ContainerComponentBuilder,
    *,
    ephemeral: bool = False,
) -> h.Snowflakeish:
    """Send a Components V2 status response through a lightbulb context.

    A thin wrapper over ``ctx.respond`` that sets the ``IS_COMPONENTS_V2`` flag, so
    every extension can emit a uniform CV2 error/success/notice without repeating the
    flag plumbing. Returns the response handle (so a progress notice can later be
    ``ctx.edit_response``-ed into its CV2 result). Pair with :func:`cv2_error` /
    :func:`cv2_success` / :func:`cv2_notice`.
    """
    return await ctx.respond(
        components=[container],
        flags=h.MessageFlag.IS_COMPONENTS_V2,
        ephemeral=ephemeral,
    )


def _embed_has_content(embed: h.Embed) -> bool:
    """Whether an embed carries anything ``embeds_to_container`` would render."""
    return bool(
        (embed.author and embed.author.name)
        or embed.title
        or embed.description
        or embed.fields
        or embed.image
        or embed.thumbnail
        or (embed.footer and embed.footer.text)
        or embed.timestamp
    )


# hikari's default media builders *upload* their media: build() returns the resource as
# an attachment, so hikari downloads a remote URL and re-uploads it on every send —
# round-tripping the bytes (a 429 from the source host, or a 413 when the file exceeds
# Discord's upload limit; the ~15 MB Lost Sector gif hits both). Discord fetches an
# external media ``url`` server-side (exactly like an embed image, via its media proxy),
# so these variants reference the URL and upload nothing.


def _url_only(built: tuple) -> tuple:
    """Keep a component ``build()`` payload (which carries ``media.url``) but drop the
    resource hikari would upload, so Discord fetches the URL instead of the bot
    re-uploading it.
    """
    payload, _resources = built
    return payload, ()


class _UrlMediaGalleryItemBuilder(h.impl.MediaGalleryItemBuilder):
    @t.override
    def build(self):
        return _url_only(super().build())


class _UrlThumbnailComponentBuilder(h.impl.ThumbnailComponentBuilder):
    @t.override
    def build(self):
        return _url_only(super().build())


def url_media_gallery(url: str) -> h.impl.MediaGalleryComponentBuilder:
    """A one-item media gallery referencing ``url`` for Discord to fetch (no upload)."""
    gallery = h.impl.MediaGalleryComponentBuilder()
    gallery.add_item(_UrlMediaGalleryItemBuilder(media=url))
    return gallery


def url_thumbnail(url: str) -> h.impl.ThumbnailComponentBuilder:
    """A thumbnail referencing ``url`` for Discord to fetch (no upload)."""
    return _UrlThumbnailComponentBuilder(media=url)


def _add_embed_to_container(
    container: h.impl.ContainerComponentBuilder,
    embed: h.Embed,
) -> None:
    """Render one embed's parts onto ``container`` (see ``embeds_to_container``)."""
    # Author -> a subtext line (masked-linked when it has a url; the icon is dropped,
    # subtext can't carry one).
    if embed.author and embed.author.name:
        name = embed.author.name
        author = f"[{name}]({embed.author.url})" if embed.author.url else name
        container.add_text_display(f"-# {author}")

    title_md: str | None = None
    if embed.title:
        title_md = (
            f"## [{embed.title}]({embed.url})" if embed.url else f"## {embed.title}"
        )
    description = embed.description or None
    thumb_url = embed.thumbnail.url if embed.thumbnail else None

    # Thumbnail (embed top-right) -> a section whose accessory is the thumbnail,
    # holding the title/description text. A section needs 1-3 text displays, so this
    # path is only taken when there is title/description text to anchor it to.
    if thumb_url and (title_md or description):
        section = h.impl.SectionComponentBuilder(accessory=url_thumbnail(thumb_url))
        for text in (title_md, description):
            if text:
                section.add_text_display(text)
        container.add_component(section)
    else:
        if title_md:
            container.add_text_display(title_md)
        if description:
            container.add_text_display(description)
        # A thumbnail with no text to anchor a section -> show it standalone so it isn't
        # silently lost.
        if thumb_url and not (title_md or description):
            container.add_component(url_media_gallery(thumb_url))

    # Fields -> one text display each. Inline layout is not representable in CV2 (no
    # columns), so inline fields stack vertically like the rest.
    for field in embed.fields:
        container.add_separator(divider=False)
        container.add_text_display(f"**{field.name}**\n{field.value}")

    # Large image -> a full-width media gallery (URL-referenced, not uploaded).
    if embed.image:
        container.add_component(url_media_gallery(embed.image.url))

    # Footer text + timestamp -> a trailing subtext line (the footer icon is dropped).
    footer_parts: list[str] = []
    if embed.footer and embed.footer.text:
        footer_parts.append(embed.footer.text)
    if embed.timestamp:
        # A dynamic Discord timestamp renders localized, like the embed's own footer ts.
        footer_parts.append(f"<t:{int(embed.timestamp.timestamp())}:f>")
    if footer_parts:
        container.add_text_display("-# " + " • ".join(footer_parts))


def embeds_to_container(
    embeds: h.Embed | t.Sequence[h.Embed],
    *,
    accent_color: h.Color | None = None,
) -> h.impl.ContainerComponentBuilder:
    """Convert one or more embeds into a single CV2 container (faithful layout).

    Each embed part is mapped to its closest CV2 primitive:

    - accent colour -> the container accent. A container has a single accent, so with
      multiple embeds the first embed's colour wins (falling back to the
      ``embed_default_color`` Autopost Setting, see :mod:`dd.common.settings`).
    - author -> a ``-#`` subtext line (masked-linked to ``author.url``; icon dropped);
    - title / description -> ``## title`` (masked-linked to ``embed.url``) + description
      text displays, wrapped in a section with the thumbnail as its accessory when the
      embed has one (mirroring the embed's top-right thumbnail);
    - fields -> one ``**name**\\nvalue`` text display each (inline layout is lost);
    - image -> a full-width media gallery;
    - footer / timestamp -> a trailing ``-#`` subtext line (icon dropped).

    Embeds are separated by divider separators. Embeds with no renderable content are
    skipped; if none render the result is an empty container (no ``.components``), which
    the caller should treat as "nothing to convert".

    Images/thumbnails are referenced by URL for Discord to fetch (see
    ``url_media_gallery``), never uploaded — so remote media never round-trips the bot.
    """
    if isinstance(embeds, h.Embed):
        embeds = [embeds]

    if accent_color is None:
        accent_color = next(
            (embed.color for embed in embeds if embed.color is not None),
            settings.get_embed_default_color_sync(),
        )

    container = h.impl.ContainerComponentBuilder(accent_color=accent_color)
    rendered = False
    for embed in embeds:
        if not _embed_has_content(embed):
            continue
        if rendered:
            container.add_separator(divider=True)
        _add_embed_to_container(container, embed)
        rendered = True

    return container


def rebuild_components(
    components: t.Sequence[h.PartialComponent],
) -> list[h.api.ComponentBuilder]:
    """Rebuild sendable CV2 component *builders* from fetched component *models*.

    hikari's ``create_message``/``edit`` accept only builders, but a fetched message
    exposes component *models* which are not builders and carry no build path (unlike
    embeds, which round-trip directly). The mirror uses this to re-send a Components V2
    source message to a destination, and the navigator to re-render CV2 autoposts. The
    CV2 *display* content types are supported — containers, text displays, separators,
    media galleries, thumbnails, files and sections — plus action-row buttons; a
    genuinely unsupported member (select menu, premium button, …) raises so a new kind
    surfaces loudly instead of mirroring blank.

    Round-trip notes: media is re-sent by URL only (``proxy_url`` is read-only and not
    reproduced — Discord re-proxies); a custom emoji on a button may not render if the
    bot doesn't share its guild; a mirrored *interactive* button is non-functional on
    the copy (kept for visual fidelity).
    """
    return [_rebuild_component(component) for component in components]


# A rebuilt button (interactive or link) — used for action rows and section accessories.
_ButtonBuilder = h.impl.InteractiveButtonBuilder | h.impl.LinkButtonBuilder
_SectionAccessory = h.impl.ThumbnailComponentBuilder | _ButtonBuilder


def _rebuild_component(component: h.PartialComponent) -> h.api.ComponentBuilder:
    if isinstance(component, h.ContainerComponent):
        container = h.impl.ContainerComponentBuilder(
            accent_color=component.accent_color
            if component.accent_color is not None
            else h.UNDEFINED,
            spoiler=component.is_spoiler,
        )
        for child in component.components:
            _add_container_child(container, child)
        return container
    if isinstance(component, h.TextDisplayComponent):
        return h.impl.TextDisplayComponentBuilder(content=component.content)
    if isinstance(component, h.SeparatorComponent):
        return _rebuild_separator(component)
    if isinstance(component, h.MediaGalleryComponent):
        return _rebuild_media_gallery(component)
    if isinstance(component, h.FileComponent):
        return _rebuild_file(component)
    if isinstance(component, h.SectionComponent):
        return _rebuild_section(component)
    if isinstance(component, h.ActionRowComponent):
        return _rebuild_action_row(component)
    raise NotImplementedError(_unrebuildable(component))


def _add_container_child(
    container: h.impl.ContainerComponentBuilder, child: h.PartialComponent
) -> None:
    """Rebuild one container child onto ``container``.

    Simple kinds use their typed ``add_*`` method (raw args); builder-typed kinds
    (media gallery, action row, section) are built and passed to ``add_component`` (the
    container-child union it accepts) as a concrete builder.
    """
    if isinstance(child, h.TextDisplayComponent):
        container.add_text_display(child.content)
    elif isinstance(child, h.SeparatorComponent):
        container.add_separator(
            spacing=child.spacing if child.spacing is not None else h.UNDEFINED,
            divider=child.divider if child.divider is not None else h.UNDEFINED,
        )
    elif isinstance(child, h.FileComponent):
        container.add_file(child.file.url, spoiler=child.is_spoiler)
    elif isinstance(child, h.MediaGalleryComponent):
        container.add_component(_rebuild_media_gallery(child))
    elif isinstance(child, h.ActionRowComponent):
        container.add_component(_rebuild_action_row(child))
    elif isinstance(child, h.SectionComponent):
        container.add_component(_rebuild_section(child))
    else:
        raise NotImplementedError(_unrebuildable(child))


def _rebuild_separator(
    component: h.SeparatorComponent,
) -> h.impl.SeparatorComponentBuilder:
    return h.impl.SeparatorComponentBuilder(
        spacing=component.spacing if component.spacing is not None else h.UNDEFINED,
        divider=component.divider if component.divider is not None else h.UNDEFINED,
    )


def _rebuild_media_gallery(
    component: h.MediaGalleryComponent,
) -> h.impl.MediaGalleryComponentBuilder:
    # Re-sent by URL only (``proxy_url`` is read-only). The URL variant references the
    # media for Discord to fetch instead of re-uploading it — a re-rendered native CV2
    # page (e.g. the ~15 MB Lost Sector gif) would otherwise 413 / re-download bytes.
    gallery = h.impl.MediaGalleryComponentBuilder()
    for item in component.items:
        gallery.add_item(
            _UrlMediaGalleryItemBuilder(
                media=item.media.url,
                description=item.description
                if item.description is not None
                else h.UNDEFINED,
                spoiler=item.is_spoiler,
            )
        )
    return gallery


def _rebuild_thumbnail(
    component: h.ThumbnailComponent,
) -> h.impl.ThumbnailComponentBuilder:
    return _UrlThumbnailComponentBuilder(
        media=component.media.url,
        description=component.description
        if component.description is not None
        else h.UNDEFINED,
        spoiler=component.is_spoiler,
    )


def _rebuild_file(component: h.FileComponent) -> h.impl.FileComponentBuilder:
    return h.impl.FileComponentBuilder(
        file=component.file.url, spoiler=component.is_spoiler
    )


def _rebuild_button(button: h.ButtonComponent) -> _ButtonBuilder:
    label = button.label if button.label is not None else h.UNDEFINED
    emoji = button.emoji if button.emoji is not None else h.UNDEFINED
    if button.url is not None:
        return h.impl.LinkButtonBuilder(
            url=button.url, label=label, emoji=emoji, is_disabled=button.is_disabled
        )
    if button.custom_id is None:
        # Premium (SKU) or otherwise unrebuildable button.
        raise NotImplementedError(_unrebuildable(button))
    return h.impl.InteractiveButtonBuilder(
        style=button.style,
        custom_id=button.custom_id,
        label=label,
        emoji=emoji,
        is_disabled=button.is_disabled,
    )


def _action_row_buttons(
    component: h.ActionRowComponent,
) -> list[_ButtonBuilder]:
    buttons: list[_ButtonBuilder] = []
    for child in component.components:
        if isinstance(child, h.ButtonComponent):
            buttons.append(_rebuild_button(child))
        else:
            # e.g. a select menu — dead in a mirrored message; surface loudly.
            raise NotImplementedError(_unrebuildable(child))
    return buttons


def _rebuild_action_row(
    component: h.ActionRowComponent,
) -> h.impl.MessageActionRowBuilder:
    row = h.impl.MessageActionRowBuilder()
    for button in _action_row_buttons(component):
        row.add_component(button)
    return row


def _rebuild_section_accessory(accessory: h.PartialComponent) -> _SectionAccessory:
    if isinstance(accessory, h.ThumbnailComponent):
        return _rebuild_thumbnail(accessory)
    if isinstance(accessory, h.ButtonComponent):
        return _rebuild_button(accessory)
    raise NotImplementedError(_unrebuildable(accessory))


def _rebuild_section(component: h.SectionComponent) -> h.impl.SectionComponentBuilder:
    section = h.impl.SectionComponentBuilder(
        accessory=_rebuild_section_accessory(component.accessory)
    )
    for child in component.components:
        if isinstance(child, h.TextDisplayComponent):
            section.add_text_display(child.content)
        else:
            raise NotImplementedError(_unrebuildable(child))
    return section


def _unrebuildable(component: h.PartialComponent) -> str:
    kind = getattr(component, "type", component)
    return f"Cannot rebuild CV2 component of type {kind!r}"


# Char/line limits ported from the lightbulb v2 ``lines_to_embeds`` paginator
# (``git show 3a92d4f:dd/beacon/modules/help.py``).
MAX_PAGE_CHARS = 1800
MAX_PAGE_LINES = 64
MAX_LINE_CHARS = 2000


def chunk_lines_to_sections(lines: t.Sequence[str]) -> list[str]:
    """Chunk help lines into page-sized markdown sections.

    Ports the ~1800-char / ~64-line page limits from the v2 ``lines_to_embeds``.
    Over-long individual lines are truncated to 2000 chars. Each returned string is
    one page's worth of content (newline-joined lines).
    """
    pages: list[str] = [""]
    for raw_line in lines:
        line = raw_line
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS]

        current = pages[-1]
        if current and (
            len(current) + len(line) > MAX_PAGE_CHARS
            or len(current.split("\n")) >= MAX_PAGE_LINES
        ):
            pages.append("")
            current = ""

        pages[-1] = (current + "\n" + line) if current else line

    return [p for p in pages if p] or [""]


# ---------------------------------------------------------------------------
# Paginator
# ---------------------------------------------------------------------------


# The paginator's controls live on the initial *interaction response*, and the
# on-timeout "disable" edit reuses that interaction's token — which Discord only
# honours for 15 minutes. The menu must therefore time out far enough before then for
# the disable edit to land; otherwise it 401s ("Invalid Webhook Token"). Cap the
# timeout at the token lifetime minus a margin for the edit's round-trip.
_INTERACTION_TOKEN_TTL = 15 * 60
_MAX_TIMEOUT = _INTERACTION_TOKEN_TTL - 60


class Paginator:
    """A miru-free paginator built on ``lightbulb.components.Menu``.

    Holds an ordered list of pages. In CV2 mode each page is a *factory* that builds
    a fresh list of component builders on demand (so the nav buttons can be injected
    into a freshly-built container each render); in fallback mode pages are
    ``h.Embed`` instances. Renders prev / disabled page-indicator / next buttons,
    disabling prev on the first page and next on the last. Edits the message in place
    and, on timeout, disables the controls.

    The page factories should *not* include the nav buttons; the paginator injects a
    fresh nav row reflecting the current index (inside the page's last container when
    present, otherwise as a top-level action row). Pass ``h.Embed`` pages for the
    classic-embed fallback.
    """

    def __init__(
        self,
        pages: t.Sequence[Page],
        *,
        timeout: int | None = None,
    ) -> None:
        if not pages:
            raise ValueError("Paginator requires at least one page")
        self._pages: list[Page] = list(pages)
        self._cv2 = not isinstance(self._pages[0], h.Embed)
        requested = timeout if timeout is not None else int(cfg.navigator_timeout)
        # Never let the menu outlive the interaction token (see ``_MAX_TIMEOUT``).
        self._timeout = min(requested, _MAX_TIMEOUT)
        self._index = 0
        # Captured in ``send`` so the controls can be disabled on timeout.
        self._ctx: lb.Context | None = None
        self._message: h.Message | None = None

        # The menu is used purely to route button presses to callbacks; its handler
        # matches on ``interaction.custom_id`` regardless of where the button is
        # rendered, so for CV2 we render visually-identical buttons (same custom ids)
        # inside the container and never send the menu's own rows.
        # Per-instance button ids: lightbulb routes a component interaction to the first
        # attached menu whose custom_ids match, with no message binding, so shared ids
        # would let a press on this paginator's message be handled by another live
        # paginator's menu. Unique ids make the match resolve to exactly this instance.
        token = uuid.uuid4().hex
        self._prev_id = f"{_PREV_CUSTOM_ID}:{token}"
        self._next_id = f"{_NEXT_CUSTOM_ID}:{token}"

        self._menu = lbc.Menu()
        self._menu.add_interactive_button(
            h.ButtonStyle.SECONDARY,
            self._on_prev,
            custom_id=self._prev_id,
            emoji=PREV_PAGE_EMOJI,
        )
        self._menu.add_interactive_button(
            h.ButtonStyle.SECONDARY,
            self._on_next,
            custom_id=self._next_id,
            emoji=NEXT_PAGE_EMOJI,
        )

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def needs_pagination(self) -> bool:
        """Whether more than one page exists (and controls are worthwhile)."""
        return len(self._pages) > 1

    # -- rendering ---------------------------------------------------------

    def _render_components(
        self, *, all_disabled: bool = False
    ) -> list[h.api.ComponentBuilder]:
        """Build the component list for the current CV2 page.

        The nav buttons are rendered inside the last container (so they share its
        accent styling) when the page ends with a container; otherwise they are
        appended as a top-level action row. The page factory is re-invoked on every
        render so no builder object is mutated across presses.
        """
        page = self._pages[self._index]
        if isinstance(page, h.Embed):
            raise TypeError("CV2 page must be a component factory, not an Embed")
        components: list[h.api.ComponentBuilder] = list(page())

        # No nav controls for a single page.
        if not self.needs_pagination:
            return components

        nav = nav_buttons_row(
            page_index=self._index,
            page_count=self.page_count,
            all_disabled=all_disabled,
            prev_id=self._prev_id,
            next_id=self._next_id,
        )
        if components and isinstance(components[-1], h.impl.ContainerComponentBuilder):
            components[-1].add_action_row(nav)
        else:
            row = h.impl.MessageActionRowBuilder()
            for button in nav:
                row.add_component(button)
            components.append(row)
        return components

    def _current_embed(self) -> h.Embed:
        page = self._pages[self._index]
        if not isinstance(page, h.Embed):
            raise TypeError("Expected an Embed page")
        return page

    # -- sending / editing -------------------------------------------------

    async def send(self, ctx: lb.Context) -> None:
        """Send page 1 from a ``lb.Context`` and attach the paginator.

        If only a single page exists no controls are attached. Otherwise this blocks
        on the menu until it stops or times out, then disables the controls.
        """
        if self._cv2:
            await ctx.respond(
                flags=h.MessageFlag.IS_COMPONENTS_V2,
                components=self._render_components(),
            )
        elif self.needs_pagination:
            await ctx.respond(embed=self._current_embed(), components=self._menu)
        else:
            await ctx.respond(embed=self._current_embed())

        if not self.needs_pagination:
            return

        # Capture the message id so we can disable the controls on timeout (there is
        # no MenuContext available outside a button press).
        self._ctx = ctx
        self._message = await ctx.interaction.fetch_initial_response()

        # ``attach`` blocks until the menu stops or times out. A press never stops
        # the menu here, so the only way out is the timeout, which lightbulb signals
        # by *raising* ``asyncio.TimeoutError`` (aliased to the builtin ``TimeoutError``
        # on 3.11+) rather than returning. Swallow it — a timed-out paginator is
        # normal, not a command failure — and then disable the controls.
        with contextlib.suppress(TimeoutError):
            await self._menu.attach(ctx.client, timeout=self._timeout)
        await self._on_timeout()

    async def _edit(self, mctx: lbc.MenuContext, *, all_disabled: bool = False) -> None:
        if self._cv2:
            # ``respond(edit=True)`` edits the initial response in place and, unlike
            # ``edit_response``, accepts the CV2 flag.
            await mctx.respond(
                edit=True,
                flags=h.MessageFlag.IS_COMPONENTS_V2,
                components=self._render_components(all_disabled=all_disabled),
            )
        elif all_disabled:
            await mctx.respond(edit=True, embed=self._current_embed(), components=[])
        else:
            await mctx.respond(
                edit=True, embed=self._current_embed(), components=self._menu
            )

    # -- callbacks ---------------------------------------------------------

    async def _on_prev(self, mctx: lbc.MenuContext) -> None:
        if self._index > 0:
            self._index -= 1
        await self._edit(mctx)

    async def _on_next(self, mctx: lbc.MenuContext) -> None:
        if self._index < self.page_count - 1:
            self._index += 1
        await self._edit(mctx)

    async def _on_timeout(self) -> None:
        """Best-effort: disable the controls on the displayed message after a timeout.

        ``edit_message`` does not take a flags argument; editing the components of a
        message that was created with ``IS_COMPONENTS_V2`` preserves that flag.

        The edit reuses the interaction token, so if it has already expired
        (``UnauthorizedError`` — the menu outlived the 15-minute token window despite
        the ``_MAX_TIMEOUT`` cap) or the message was since deleted (``NotFoundError``)
        the cleanup is simply skipped: a stale paginator is cosmetic, never a command
        failure.
        """
        if self._ctx is None or self._message is None:
            return
        with contextlib.suppress(h.UnauthorizedError, h.NotFoundError):
            if self._cv2:
                await self._ctx.interaction.edit_message(
                    self._message,
                    components=self._render_components(all_disabled=True),
                )
            else:
                await self._ctx.interaction.edit_message(self._message, components=[])
