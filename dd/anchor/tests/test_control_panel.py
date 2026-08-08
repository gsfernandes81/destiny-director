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

import json
import typing as t

import aiohttp.web
import pytest

from dd.anchor import web
from dd.anchor.extensions import control_panel

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


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in — the panel handler reads nothing off it."""


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


async def test_render_lists_cards_href_and_title_sorted(clean_cards: None) -> None:
    # Register out of order; the panel sorts by (title, …) for a stable display.
    web.register_card(web.Card("Weekly Reset", "compose the post", "/weekly_reset"))
    web.register_card(web.Card("Rotation Editor", "edit rotations", "/rotation"))

    html_out = control_panel._render_panel_html()

    assert 'href="/rotation"' in html_out
    assert 'href="/weekly_reset"' in html_out
    assert "Rotation Editor" in html_out
    assert "Weekly Reset" in html_out
    # Sorted: "Rotation Editor" (R) renders before "Weekly Reset" (W).
    assert html_out.index("Rotation Editor") < html_out.index("Weekly Reset")


async def test_render_escapes_html_in_card_fields(clean_cards: None) -> None:
    web.register_card(web.Card("A & <b>", "desc <script>", "/x?a=1&b=2"))

    html_out = control_panel._render_panel_html()

    # The card grid must not contain the raw, unescaped markup we fed in.
    assert "<b>" not in html_out
    assert "<script>" not in html_out
    assert "A &amp; &lt;b&gt;" in html_out
    assert "desc &lt;script&gt;" in html_out
    assert "/x?a=1&amp;b=2" in html_out


async def test_render_empty_registry_does_not_crash(clean_cards: None) -> None:
    html_out = control_panel._render_panel_html()

    assert "No web tools are available." in html_out
    assert "<!--__CARDS__-->" not in html_out


async def test_handle_panel_returns_html_response(clean_cards: None) -> None:
    web.register_card(web.Card("Rotation Editor", "edit rotations", "/rotation"))

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
    feeds = {c["feed"] for c in payload["channels"]}
    assert "lost_sector" in feeds


async def test_bot_info_resolves_channel_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A raw snowflake tells the reader nothing; the point of the panel is the name.
    class _Channel:
        name = "lost-sector"

    class _Bot:
        async def fetch_channel(self, _channel_id: int) -> _Channel:
            return _Channel()

    monkeypatch.setattr(control_panel, "_bot", _Bot())
    payload = json.loads(
        _text(await control_panel._handle_bot_info(_as_request(_FakeRequest())))
    )
    named = [c for c in payload["channels"] if c["channelName"]]
    assert named and all(c["channelName"] == "#lost-sector" for c in named)


async def test_bot_info_survives_an_unresolvable_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not in the guild, channel deleted, bot still starting — none of which should cost
    # the panel its whole config dump.
    class _Bot:
        async def fetch_channel(self, _channel_id: int) -> object:
            raise RuntimeError("not found")

    monkeypatch.setattr(control_panel, "_bot", _Bot())
    payload = json.loads(
        _text(await control_panel._handle_bot_info(_as_request(_FakeRequest())))
    )
    assert payload["channels"]
    assert all(c["channelName"] is None for c in payload["channels"])
    # The id is still there, so the row degrades to a snowflake rather than vanishing.
    assert any(c["channelId"] for c in payload["channels"])


async def test_bot_stop_503s_before_the_bot_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Routes are live from web.start(); _bot is only set on StartedEvent, so a request
    # in that window must not blow up.
    monkeypatch.setattr(control_panel, "_bot", None)
    resp = await control_panel._handle_bot_stop(_as_request(_FakeRequest()))
    assert resp.status == 503


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
    monkeypatch.setattr(control_panel, "_bot", sentinel)
    monkeypatch.setattr(control_panel.lifecycle, "request_shutdown", _request_shutdown)

    resp = await control_panel._handle_bot_stop(_as_request(_FakeRequest()))
    assert json.loads(_text(resp)) == {"ok": True, "stopping": True}
    assert seen["bot"] is sentinel
    assert seen["exit_code"] == control_panel.lifecycle.STOP_EXIT_CODE


async def test_panel_hosts_the_bot_actions_and_modals(clean_cards: None) -> None:
    body = _text(await control_panel._handle_panel(_as_request(_FakeRequest())))

    assert 'id="infoBtn"' in body
    assert 'id="stopBtn"' in body
    # `danger` on the dialog is what makes the shutdown modal read as a warning rather
    # than another form — the copy below is only half of it.
    assert '<dialog class="panelmodal danger" id="stopDialog">' in body
    # Shutting down takes the panel with it; the dialog must say so.
    assert "panel runs inside the bot" in body
    assert "/static/control_panel.js" in body
    assert "<script>" not in body  # CSP is script-src 'self'


async def test_bot_routes_registered() -> None:
    app = aiohttp.web.Application()
    control_panel.register_panel_routes(app)
    paths = {getattr(r.resource, "canonical", None) for r in app.router.routes()}
    assert {"/", "/bot/info", "/bot/stop"} <= paths
