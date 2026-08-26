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

"""The web Components-V2 builder's HTTP surface — the replacement for ``/post
components``' in-Discord builder.

A draft (:class:`Cv2Draft`) is minted with a target, and the page loads it, autosaves as
the author edits, and publishes when the author confirms. Two things mint one: a Discord
command (``posts.py``, which decides the target from the invocation), and — for a
one-off custom post started from the control panel — ``POST /cv2-builder/new``, where
the browser names the target channel and the **server independently vets it** before
it is stored (see :func:`_handle_new`). Either way the row carries **what publishing
means** (post / edit / send a copy) and where it lands.

``GET /custom-post`` (``web_static/custom_post.html``) is the browser's half of that
second path: one channel picker and one button, whose only job is to produce the
``channel_id`` the mint route will vet. It is a separate page and not a step inside the
builder because the target is fixed at mint and immutable afterwards — the question has
to be asked before the draft exists.

Routes (all behind the shared Discord OAuth middleware; every draft-scoped one is
additionally scoped to the draft's creator — see :meth:`Cv2Draft.get_for_user`):

- ``GET  /custom-post``                  pick the channel; the doorway to the two below
- ``POST /cv2-builder/new``              vet a channel, mint a draft, return its path
- ``GET  /cv2-builder/{draft}``          the static page shell
- ``GET  /cv2-builder/{draft}/data``     seed nodes, action copy, guild emoji
- ``POST /cv2-builder/{draft}/save``     autosave the node list
- ``POST /cv2-builder/{draft}/preview``  the tree the server would post (confirmation)
- ``POST /cv2-builder/{draft}/publish``  perform the action, return the message link

**Trust boundary.** Rendering is the client's job — the canvas is the live editing
surface, so a round-trip per keystroke is not an option, and one shared renderer
(``web_static/cv2_render.js``) draws every preview surface. None of that is
load-bearing. ``/publish`` resolves the tree's ``:name:`` shortcodes (the client only
ever *renders* them; Discord needs the ``<:name:id>`` mention in the payload), re-runs
:func:`cv2_nodes.validate` on the result and sends through :class:`RawComponentBuilder`
from the node list *it* was given, so a tampered or stale client cannot post something
the server did not independently accept.

The target is the other half of that boundary. A browser names one **exactly once, at
mint**, and only through :func:`_handle_new`, which refuses anything
:func:`autopost_settings.check_channel` won't vouch for — right guild, postable type,
bot fully permitted there — and stores the guild id it resolved itself rather than one
the client also sent. From then on the target is a property of the row:
:func:`_handle_publish` reads ``target_channel_id`` off the draft and **never** off the
request, so a later request cannot redirect an existing draft at a channel that was
never vetted.

What ``/preview`` is *for* changed with that, and the distinction is worth keeping
straight. It used to return server-rendered HTML, which made the confirmation dialog a
second, independent implementation of the render — a client-side bug was visible there
before an irreversible send. Sharing the renderer gives that up, so ``/preview`` now
returns the **sanitized, validated node tree**: the confirmation still shows something
the server vouched for, and it differs from the canvas exactly where
:func:`cv2_nodes.sanitize_for_preview` changed something, which is the part worth
seeing. What replaces the lost cross-check is the golden corpus in
``dd/anchor/preview_fixtures``, which holds the renderer to a pinned output from both
languages. See ``docs/architecture.md``, "Rendering a message on the web".

Drafts are scratch space, so this module also owns their disposal: :func:`_prune_drafts`
runs once at boot and again on a daily cron (see :func:`_on_started`).
"""

import logging
import typing as t
import uuid
from pathlib import Path

import aiocron
import aiohttp.web
import hikari as h
import lightbulb as lb

from ...common import cfg, settings
from ...common.schemas import Cv2Draft
from ...common.utils import fetch_emoji_dict
from .. import cv2_nodes, web
from ..cv2_raw import RawComponentBuilder
from . import autopost_settings
from .web_auth import authed_user_id

loader = lb.Loader()

_PAGE_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "cv2_builder.html"
)

