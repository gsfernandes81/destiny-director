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

"""``GET /send`` — the chooser over the six scheduled feeds.

The page owns almost no behaviour: its buttons are ``feed_actions``' and its dialogs are
the shared ones. What it does own is a set of **row states**, and those are the whole
design — each says something different about what an operator may do next, and each
fails silently if it regresses:

1. **Which feeds are listed at all.** Six, from the catalog. The other six have no
   schedule to pre-empt, so a row for them could only carry buttons that never work.
2. **Dimmed-with-a-reason, never removed.** A feed with no channel keeps a live Preview
   and a dim Send under a sentence pointing at the page that fixes it. A vanished button
   reads as a missing feature; a dim one reads as a state.
3. **Switched off is not switched broken.** A feed whose schedule is off renders like
   the others with a quiet line — being off is exactly when a manual push is wanted —
   and Send stays live. This is the one row state that must NOT be a note.
4. **Nothing is built on load.** The row states are all cheap facts; a page that reached
   for a producer would spend six live Bungie round trips per refresh, and would take an
   Iron-Banner-shaped exception on a page that has nothing to do with Iron Banner.

Exercised with a fake request (no live server); auth is the web_auth middleware, covered
in test_web_auth.py.
"""

import asyncio
import importlib
import typing as t
from unittest.mock import MagicMock

import aiohttp.web
import pytest
from sqlalchemy import delete

from dd.anchor import autopost, web
from dd.anchor.extensions import send_page as page
from dd.common import (
    feeds as dd_feeds,
    schemas,
    settings,
)
from dd.hmessage import HMessage

pytestmark = pytest.mark.asyncio

# A card registers when its module imports, so the "Send a post" group holds only what
# a session happened to load. Import the siblings this card is ordered against
# explicitly — otherwise running this file alone compares against an empty group, which
# is the shape of assertion that passes forever while testing nothing.
for _sibling in ("weekly_reset", "trials", "cv2_builder_page"):
    importlib.import_module(f"dd.anchor.extensions.{_sibling}")

CHANNEL = 900_001


@pytest.fixture(autouse=True)
def _clean_settings() -> t.Iterator[None]:
    """Start each test from an empty auto_post_settings table (session-scoped DB)."""

    async def _clear() -> None:
        async with schemas.db_session() as session, session.begin():
            await session.execute(delete(schemas.AutoPostSettings))

    asyncio.run(_clear())
    settings.reset_cache_for_tests()
    yield


async def _never_built(**_kwargs: object) -> HMessage:
    """Stand-in constructor. Rendering a row must never reach for one — see (4)."""
    raise AssertionError("the send page must not build a post to render a row")


