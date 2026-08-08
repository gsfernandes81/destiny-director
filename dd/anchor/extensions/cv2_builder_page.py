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

A Discord command writes a :class:`Cv2Draft` and replies with a link here; the page
loads the draft, autosaves as the author edits, and publishes when they confirm. The
draft row carries **what publishing means** (post / edit / send a copy), so
the browser never supplies a target it could tamper with — it only ever sends nodes.

Routes (all behind the shared Discord OAuth middleware, and all additionally scoped to
the draft's creator — see :meth:`Cv2Draft.get_for_user`):

- ``GET  /cv2-builder/{draft}``          the static page shell
- ``GET  /cv2-builder/{draft}/data``     seed nodes, action copy, guild emoji
- ``POST /cv2-builder/{draft}/save``     autosave the node list
- ``POST /cv2-builder/{draft}/preview``  the tree the server would post (confirmation)
- ``POST /cv2-builder/{draft}/publish``  perform the action, return the message link

**Trust boundary.** Rendering is the client's job — the canvas is the live editing
surface, so a round-trip per keystroke is not an option, and one shared renderer
(``web_static/cv2_render.js``) draws every preview surface. None of that is
load-bearing. ``/publish`` re-runs :func:`cv2_nodes.validate` and sends through
:class:`RawComponentBuilder` from the node list *it* was given, so a tampered or stale
client cannot post something the server did not independently accept.

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
"""

import logging
import typing as t
import uuid
from pathlib import Path

import aiohttp.web
import hikari as h
import lightbulb as lb

from ...common import cfg, settings
from ...common.schemas import Cv2Draft
from ...common.utils import fetch_emoji_dict
from .. import cv2_nodes, web
from ..cv2_raw import RawComponentBuilder
from .web_auth import authed_user_id

loader = lb.Loader()

_PAGE_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "cv2_builder.html"
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
    user_id = authed_user_id(request)

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
    await Cv2Draft.save_nodes(draft_id, user_id, nodes)
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


# --- draft creation (used by the Discord commands) ----------------------------------


async def new_draft(
    *,
    user_id: int,
    action: str,
    nodes: list[cv2_nodes.Node] | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> str:
    """Create a draft and return the URL the author should open.

    Called from ``posts.py``: the Discord side decides the target, the web side only
    ever edits nodes.
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
    return f"{cfg.public_base_url.rstrip('/')}/cv2-builder/{draft_id}"


# --- registration --------------------------------------------------------------------


def register_cv2_builder_routes(app: aiohttp.web.Application) -> None:
    app.router.add_get("/cv2-builder/{draft}", _handle_page)
    app.router.add_get("/cv2-builder/{draft}/data", _handle_data)
    app.router.add_post("/cv2-builder/{draft}/save", _handle_save)
    app.router.add_post("/cv2-builder/{draft}/preview", _handle_preview)
    app.router.add_post("/cv2-builder/{draft}/publish", _handle_publish)


web.register_routes(register_cv2_builder_routes)


@loader.listener(h.StartedEvent)
async def _on_started(event: h.StartedEvent) -> None:
    # Drop stale drafts once per boot — they are scratch space, not history. (The bot
    # itself is stashed centrally in dd.anchor.web; the routes read it from there.)
    try:
        removed = await Cv2Draft.prune()
        if removed:
            logging.info("CV2 builder: pruned %d stale draft(s)", removed)
    except Exception as e:
        logging.warning("CV2 builder: draft prune failed: %r", e)
