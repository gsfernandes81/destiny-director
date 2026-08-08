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

"""Shared core for the anchor's *hybrid* followable producers.

A "hybrid" post (e.g. the Weekly Reset Overview, Trials of Osiris) is one a reset-day
cron seeds as an uncrossposted draft, a team member fills through an owner-authenticated
web form, and publishing crossposts so beacon mirrors it to followers. Every such post
shares the same machinery; this module is that machinery, factored out of
``extensions/weekly_reset.py`` so a second producer (``extensions/trials.py``) reuses it
instead of copying it.

This module lives OUTSIDE ``extensions/`` on purpose: the extension loader discovers
loadable modules with ``pkgutil.iter_modules`` over ``dd.anchor.extensions`` only, so a
core module here is never mistaken for a loadable extension. It imports only lower
layers (``cfg``, ``bungie_api``, ``HMessage``) and never a producer module, so there is
no import cycle.

Auth is deliberately absent: every anchor web surface is gated centrally by the
Discord-OAuth middleware in ``extensions/web_auth.py`` (which also does the cross-origin
check on unsafe methods), so producers — and this core — carry no session/cookie/origin
code.
"""

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import re
import typing as t
from pathlib import Path

import aiohttp.web
import aiosqlite
import hikari as h

from dd.hmessage import HMessage

from ..common import schemas, settings
from ..common.bot import CachedFetchBot
from ..common.components import footer_button_specs, link_button_row
from ..common.utils import fetch_emoji_dict
from . import utils, web
from .extensions import bungie_api as api

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reset-time boundaries (deterministic, no API)
# ---------------------------------------------------------------------------

#: A known Tuesday 17:00 UTC weekly-reset boundary (matches beacon's weekly_reset ref).
REFERENCE_RESET = dt.datetime(2023, 7, 18, 17, tzinfo=dt.UTC)
WEEK = dt.timedelta(days=7)


def current_reset_ts(now: dt.datetime | None = None) -> int:
    """Unix ts of the reset boundary for the week containing ``now`` (Tue 17:00 UTC)."""
    now = now or dt.datetime.now(tz=dt.UTC)
    weeks = (now - REFERENCE_RESET) // WEEK
    return int((REFERENCE_RESET + weeks * WEEK).timestamp())


def next_reset_ts(reset_ts: int) -> int:
    """First reset boundary strictly after ``reset_ts`` — i.e. the next Tuesday.

    ``reset_ts`` is the *current* week's boundary (which drives the rotators), so
    this is the moment the post's content resets, shown on the ``Resets:`` line.
    """
    return reset_ts + int(WEEK.total_seconds())


def rotator_index(anchor_ts: int, reset_ts: int, length: int) -> int:
    """Which cycle entry is active this week (weeks since anchor, mod list length)."""
    if length <= 0:
        return 0
    weeks = (reset_ts - anchor_ts) // int(WEEK.total_seconds())
    return weeks % length


def compute_rotator(
    pairs: t.Sequence[tuple[str, str]], anchor_ts: int, reset_ts: int
) -> tuple[str, str]:
    if not pairs:
        return ("", "")
    return pairs[rotator_index(anchor_ts, reset_ts, len(pairs))]


# ---------------------------------------------------------------------------
# Weapon slot
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class WeaponRef:
    """A weapon slot: enough to render a light.gg-linked, emoji-prefixed line.

    Derived weapons carry a ``hash`` (so we can deep-link light.gg and infer the
    weapon-type emoji); hand-typed weapons may have no hash (plain text, no link).
    """

    name: str
    hash: int | None = None
    #: weapon-type emoji name (e.g. "pulse_rifle"); only needed for the Zavala line.
    emoji_name: str | None = None

    @property
    def lightgg_url(self) -> str | None:
        return f"https://light.gg/db/items/{self.hash}" if self.hash else None

    def markdown(self) -> str:
        """``[Name](url)`` when we have a hash, else plain ``Name``."""
        url = self.lightgg_url
        return f"[{self.name}]({url})" if url else self.name

    @classmethod
    def from_item(cls, item: "api.DestinyItem") -> "WeaponRef":
        return cls(name=item.name, hash=item.hash, emoji_name=item.expected_emoji_name)

    def to_dict(self) -> dict[str, t.Any]:
        return {"name": self.name, "hash": self.hash, "emoji_name": self.emoji_name}

    @classmethod
    def from_dict(cls, d: t.Mapping[str, t.Any]) -> "WeaponRef":
        return cls(name=d["name"], hash=d.get("hash"), emoji_name=d.get("emoji_name"))


# ---------------------------------------------------------------------------
# Components V2 renderer
# ---------------------------------------------------------------------------


def build_cv2(
    body: str,
    image_url: str | None,
    *,
    buttons: t.Sequence[tuple[str, str]] = (),
) -> HMessage:
    """Wrap an emoji-substituted body + image + footer button row in a CV2 HMessage.

    ``buttons`` are ``(label, url)`` link-button specs (from
    :func:`dd.common.components.footer_button_specs`); when given, a divider + a single
    action row of link buttons is appended. The old ``-# via Destiny Director (Kyber)``
    credit line is gone — the button row is the footer now.

    Deliberately sync (uses ``get_embed_default_color_sync``, not the awaitable getter):
    called from too many sync call sites, on both bots and in tests, and its output must
    stay byte-identical to :func:`post_spec_nodes`'s (see that function's docstring and
    ``test_post_spec_nodes_matches_build_cv2``) — an async split between the two would
    threaten that.
    """
    container = h.impl.ContainerComponentBuilder(
        accent_color=settings.get_embed_default_color_sync()
    )
    container.add_text_display(body)
    if image_url:
        gallery = h.impl.MediaGalleryComponentBuilder()
        gallery.add_media_gallery_item(image_url)
        container.add_component(gallery)
    if buttons:
        container.add_separator(divider=True)
        container.add_component(link_button_row(buttons))
    return HMessage(components=[container])


