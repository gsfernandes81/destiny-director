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

"""``GET /send`` — send one of the scheduled posts right now.

**A chooser, not a new capability.** Every row's two buttons are the pair
:mod:`dd.anchor.extensions.feed_actions` already renders on ``/feeds``, driving the same
``GET /feed/{name}/preview`` and ``POST /feed/{name}/send`` through the same two
dialogs. This module contributes no endpoint of its own: it decides which feeds are
listed, what each row says, and nothing else. The page exists because the landing row
promised somewhere to go, and a row labelled "Send a scheduled post now" that opens a
page of twelve toggles and eight colour pickers is a row that lied.

**Six rows, and no more.** The catalog's six
:attr:`~dd.common.feeds.FeedKind.ANCHOR_CRON` feeds, in catalog order. The other six
are not sendable from here by construction — two are written on a form (their own pages
publish them) and four are written by someone else entirely — so listing them would be
listing rows whose buttons could only be dimmed forever.

**No primary button on the page.** Six filled buttons is a page shouting at itself. The
one filled button in this flow is *Send now* inside the confirmation dialog, which is
where the single irreversible click actually is.

**Nothing is built on load.** A row says what it knows for free: the feed's name, its
description, where it would go, and whether it is switched on. Whether the post can be
*built* right now is not knowable without building it — Iron Banner between events
raises — and answering that for six feeds up front is six live Bungie round trips
charged to every page load, for a question the preview modal answers on demand for the
one feed the operator cares about.

Authentication is the shared Discord-OAuth middleware in ``web_auth`` (it protects every
non-allowlisted route), so there is no auth code here.
"""

import asyncio
import html
import logging
from pathlib import Path

import aiohttp.web
import lightbulb as lb

from ...common import (
    feeds as dd_feeds,
    schemas,
)
from .. import web
from . import feed_actions

logger = logging.getLogger(__name__)

# No commands or listeners live here, but load_extensions_strict → load_extensions
# requires every extension module to expose a Loader, so define one.
loader = lb.Loader()

_SEND_HTML_PATH = Path(__file__).resolve().parent.parent / "web_static" / "send.html"
_FEEDS_PLACEHOLDER = "<!--__FEEDS__-->"


def _scheduled_feeds() -> tuple[dd_feeds.Followable, ...]:
    """The feeds this page offers: the ones anchor produces on a schedule.

    Derived from the catalog rather than from ``registered_feeds()``, so the list is the
    same six on every deploy. A feed whose producer did not load in this process still
    gets a row, dimmed and explained by
    :func:`~dd.anchor.extensions.feed_actions.actions_html` — an absent row would read
    as a feed that does not exist, which is a different and wrong thing to say.
    """
    return tuple(feed for feed in dd_feeds.FOLLOWABLES if feed.has_toggle)


#: The landing row's description: the six feeds this page offers, by name. Built from
#: the catalog rather than typed out, so the row cannot promise a feed the page does
#: not list (or fall silent about one it does).
_CARD_DESCRIPTION = ", ".join(feed.display_name for feed in _scheduled_feeds())


async def _channel_labels(channel_ids: dict[str, int]) -> dict[str, str]:
    """``{slug: "#channel-name"}`` — one entry for **every** configured channel.

    A raw snowflake tells the reader nothing, and the destination is the one fact on
    this page an operator checks before pressing anything. Resolution is per-channel and
    best-effort — the bot may still be starting, may not be in the guild, or the channel
    may have been deleted — and a failure falls back to naming the id, in the same words
    the feeds page's picker uses for an id it cannot resolve.

    Total over its input, and that is the point: whether a feed HAS a channel is what
    Send's availability turns on, and it must not be confused with whether this process
    can currently put a name to it. Dropping the unresolvable ones instead would dim
    Send on every row for the first seconds after a deploy, under a sentence blaming a
    missing channel that is in fact set.

    Reads through :meth:`~dd.common.bot.CachedFetchBot.fetch_channel`, so a warm cache
    makes this free; the fetches that do go out run together rather than one after
    another.
    """
    bot = web.get_bot()

    async def _name(channel_id: int) -> str | None:
        if bot is None:
            return None
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            logger.info("Could not resolve channel %s for /send", channel_id)
            return None
        return getattr(channel, "name", None)

    slugs = list(channel_ids)
    names = await asyncio.gather(*(_name(channel_ids[slug]) for slug in slugs))
    return {
        slug: f"#{name}" if name else f"Unknown channel ({channel_ids[slug]})"
        for slug, name in zip(slugs, names, strict=True)
    }


