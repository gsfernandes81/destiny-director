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

"""CV2 builder page routes — exercised with a fake request (no live server).

Two things here are load-bearing and everything else is plumbing:

1. **Every route is creator-scoped.** OAuth (tested in ``test_web_auth.py``) proves the
   caller is *an* owner; these routes must additionally refuse another owner's draft, or
   the id in the URL would be the only thing standing between two authors' drafts.
2. **The server is the authority on validity.** The client blocks its own Post button on
   the same rules, but a hand-rolled POST must still not be able to publish an invalid
   tree — so ``/publish`` re-validates and never reaches Discord when it shouldn't.
3. **The server is the authority on the target.** ``/cv2-builder/new`` is the one
   route a browser may name a publish target through, and it stores one only if
   ``autopost_settings.check_channel`` vouches for it — so the row ``/publish`` reads
   from can never hold a channel nobody vetted.
"""

import json
import typing as t
import uuid
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

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

OWNER = 4242
OTHER_OWNER = 9999
CHANNEL = 777
GUILD = 555

GOOD_NODES = [{"type": 10, "content": "# Hello"}]
EMPTY_TEXT_NODES = [{"type": 10, "content": "   "}]


class _FakeRequest(dict):
    """Enough of ``aiohttp.web.Request`` for these handlers: the auth key, the path
    match info, and a JSON body."""

    def __init__(self, draft_id: str, user_id: int, body: dict | None = None) -> None:
        super().__init__()
        self[AUTH_USER_KEY] = user_id
        self.match_info = {"draft": draft_id}
        self._body = body

    async def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _req(draft_id: str, user_id: int = OWNER, body: dict | None = None):
    return t.cast(aiohttp.web.Request, _FakeRequest(draft_id, user_id, body))


def _payload(resp: aiohttp.web.Response) -> dict:
    assert resp.text is not None
    return json.loads(resp.text)


async def _draft(action: str = Cv2Draft.ACTION_POST, **over) -> str:
    draft_id = uuid.uuid4().hex
    kwargs: dict = dict(
        id=draft_id,
        created_by=OWNER,
        action=action,
        nodes=GOOD_NODES,
        guild_id=GUILD,
        target_channel_id=CHANNEL,
    )
    kwargs.update(over)
    await Cv2Draft.create(**kwargs)
    return draft_id


class _StubMessage:
    def __init__(self, message_id: int = 31337) -> None:
        self.id = message_id
        self.channel_id = CHANNEL


class _StubChannel:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, components=None, **kwargs):
        self.sent.append(components)
        return _StubMessage()


class _StubBot:
    """Records what reached Discord, so a test can assert nothing did."""

    def __init__(self) -> None:
        self.channel = _StubChannel()
        self.edits: list = []
        self.rest = self

    async def fetch_channel(self, channel_id: int):
        return self.channel

    async def edit_message(self, channel_id, message_id, **kwargs):
        self.edits.append((channel_id, message_id, kwargs))
        return _StubMessage(message_id)


@pytest.fixture
def stub_bot(monkeypatch: pytest.MonkeyPatch) -> _StubBot:
    bot = _StubBot()
    monkeypatch.setattr(web, "_bot", bot)
    # The emoji map is a REST round-trip; short-circuit it (its own degrade path is
    # covered by the try/except in _emoji_map).
    monkeypatch.setattr(page, "_emoji_cache", {})
    return bot


# --- creator scoping ------------------------------------------------------------


async def test_data_returns_the_draft_for_its_creator(stub_bot: _StubBot) -> None:
    draft_id = await _draft()

    payload = _payload(await page._handle_data(_req(draft_id)))

    assert payload["action"] == Cv2Draft.ACTION_POST
    assert payload["nodes"] == GOOD_NODES
    assert payload["target_channel_mention"] == f"<#{CHANNEL}>"
    assert payload["published_message_link"] is None