def emoji_payload(emoji_dict: dict[str, h.Emoji]) -> dict[str, str]:
    """``{name: url}`` for a client-side render's ``:shortcode:`` substitution.

    The bare-string shape ``cv2_model.js``'s ``emojiEntry`` accepts. Buttons need an id
    as well as a url, but a *rendered* post never does — a captured ``<:name:id>``
    carries its own id, and a typed ``:name:`` only ever becomes an ``<img>``.
    """
    return {name: str(getattr(emoji, "url", "")) for name, emoji in emoji_dict.items()}


def post_spec_nodes(spec: "PostSpec") -> list[dict[str, t.Any]]:
    """The same post as :func:`build_cv2`, as a raw CV2 node list.

    The preview surfaces render the *post*, and the post is a node tree — so rather than
    approximate it in a second markup vocabulary (which is what ``.post-*`` was), hand
    the previewer the tree and let the shared renderer draw it. Structure is pinned to
    ``build_cv2`` by ``test_post_spec_nodes_matches_build_cv2`` — two descriptions of
    one post is exactly the drift this whole change exists to remove.
    """
    children: list[dict[str, t.Any]] = [{"type": 10, "content": spec.body}]
    if spec.image_url and spec.image_url.startswith(("http://", "https://")):
        children.append(
            {"type": 12, "items": [{"media": {"url": spec.image_url}}]},
        )
    buttons = [
        {"type": 2, "style": 5, "label": str(label), "url": str(url)}
        for label, url in spec.buttons
        if str(url).startswith(("http://", "https://"))
    ]
    if buttons:
        # No explicit `spacing`: hikari omits it too, and the renderer reads its absence
        # as the small default. Matching exactly is what lets the two be compared.
        children.append({"type": 14, "divider": True})
        children.append({"type": 1, "components": buttons})
    return [
        {
            "type": 17,
            "accent_color": settings.get_embed_default_color_sync(),
            "components": children,
        }
    ]


# ---------------------------------------------------------------------------
# Body-text helpers
# ---------------------------------------------------------------------------
#
# Producers write their post body as a markdown string; how that markdown *draws* is
# the shared client renderer's business (``web_static/cv2_render.js``). What lives here
# is the formatting a producer does to build the string itself.

# ---------------------------------------------------------------------------
# PostSpec — the format-tagged, serializable description a previewer renders
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PostSpec:
    """A format-tagged, JSON-friendly description of a post.

    The one description every preview surface starts from — the weekly_reset/trials web
    forms and the rotation preview wall today, the user-commands manager later.
    :func:`post_spec_nodes` turns it into the CV2 node tree ``build_cv2`` would send,
    which is what a previewer actually draws.

    Only the ``cv2`` kind exists: a markdown body + optional image + footer buttons, the
    shape every current producer emits via ``build_body``. An ``embed`` kind is reserved
    for the user-commands manager (the first classic-embed consumer) and tracked in
    ``plans/website_user_commands.md``; the shared renderer already draws embeds, so
    that variant needs a ``post_spec_nodes`` branch rather than a new renderer.
    """

    kind: str
    body: str = ""
    image_url: str | None = None
    #: Footer link buttons as ``(label, url)`` pairs (tuple so the frozen dataclass
    #: stays hashable) — rendered below the body/image in place of the old credit line.
    buttons: tuple[tuple[str, str], ...] = ()

    @classmethod
    def cv2(
        cls,
        body: str,
        image_url: str | None = None,
        buttons: t.Sequence[tuple[str, str]] = (),
    ) -> "PostSpec":
        """A Components-V2 post: a markdown body + optional image + footer buttons."""
        return cls(
            kind="cv2",
            body=body,
            image_url=image_url,
            buttons=tuple((str(label), str(url)) for label, url in buttons),
        )

    @classmethod
    def from_payload(cls, payload: t.Mapping[str, t.Any]) -> "PostSpec":
        """Parse a client-supplied spec (the future ``POST /post/preview`` body).

        Defaults to the ``cv2`` kind. Raises :class:`ValueError` on an unknown kind so a
        route can 422 it — the ``embed`` kind isn't renderable until its branch lands
        with the user-commands work. Buttons are coerced to ``(str, str)`` pairs here;
        non-http(s) URLs are dropped at render time (see :func:`post_spec_nodes`).
        """
        kind = payload.get("kind", "cv2")
        if kind != "cv2":
            raise ValueError(f"Unsupported post kind: {kind!r}")
        raw_buttons = payload.get("buttons") or []
        buttons = [
            (str(b.get("label", "")), str(b.get("url", "")))
            for b in raw_buttons
            if isinstance(b, t.Mapping)
        ]
        return cls.cv2(
            body=str(payload.get("body", "")),
            image_url=(payload.get("image_url") or None),
            buttons=buttons,
        )


