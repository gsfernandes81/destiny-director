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

"""``GET /custom-post`` — the channel-picking doorway in front of the CV2 builder.

The page itself is a static shell, so what is worth testing here is what the shell rests
on and what it would break silently without:

1. **The route and the card exist and point at each other.** A card is the only way to
   this page; a card whose href 404s is a dead end nothing else reports.
2. **The picker's rule is the server's rule.** The page sorts announcement channels
   first while leaving text channels selectable, which is only correct because the mint
   route vets with ``announce_only`` off. If that ever flipped, the page would keep
   offering channels the server had started refusing — and the refusal would arrive one
   click later, after the choice looked accepted.
3. **A refusal arrives as the validator's own sentence.** ``check_channel`` is written
   so the reason IS the payload ("the bot is missing permissions there: Embed Links."),
   and the page shows it verbatim. Flattening it anywhere along the way costs the only
   actionable part of the message, and nothing would fail — it would just get vaguer.

The picker's ORDERING is client-side and pinned in
``web_static/tests/custom_post.test.js`` (``make test-js``), where the sort actually
lives; what is asserted here is the server-side half that ordering depends on.
"""

import importlib
import json
import typing as t
from unittest.mock import MagicMock

import aiohttp.web
import hikari as h
import pytest

from dd.anchor import web
from dd.anchor.extensions import (
    autopost_settings as aps,
    cv2_builder_page as page,
)
from dd.anchor.extensions.web_auth import AUTH_USER_KEY
from dd.common import cfg
from dd.common.schemas import Cv2Draft

pytestmark = pytest.mark.asyncio

# A card registers when its module imports, so the "Send a post" group is only populated
# by whatever a session happened to load. Load the siblings this card must sort behind
# explicitly — otherwise running this file on its own compares against an empty group,
# which is the shape of assertion that passes forever while testing nothing.
for _sibling in ("weekly_reset", "trials"):
    importlib.import_module(f"dd.anchor.extensions.{_sibling}")

OWNER = 4242
CHANNEL = 777
GUILD = 555

_STATIC_DIR = page._CUSTOM_POST_HTML_PATH.parent


class _FakeRequest(dict):
    """Enough of ``aiohttp.web.Request`` for these handlers: the signed-in user and a
    JSON body."""

    def __init__(self, user_id: int = OWNER, body: dict | None = None) -> None:
        super().__init__()
        self[AUTH_USER_KEY] = user_id
        self.match_info: dict[str, str] = {}
        self._body = body

    async def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _req(body: dict | None = None, user_id: int = OWNER):
    return t.cast(aiohttp.web.Request, _FakeRequest(user_id, body))


def _payload(resp: aiohttp.web.Response) -> dict:
    assert resp.text is not None
    return json.loads(resp.text)


# --- the route and the card ---------------------------------------------------------


async def test_the_page_route_is_registered() -> None:
    app = aiohttp.web.Application()
    page.register_cv2_builder_routes(app)

    paths = {
        r.resource.canonical for r in app.router.routes() if r.resource is not None
    }
    assert "/custom-post" in paths


async def test_the_page_serves_its_shell() -> None:
    resp = await page._handle_custom_post_page(_req())

    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.text is not None
    assert "Custom one-off post" in resp.text


async def test_the_shell_loads_the_script_that_drives_it() -> None:
    """A renamed or missing asset would otherwise only show up as a page that renders
    its header and then does nothing at all — no picker, no error, no console clue that
    the file it wanted is not there."""
    shell = page._CUSTOM_POST_HTML_PATH.read_text(encoding="utf-8")

    for asset in (
        "/static/shared.js",
        "/static/vendor/tom-select.complete.min.js",
        "/static/custom_post.js",
        "/static/tom_select_dark.css",
    ):
        assert asset in shell, asset
        assert (_STATIC_DIR / asset.removeprefix("/static/")).exists(), asset


async def test_the_page_reuses_the_existing_channel_endpoint() -> None:
    """The picker is fed by ``/autopost_settings/channels``, which is scope-agnostic and
    already returns the ``announce`` flag this page sorts on. A second endpoint would be
    a second answer to "where may this bot post"."""
    script = (_STATIC_DIR / "custom_post.js").read_text(encoding="utf-8")

    assert "/autopost_settings/channels" in script
    assert "/cv2-builder/new" in script