# The channel-picking doorway in front of the builder (``GET /custom-post``). A page of
# its own rather than a step inside the builder because a draft's target is fixed at
# mint — :func:`_handle_new` vets it once and stores what it resolved, and
# :func:`_handle_publish` reads it off the row and never off the request. That is what
# makes a later request unable to redirect an existing draft; the cost is that the
# question has to be answered before there is a draft to answer it on.
_CUSTOM_POST_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "custom_post.html"
)

# Emoji dicts are a REST round-trip per guild; the builder asks for one on every page
# load, so keep the last result briefly rather than re-fetching per draft.
_emoji_cache: dict[str, dict[str, t.Any]] | None = None


async def _emoji_map() -> dict[str, dict[str, t.Any]]:
    """``{name: {url, id, animated}}`` for the client's emoji handling.

    The URL drives ``:shortcode:`` substitution in the renderer (``cv2_model.js``'s
    ``emojiEntry``). The **id** is what a *button* needs: Discord renders a custom emoji
    on one only from ``{"id": …, "name": …}`` — a name alone is valid for a unicode
    emoji and silently nothing for a custom one, which is why this map is richer than
    the ``{name: url}`` shape :func:`hybrid_post_core.emoji_payload` sends elsewhere.

    A failure here costs shortcode rendering across the page — the canvas, the button
    emoji picker and the publish confirmation all resolve from this one map now — so it
    degrades to an empty map (shortcodes render as their literal text) rather than
    failing the page.
    """
    global _emoji_cache
    if _emoji_cache is not None:
        return _emoji_cache
    try:
        emoji_dict = await fetch_emoji_dict(t.cast(h.GatewayBot, web.require_bot()))
        _emoji_cache = {
            name: {
                "url": str(getattr(emoji, "url", "")),
                "id": str(getattr(emoji, "id", "") or ""),
                "animated": bool(getattr(emoji, "is_animated", False)),
            }
            for name, emoji in emoji_dict.items()
        }
    except Exception as e:
        logging.warning("CV2 builder: could not resolve the emoji dict: %r", e)
        _emoji_cache = {}
    return _emoji_cache


async def _substitute_emoji(nodes: list[cv2_nodes.Node]) -> list[cv2_nodes.Node]:
    """Resolve the tree's ``:name:`` shortcodes against the guild's emoji.

    Fetched fresh rather than read off ``_emoji_map``'s cache: a publish is rare (that
    cache exists for the per-page-load round-trip) and an emoji added since the page
    loaded should still resolve. A fetch failure leaves the tree alone and logs — the
    post goes out with literal shortcodes, which is worse than a mention but much better
    than losing the send.
    """
    try:
        emoji_dict = await fetch_emoji_dict(t.cast(h.GatewayBot, web.require_bot()))
    except Exception as e:
        logging.warning("CV2 builder: publishing without emoji substitution: %r", e)
        return nodes
    return cv2_nodes.substitute_emoji(nodes, emoji_dict)


async def _load_draft(request: aiohttp.web.Request) -> Cv2Draft:
    """The requested draft, or 404 — creator-scoped, so another owner gets the same
    answer as a stranger rather than a hint that the draft exists."""
    draft_id = request.match_info["draft"]
    draft = await Cv2Draft.get_for_user(draft_id, authed_user_id(request))
    if draft is None:
        raise aiohttp.web.HTTPNotFound(text="No such draft.")
    return draft


async def _nodes_from_body(request: aiohttp.web.Request) -> list[cv2_nodes.Node]:
    """The ``nodes`` list from a JSON body, rejecting anything not shaped like one."""
    try:
        body = await request.json()
    except Exception:
        raise aiohttp.web.HTTPBadRequest(text="Expected a JSON body.") from None
    nodes = body.get("nodes") if isinstance(body, dict) else None
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        raise aiohttp.web.HTTPBadRequest(text="`nodes` must be a list of components.")
    return nodes


def _message_link(guild_id: int | None, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}/{message_id}"


def _builder_path(draft_id: str) -> str:
    """The same-origin path a draft's editor lives at."""
    return f"/cv2-builder/{draft_id}"


# Column attributes read off an ORM instance are plain ints at runtime, but the query
# layer types them as `Column[Unknown]`. Coercing at the boundary keeps the Discord call
# sites honestly typed instead of suppressing the whole module in ty.toml.
def _as_int(value: t.Any) -> int:
    return int(value)