# ---------------------------------------------------------------------------
# Draft metadata (post message id, publish status, "needs attention" flags)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DraftMeta:
    #: Id of the single in-channel post in the followable; 0 = not posted.
    message_id: int = 0
    #: Wall-clock reset boundary (``current_reset_ts()`` at post time) of the period the
    #: tracked ``message_id`` belongs to. Stamped from the clock, NOT the draft's
    #: (user-overridable) ``reset_ts``, so it always names the real period. Lets the
    #: form tell if the tracked post is *this* period's (see :meth:`is_current`). A
    #: legacy doc predating this field carries 0.
    reset_ts: int = 0
    #: Whether that post has been crossposted (broadcast to followers via beacon).
    crossposted: bool = False
    #: "draft" (no post) | "posted" (uncrossposted) | "published" (crossposted).
    status: str = "draft"
    last_edited_by: int = 0
    last_edited_ts: int = 0
    needs_attention: list[str] = dataclasses.field(default_factory=list)

    def is_current(self, reset_ts: int) -> bool:
        """Whether the tracked post belongs to reset period ``reset_ts``.

        Drives the form's Edit/Delete-vs-Create split. True only when a post exists AND
        its stamped period matches ``reset_ts``. A post whose stamp names any *other*
        period is not current — the form starts a fresh draft for ``reset_ts`` and
        offers Create, and the old post stays in the channel as that week's history.
        This covers the common after-reset case (stamp = last week), a future stamp, and
        a legacy ``reset_ts == 0`` doc from before per-period tracking: 0 names no known
        period, so its post is treated as a past post to be superseded by a Create
        rather than edited in place forever. (Editing never re-stamps ``reset_ts`` —
        only :func:`_send_new_post` does — so a 0 stamp that counted as "current" would
        stay stuck on Edit every week and never flip to Create.) NB for producers whose
        post is optional (e.g. Trials): a ``False`` here is a normal "no post this
        period" state, not an error.
        """
        return self.message_id != 0 and self.reset_ts == reset_ts

    def to_dict(self) -> dict[str, t.Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: t.Mapping[str, t.Any] | None) -> "DraftMeta":
        if not d:
            return cls()
        status = d.get("status", "draft")
        # Back-compat: pre-lifecycle docs stored ``published_message_id`` (and no
        # ``crossposted``). Read the old key into ``message_id`` and default
        # ``crossposted`` from the legacy "published" status. ``reset_ts`` predates the
        # per-period tracking, so old docs default it to 0 — which ``is_current`` treats
        # as a past period, so the next form load offers Create (the legacy post becomes
        # that week's history) instead of editing it in place forever.
        message_id = int(d.get("message_id", d.get("published_message_id", 0)) or 0)
        return cls(
            message_id=message_id,
            reset_ts=int(d.get("reset_ts", 0) or 0),
            crossposted=bool(d.get("crossposted", status == "published")),
            status=status,
            last_edited_by=int(d.get("last_edited_by", 0)),
            last_edited_ts=int(d.get("last_edited_ts", 0)),
            needs_attention=list(d.get("needs_attention") or []),
        )


# ---------------------------------------------------------------------------
# Publish-time error messaging
# ---------------------------------------------------------------------------


def _discord_error_note(exc: Exception) -> str:
    """A short, user-facing reason for a failed in-channel post/edit/crosspost.

    Discord rejects proxied/temporary image URLs (e.g. ``images-ext-*.discordapp.net``
    or ``media.discordapp.net/external/…`` links copied from an embed/tweet) with an
    "Invalid resource" 401 — the most common cause of a failed post here. Surface a
    concrete hint for that; otherwise pass the trimmed Discord message through.
    """
    msg = str(getattr(exc, "message", "") or exc)
    if "Invalid resource" in msg or "discordapp.net/external/" in msg:
        return (
            "Discord rejected the image URL — it looks like a Discord/social-media "
            "proxy link. Paste the original direct image URL instead (e.g. the "
            "https://pbs.twimg.com/… link, or a Discord attachment URL)."
        )
    # A crosspost give-up (:class:`~dd.anchor.utils.CrosspostError`) is about publishing
    # to followers, not the post's content — the in-channel post itself is fine, so
    # don't frame it as a rejected post.
    if isinstance(exc, utils.CrosspostError):
        return f"Couldn't publish to followers: {msg[:200]}"
    return f"Discord rejected the post: {msg[:200]}"