@pytest.mark.parametrize(
    "handler_name, body",
    [
        ("_handle_data", None),
        ("_handle_save", {"nodes": GOOD_NODES}),
        ("_handle_preview", {"nodes": GOOD_NODES}),
        ("_handle_publish", {"nodes": GOOD_NODES}),
    ],
)
async def test_every_route_404s_another_owners_draft(
    stub_bot: _StubBot, handler_name: str, body: dict | None
) -> None:
    """A different owner is told the draft does not exist, not that it is forbidden —
    same answer a stranger gets, so the id leaks nothing."""
    draft_id = await _draft()
    handler = getattr(page, handler_name)

    with pytest.raises(aiohttp.web.HTTPNotFound):
        await handler(_req(draft_id, user_id=OTHER_OWNER, body=body))

    assert stub_bot.channel.sent == []
    assert stub_bot.edits == []


async def test_unknown_draft_is_404() -> None:
    with pytest.raises(aiohttp.web.HTTPNotFound):
        await page._handle_data(_req("nosuchdraft"))


# --- body validation ------------------------------------------------------------


@pytest.mark.parametrize("body", [{}, {"nodes": "not a list"}, {"nodes": [1, 2]}])
async def test_save_rejects_a_malformed_body(body: dict) -> None:
    draft_id = await _draft()
    with pytest.raises(aiohttp.web.HTTPBadRequest):
        await page._handle_save(_req(draft_id, body=body))


async def test_save_round_trips_the_nodes() -> None:
    draft_id = await _draft()
    new_nodes = [{"type": 10, "content": "edited on the web"}]

    resp = await page._handle_save(_req(draft_id, body={"nodes": new_nodes}))

    assert resp.status == 200
    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None and draft.nodes == new_nodes


# --- preview --------------------------------------------------------------------


async def test_preview_returns_the_sanitized_tree(stub_bot: _StubBot) -> None:
    """The confirmation renders client-side now, so this route returns DATA.

    What makes it worth a round-trip is the sanitize: the tree that comes back is the
    one the server would actually post, so a mid-construction block shows in the
    confirmation as the placeholder it will publish as rather than as the author left
    it. Escaping is the renderer's job and is pinned by the shared corpus
    (``dd/anchor/preview_fixtures``), not re-asserted here.
    """
    draft_id = await _draft()

    payload = _payload(
        await page._handle_preview(
            _req(
                draft_id,
                # An empty container is exactly the mid-construction state sanitize
                # exists for: unsendable as-is, and Discord would reject the edit.
                body={"nodes": [{"type": 17, "components": []}]},
            )
        )
    )

    assert payload["nodes"] == [
        {
            "type": 17,
            "components": [
                {"type": 10, "content": "-# ⚠️ empty container — open it to add blocks"}
            ],
        }
    ]


async def test_preview_leaves_a_sound_tree_alone(stub_bot: _StubBot) -> None:
    draft_id = await _draft()

    payload = _payload(
        await page._handle_preview(_req(draft_id, body={"nodes": GOOD_NODES}))
    )

    assert payload["nodes"] == GOOD_NODES


# --- publish --------------------------------------------------------------------


async def test_publish_sends_and_records_the_message(stub_bot: _StubBot) -> None:
    draft_id = await _draft()

    payload = _payload(
        await page._handle_publish(_req(draft_id, body={"nodes": GOOD_NODES}))
    )

    assert len(stub_bot.channel.sent) == 1
    assert payload["link"].endswith(f"/{GUILD}/{CHANNEL}/31337")
    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None and draft.published_message_id == 31337


async def test_publish_edits_in_place_for_an_edit_draft(stub_bot: _StubBot) -> None:
    draft_id = await _draft(action=Cv2Draft.ACTION_EDIT, target_message_id=12345)

    await page._handle_publish(_req(draft_id, body={"nodes": GOOD_NODES}))

    assert stub_bot.channel.sent == []  # an edit must never post a new message
    (channel_id, message_id, _kwargs) = stub_bot.edits[0]
    assert (channel_id, message_id) == (CHANNEL, 12345)