def _as_opt_int(value: t.Any) -> int | None:
    return None if value is None else int(value)


# --- handlers ----------------------------------------------------------------------


async def _handle_page(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # The shell is static; the draft is fetched by the page so a bad id renders a
    # friendly in-page message instead of a bare 404 document.
    return aiohttp.web.Response(
        text=_PAGE_HTML_PATH.read_text(encoding="utf-8"), content_type="text/html"
    )


async def _handle_custom_post_page(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    # Static shell, like the builder's own. The channel list is fetched by the page from
    # ``/autopost_settings/channels``, so a bot that is still starting renders a page
    # that says so rather than a route that refuses to render at all.
    return aiohttp.web.Response(
        text=_CUSTOM_POST_HTML_PATH.read_text(encoding="utf-8"),
        content_type="text/html",
    )


async def _handle_data(request: aiohttp.web.Request) -> aiohttp.web.Response:
    draft = await _load_draft(request)

    channel_id = _as_opt_int(draft.target_channel_id)
    published_id = _as_opt_int(draft.published_message_id)
    channel_mention = f"<#{channel_id}>" if channel_id else None
    published_link = (
        _message_link(_as_opt_int(draft.guild_id), channel_id, published_id)
        if published_id and channel_id
        else None
    )
    return aiohttp.web.json_response(
        {
            "action": draft.action,
            "nodes": draft.nodes or [],
            "emoji": await _emoji_map(),
            "default_accent": int(await settings.get_embed_default_color()),
            "target_channel_mention": channel_mention,
            "published_message_link": published_link,
        }
    )


async def _handle_save(request: aiohttp.web.Request) -> aiohttp.web.Response:
    draft = await _load_draft(request)
    nodes = await _nodes_from_body(request)
    await Cv2Draft.save_nodes(draft.id, authed_user_id(request), nodes)
    return aiohttp.web.json_response({"ok": True})


async def _handle_preview(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """The tree the server would post, for the confirmation dialog to draw.

    :func:`cv2_nodes.sanitize_for_preview` downgrades a mid-construction node — an empty
    container, a section still missing its accessory — to placeholder text, so what
    comes back is always something Discord would accept. The client renders it with the
    same module it draws the canvas with; the authority here is the *data*, not markup.

    No emoji dict: the page already holds one from ``/data`` and resolves shortcodes
    itself, so a preview no longer costs a Discord round-trip. No problem list either:
    the client blocks the button on its own copy of the rules, and the rules that
    actually matter are re-run in :func:`_handle_publish`.
    """
    await _load_draft(request)  # 404s a draft that isn't the caller's
    nodes = await _nodes_from_body(request)
    return aiohttp.web.json_response({"nodes": cv2_nodes.sanitize_for_preview(nodes)})


async def _handle_publish(request: aiohttp.web.Request) -> aiohttp.web.Response:
    draft = await _load_draft(request)
    nodes = await _nodes_from_body(request)
    # What the author typed, kept for the autosave below: the draft stores shortcodes so
    # a later edit shows `:armor:` in the editor rather than a raw mention.
    authored_nodes = nodes
    user_id = authed_user_id(request)

    # Resolve `:name:` shortcodes to `<:name:id>` mentions *before* validating, for the
    # same reason `finalize_cv2_post` does it before `guard_cv2_hmessage`: a mention is
    # ~20 characters longer than the shortcode it replaces, and Discord's 4000-character
    # cap counts the mention. Validating the authored text would pass a tree Discord
    # then refuses — on a loot table with forty item icons, by roughly 800 characters.
    nodes = await _substitute_emoji(nodes)

    # Re-validate server-side. The client blocks the button on the same rules, but the
    # rules that matter are the ones enforced here.
    problems = cv2_nodes.validate(nodes)
    if problems:
        return aiohttp.web.json_response({"error": " ".join(problems)}, status=400)

    bot = web.require_bot()
    components = [RawComponentBuilder(node) for node in nodes]
    channel_id = _as_opt_int(draft.target_channel_id)
    if channel_id is None:
        return aiohttp.web.json_response(
            {"error": "This draft has no target channel."}, status=400
        )

    try:
        if draft.action == Cv2Draft.ACTION_EDIT:
            message = await bot.rest.edit_message(
                channel_id,
                _as_int(draft.target_message_id),
                components=components,
                flags=h.MessageFlag.IS_COMPONENTS_V2,
            )
        else:
            channel = t.cast(h.TextableChannel, await bot.fetch_channel(channel_id))
            message = await channel.send(components=components)
    except h.ForbiddenError:
        return aiohttp.web.json_response(
            {
                "error": "I don't have permission to post in that channel. "
                "Check my role, then try again."
            },
            status=403,
        )
    except h.BadRequestError as e:
        # Discord's own rejection is the most useful thing to show — it names the
        # offending field far better than a generic message could.
        return aiohttp.web.json_response(
            {"error": f"Discord rejected the message: {e}"}, status=400
        )
    except Exception as e:
        logging.exception(e)
        return aiohttp.web.json_response(
            {"error": "Something went wrong sending that. It has been logged."},
            status=500,
        )

    draft_id = str(draft.id)
    await Cv2Draft.save_nodes(draft_id, user_id, authored_nodes)
    await Cv2Draft.mark_published(draft_id, user_id, int(message.id))
    return aiohttp.web.json_response(
        {
            "ok": True,
            "link": _message_link(
                _as_opt_int(draft.guild_id),
                int(message.channel_id),
                int(message.id),
            ),
        }
    )


# --- draft creation ------------------------------------------------------------------


async def new_draft_id(
    *,
    user_id: int,
    action: str,
    nodes: list[cv2_nodes.Node] | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    """Create a draft and return its id.

    Split out from :func:`new_draft` because the two callers want different things back.
    The Discord side needs an absolute URL to put in a message; a web caller wants the
    same-origin path (:func:`_builder_path`) and must not go through
    ``cfg.public_base_url``, which is legitimately empty on a local dev box — an
    absolute URL built from it would be a dead link on the one deployment where the
    whole flow is most likely to be exercised.
    """
    draft_id = uuid.uuid4().hex
    await Cv2Draft.create(
        id=draft_id,
        created_by=user_id,
        action=action,
        nodes=nodes or [],
        guild_id=guild_id,
        target_channel_id=channel_id,
        target_message_id=message_id,
    )
    return draft_id


async def new_draft(
    *,
    user_id: int,
    action: str,
    nodes: list[cv2_nodes.Node] | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    """Create a draft and return the absolute URL the author should open.

    Called from ``posts.py``: the Discord side decides the target, the web side only
    ever edits nodes. The reply is read in a Discord client, which has no origin to
    resolve a relative path against, so this one is absolute.
    """
    draft_id = await new_draft_id(
        user_id=user_id,
        action=action,
        nodes=nodes,
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=message_id,
    )
    return f"{cfg.public_base_url.rstrip('/')}{_builder_path(draft_id)}"


async def _handle_new(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Vet a browser-supplied target channel, mint a draft for it, return its path.

    This is the one place a browser gets to name a publish target, and the point of the
    route is the vetting: :func:`autopost_settings.check_channel` is the same validator
    the settings page's channel fields save through, run here with ``announce_only``
    off (a one-off custom post is just a message — nothing follows it, so a plain text
    channel is fine) and the guild scope a non-control-scoped field uses. It fails
    closed, and its refusal is a sentence written to be shown, so it is passed straight
    through rather than flattened into "that didn't work".

    Returns a *relative* path and lets the page decide whether to navigate: a JSON
    endpoint that answers 302 is a trap for the ``fetch()`` calling it, which follows
    the redirect and hands its caller the builder's HTML instead of an answer.
    """
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "Expected a JSON body."}, status=400)

    raw_channel_id = body.get("channel_id") if isinstance(body, dict) else None
    try:
        channel_id = int(str(raw_channel_id).strip())
    except (TypeError, ValueError):
        channel_id = 0
    if channel_id <= 0:
        return aiohttp.web.json_response(
            {"error": "Pick a channel to post in."}, status=400
        )

    check = await autopost_settings.check_channel(
        channel_id,
        announce_only=False,
        allowed_guild_ids=autopost_settings.allowed_guild_ids(),
    )
    if check.problem is not None or check.channel is None:
        return aiohttp.web.json_response(
            # The reason IS the payload — see check_channel. Only a lead-in is added.
            {"error": f"Can't post in that channel: {check.problem}"},
            status=400,
        )

    draft_id = await new_draft_id(
        user_id=authed_user_id(request),
        action=Cv2Draft.ACTION_POST,
        # The guild the server resolved from the channel it fetched, not one the client
        # was also trusted to name alongside the channel id.
        guild_id=int(check.channel.guild_id),
        channel_id=channel_id,
    )
    return aiohttp.web.json_response({"path": _builder_path(draft_id)})


# --- registration --------------------------------------------------------------------


def register_cv2_builder_routes(app: aiohttp.web.Application) -> None:
    # Registered before the ``{draft}`` routes so the static path is the first candidate
    # the router tries: "new" is a legal draft id as far as the dynamic pattern is
    # concerned, and this way nothing rests on how aiohttp orders the two.
    app.router.add_post("/cv2-builder/new", _handle_new)
    app.router.add_get("/cv2-builder/{draft}", _handle_page)
    app.router.add_get("/cv2-builder/{draft}/data", _handle_data)
    app.router.add_post("/cv2-builder/{draft}/save", _handle_save)
    app.router.add_post("/cv2-builder/{draft}/preview", _handle_preview)
    app.router.add_post("/cv2-builder/{draft}/publish", _handle_publish)
    # The page that feeds /cv2-builder/new. Registered here rather than in a module of
    # its own because it is one static shell whose only server-side contract is the mint
    # route directly above it — splitting them would put the two halves of one flow in
    # two files that have to agree.
    app.router.add_get("/custom-post", _handle_custom_post_page)


web.register_routes(register_cv2_builder_routes)
web.register_card(
    web.Card(
        "Custom one-off post",
        # The reviewed design's own wording for this row, taken verbatim rather than
        # paraphrased — the landing page is a directory, and its descriptions were
        # written together to sit at one register.
        "Build a message from scratch and send it to a channel.",
        "/custom-post",
        web.CardGroup.SEND,
        # After Weekly Reset (10) and Trials (20), and after "Send a scheduled post now"
        # (30) — the group runs most-frequent errand first, and writing a post from
        # nothing is the rarest of the four.
        40,
        # "Start", not the default "Open": this row does not open something that already
        # exists. The reviewed design gives group 1 the verbs Open / Open / Choose /
        # Start, and the differences are the point — four buttons all reading "Open"
        # would say nothing about which is which.
        action="Start",
    )
)


async def _prune_drafts() -> None:
    """Drop drafts past :meth:`Cv2Draft.prune`'s retention. Never raises.

    Contained the same way the boot pass always was: a prune is housekeeping on scratch
    space, so a DB blip is worth a log line and nothing more — and on the cron path an
    escaping exception would take the scheduled job down with it, so the next day's
    sweep would not run either.
    """
    try:
        removed = await Cv2Draft.prune()
        if removed:
            logging.info("CV2 builder: pruned %d stale draft(s)", removed)
    except Exception as e:
        logging.warning("CV2 builder: draft prune failed: %r", e)


@loader.listener(h.StartedEvent)
async def _on_started(event: h.StartedEvent) -> None:
    # Drop stale drafts once per boot — they are scratch space, not history. (The bot
    # itself is stashed centrally in dd.anchor.web; the routes read it from there.)
    # This pass is what cleans up after a long outage; the cron below is what keeps a
    # long-lived process from accumulating drafts between restarts, which matters now
    # that a web button mints one per click rather than a Discord command per draft.
    await _prune_drafts()

    # 04:00 UTC: deliberately NOT 17:00, which is when every scheduled producer in this
    # bot fires (lost_sector, eververse, portal_ops, iron_banner, xur, ada) — a delete
    # has no reason to contend with the day's posting. 04:00 is otherwise unoccupied.
    # Registered here rather than at import time so it only starts on a live bot.
    @aiocron.crontab("0 4 * * *", start=True)
    async def prune_cv2_drafts() -> None:
        await _prune_drafts()