# ---------------------------------------------------------------------------
# Producer spec + publishing
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class HybridPostSpec:
    """The producer-specific hooks the generic publish/route code needs.

    One instance per producer (weekly_reset, trials). Context objects (the producer's
    ``*Context`` dataclass) are opaque to this module — they are only passed back to
    ``render``/``validate``, so the callables are typed with ``...`` parameters rather
    than a shared context type. ``render`` in particular should late-resolve the
    producer's ``format_*`` (so a monkeypatched renderer is honoured) — see
    ``weekly_reset``'s spec construction.
    """

    #: feed slug this post publishes to (dd.common.settings.FOLLOWABLE_SLUGS key).
    followable_key: str
    #: Human name of the post for the Create/Edit 409 messages (e.g. "Trials post").
    post_noun: str
    #: ``() -> int`` — the current reset-period boundary used for
    #: ``DraftMeta.is_current`` (the create-vs-edit split). A producer-supplied hook
    #: (usually a late-binding wrapper over its module ``current_reset_ts``) so a test
    #: that monkeypatches the producer's ``current_reset_ts`` steers the route code's
    #: notion of "now".
    current_reset_ts: t.Callable[..., int]
    #: async ``(ctx, bot) -> HMessage`` — render the context to the CV2 message.
    render: t.Callable[..., t.Awaitable[HMessage]]
    #: ``(ctx) -> list[str]`` — publish-blocking problems (empty = ok).
    validate: t.Callable[..., list[str]]
    #: ``(ctx) -> str`` — the post body markdown (for the live preview).
    build_body: t.Callable[..., str]
    #: async ``() -> ctx | None`` — load the persisted draft (None = none saved).
    load_draft: t.Callable[..., t.Awaitable[t.Any]]
    #: async ``(ctx) -> None`` — persist the draft.
    save_draft: t.Callable[..., t.Awaitable[None]]
    #: async ``() -> ctx`` — build a fresh seeded draft (form-load fallback).
    build_context: t.Callable[..., t.Awaitable[t.Any]]
    #: async ``(payload) -> ctx`` — server-side context from the form JSON.
    context_from_payload: t.Callable[..., t.Awaitable[t.Any]]
    #: async ``() -> DraftMeta`` — load the draft metadata row.
    load_meta: t.Callable[..., t.Awaitable[DraftMeta]]
    #: async ``(meta) -> None`` — persist the draft metadata row.
    save_meta: t.Callable[..., t.Awaitable[None]]
    #: async ``(draft, meta) -> dict`` — the page bootstrap JSON for the form.
    build_bootstrap: t.Callable[..., t.Awaitable[dict[str, t.Any]]]
    #: async ``(payload, ctx) -> None`` — persist the carried-over default image if the
    #: form's "use as default" box is ticked (else a no-op).
    persist_default_image: t.Callable[..., t.Awaitable[None]]
    #: The producer's web-form HTML template (bootstrap placeholder substituted in).
    form_html_path: Path
    #: Serialises read-modify-write of the shared draft doc (single bot process).
    draft_lock: asyncio.Lock
    #: Optional async ``(ctx) -> None`` fired ONCE when a post transitions to
    #: crossposted (published to followers) — i.e. it actually went live this period.
    #: Producers use it for "on publish" side effects (Trials advances its loot-set
    #: rotation here); NOT fired for uncrossposted posts/edits or the seeding cron, so a
    #: draft that is never published (or is deleted) has no effect.
    on_published: t.Callable[..., t.Awaitable[None]] | None = None
    #: Post-specific footer "guide" links (``(label, url)``); the shared Support button
    #: is appended by :func:`~dd.common.components.footer_button_specs`. Drives both the
    #: published button row and the preview's rendered buttons.
    footer_guides: tuple[tuple[str, str], ...] = ()

    @property
    def channel_id(self) -> int:
        # Sync (a property can't be awaited) — see
        # settings.get_followable_channel_sync's docstring; call sites needing the
        # live value use the async getter directly.
        return settings.get_followable_channel_sync(self.followable_key)


# ---------------------------------------------------------------------------
# Preview emoji cache (shared by every producer's /preview route)
# ---------------------------------------------------------------------------

#: Short-lived cache of the guild emoji dict used to render the rich preview. Each form
#: POSTs on every ~400 ms keystroke, so a REST fetch per request would hammer Discord —
#: cache the dict for a few minutes instead. The dict is the same Kyber guild for every
#: producer, so one process-wide cache serves them all.
_EMOJI_CACHE_TTL = dt.timedelta(minutes=5)
_emoji_cache: dict[str, h.Emoji] | None = None
_emoji_cache_at: dt.datetime | None = None


async def preview_emoji_dict(bot: CachedFetchBot | None) -> dict[str, h.Emoji]:
    """The Kyber guild emoji dict for the preview, cached with a short TTL.

    Returns an empty dict (no emoji substitution) when the bot isn't up yet or the fetch
    fails, so the preview degrades to escaped ``:name:`` text rather than erroring.
    """
    global _emoji_cache, _emoji_cache_at
    if bot is None:
        return {}
    now = dt.datetime.now(tz=dt.UTC)
    if (
        _emoji_cache is not None
        and _emoji_cache_at is not None
        and now - _emoji_cache_at < _EMOJI_CACHE_TTL
    ):
        return _emoji_cache
    try:
        _emoji_cache = await fetch_emoji_dict(bot)
        _emoji_cache_at = now
    except Exception:
        logger.warning("hybrid_post_core: preview emoji fetch failed", exc_info=True)
        return _emoji_cache or {}
    return _emoji_cache


# ---------------------------------------------------------------------------
# Web-form routes (auth is enforced centrally by the web_auth middleware)
# ---------------------------------------------------------------------------
#
# One set of handler bodies serves every producer; the producer-specific bits (context
# model, bootstrap payload, option pools) come through ``spec``. Each producer keeps six
# thin ``_handle_*`` wrappers that pass its ``spec`` and ``web.get_bot()`` in, read at
# call time so they track the one stash.
#
# The bot arrives as an argument rather than being read here, so these stay pure
# functions of their inputs (the tests call them with a fake bot); ``None`` is answered
# with the same 503 body ``web.require_bot()`` produces via its middleware, so a request
# that lands too early reads identically whichever route it hit.


def _bot_starting() -> aiohttp.web.Response:
    return web.bot_not_ready_response()


def _retire_meta(meta: DraftMeta) -> None:
    """Forget the tracked post, keeping the draft data so Create can re-post it.

    The reset every "the post is gone" path performs — deletion from the form, and
    :func:`reconcile_missing_post` finding it deleted in Discord.
    """
    meta.message_id = 0
    meta.reset_ts = 0
    meta.crossposted = False
    meta.status = "draft"


