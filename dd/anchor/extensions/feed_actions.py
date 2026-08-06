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

"""Per-feed actions — the web replacement for ``/<feed> send`` and ``show``.

**Two endpoints and no page.** The actions live on each feed's row on
``/autopost_settings``, and the rendered post appears in that page's modals: a feed has
no state a page could show that the row does not, so a page was only ever a click in the
way. What is deliberately not here is anything beyond the two actions — no toggle (it
stays solely on ``/autopost_settings``, so one row has one write path) and no status or
health, which is the deferred observability work in ``plans/anchor_web_ia.md`` §4.

Routes (both behind the shared Discord-OAuth middleware in ``web_auth``, which also
Origin-checks the POST, so there is no auth code here):

- ``GET  /feed/{name}/preview``  build the post now, return its node tree
- ``POST /feed/{name}/send``     publish it to the feed's channel

``/preview`` returns the **payload**, not HTML, and the page draws it with the shared
renderer in ``web_static/cv2_render.js`` — matching ``/mirror-logs/render``'s contract,
so both preview surfaces go through one renderer.

**Why send returns before the post lands.** Both announcers retry construction forever
with backoff (``autopost.discord_announcer``, ``xur.api_to_discord_announcer``), and
``utils.send_message`` retries too — deliberately, so a transient Bungie/Discord blip
never drops a post. Awaiting that inside an HTTP handler would mean a request that can
hang for hours, and ``api_to_discord_announcer`` posts its placeholder to the live
channel *before* constructing, so its retries must not be bounded from outside (see the
"once we've committed to posting, we always finish" invariant in ``xur.py``). So the
handler builds the post once up front — that catches the common failure, a constructor
error, synchronously and without touching the channel — then hands the announcer to a
background task and returns. Delivery is observable in the mirror log.
"""

import asyncio
import logging
import typing as t

import aiohttp.web
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage
from dd.hmessage.snapshot import cv2_payload

from ...common.bot import CachedFetchBot
from .. import web
from ..autopost import Feed, registered_feeds

logger = logging.getLogger(__name__)

loader = lb.Loader()

# The live bot, stashed at StartedEvent so the routes can reach the REST client (the
# pattern weekly_reset / rotation_editor / cv2_builder_page already use).
_bot: CachedFetchBot | None = None

# Feeds with a send in flight, so a double-click or a second tab can't fire two posts.
# `send_message`'s own dedupe only guards retry-races within one send — it does not stop
# two deliberate sends.
_sending: set[str] = set()
# Strong refs to the in-flight send tasks, so they are not garbage collected mid-flight.
_send_tasks: set[asyncio.Task] = set()


@loader.listener(h.StartedEvent)
async def _on_started(_event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED):
    global _bot
    _bot = bot


class BotNotReady(RuntimeError):
    """Raised when a route needs the bot before ``StartedEvent`` has stashed it."""


def _require_bot() -> CachedFetchBot:
    # A plain exception, not HTTPServiceUnavailable. Both call sites are inside a
    # `except Exception` that reports `str(e)` to the page, so nothing ever propagated
    # this as an HTTP response — it just arrived on screen as "Service Unavailable",
    # aiohttp's stringification of the class, with the sentence explaining what to do
    # dropped on the floor. That sentence is the whole value of the error.
    if _bot is None:
        raise BotNotReady("The bot is still starting — try again in a moment.")
    return _bot


def _feed_or_404(request: aiohttp.web.Request) -> Feed:
    """The registered feed named in the path, or a 404."""
    feed = registered_feeds().get(request.match_info.get("name", ""))
    if feed is None:
        raise aiohttp.web.HTTPNotFound(text="No such feed.")
    return feed


def _title(name: str) -> str:
    """``lost_sector`` → ``Lost Sector`` — display copy for a followable name."""
    return name.replace("_", " ").title()


async def _build(feed: Feed) -> HMessage:
    """Build the feed's post once, as the producer would right now."""
    return await feed.message_constructor_coro(bot=_require_bot())