async def test_publish_revalidates_server_side(stub_bot: _StubBot) -> None:
    """The client disables its own button on the same rule; this proves a hand-rolled
    POST cannot get past it either."""
    draft_id = await _draft()

    resp = await page._handle_publish(_req(draft_id, body={"nodes": EMPTY_TEXT_NODES}))

    assert resp.status == 400
    assert "empty" in _payload(resp)["error"]
    assert stub_bot.channel.sent == []  # nothing reached Discord
    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None and draft.published_message_id is None


async def test_publish_without_a_target_channel_is_refused(stub_bot: _StubBot) -> None:
    draft_id = await _draft(target_channel_id=None)

    resp = await page._handle_publish(_req(draft_id, body={"nodes": GOOD_NODES}))

    assert resp.status == 400
    assert stub_bot.channel.sent == []


# --- draft creation (the Discord side) ------------------------------------------


async def test_new_draft_returns_a_url_and_persists_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page.cfg, "public_base_url", "https://example.invalid/")

    url = await page.new_draft(
        user_id=OWNER,
        action=Cv2Draft.ACTION_COPY,
        nodes=GOOD_NODES,
        guild_id=GUILD,
        channel_id=CHANNEL,
    )

    assert url.startswith("https://example.invalid/cv2-builder/")
    draft_id = url.rsplit("/", 1)[-1]
    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None
    assert draft.action == Cv2Draft.ACTION_COPY
    assert draft.target_channel_id == CHANNEL
    # No double slash from a trailing-slash base URL.
    assert "//cv2-builder" not in url


async def test_new_draft_id_returns_a_bare_id_not_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The web path's reason for existing: an id, with no ``public_base_url`` involved.

    That variable is legitimately empty on a local dev box, so a web flow composing an
    absolute URL from it would hand the browser a dead link on the very deployment the
    flow is most likely to be tried on.
    """
    monkeypatch.setattr(page.cfg, "public_base_url", "")

    draft_id = await page.new_draft_id(user_id=OWNER, action=Cv2Draft.ACTION_POST)

    assert "/" not in draft_id
    assert await Cv2Draft.get_for_user(draft_id, OWNER) is not None


# --- minting a draft from the web (POST /cv2-builder/new) -----------------------


_FULL_PERMS = (
    h.Permissions.VIEW_CHANNEL
    | h.Permissions.SEND_MESSAGES
    | h.Permissions.EMBED_LINKS
    | h.Permissions.USE_EXTERNAL_EMOJIS
)


def _new_req(body: dict | None, user_id: int = OWNER):
    """A request to the mint route, which is not draft-scoped (no draft exists yet)."""
    return t.cast(aiohttp.web.Request, _FakeRequest("", user_id, body))


@pytest.fixture
def vettable_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bot for which ``CHANNEL`` passes every check the mint route makes.

    Deliberately a plain ``GUILD_TEXT`` channel: a one-off custom post is just a message
    — nothing follows it — so the route runs the validator with ``announce_only=False``
    and a text channel must be accepted. Guild scope is pinned rather than inherited
    from the ambient environment, since ``TEST_ENV`` would otherwise silently move the
    allowed set off ``GUILD``.
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


async def test_new_mints_a_draft_for_a_vetted_channel(vettable_channel: None) -> None:
    resp = await page._handle_new(_new_req({"channel_id": str(CHANNEL)}))
    payload = _payload(resp)

    assert resp.status == 200
    # Relative, and not a 302: the page decides whether to navigate, and a fetch() that
    # followed a redirect would be handed the builder's HTML instead of an answer.
    assert payload["path"].startswith("/cv2-builder/")
    assert "://" not in payload["path"]

    draft_id = payload["path"].rsplit("/", 1)[-1]
    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None
    assert draft.action == Cv2Draft.ACTION_POST
    assert draft.target_channel_id == CHANNEL
    # Resolved from the channel the server fetched, not taken from the request.
    assert draft.guild_id == GUILD


async def test_new_credits_the_authenticated_user(vettable_channel: None) -> None:
    """``created_by`` comes from the session, so a body claiming otherwise is ignored —
    otherwise a draft could be minted into another owner's creator-scoped space."""
    resp = await page._handle_new(
        _new_req({"channel_id": str(CHANNEL), "created_by": OTHER_OWNER}, user_id=OWNER)
    )

    draft_id = _payload(resp)["path"].rsplit("/", 1)[-1]
    assert await Cv2Draft.get_for_user(draft_id, OTHER_OWNER) is None
    assert await Cv2Draft.get_for_user(draft_id, OWNER) is not None