async def reconcile_missing_post(
    spec: HybridPostSpec, meta: DraftMeta, bot: CachedFetchBot | None
) -> DraftMeta:
    """The ``DraftMeta`` the caller should trust — retiring the tracked post if gone.

    ``DraftMeta`` records what we posted; it cannot know what happened to it afterwards.
    Someone deleting the message in Discord used to leave the form offering **Edit** for
    a message that no longer exists — the edit then fails, and Create is nowhere to be
    found. Checked once per form load.

    Persisting the answer is the point, not a side effect. Reporting it to the caller
    alone would fix the render and nothing else: ``post_action`` re-derives
    ``post_this_period`` from ``meta.is_current()`` on its own, so a form that had been
    talked into offering Create would get a 409 telling it a post already exists — a
    button the server refuses. Writing the record back keeps ``is_current()`` the single
    source of truth, so no consumer needs a second predicate.

    Returns a meta rather than a verdict because the fetch runs OUTSIDE ``draft_lock``
    (a Discord REST call can take seconds, and that lock serialises create/edit/delete —
    holding it across a probe on every form GET would let one hung fetch wedge every
    write). By the time a 404 is confirmed, another tab or the cron may have created a
    new post, or the delete handler may have retired the record. A bool cannot express
    the difference: ``True`` is wrong in the second case, ``False`` in the first. The
    locked re-read sees whichever happened, and the caller renders from *that*.

    Unknown counts as *present*: a REST blip or a bot that is still starting must not
    flip the form into offering a second post for the period. Only a definite "not
    found" (Discord 404) retires the record.
    """
    if not meta.message_id or bot is None:
        return meta
    try:
        await bot.fetch_message(spec.channel_id, meta.message_id)
    except h.NotFoundError:
        logger.info(
            "Tracked post %s is gone from channel %s; retiring the record",
            meta.message_id,
            spec.channel_id,
        )
        async with spec.draft_lock:
            # Re-read under the lock: a create/edit/delete may have landed since the
            # unlocked fetch. If the persisted record no longer names the message we
            # probed, that newer record is the truth — hand it back untouched, whether
            # it tracks a fresh post (render Edit) or was itself retired (render
            # Create). Only an unchanged record gets retired and persisted.
            current = await spec.load_meta()
            if current.message_id == meta.message_id:
                _retire_meta(current)
                await spec.save_meta(current)
            return current
    except Exception:
        # Anything else (rate limit, forbidden, transport) is not evidence of deletion.
        logger.warning(
            "Could not confirm tracked post %s still exists; assuming it does",
            meta.message_id,
            exc_info=True,
        )
    return meta


async def form_get(
    spec: HybridPostSpec, request: aiohttp.web.Request, bot: CachedFetchBot | None
) -> aiohttp.web.Response:
    # Auth is enforced by the web_auth middleware; this just renders the form.
    meta = await spec.load_meta()
    # When a post exists for this period, open the form on the saved draft that tracks
    # it (so you edit what's live); else start a fresh draft for the current period.
    # Keyed off the post's tracked period (is_current), NOT the draft's own reset_ts —
    # a user-overridable display field that must not decide staleness. A producer whose
    # post is optional (Trials) simply reports post_this_period False when none exists.
    # When the record claims a post for this period, verify it against Discord and adopt
    # whatever comes back: the same record (still live), a retired one (deleted in
    # Discord — offer Create, and the persisted record agrees so post_action will not
    # refuse it), or a newer record another writer landed while we were checking (offer
    # Edit for the post that now exists). `meta` is rebound rather than mutated, so the
    # bootstrap below — which re-derives its own flags from meta.is_current() — sees the
    # same truth the render does.
    now_ts = spec.current_reset_ts()
    if meta.is_current(now_ts):
        meta = await reconcile_missing_post(spec, meta, bot)
    post_this_period = meta.is_current(now_ts)
    draft = (await spec.load_draft() if post_this_period else None) or (
        await spec.build_context()
    )
    bootstrap = await spec.build_bootstrap(draft, meta)
    # Escape "<" so a "</script>" in the data can't break out of the inline <script>.
    bootstrap_js = json.dumps(bootstrap).replace("<", "\\u003c")
    page = spec.form_html_path.read_text(encoding="utf-8").replace(
        "/*__BOOTSTRAP__*/ null", bootstrap_js
    )
    return aiohttp.web.Response(text=page, content_type="text/html")


