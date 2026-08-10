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

# Control panel: the card registry (web.register_card / registered_cards) and the panel
# route handler, exercised with a lightweight fake request (no live server).
# Authentication is handled centrally by the web_auth middleware (covered in
# test_web_auth.py), so the handler assumes an already-authenticated request.

import datetime as dt
import json
import typing as t

import aiohttp.web
import pytest

from dd.anchor import web
from dd.anchor.extensions import control_panel
from dd.common import iron_banner

pytestmark = pytest.mark.asyncio


def _text(resp: aiohttp.web.Response) -> str:
    """Response.text is typed ``str | None``; every handler here sets it."""
    assert resp.text is not None
    return resp.text


def _as_request(req: "_FakeRequest") -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, req)


@pytest.fixture
def clean_cards() -> t.Iterator[None]:
    """Isolate the module-level card registry so tests don't leak into each other."""
    saved = list(web._cards)
    web._cards.clear()
    try:
        yield
    finally:
        web._cards[:] = saved


@pytest.fixture(autouse=True)
def quiet_iron_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live Iron Banner week, and no database, for every test that isn't about it.

    Rendering the panel now asks the rotation store whether Iron Banner is on. That is
    one PK SELECT in production and an irrelevant dependency in a test about card
    grouping, so it answers "no" by default; the two tests that care patch it again.
    """

    async def _empty() -> iron_banner.IronBannerRotation:
        return iron_banner.IronBannerRotation([])

    monkeypatch.setattr(control_panel.iron_banner, "load_rotation", _empty)


def _live_iron_banner_week(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put an Iron Banner event around *now*."""
    now = int(dt.datetime.now(tz=dt.UTC).timestamp())

    async def _rotation() -> iron_banner.IronBannerRotation:
        return iron_banner.IronBannerRotation(
            [iron_banner.Event(now - 3600, now + 3600, "Pool", ["Control"], [])]
        )

    monkeypatch.setattr(control_panel.iron_banner, "load_rotation", _rotation)


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in — the panel handler reads nothing off it."""


# --- grouping ------------------------------------------------------------------------
#
# The panel used to sort every card alphabetically by title, which put the two most
# frequent errands last. Cards now carry the errand they belong to, and the group order
# is the enum's declaration order rather than anything about the words.


async def test_groups_render_in_declaration_order_not_alphabetically(
    clean_cards: None,
) -> None:
    for group in reversed(list(web.CardGroup)):
        web.register_card(web.Card(f"card for {group.name}", "", "/x", group))

    assert [group for group, _ in web.grouped_cards()] == list(web.CardGroup)


async def test_within_a_group_order_wins_over_title(clean_cards: None) -> None:
    # Weekly Reset before Trials because it is the more frequent errand — alphabetical
    # would put Trials first, which is exactly the ranking being replaced.
    web.register_card(
        web.Card("Trials of Osiris", "", "/trials", web.CardGroup.SEND, 20)
    )
    web.register_card(
        web.Card("Weekly Reset", "", "/weekly_reset", web.CardGroup.SEND, 10)
    )

    ((_, cards),) = web.grouped_cards()

    assert [c.title for c in cards] == ["Weekly Reset", "Trials of Osiris"]


async def test_equal_order_falls_back_to_title(clean_cards: None) -> None:
    web.register_card(web.Card("Beta", "", "/b", web.CardGroup.DATA, 10))
    web.register_card(web.Card("Alpha", "", "/a", web.CardGroup.DATA, 10))

    ((_, cards),) = web.grouped_cards()

    assert [c.title for c in cards] == ["Alpha", "Beta"]


async def test_an_empty_group_is_omitted_not_rendered_headless(
    clean_cards: None,
) -> None:
    # A heading over nothing reads as something broken.
    web.register_card(web.Card("Feeds", "", "/feeds", web.CardGroup.ADMIN))

    assert [group for group, _ in web.grouped_cards()] == [web.CardGroup.ADMIN]


async def test_a_card_defaults_to_admin_so_an_unconverted_module_still_appears(
    clean_cards: None,
) -> None:
    # The three positional fields are the whole of the old signature; a module that has
    # not been converted yet must not vanish from the panel.
    web.register_card(web.Card("Legacy", "desc", "/legacy"))

    ((group, cards),) = web.grouped_cards()

    assert group is web.CardGroup.ADMIN
    assert cards[0].danger is False


async def test_register_card_appends_and_registered_cards_returns_copy(
    clean_cards: None,
) -> None:
    card = web.Card("Alpha", "first tool", "/alpha")
    web.register_card(card)

    cards = web.registered_cards()
    assert cards == [card]
    # registered_cards returns a copy — mutating it must not touch the registry.
    cards.append(web.Card("Beta", "x", "/beta"))
    assert web.registered_cards() == [card]


async def test_render_lists_cards_href_and_title_in_order(clean_cards: None) -> None:
    # Register out of display order; the panel renders them by (order, title).
    web.register_card(
        web.Card(
            "Trials of Osiris", "the weekend post", "/trials", web.CardGroup.SEND, 20
        )
    )
    web.register_card(
        web.Card(
            "Weekly Reset", "the reset post", "/weekly_reset", web.CardGroup.SEND, 10
        )
    )

    html_out = await control_panel._render_panel_html()

    assert 'href="/trials"' in html_out
    assert 'href="/weekly_reset"' in html_out
    assert html_out.index("Weekly Reset") < html_out.index("Trials of Osiris")


async def test_render_emits_every_group_heading_in_enum_order(
    clean_cards: None,
) -> None:
    # The whole point of the rewrite: the reader's errand, not the alphabet, decides
    # what comes first. Register backwards to prove the order is not contribution order.
    for group in reversed(list(web.CardGroup)):
        web.register_card(web.Card(f"{group.name} page", "", f"/{group.name}", group))

    html_out = await control_panel._render_panel_html()

    positions = [html_out.index(group.value) for group in web.CardGroup]
    assert positions == sorted(positions)


async def test_a_danger_card_is_the_only_one_rendered_red(clean_cards: None) -> None:
    web.register_card(web.Card("Quiet", "", "/quiet", web.CardGroup.ADMIN, 10))
    web.register_card(
        web.Card("Loud", "", "/loud", web.CardGroup.ADMIN, 20, danger=True)
    )

    html_out = await control_panel._render_panel_html()

    assert '<a class="row danger" href="/loud">' in html_out
    assert '<a class="row" href="/quiet">' in html_out


async def test_iron_banner_week_de_emphasises_the_trials_row(
    clean_cards: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # De-emphasis, not a gate: the row keeps its href, because "Trials isn't on" is a
    # fact about the weekend rather than a reason to lock the form.
    _live_iron_banner_week(monkeypatch)
    web.register_card(
        web.Card("Trials of Osiris", "the weekend post", "/trials", web.CardGroup.SEND)
    )

    html_out = await control_panel._render_panel_html()

    assert '<a class="row quiet" href="/trials">' in html_out
    assert control_panel._IRON_BANNER_NOTE in html_out
    assert "the weekend post" not in html_out


async def test_a_failed_iron_banner_lookup_renders_trials_normally(
    clean_cards: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # load_rotation raises when the store is unreadable and nothing has loaded in this
    # process yet — a cold start during a DB blip, i.e. exactly when someone is opening
    # the panel to find out what is wrong. Rotation data must not take down the front
    # door.
    async def _boom() -> iron_banner.IronBannerRotation:
        raise RuntimeError("no rotation data and no cache")

    monkeypatch.setattr(control_panel.iron_banner, "load_rotation", _boom)
    web.register_card(
        web.Card("Trials of Osiris", "the weekend post", "/trials", web.CardGroup.SEND)
    )

    html_out = await control_panel._render_panel_html()

    assert '<a class="row" href="/trials">' in html_out
    assert "the weekend post" in html_out
    assert control_panel._IRON_BANNER_NOTE not in html_out


async def test_sign_out_posts_a_form_rather_than_following_a_link(
    clean_cards: None,
) -> None:
    # web_auth made /auth/logout POST-only and origin-checked because a GET logout is
    # triggerable cross-site by an <img> or a prefetch. A styled anchor here would
    # reopen exactly that hole.
    html_out = await control_panel._render_panel_html()

    assert '<form method="post" action="/auth/logout">' in html_out
    assert 'href="/auth/logout"' not in html_out


async def test_render_escapes_html_in_card_fields(clean_cards: None) -> None:
    web.register_card(web.Card("A & <b>", "desc <script>", "/x?a=1&b=2"))

    html_out = await control_panel._render_panel_html()

    # The rows must not contain the raw, unescaped markup we fed in.
    assert "<b>" not in html_out
    assert "<script>" not in html_out
    assert "A &amp; &lt;b&gt;" in html_out
    assert "desc &lt;script&gt;" in html_out
    assert "/x?a=1&amp;b=2" in html_out


async def test_render_empty_registry_still_renders_the_panel_s_own_rows(
    clean_cards: None,
) -> None:
    # Configured channels, Sign out and Shut down are actions on this page rather than
    # links to another one, so they survive an empty card registry — and the last group
    # is therefore never empty.
    html_out = await control_panel._render_panel_html()

    assert 'id="infoBtn"' in html_out
    assert 'id="stopBtn"' in html_out
    assert web.CardGroup.ADMIN.value in html_out
    assert "<!--__GROUPS__-->" not in html_out


async def test_handle_panel_returns_html_response(clean_cards: None) -> None:
    web.register_card(web.Card("Rotation data", "edit rotations", "/rotation"))

    resp = await control_panel._handle_panel(
        t.cast(aiohttp.web.Request, _FakeRequest())
    )

    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.text is not None
    assert 'href="/rotation"' in resp.text


# --- bot administration --------------------------------------------------------------
#
# The web replacement for /anchor info and /anchor stop. On the panel itself rather than
# a page of its own: two actions and a read-only dump, on the bot's front door.


async def test_bot_info_reports_configured_channels() -> None:
    resp = await control_panel._handle_bot_info(_as_request(_FakeRequest()))
    payload = json.loads(_text(resp))

    assert payload["bot"] == "anchor"
    # Snowflakes exceed JS's safe-integer range, so ids travel as strings.
    assert isinstance(payload["controlServerId"], str)
    assert isinstance(payload["testEnv"], list)
    # By display name, not slug: the dialog says "Lost Sector", never `lost_sector`.
    feeds = {c["feed"] for c in payload["channels"]}
    assert "Lost Sector" in feeds
    assert "lost_sector" not in feeds


async def test_bot_info_resolves_channel_names(
    monkeypatch: pytest.MonkeyPatch, configured_followables: dict[str, int]
) -> None:
    # A raw snowflake tells the reader nothing; the point of the panel is the name.
    class _Channel:
        name = "lost-sector"

    class _Bot:
        async def fetch_channel(self, _channel_id: int) -> _Channel:
            return _Channel()

    monkeypatch.setattr(web, "_bot", _Bot())
    payload = json.loads(
        _text(await control_panel._handle_bot_info(_as_request(_FakeRequest())))
    )
    named = [c for c in payload["channels"] if c["channelName"]]
    assert named and all(c["channelName"] == "#lost-sector" for c in named)


async def test_bot_info_survives_an_unresolvable_channel(
    monkeypatch: pytest.MonkeyPatch, configured_followables: dict[str, int]
) -> None:
    # Not in the guild, channel deleted, bot still starting — none of which should cost
    # the panel its whole config dump.
    class _Bot:
        async def fetch_channel(self, _channel_id: int) -> object:
            raise RuntimeError("not found")

    monkeypatch.setattr(web, "_bot", _Bot())
    payload = json.loads(
        _text(await control_panel._handle_bot_info(_as_request(_FakeRequest())))
    )
    assert payload["channels"]
    assert all(c["channelName"] is None for c in payload["channels"])
    # The id is still there, so the row degrades to a snowflake rather than vanishing.
    assert any(c["channelId"] for c in payload["channels"])


async def test_bot_stop_before_the_bot_is_up_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Routes are live from web.start(); the bot is stashed immediately before that, so a
    # request in that window must not shut anything down. The handler only says "I need
    # the bot" — web's middleware is what turns that into the one shared 503 (covered in
    # test_web_bot.py), so the handler carries no status or wording of its own.
    monkeypatch.setattr(web, "_bot", None)
    with pytest.raises(web.BotNotReady):
        await control_panel._handle_bot_stop(_as_request(_FakeRequest()))


async def test_bot_stop_schedules_a_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exit 0, so Railway's ON_FAILURE policy leaves it down — a non-zero exit would be
    # read as a crash and restarted. And *scheduled*, not awaited: the panel is served
    # by the process being stopped, so the response has to be written first.
    seen: dict[str, object] = {}

    async def _request_shutdown(bot: object, exit_code: int) -> None:
        seen["bot"] = bot
        seen["exit_code"] = exit_code

    sentinel = object()
    monkeypatch.setattr(web, "_bot", sentinel)
    monkeypatch.setattr(control_panel.lifecycle, "request_shutdown", _request_shutdown)

    resp = await control_panel._handle_bot_stop(_as_request(_FakeRequest()))
    assert json.loads(_text(resp)) == {"ok": True, "stopping": True}
    assert seen["bot"] is sentinel
    assert seen["exit_code"] == control_panel.lifecycle.STOP_EXIT_CODE


async def test_panel_hosts_the_bot_actions_and_modals(clean_cards: None) -> None:
    body = _text(await control_panel._handle_panel(_as_request(_FakeRequest())))

    assert 'id="infoBtn"' in body
    # The destructive row is the only red thing on the page.
    assert '<button type="button" class="row danger" id="stopBtn">' in body
    # `danger` on the dialog is what makes the shutdown modal read as a warning rather
    # than another form — the copy below is only half of it. `consequence` is the motion
    # half: it opens at --dur-slow, so arriving at it takes a beat.
    assert '<dialog class="panelmodal danger consequence" id="stopDialog">' in body
    # Shutting down takes the panel with it; the dialog must say so.
    assert "panel runs inside the bot" in body
    assert "/static/control_panel.js" in body
    assert "<script>" not in body  # CSP is script-src 'self'


async def test_bot_routes_registered() -> None:
    app = aiohttp.web.Application()
    control_panel.register_panel_routes(app)
    paths = {getattr(r.resource, "canonical", None) for r in app.router.routes()}
    assert {"/", "/bot/info", "/bot/stop"} <= paths