async def _handle_preview(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Render what the producer would post right now.

    Fetched on click, not on page load: the Bungie-backed constructors (xur, eververse,
    ada, portal_ops) hit the live API and can take seconds or fail outright. A build
    failure is a legitimate answer here — Iron Banner between events raises, and the
    Discord ``show`` reported that the same way — so it renders as a message in the
    preview box rather than a 500.
    """
    feed = _feed_or_404(request)
    try:
        hmsg = await _build(feed)
    except Exception as e:
        logger.exception("feed preview failed for %s", feed.name)
        return aiohttp.web.json_response({"error": str(e) or e.__class__.__name__})
    # The same shape /mirror-logs/render returns for a captured version, so the page
    # draws it with the identical CV2Render.snapshotSpec call.
    return aiohttp.web.json_response(
        {"kind": "snapshot", "payload": cv2_payload(hmsg), "message_kind": "cv2"}
    )


def _announce(feed: Feed, publish: bool) -> t.Awaitable[t.Any]:
    """The announcer call for this feed, as an un-awaited awaitable."""
    assert feed.message_announcer_coro is not None  # guarded by the caller
    return feed.message_announcer_coro(
        bot=_require_bot(),
        channel_id=feed.channel_id,
        check_enabled=False,
        construct_message_coro=feed.message_constructor_coro,
        publish_message=publish,
        cv2=feed.cv2,
    )


async def _run_send(feed: Feed, publish: bool) -> None:
    """Await the announcer, then release the feed's in-flight slot."""
    try:
        await _announce(feed, publish)
        logger.info("Manual send of %s finished", feed.name)
    except Exception:
        logger.exception("Manual send of %s failed", feed.name)
    finally:
        _sending.discard(feed.name)


async def _handle_send(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Publish the feed's post to its channel.

    Auth + Origin (CSRF) are already enforced by the middleware. Returns as soon as the
    post builds cleanly and the announcer is running — see the module docstring.
    """
    feed = _feed_or_404(request)
    # Falsy, not `is None`: `register_feed` normalises 0 to None, and this is the guard
    # standing between a dormant feed and a post announced into channel 0.
    if not feed.channel_id:
        return aiohttp.web.json_response(
            {"error": f"{_title(feed.name)} is dormant — no channel configured."},
            status=409,
        )
    if feed.message_announcer_coro is None:
        return aiohttp.web.json_response(
            {"error": "No announcer is configured for this feed."}, status=409
        )
    if feed.name in _sending:
        return aiohttp.web.json_response(
            {"error": "A send is already in flight for this feed."}, status=409
        )

    # Claim the slot here, before the first await — not after the build below. The
    # build takes seconds on the Bungie-backed feeds, and a check that far from its
    # claim is not a guard: two requests arriving inside that window both saw an empty
    # set, and both posted. Adding immediately after the `in` test is atomic, because
    # nothing between them yields to the loop.
    _sending.add(feed.name)

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    # A body that parses but isn't an object (`[1, 2]`) would make `.get` raise, and an
    # exception here would strand the slot we just claimed.
    publish = bool(payload.get("publish", True)) if isinstance(payload, dict) else True

    # Build once here so a constructor failure (the common one) is reported in the
    # response, before anything is posted. The announcer rebuilds — an extra API fetch
    # on a rare manual action, in exchange for not posting a placeholder into the live
    # channel only to have construction fail behind it.
    try:
        await _build(feed)
    except Exception as e:
        _sending.discard(feed.name)
        logger.exception("Manual send of %s aborted: build failed", feed.name)
        return aiohttp.web.json_response(
            {"error": f"Building the post failed, nothing was sent: {e}"}, status=502
        )

    # From here the task owns the slot and releases it in `_run_send`.
    task = asyncio.create_task(_run_send(feed, publish))
    _send_tasks.add(task)
    task.add_done_callback(_send_tasks.discard)
    logger.info("Manual send of %s started (publish=%s)", feed.name, publish)
    return aiohttp.web.json_response({"ok": True, "started": True})


def register_feed_action_routes(app: aiohttp.web.Application) -> None:
    """Add the per-feed action routes to the shared persistent app."""
    app.router.add_get("/feed/{name}/preview", _handle_preview)
    app.router.add_post("/feed/{name}/send", _handle_send)


web.register_routes(register_feed_action_routes)