async def post_action(
    spec: HybridPostSpec,
    request: aiohttp.web.Request,
    bot: CachedFetchBot | None,
    *,
    create: bool,
) -> aiohttp.web.Response:
    """Shared backend for the Create/Edit (± publish) form actions.

    ``create=True`` sends a brand-new in-channel post for the current period (409 if one
    already exists — the form hides the button, this enforces it server-side, and
    forgets any stale prior-period id so we never edit a past post). ``create=False``
    edits the existing current-period post in place (409 if there is none).

    ``payload["publish"]`` selects the crosspost behaviour: publishing validates
    strictly and broadcasts to followers (blocking ``problems`` on failure); the plain
    post/edit is lenient (advisory ``warnings``, the draft is kept even if Discord
    rejects the post) — but a failed send/edit is a blocking ``problem`` so the form
    can't show a false "done". Both persist the draft so the saved copy tracks it.
    """
    if bot is None:
        return _bot_starting()
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "Malformed body."}, status=400)
    publish = bool(payload.get("publish"))
    ctx = await spec.context_from_payload(payload)

    async with spec.draft_lock:
        meta = await spec.load_meta()
        post_this_period = meta.is_current(spec.current_reset_ts())
        if create and post_this_period:
            return aiohttp.web.json_response(
                {
                    "error": f"A {spec.post_noun} already exists for this period — "
                    "edit or delete it instead."
                },
                status=409,
            )
        if not create and not post_this_period:
            return aiohttp.web.json_response(
                {
                    "error": f"No {spec.post_noun} exists for this period yet — "
                    "create one first."
                },
                status=409,
            )
        # Create drops the message-tracking fields so a fresh message is sent (and any
        # stale prior-period id is forgotten), while keeping editorial metadata like the
        # last editor; edit keeps the current meta so its message is updated in place.
        if create:
            meta.message_id = 0
            meta.reset_ts = 0
            meta.crossposted = False
            meta.status = "draft"
        meta.last_edited_ts = int(dt.datetime.now(tz=dt.UTC).timestamp())
        await spec.save_draft(ctx)

        note: str | None = None
        was_crossposted = meta.crossposted
        if publish:
            # Publishing validates strictly and crossposts; problems block the send.
            try:
                meta, note = await publish_draft(spec, bot, ctx, meta)
            except ValueError as exc:
                return aiohttp.web.json_response(
                    {"problems": str(exc).split("; ")}, status=422
                )
            except Exception as exc:  # Discord rejected the post/crosspost (bad image…)
                logger.warning("%s: publish failed", spec.followable_key, exc_info=True)
                # ``publish_draft`` may have already SENT the message (stamping
                # ``meta.message_id``) and only failed on the crosspost — persist the
                # meta so that live post isn't orphaned (a next Create would duplicate
                # it). Safe when nothing was sent: message_id is still 0.
                await spec.save_meta(meta)
                return aiohttp.web.json_response(
                    {"problems": [_discord_error_note(exc)]}, status=502
                )
            warnings: list[str] = []
        else:
            # Post/edit the uncrossposted message. Content problems (validate) are
            # non-blocking advisory warnings, but if the send/edit itself fails (e.g. a
            # bad image URL) the message did NOT change — report that as a blocking
            # problem so the form can't show a false "done ✓". The draft stays saved, so
            # the user fixes and retries without losing their in-page edits.
            warnings = spec.validate(ctx)
            try:
                meta = await post_or_edit_unpublished(spec, bot, ctx, meta)
            except Exception as exc:
                logger.warning(
                    "%s: in-channel post update failed",
                    spec.followable_key,
                    exc_info=True,
                )
                return aiohttp.web.json_response(
                    {"problems": [_discord_error_note(exc)]}, status=502
                )
        await spec.save_meta(meta)
        # Optionally persist this period's image as the carried-over default for future
        # drafts. An empty image URL with the box ticked clears the default.
        await spec.persist_default_image(payload, ctx)
        # Fire the "on publish" hook ONCE, only when this action actually took the post
        # live (uncrossposted -> crossposted). Uncrossposted posts/edits and the seeding
        # cron never reach here, so a draft that's never published has no side effect.
        if not was_crossposted and meta.crossposted and spec.on_published is not None:
            await spec.on_published(ctx)
    logger.info(
        "%s: %s via web form (publish=%s)",
        spec.followable_key,
        "created" if create else "edited",
        publish,
    )
    return aiohttp.web.json_response(
        {
            "ok": True,
            "note": note,
            "warnings": warnings,
            "post_this_period": meta.is_current(spec.current_reset_ts()),
            "crossposted": meta.crossposted,
        }
    )


async def preview(
    spec: HybridPostSpec, request: aiohttp.web.Request, bot: CachedFetchBot | None
) -> aiohttp.web.Response:
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Malformed body.")
    ctx = await spec.context_from_payload(payload)
    # The preview IS the post: this hands back the very node tree `build_cv2` would send
    # (pinned to it by test_post_spec_nodes_matches_build_cv2), and the page draws it
    # with the shared renderer every preview surface uses. It used to return HTML from a
    # second, flatter markup vocabulary that only approximated the container the post
    # actually is.
    #
    # The emoji map rides along rather than sitting in the page bootstrap, so the
    # response carries everything needed to draw it. Cheap: `preview_emoji_dict` is a
    # short-TTL cache, so a burst of previews costs no extra Discord traffic.
    emoji_dict = await preview_emoji_dict(bot)
    post = PostSpec.cv2(
        spec.build_body(ctx),
        ctx.image_url,
        buttons=footer_button_specs(guides=spec.footer_guides),
    )
    return aiohttp.web.json_response(
        {"nodes": post_spec_nodes(post), "emoji": emoji_payload(emoji_dict)}
    )


async def delete(
    spec: HybridPostSpec, request: aiohttp.web.Request, bot: CachedFetchBot | None
) -> aiohttp.web.Response:
    if bot is None:
        return _bot_starting()
    # Delete the in-channel post and reset the draft to unposted, under the same lock
    # the create/edit paths use. Deleting a crossposted message propagates the deletion
    # to following channels (and beacon mirrors the delete), so the post is removed
    # everywhere; the persisted draft data is kept so a later Create re-posts it.
    async with spec.draft_lock:
        meta = await spec.load_meta()
        if meta.message_id:
            channel_id = spec.channel_id
            try:
                await bot.rest.delete_message(channel_id, meta.message_id)
            except h.NotFoundError:
                pass  # already gone — fall through and reset the meta
            except Exception as exc:  # keep the meta so the post isn't orphaned
                logger.warning("%s: delete failed", spec.followable_key, exc_info=True)
                return aiohttp.web.json_response(
                    {"ok": False, "error": _discord_error_note(exc)}, status=502
                )
            _retire_meta(meta)
            await spec.save_meta(meta)
    return aiohttp.web.json_response({"ok": True})


async def _send_new_post(
    spec: HybridPostSpec, bot: CachedFetchBot, hmessage: HMessage, meta: DraftMeta
) -> None:
    """Send a fresh uncrossposted post and stamp its id + reset period onto ``meta``.

    Shared by the two "first post of the period" paths (:func:`post_or_edit_unpublished`
    and :func:`publish_draft`'s fallback). ``reset_ts`` is stamped from the producer's
    reset clock (``spec.current_reset_ts()``), NOT ``ctx.reset_ts`` — the draft boundary
    is a display field the user can override, but the tracked period must name the real
    reset period so :meth:`DraftMeta.is_current` stays correct.
    """
    posted = await utils.send_message(bot, hmessage, spec.channel_id, crosspost=False)
    meta.message_id = posted.id
    meta.reset_ts = spec.current_reset_ts()