async def test_the_card_points_at_the_page_and_sits_last_in_its_group() -> None:
    card = next(c for c in web.registered_cards() if c.href == "/custom-post")

    assert card.title == "Custom one-off post"
    assert card.group is web.CardGroup.SEND
    assert not card.danger
    # The row's own verb. Nothing exists to "Open" here — the row is where the post
    # begins — and the design gives group 1 four deliberately different verbs.
    assert card.action == "Start"
    # Behind Weekly Reset (10) and Trials (20) — the group is ordered by how often the
    # errand comes up, and writing a post from nothing is the rarest of them.
    others = [
        c
        for c in web.registered_cards()
        if c.group is web.CardGroup.SEND and c.href != "/custom-post"
    ]
    assert {"/weekly_reset", "/trials"} <= {c.href for c in others}
    assert card.order > max(c.order for c in others)


# --- the rule the picker rests on ----------------------------------------------------


_FULL_PERMS = (
    h.Permissions.VIEW_CHANNEL
    | h.Permissions.SEND_MESSAGES
    | h.Permissions.EMBED_LINKS
    | h.Permissions.USE_EXTERNAL_EMOJIS
)


@pytest.fixture
def _text_channel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A plain (non-announcement) text channel the bot can fully post in.

    Guild scope is pinned rather than inherited from the ambient environment, since
    ``TEST_ENV`` would otherwise move the allowed set off ``GUILD``.
    """
    monkeypatch.setattr(cfg, "kyber_discord_server_id", GUILD)
    monkeypatch.setattr(cfg, "control_discord_server_id", -1)
    monkeypatch.setattr(cfg, "test_env", ())

    channel = MagicMock(spec=h.GuildTextChannel)
    channel.guild_id = GUILD
    channel.type = h.ChannelType.GUILD_TEXT

    class _Rest:
        async def fetch_channel(self, _channel_id: int) -> h.GuildTextChannel:
            return channel

        async def fetch_member(self, _guild_id: int, _user_id: int) -> t.Any:
            return object()

    class _FakeBot:
        rest = _Rest()

        def get_me(self) -> h.OwnUser:
            return t.cast(h.OwnUser, MagicMock(spec=h.OwnUser, id=999))

    monkeypatch.setattr(web, "_bot", _FakeBot())
    monkeypatch.setattr(
        aps, "calculate_permissions", lambda _member, _chan: _FULL_PERMS
    )
    return channel


@pytest.mark.integration
async def test_a_plain_text_channel_is_accepted(
    _text_channel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason this picker differs from the feed pickers.

    A feed's channel must be an announcement channel or no other server can follow it;
    a one-off is sent straight to the channel, so a text channel is fine — and the page
    says so by listing it (second) rather than hiding it. This is the server agreeing.
    """
    resp = await page._handle_new(_req({"channel_id": str(CHANNEL)}))
    payload = _payload(resp)

    assert resp.status == 200
    draft_id = payload["path"].rsplit("/", 1)[-1]
    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None and draft.target_channel_id == CHANNEL


@pytest.mark.integration
async def test_an_announcement_channel_is_accepted_too(
    _text_channel: MagicMock,
) -> None:
    """Sorting announcement channels first must not turn into refusing them: the strict
    picker's *preferred* type is still the ordinary case here."""
    _text_channel.type = h.ChannelType.GUILD_NEWS

    resp = await page._handle_new(_req({"channel_id": str(CHANNEL)}))

    assert resp.status == 200


# --- the error path -----------------------------------------------------------------


async def test_a_refusal_carries_the_validators_own_sentence(
    _text_channel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentence the page puts on screen, end to end through the real validator.

    Driven at the permissions branch specifically, because that is the refusal that
    names something the operator can go and fix — and it is the one a summary like
    "couldn't use that channel" would destroy the most.
    """
    monkeypatch.setattr(
        aps,
        "calculate_permissions",
        lambda _member, _chan: h.Permissions.VIEW_CHANNEL | h.Permissions.SEND_MESSAGES,
    )

    resp = await page._handle_new(_req({"channel_id": str(CHANNEL)}))
    payload = _payload(resp)

    assert resp.status == 400
    assert (
        "the bot is missing permissions there: Embed Links, Use External Emoji."
        in payload["error"]
    )
    assert "path" not in payload


async def test_a_refusal_before_the_bot_is_up_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The likeliest refusal in practice, and the one the empty-picker line echoes: the
    page is reachable the moment the web app binds, which is before the gateway has
    finished starting."""
    monkeypatch.setattr(web, "_bot", None)

    resp = await page._handle_new(_req({"channel_id": str(CHANNEL)}))

    assert resp.status == 400
    assert "hasn't finished starting yet" in _payload(resp)["error"]
