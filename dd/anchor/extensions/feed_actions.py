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

**Two endpoints and no page of its own.** The actions live on a feed's row — on
``/feeds``, and on the ``/send`` chooser (:mod:`dd.anchor.extensions.send_page`) — and
the rendered post appears in a modal there: a feed has no state a *per-feed* page could
show that its row does not, so such a page was only ever a click in the way. What is
deliberately not here is anything beyond the two actions — no toggle (it stays solely on
``/feeds``, so one row has one write path) and no status or health, which is the
deferred observability work in ``plans/anchor_web_ia.md`` §4.

Two pages carry them, so this module owns the **markup** of the pair as well as the
endpoints behind it: :func:`actions_html` renders the buttons and the sentence under a
dimmed one, and :func:`splice_modals` puts the shared dialogs
(``web_static/feed_modals.html``, driven by ``web_static/feed_actions.js``) into a
page's shell. The send confirmation is where the one irreversible click on the panel
is armed; a second copy of it on the second page would be a second set of words about
what sending means, and it would drift.

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
import html
import logging
import typing as t
from pathlib import Path

import aiohttp.web
import lightbulb as lb

from dd.hmessage import HMessage
from dd.hmessage.snapshot import cv2_payload

from ...common import (
    feeds as dd_feeds,
    settings as dd_settings,
)
from .. import web
from ..autopost import Feed, registered_feeds

logger = logging.getLogger(__name__)

# Nothing is registered on it — the routes are contributed to the web app at import time
# and the bot comes from web.require_bot() — but load_extensions_strict requires every
# extension module to expose a Loader, so it stays.
loader = lb.Loader()

# --- the shared markup ---------------------------------------------------------------

_MODALS_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "feed_modals.html"
)
#: What a page carrying the Preview/Send pair marks the spot with.
MODALS_PLACEHOLDER = "<!--__FEED_MODALS__-->"

#: What an unset channel says, wherever one is shown. A sentence, not a blank: an empty
#: control is indistinguishable from one that failed to load, and "no channel" is a real
#: state a feed can be in rather than an absence of information. Used by the feeds
#: page's picker (its pre-JS ``<option>`` and its placeholder, see
#: autopost_settings.js) and by the /send row's destination field — the same fact twice.
NO_CHANNEL_LABEL = "No channel set"

#: Where a feed's channel is actually set — the one page that can fix a row whose Send
#: is dimmed for want of one.
FEEDS_PATH = "/feeds"


def splice_modals(shell: str) -> str:
    """Put the preview/send dialogs into ``shell`` at :data:`MODALS_PLACEHOLDER`.

    Read per request like every other template here, so an edited partial shows up on a
    refresh rather than at the next restart.
    """
    return shell.replace(
        MODALS_PLACEHOLDER, _MODALS_PATH.read_text(encoding="utf-8"), 1
    )


def note_html(text: str, level: str, *, link: tuple[str, str] | None = None) -> str:
    """One explanatory line under a row: a bullet and a sentence.

    ``level`` is ``"warn"`` for a state the operator can fix and ``"err"`` for something
    broken. There is no third level; a note that is neither is not a note.

    ``link`` is an optional ``(href, sentence)`` closing the note, for when the fix is
    on a different page than the one being read. It is a whole sentence rather than a
    word mid-text because that is what survives being read as a link out of context.
    """
    tail = ""
    if link is not None:
        href, label = link
        tail = f' <a href="{html.escape(href)}">{html.escape(label)}</a>'
    return (
        f'<p class="note {level}">'
        '<span class="bullet" aria-hidden="true">•</span> '
        f"<span>{html.escape(text)}{tail}</span></p>"
    )


class FeedActions(t.NamedTuple):
    """The two halves of a feed's action markup, so each page can place them.

    They come off one call and never separately: the pair's whole rule is that an
    unavailable button is dimmed **and explained**, and a function that handed out only
    the buttons would let a page render a dimmed control with no sentence under it. What
    differs between the pages is only where the sentence goes — beside the row's other
    copy on /feeds, on its own line under the row on /send, where the buttons sit in a
    horizontal head that a paragraph has no business joining.
    """

    #: The Preview / Send now pair, for wherever the row puts its controls.
    buttons: str
    #: The reason one of them is dim, or "" when both are live.
    notes: str