async def post_or_edit_unpublished(
    spec: HybridPostSpec, bot: CachedFetchBot, ctx: t.Any, meta: DraftMeta
) -> DraftMeta:
    """Create-or-update the *uncrossposted* in-channel post for the current draft.

    The first call (``message_id == 0``) sends the assembled post to the followable
    WITHOUT crossposting, so the team can see and iterate it in Discord before it is
    broadcast. Later calls edit that message in place — this works whether the post is
    still uncrossposted or already published (an edit to a crossposted message
    re-mirrors via beacon). Returns the updated ``meta`` (caller persists); never
    crossposts.
    """
    hmessage = await spec.render(ctx, bot)
    channel_id = spec.channel_id
    if meta.message_id == 0:
        await _send_new_post(spec, bot, hmessage, meta)
        meta.status = "posted"
    else:
        await bot.rest.edit_message(
            channel_id, meta.message_id, components=hmessage.components
        )
    return meta


async def publish_draft(
    spec: HybridPostSpec, bot: CachedFetchBot, ctx: t.Any, meta: DraftMeta
) -> tuple[DraftMeta, str]:
    """Publish (crosspost) the existing in-channel post to the followable's channel.

    Publishing means *crossposting the post the team has iterated on* (created by
    :func:`post_or_edit_unpublished`), not sending a fresh message: the draft is first
    synced onto that message in place, then the message is crossposted so beacon mirrors
    it to every follower. Crossposting is idempotent — a re-publish (or any later
    save-driven edit) just re-mirrors the edit, no duplicate. Falls back to
    post-then-crosspost when nothing has been posted yet (``message_id == 0``). Returns
    the updated ``meta`` and a short note; raises ``ValueError`` (the joined
    ``spec.validate`` problems) instead of publishing an invalid post.
    """
    problems = spec.validate(ctx)
    if problems:
        raise ValueError("; ".join(problems))
    hmessage = await spec.render(ctx, bot)
    channel_id = spec.channel_id
    was_crossposted = meta.crossposted
    if meta.message_id:
        # Sync the in-channel post to the current draft before broadcasting it.
        await bot.rest.edit_message(
            channel_id, meta.message_id, components=hmessage.components
        )
    else:
        # No post yet (e.g. publish before any save): post it first, uncrossposted.
        await _send_new_post(spec, bot, hmessage, meta)
    # Strict crosspost: bounded retries that RAISE on give-up (a permanent error or the
    # attempt budget), so a failed publish returns an error to the form instead of the
    # request hanging forever — and we only reach the "published" stamp below on a real
    # crosspost, never on a silent non-news skip.
    await utils.crosspost_message_with_retries(
        bot, channel_id, meta.message_id, max_attempts=4
    )
    meta.crossposted = True
    meta.status = "published"
    note = (
        "✏️ Updated the published post — beacon re-mirrors the edit."
        if was_crossposted
        else "✅ Published and crossposted — beacon will mirror it out."
    )
    return meta, note


# ---------------------------------------------------------------------------
# Manifest weapon pool + resolver (shared by every producer's reward pickers)
# ---------------------------------------------------------------------------

#: One weapon/armour row: (name, hash, itemTypeDisplayName, itemType, rarity).
WeaponItem = tuple[str, int, str, int, str]

#: Rows pulled per ``fetchmany`` when scanning a manifest table. Big enough that the
#: round-trip overhead is noise, small enough that the raw JSON of a batch is a rounding
#: error next to the table (see :func:`iter_weapon_items`).
_ROW_BATCH = 200


async def iter_weapon_items(cursor: t.Any) -> list[WeaponItem]:
    """Read the manifest's named, non-dummy weapons/armour via ``cursor``, deduped.

    Runs the ``DestinyInventoryItemDefinition`` query on the caller-owned sqlite cursor
    (so a producer can share one manifest connection across several reads) and returns
    one row per (name, type), newest hash winning — the pool the reward autocomplete and
    :func:`resolve_weapon` search. Whites/greens and redacted/dummy items are dropped.

    Rows are consumed in ``fetchmany`` batches, not one ``fetchall()``: the item table
    is the manifest's largest and materialising all ~39k raw JSON strings at once cost
    240 MB (+284 MB RSS) before a single one was even parsed. Only the deduped result
    survives the loop, so batching is output-identical and drops the peak to ~6 MB.
    """
    item_by_key: dict[tuple[str, str], WeaponItem] = {}
    await cursor.execute("SELECT json FROM DestinyInventoryItemDefinition")
    while batch := await cursor.fetchmany(_ROW_BATCH):
        for (row,) in batch:
            defn = json.loads(row)
            item_type = defn.get("itemType")
            if item_type not in (2, 3) or defn.get("redacted"):
                continue
            rarity = (defn.get("inventory") or {}).get("tierTypeName", "")
            if rarity in ("", "Common", "Basic"):  # drop dummies/whites/greens
                continue
            name = (defn.get("displayProperties") or {}).get("name")
            if not name:
                continue
            type_name = defn.get("itemTypeDisplayName", "")
            hash_ = int(defn["hash"])
            key = (name.lower(), type_name.lower())
            existing = item_by_key.get(key)
            if existing is None or hash_ > existing[1]:  # keep the newest hash
                item_by_key[key] = (name, hash_, type_name, item_type, rarity)
    return sorted(item_by_key.values(), key=lambda e: e[0].lower())