@pytest.fixture
def _registered_feeds(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[None]:
    """Register two of the six, so both the live and the not-loaded row states appear.

    The real registry is filled by the producer modules at import time; a test relying
    on some other test having imported them would pass or fail by ordering.
    """
    monkeypatch.setattr(
        autopost,
        "_feeds",
        {
            slug: autopost.Feed(name=slug, message_constructor_coro=_never_built)
            for slug in ("lost_sector", "portal_ops")
        },
    )
    yield


@pytest.fixture
def _no_bot(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[None]:
    """No live bot, so a channel id resolves to its fallback label, not a name."""
    monkeypatch.setattr(web, "_bot", None)
    yield


@pytest.fixture
def _bot_naming_channels(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[None]:
    """A bot that names one channel and cannot see any other."""

    class _Channel:
        name = "lost-sector"

    class _FakeBot:
        async def fetch_channel(self, channel_id: int) -> object:
            if channel_id != CHANNEL:
                raise LookupError("no such channel")
            return _Channel()

    monkeypatch.setattr(web, "_bot", _FakeBot())
    yield


def _req() -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, MagicMock(spec=aiohttp.web.Request))


def _row(body: str, slug: str) -> str:
    """Just the one feed's card, so a row assertion cannot be satisfied by a sibling."""
    start = body.index(f'data-action="preview" data-slug="{slug}"')
    start = body.rindex('<section class="feed">', 0, start)
    return body[start : body.index("</section>", start)]


# --- which feeds get a row ----------------------------------------------------------


@pytest.mark.integration
async def test_the_page_lists_every_scheduled_feed_in_catalog_order() -> None:
    body = await page._render_send_html()

    scheduled = [f for f in dd_feeds.FOLLOWABLES if f.has_toggle]
    assert len(scheduled) == 6, "the catalog's scheduled set moved; check this page"
    positions = [body.index(f'data-slug="{f.slug}"') for f in scheduled]
    assert positions == sorted(positions), "rows must follow the catalog's own order"


@pytest.mark.integration
async def test_feeds_with_no_schedule_get_no_row() -> None:
    """Trials and Weekly Reset are published from their own forms, and the last four are
    written by somebody else entirely. A row here could only offer buttons that never
    work."""
    body = await page._render_send_html()

    for feed in dd_feeds.FOLLOWABLES:
        if feed.has_toggle:
            continue
        assert f'data-action="send" data-slug="{feed.slug}"' not in body, feed.slug


# --- the row states -----------------------------------------------------------------


@pytest.mark.integration
async def test_a_feed_with_no_channel_dims_send_and_keeps_preview(
    _registered_feeds: None, _no_bot: None
) -> None:
    """Portal Ops ships with no channel. Building the post costs nothing and answers
    "what would it even say", so Preview stays live; only Send has nowhere to go."""
    body = await page._render_send_html()
    row = _row(body, "portal_ops")

    assert 'data-action="preview" data-slug="portal_ops" title=' in row
    assert 'data-action="send" data-slug="portal_ops" disabled' in row
    # The reason, and the page that fixes it — not a control that quietly vanished.
    assert 'class="note warn"' in row
    assert "No channel set" in row
    assert 'href="/feeds"' in row


@pytest.mark.integration
async def test_a_feed_that_is_switched_off_still_sends(
    _registered_feeds: None, _no_bot: None
) -> None:
    """Being switched off is exactly when a manual push is wanted, so Send stays armed
    and the row says why it is quiet — as a plain line, not as a warning to fix."""
    await schemas.AutoPostSettings.set_enabled("lost_sector", False)
    await schemas.AutoPostSettings.set_value("lost_sector_channel", str(CHANNEL))

    row = _row(await page._render_send_html(), "lost_sector")

    assert 'data-action="send" data-slug="lost_sector" title=' in row
    assert "disabled" not in row
    assert 'class="offnote"' in row
    assert 'class="note' not in row


@pytest.mark.integration
async def test_a_feed_whose_producer_is_not_loaded_is_dimmed_and_explained(
    _registered_feeds: None, _no_bot: None
) -> None:
    """Only two feeds are registered here, so Xûr stands in for a build whose producer
    did not load in this process — both actions dim, and the note is an error rather
    than a state the operator can fix."""
    await schemas.AutoPostSettings.set_value("xur_channel", str(CHANNEL))

    row = _row(await page._render_send_html(), "xur")

    assert 'data-action="preview" data-slug="xur" disabled' in row
    assert 'data-action="send" data-slug="xur" disabled' in row
    assert 'class="note err"' in row


@pytest.mark.integration
async def test_rendering_a_row_never_builds_a_post(_registered_feeds: None) -> None:
    """The registered constructors raise on sight; reaching one is the failure."""
    await schemas.AutoPostSettings.set_value("lost_sector_channel", str(CHANNEL))

    assert await page._render_send_html()


# --- the destination ----------------------------------------------------------------


@pytest.mark.integration
async def test_the_destination_is_shown_by_name(
    _registered_feeds: None, _bot_naming_channels: None
) -> None:
    await schemas.AutoPostSettings.set_value("lost_sector_channel", str(CHANNEL))

    row = _row(await page._render_send_html(), "lost_sector")

    assert "#lost-sector" in row


@pytest.mark.integration
async def test_an_unresolvable_channel_falls_back_to_its_id(
    _registered_feeds: None, _bot_naming_channels: None
) -> None:
    """A channel the bot cannot see must not cost the page its row — and the id is still
    worth showing, since it is what an operator would go looking for."""
    await schemas.AutoPostSettings.set_value("xur_channel", "424242")

    row = _row(await page._render_send_html(), "xur")

    assert "424242" in row
    assert 'data-action="send" data-slug="xur"' in row


@pytest.mark.integration
async def test_a_channel_that_cannot_be_named_yet_still_arms_send(
    _registered_feeds: None, _no_bot: None
) -> None:
    """Whether a feed HAS a channel and whether this process can put a name to it are
    two different questions. Conflating them dimmed Send on every row for the first
    seconds after a deploy, under a sentence blaming a channel that was in fact set."""
    await schemas.AutoPostSettings.set_value("lost_sector_channel", str(CHANNEL))

    row = _row(await page._render_send_html(), "lost_sector")

    assert str(CHANNEL) in row
    assert "disabled" not in row
    assert 'class="note' not in row


@pytest.mark.integration
async def test_a_cleared_channel_reads_as_no_channel(
    _registered_feeds: None, _bot_naming_channels: None
) -> None:
    """ "0" is an explicit clear and a missing row was never set; both mean the same
    thing to this page, and neither may be shown as a channel id."""
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "0")

    row = _row(await page._render_send_html(), "lost_sector")

    assert 'class="field empty"' in row
    assert 'data-action="send" data-slug="lost_sector" disabled' in row


# --- the page itself ----------------------------------------------------------------


async def test_the_route_is_registered() -> None:
    app = aiohttp.web.Application()
    page.register_send_page_routes(app)

    paths = {
        r.resource.canonical for r in app.router.routes() if r.resource is not None
    }
    assert "/send" in paths


@pytest.mark.integration
async def test_the_page_serves_its_shell_with_the_shared_modals() -> None:
    resp = await page._handle_send_page(_req())

    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.text is not None
    assert "Send a post now" in resp.text
    # The dialogs are spliced in from feed_modals.html, not copied into this shell.
    assert page.feed_actions.MODALS_PLACEHOLDER not in resp.text
    assert '<dialog class="feedmodal" id="previewDialog">' in resp.text
    assert '<dialog class="feedmodal" id="sendDialog">' in resp.text
    assert resp.text.count('class="modalpreview cv2-preview"') == 2


@pytest.mark.integration
async def test_the_only_filled_button_is_the_one_in_the_send_dialog(
    _registered_feeds: None, _no_bot: None
) -> None:
    """Six filled buttons is a page shouting at itself. The primary belongs where the
    single irreversible click is, which is inside the confirmation."""
    body = await page._render_send_html()

    assert body.count('class="primary"') == 1
    assert body.index('class="primary"') > body.index('id="sendDialog"')


async def test_the_shell_loads_the_assets_that_drive_it() -> None:
    shell = page._SEND_HTML_PATH.read_text(encoding="utf-8")
    static = page._SEND_HTML_PATH.parent

    for asset in (
        "/static/shared.js",
        "/static/cv2_model.js",
        "/static/cv2_render.js",
        "/static/feed_actions.js",
        "/static/feed_actions.css",
        "/static/cv2_preview.css",
    ):
        assert asset in shell, asset
        assert (static / asset.removeprefix("/static/")).exists(), asset


# --- the landing row ----------------------------------------------------------------


async def test_the_card_points_at_the_page_and_sits_between_its_siblings() -> None:
    card = next(c for c in web.registered_cards() if c.href == "/send")

    assert card.title == "Send a scheduled post now"
    assert card.group is web.CardGroup.SEND
    assert not card.danger
    # The design's own verb for this row: what is on the other side is a choice.
    assert card.action == "Choose"
    # Not tinted. The tint marks the rows that ARE the errand — the two posts somebody
    # sits down and writes; this one opens a chooser.
    assert not card.featured

    by_href = {c.href: c for c in web.registered_cards()}
    assert by_href["/trials"].order < card.order < by_href["/custom-post"].order


async def test_the_card_names_the_feeds_the_page_actually_offers() -> None:
    """The row promises a list; the page has to be that list. Both come off the catalog,
    so the only way they can disagree is if one of them stops doing so."""
    card = next(c for c in web.registered_cards() if c.href == "/send")

    for feed in dd_feeds.FOLLOWABLES:
        assert (feed.display_name in card.description) is feed.has_toggle, feed.slug