def actions_html(
    slug: str, *, label: str, channel_set: bool, fix_channel_here: bool
) -> FeedActions:
    """The Preview / Send now pair for one feed, and the reason either is unavailable.

    The replacement for the old ``/<feed> show`` and ``send`` commands. They live on the
    feed's row rather than a per-feed page: a feed has no state such a page could show
    that the row does not already, so it would be a click in the way. The rendered post
    appears in the shared modals (see :func:`splice_modals`), not in the row.

    An unavailable action is **dimmed and explained, never removed**. A missing button
    reads as a feature this build does not have; a dim one under a sentence reads as a
    state, which is what it is — and both of these states are ones the operator can act
    on. Preview survives a missing channel deliberately: building the post needs no
    destination, and a feed you cannot send yet is exactly one worth looking at.

    ``label`` is the feed's display name, carried on the button rather than looked up in
    the DOM: the two pages that render these buttons draw the name into different
    markup, and the script driving them should not have to know both shapes.

    ``fix_channel_here`` says whether the page being rendered is the one where a channel
    is chosen. It decides only where the "no channel" note points — at the picker
    further down this page, or at :data:`FEEDS_PATH` — never whether the note appears.
    """
    live = slug in registered_feeds()

    def _button(action: str, text: str, title: str, *, enabled: bool) -> str:
        return (
            f'<button type="button" class="feedaction small" data-action="{action}"'
            f' data-slug="{html.escape(slug)}"'
            f"{'' if enabled else ' disabled'}"
            f' title="{title}"'
            f' data-label="{html.escape(label)}">{text}</button>'
        )

    notes = ""
    if not live:
        notes += note_html(
            "This feed's producer isn't loaded in this process, so there's nothing to "
            "build or send.",
            "err",
        )
    elif not channel_set:
        notes += note_html(
            "No channel set, so there's nowhere to send it"
            + (
                " — pick one below. Preview still works."
                if fix_channel_here
                else ". Preview still works."
            ),
            "warn",
            link=None
            if fix_channel_here
            else (FEEDS_PATH, "Pick one on the Feeds page."),
        )
    buttons = (
        '<div class="rowactions">'
        + _button(
            "preview",
            "Preview",
            "Builds the post exactly as the producer would right now, and shows it."
            " Nothing is sent. The data comes from the live API, so this can take a few"
            " seconds.",
            enabled=live,
        )
        + _button(
            "send",
            "Send now",
            "Posts to this feed&#39;s channel immediately, and (unless you say"
            " otherwise) pushes it out to every server that follows the feed.",
            enabled=live and channel_set,
        )
        + "</div>"
    )
    return FeedActions(buttons, notes)


# Feeds with a send in flight, so a double-click or a second tab can't fire two posts.
# `send_message`'s own dedupe only guards retry-races within one send — it does not stop
# two deliberate sends.
_sending: set[str] = set()
# Strong refs to the in-flight send tasks, so they are not garbage collected mid-flight.
_send_tasks: set[asyncio.Task] = set()


def _feed_or_404(request: aiohttp.web.Request) -> Feed:
    """The registered feed named in the path, or a 404."""
    feed = registered_feeds().get(request.match_info.get("name", ""))
    if feed is None:
        raise aiohttp.web.HTTPNotFound(text="No such feed.")
    return feed


async def _build(feed: Feed) -> HMessage:
    """Build the feed's post once, as the producer would right now.

    ``BotNotReady`` is a plain exception, so both call sites' ``except Exception`` catch
    it and report its sentence to the page — which is the point of it not being an
    ``HTTPServiceUnavailable``, whose stringification ("Service Unavailable") drops the
    part telling the operator what to do.
    """
    return await feed.message_constructor_coro(bot=web.require_bot())


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


def _announce(feed: Feed, channel_id: int, publish: bool) -> t.Awaitable[t.Any]:
    """The announcer call for this feed, as an un-awaited awaitable."""
    assert feed.message_announcer_coro is not None  # guarded by the caller
    return feed.message_announcer_coro(
        bot=web.require_bot(),
        channel_id=channel_id,
        check_enabled=False,
        construct_message_coro=feed.message_constructor_coro,
        publish_message=publish,
        cv2=feed.cv2,
    )


async def _run_send(feed: Feed, channel_id: int, publish: bool) -> None:
    """Await the announcer, then release the feed's in-flight slot."""
    try:
        await _announce(feed, channel_id, publish)
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
    # Resolved here, not held from boot: a channel set or changed on the Autopost
    # Settings page takes effect on the operator's very next click. A cached read (30s
    # TTL), and this handler is human-driven, so it costs nothing worth counting.
    #
    # Falsy, not `is None`: an unset followable reads as the integer 0, and this is the
    # guard standing between a dormant feed and a post announced into channel 0 — which
    # is what used to happen when the check was `is None`.
    channel_id = await dd_settings.get_followable_channel(feed.name)
    if not channel_id:
        return aiohttp.web.json_response(
            {
                # "Dormant" is the settings page's word for this state, not something
                # an admin would say; and the sentence has to point at the fix, since
                # this is the only place the problem surfaces.
                "error": f"{dd_feeds.FEEDS[feed.name].display_name} has no channel to"
                " post to yet — pick one on the Feeds page."
            },
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
    task = asyncio.create_task(_run_send(feed, channel_id, publish))
    _send_tasks.add(task)
    task.add_done_callback(_send_tasks.discard)
    logger.info("Manual send of %s started (publish=%s)", feed.name, publish)
    return aiohttp.web.json_response({"ok": True, "started": True})


def register_feed_action_routes(app: aiohttp.web.Application) -> None:
    """Add the per-feed action routes to the shared persistent app."""
    app.router.add_get("/feed/{name}/preview", _handle_preview)
    app.router.add_post("/feed/{name}/send", _handle_send)


web.register_routes(register_feed_action_routes)