async def test_new_refuses_an_unusable_channel_with_the_validators_own_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validator's whole design is that the reason is the payload, so the route
    surfaces the sentence verbatim rather than flattening it to "that didn't work".

    Driven through the real validator (bot not up is its fail-closed branch), so this
    breaks if the refusal ever stops reaching the caller.
    """
    monkeypatch.setattr(web, "_bot", None)

    resp = await page._handle_new(_new_req({"channel_id": str(CHANNEL)}))
    payload = _payload(resp)

    assert resp.status == 400
    assert (
        "the bot hasn't finished starting yet — try again in a moment."
        in payload["error"]
    )
    assert "path" not in payload


async def test_new_refuses_a_channel_outside_the_allowed_guilds(
    vettable_channel: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same guild scope the settings page's non-control-scoped channel fields use: the
    # browser may name a channel, but not one in a server this bot doesn't serve.
    monkeypatch.setattr(cfg, "kyber_discord_server_id", GUILD + 1)

    resp = await page._handle_new(_new_req({"channel_id": str(CHANNEL)}))

    assert resp.status == 400
    assert "server this setting can't post to" in _payload(resp)["error"]


@pytest.mark.parametrize(
    "body", [None, {}, {"channel_id": None}, {"channel_id": "not a number"}]
)
async def test_new_rejects_a_body_with_no_usable_channel_id(body: dict | None) -> None:
    resp = await page._handle_new(_new_req(body))

    assert resp.status == 400
    assert "path" not in _payload(resp)


async def test_routes_are_registered() -> None:
    app = aiohttp.web.Application()
    page.register_cv2_builder_routes(app)
    paths = {
        r.resource.canonical for r in app.router.routes() if r.resource is not None
    }
    assert "/cv2-builder/new" in paths
    assert "/cv2-builder/{draft}" in paths
    assert "/cv2-builder/{draft}/publish" in paths


# --- draft disposal -------------------------------------------------------------


async def test_prune_drafts_contains_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daily cron calls this; an escaping exception would take the scheduled job
    down with it, so the next day's sweep would not run either."""

    async def _boom(*_args, **_kwargs) -> int:
        raise RuntimeError("db is down")

    monkeypatch.setattr(Cv2Draft, "prune", _boom)

    await page._prune_drafts()  # must not raise


async def test_startup_prunes_once_and_schedules_a_daily_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot-time prune (what cleans up after a long outage) *and* a recurring one (what
    keeps a long-lived process from accumulating drafts a web button mints per click).

    The schedule must also stay off 17:00 UTC, where every producer in this bot fires —
    a delete has no reason to contend with the day's posting.
    """
    pruned: list[int] = []
    specs: list[str] = []

    async def _fake_prune(*_args, **_kwargs) -> int:
        pruned.append(1)
        return 0

    def _fake_crontab(spec: str, **_kwargs):
        specs.append(spec)
        return lambda fn: fn

    monkeypatch.setattr(Cv2Draft, "prune", _fake_prune)
    monkeypatch.setattr(page.aiocron, "crontab", _fake_crontab)

    await page._on_started(t.cast(h.StartedEvent, object()))

    assert pruned == [1]
    assert specs == ["0 4 * * *"]
    assert not specs[0].startswith("0 17 ")


async def test_page_shell_is_servable() -> None:
    # A missing/renamed template would otherwise only fail in production.
    assert page._PAGE_HTML_PATH.exists()
    assert "cv2_builder_page.js" in page._PAGE_HTML_PATH.read_text()