#: Process-wide cache of the manifest weapon/armour pool + its build lock. Every
#: producer's reward pickers search the SAME pool, so the ~4166-row
#: DestinyInventoryItemDefinition scan + JSON decode runs once and is held in a single
#: list, not one copy per producer.
_weapon_pool: list[WeaponItem] | None = None
_weapon_pool_lock = asyncio.Lock()


async def get_weapon_pool() -> list[WeaponItem]:
    """Build (once) and cache the manifest weapon/armour pool, shared process-wide.

    Opens its own short-lived manifest connection and runs :func:`iter_weapon_items`;
    the result is cached so subsequent callers (every producer + its prewarm) reuse it
    rather than re-scanning the item table. On any failure returns ``[]`` **without
    caching**: the caller degrades to a manifest-less form and a later call retries, so
    a transient manifest error doesn't permanently disable the reward pickers.
    """
    global _weapon_pool
    if _weapon_pool is not None:
        return _weapon_pool
    async with _weapon_pool_lock:
        if _weapon_pool is None:
            try:
                path = await api._get_latest_manifest(schemas.BungieCredentials.api_key)
                async with aiosqlite.connect(path) as con:
                    cur = await con.cursor()
                    _weapon_pool = await iter_weapon_items(cur)
            except Exception:
                logger.warning("manifest weapon-pool build failed", exc_info=True)
                return []
        return _weapon_pool


#: Ordered emoji-name aliases tried when a weapon type's primary slug isn't a guild
#: emoji, before falling through to the generic ``:weapon:``. The manifest calls a bow a
#: "Combat Bow" (→ ``combat_bow``, which the guild has), but a stray ``bow`` slug should
#: still land on the bow icon rather than the generic one.
WEAPON_EMOJI_FALLBACKS: dict[str, tuple[str, ...]] = {"bow": ("combat_bow",)}


def weapon_emoji_name(emoji_name: str | None, available: t.Container[str]) -> str:
    """The best guild emoji name for a weapon type: its slug, an alias, else ``weapon``.

    Tries the type's own slug, then :data:`WEAPON_EMOJI_FALLBACKS` aliases, then the
    generic ``weapon`` — returning the first that ``available`` (the guild emoji names)
    has. Shared by the Trials and Iron Banner producers so the ``bow`` → ``combat_bow``
    fallback (and the ``weapon`` default) isn't duplicated.
    """
    for name in (emoji_name, *WEAPON_EMOJI_FALLBACKS.get(emoji_name or "", ())):
        if name and name in available:
            return name
    return "weapon"


#: A trailing ``" (Type)"`` the rotation editor's item autocomplete appends to a stored
#: weapon value (``"Felwinter's Lie (Shotgun)"``) for disambiguation.
_WEAPON_TYPE_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")


def strip_weapon_type(value: str) -> str:
    """Drop the editor's trailing ``" (Type)"`` so a value resolves to a manifest name.

    :func:`resolve_weapon` matches a bare manifest name or a numeric hash; a bare
    (baked-default) name passes through unchanged. Shared by the Trials/Iron Banner
    producers and the rotation-editor preview.
    """
    return _WEAPON_TYPE_SUFFIX.sub("", value).strip()


async def resolve_weapon_lines(
    names: t.Iterable[str], available: t.Container[str]
) -> list[str]:
    """Resolve weapon names to ``":emoji: [name](light.gg)"`` post lines.

    Each name is stripped of the editor's type suffix, resolved against the shared
    manifest pool (light.gg deep link + weapon-type emoji), and prefixed with its type
    emoji — falling back to the generic ``:weapon:`` for a type the guild has no emoji
    for. ``available`` is the set of guild emoji names for the surface being rendered
    (the live post's emoji dict, or the editor preview's cached one). Empty/unresolvable
    names are dropped. Used by the Iron Banner producer and its editor preview.
    """
    items = await get_weapon_pool()
    lines: list[str] = []
    for name in names:
        weapon = resolve_weapon(strip_weapon_type(str(name)), items)
        if weapon is None or not weapon.name:
            continue
        lines.append(
            f":{weapon_emoji_name(weapon.emoji_name, available)}: {weapon.markdown()}"
        )
    return lines


def resolve_weapon(value: str, items: t.Sequence[WeaponItem]) -> WeaponRef | None:
    """A hash (picked from autocomplete) -> full WeaponRef; else a plain typed name.

    ``value`` is either a manifest hash (an autocomplete pick) resolved against
    ``items`` to a light.gg-linked, emoji-typed :class:`WeaponRef`, a case-insensitive
    name match, or — failing both — a hash-less plain-text ``WeaponRef`` for a
    free-typed name. An empty ``value`` clears the slot (``None``).
    """
    value = value.strip()
    if not value:
        return None
    # ASCII digits only. ``str.isdigit()`` is true for superscripts and enclosed forms
    # too ("²", "①"), which ``int()`` then refuses — so the bare check let a lone "²"
    # in a weapon slot 500 the form instead of becoming a weapon named "²". A manifest
    # hash is ASCII digits; this is the same narrow reading the emoji-id checks take.
    if value.isascii() and value.isdigit():
        wanted = int(value)
        for name, hash_, type_name, _item_type, _rarity in items:
            if hash_ == wanted:
                return WeaponRef(name, hash_, api.likely_emoji_name(type_name))
    for name, hash_, type_name, _item_type, _rarity in items:
        if name.lower() == value.lower():
            return WeaponRef(name, hash_, api.likely_emoji_name(type_name))
    return WeaponRef(name=value)