def _render_row(
    feed: dd_feeds.Followable, *, enabled: bool, channel_label: str | None
) -> str:
    """One feed's row: what it is, where it goes, and the two things you can do with it.

    A feed that is switched off renders exactly like the others, with a quiet line
    saying so — being switched off is precisely when a manual push is wanted, and it is
    the one state of a row here that is not a reason to dim anything. That line is
    deliberately not a :func:`~dd.anchor.extensions.feed_actions.note_html`: notes are a
    state to fix (``warn``) or something broken (``err``), and this is neither.

    The row is a head and then its explanations. Everything on the head sits on one line
    — name, destination, the two buttons — and every sentence about the row goes under
    it at full width, so a two-line reason cannot push the buttons around.
    """
    field = (
        f'<span class="field">{html.escape(channel_label)}</span>'
        if channel_label
        else f'<span class="field empty">{feed_actions.NO_CHANNEL_LABEL}</span>'
    )
    off = (
        ""
        if enabled
        else '<p class="offnote">Switched off, so it isn\'t posting on its own '
        "schedule — sending it by hand still works.</p>"
    )
    actions = feed_actions.actions_html(
        feed.slug,
        label=feed.display_name,
        channel_set=channel_label is not None,
        # The channel is set on /feeds, not here.
        fix_channel_here=False,
    )
    return (
        '<section class="feed">'
        '<div class="head">'
        f'<span class="name">{html.escape(feed.display_name)}'
        f'<span class="desc">{html.escape(feed.desc)}</span></span>'
        f"{field}{actions.buttons}"
        "</div>"
        f"{actions.notes}{off}"
        "</section>"
    )


async def _render_send_html() -> str:
    """Render ``/send``: one row per scheduled feed, plus the shared dialogs."""
    # One bulk read for the whole page, as /feeds does — both facts a row shows (its
    # switch and its channel) are rows of the same table.
    rows = await schemas.AutoPostSettings.get_all_rows()
    feeds = _scheduled_feeds()
    # "0" is an explicit clear and None a slug never written; both mean the feed has
    # nowhere to post, which is what Send's availability turns on.
    channel_ids = {
        feed.slug: int(value)
        for feed in feeds
        if (value := rows.get(feed.channel_key, (None, None))[1]) not in (None, "", "0")
    }
    labels = await _channel_labels(channel_ids)
    body = "".join(
        _render_row(
            feed,
            enabled=bool(rows.get(feed.slug, (None, None))[0]),
            channel_label=labels.get(feed.slug),
        )
        for feed in feeds
    )
    shell = _SEND_HTML_PATH.read_text(encoding="utf-8").replace(
        _FEEDS_PLACEHOLDER, body
    )
    return feed_actions.splice_modals(shell)


async def _handle_send_page(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=await _render_send_html(), content_type="text/html"
    )


def register_send_page_routes(app: aiohttp.web.Application) -> None:
    """Add the chooser to the shared persistent app. The actions it fires are
    ``feed_actions``' two routes, registered by that module."""
    app.router.add_get("/send", _handle_send_page)


web.register_routes(register_send_page_routes)
web.register_card(
    web.Card(
        "Send a scheduled post now",
        _CARD_DESCRIPTION,
        "/send",
        web.CardGroup.SEND,
        # After Weekly Reset (10) and Trials (20), before Custom one-off post (40): the
        # group runs most-frequent errand first, and pushing a scheduled post early
        # comes up less often than writing either of the two weekly posts.
        30,
        # The reviewed design's own verb for this row. Not "Open": what is on the other
        # side is a choice among six, and group 1's four rows are deliberately four
        # different verbs.
        action="Choose",
        # Deliberately NOT featured. The tint marks the rows that ARE the errand — the
        # two posts somebody sits down and writes. This one opens a chooser.
    )
)
