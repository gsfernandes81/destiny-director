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

# Per-feed actions: preview returns the node tree the shared renderer draws (and reports
# a build failure as data rather than a 500), and send guards its preconditions —
# dormant feed, no announcer, one already in flight — and never posts when the build
# fails. The buttons that call these live on /autopost_settings; see
# test_autopost_settings.py. Exercised with fake requests (no live server); auth is the
# web_auth middleware, covered in test_web_auth.py.

import asyncio
import json
import typing as t

import aiohttp.web
import hikari as h
import pytest

from dd.anchor import autopost, web
from dd.anchor.extensions import feed_actions
from dd.hmessage import HMessage

pytestmark = pytest.mark.asyncio


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in: path vars plus an awaitable ``.json()``."""

    def __init__(self, name: str, payload: object | None = None) -> None:
        self.match_info = {"name": name}
        self._payload = payload

    async def json(self) -> object:
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _as_request(req: _FakeRequest) -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, req)


def _text(resp: aiohttp.web.Response) -> str:
    """Response.text is typed ``str | None``; every handler here sets it."""
    assert resp.text is not None
    return resp.text


def _post() -> HMessage:
    """A minimal CV2 post — one container holding one text display."""
    container = h.impl.ContainerComponentBuilder()
    container.add_text_display("Hello from the feed")
    return HMessage(components=[container])


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[dict[str, t.Any]]:
    """Swap the import-time feed registry for a controlled one, and stub the bot.

    The real registry is populated by the producer modules at import; tests need
    predictable feeds (including a dormant one) without touching them.

    Also stubs the settings read the send handler now does per request, so a test can
    say what a feed's channel is — and change it mid-test — without a database. The
    yielded dict is that answer, keyed by feed name; the fixture is requested by name
    where a test needs to write to it.
    """
    monkeypatch.setattr(autopost, "_feeds", {})
    monkeypatch.setattr(web, "_bot", object())
    monkeypatch.setattr(feed_actions, "_sending", set())

    channels: dict[str, t.Any] = {"xur": 123}

    async def _get_followable_channel(feed: str) -> t.Any:
        # Deliberately `t.Any` rather than the production `int`: the handler's guard is
        # falsy-based, and a couple of tests below feed it None to pin that.
        return channels.get(feed, 0)

    monkeypatch.setattr(
        feed_actions.dd_settings, "get_followable_channel", _get_followable_channel
    )
    yield channels


async def _constructor(**_kwargs: object) -> HMessage:
    return _post()


async def _failing_constructor(**_kwargs: object) -> HMessage:
    raise RuntimeError("no event scheduled")


def _register(
    name: str = "xur",
    *,
    constructor: t.Callable[..., t.Awaitable[HMessage]] | None = None,
    announcer: t.Callable[..., t.Awaitable[t.Any]] | None = None,
) -> None:
    autopost.register_feed(
        autopost.Feed(
            name=name,
            message_constructor_coro=constructor or _constructor,
            message_announcer_coro=announcer,
        )
    )


# --- the page shell ----------------------------------------------------------------


async def test_unknown_feed_404s() -> None:
    with pytest.raises(aiohttp.web.HTTPNotFound):
        await feed_actions._handle_preview(_as_request(_FakeRequest("nope")))


# --- preview -----------------------------------------------------------------------


async def test_preview_returns_the_node_tree_for_the_shared_renderer() -> None:
    # The same {kind, payload, message_kind} shape /mirror-logs/render serves, so the
    # page draws it with the identical CV2Render.snapshotSpec call.
    _register()
    res = await feed_actions._handle_preview(_as_request(_FakeRequest("xur")))
    payload = json.loads(_text(res))
    assert payload["kind"] == "snapshot"
    assert payload["message_kind"] == "cv2"
    assert "Hello from the feed" in json.dumps(payload["payload"])


async def test_preview_reports_a_build_failure_as_data() -> None:
    # Iron Banner between events raises; the Discord `show` reported that inline, so the
    # page must render it in the preview box rather than 500.
    _register(constructor=_failing_constructor)
    res = await feed_actions._handle_preview(_as_request(_FakeRequest("xur")))
    assert "no event scheduled" in json.loads(_text(res))["error"]


async def test_preview_works_while_dormant(
    _isolated_registry: dict[str, t.Any],
) -> None:
    # Construction needs no channel, so a dormant feed still previews.
    _isolated_registry["xur"] = 0
    _register()
    res = await feed_actions._handle_preview(_as_request(_FakeRequest("xur")))
    assert "Hello from the feed" in json.dumps(json.loads(_text(res))["payload"])


# --- send --------------------------------------------------------------------------


async def test_send_starts_the_announcer_and_returns() -> None:
    seen: dict[str, t.Any] = {}
    started = asyncio.Event()

    async def _announcer(**kwargs: t.Any) -> None:
        seen.update(kwargs)
        started.set()

    _register(announcer=_announcer)
    res = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert json.loads(_text(res)) == {"ok": True, "started": True}

    await asyncio.wait_for(started.wait(), timeout=1)
    assert seen["channel_id"] == 123
    # A manual send always posts, whatever the autopost toggle says.
    assert seen["check_enabled"] is False
    assert seen["publish_message"] is True


async def test_send_honours_publish_false() -> None:
    seen: dict[str, t.Any] = {}
    started = asyncio.Event()

    async def _announcer(**kwargs: t.Any) -> None:
        seen.update(kwargs)
        started.set()

    _register(announcer=_announcer)
    await feed_actions._handle_send(
        _as_request(_FakeRequest("xur", {"publish": False}))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert seen["publish_message"] is False


@pytest.mark.parametrize("dormant_channel", [None, 0])
async def test_send_refuses_a_dormant_feed(
    dormant_channel: int | None, _isolated_registry: dict[str, t.Any]
) -> None:
    # 0 is the shape production actually produces: a followable with no DB row reads
    # back as 0 from settings.get_followable_channel, not None. A guard written as
    # `is None` let Send answer "started" and announce into channel 0 — failing where
    # only the log could see it. Both spellings are fed in, so the guard stays falsy-
    # based rather than drifting back to an identity test.
    called = False

    async def _announcer(**_kwargs: t.Any) -> None:
        nonlocal called
        called = True

    _isolated_registry["xur"] = dormant_channel
    _register(announcer=_announcer)
    res = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert res.status == 409
    # The sentence names the feed and points at the fix — this is the only place a
    # channel-less feed surfaces, so "dormant" (the settings page's word) would leave
    # the reader with nothing to do about it.
    err = json.loads(_text(res))["error"]
    assert "no channel" in err and "Feeds page" in err
    assert not called


async def test_send_reads_the_channel_at_request_time(
    _isolated_registry: dict[str, t.Any],
) -> None:
    # The point of dropping Feed.channel_id: the channel came from an import-time
    # snapshot, so a channel set (or moved) on the Autopost Settings page did nothing
    # until the bot was restarted. Registration here happens while the feed is dormant,
    # and the send still lands in the channel configured afterwards.
    seen: dict[str, t.Any] = {}
    started = asyncio.Event()

    async def _announcer(**kwargs: t.Any) -> None:
        seen.update(kwargs)
        started.set()

    _isolated_registry["xur"] = 0
    _register(announcer=_announcer)

    _isolated_registry["xur"] = 456
    res = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert res.status == 200

    await asyncio.wait_for(started.wait(), timeout=1)
    assert seen["channel_id"] == 456


async def test_send_refuses_without_an_announcer() -> None:
    _register(announcer=None)
    res = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert res.status == 409


async def test_send_does_not_post_when_the_build_fails() -> None:
    # The pre-flight build is the whole point of building before spawning: a broken
    # constructor must not reach the announcer, which would post a placeholder first.
    called = False

    async def _announcer(**_kwargs: t.Any) -> None:
        nonlocal called
        called = True

    _register(constructor=_failing_constructor, announcer=_announcer)
    res = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert res.status == 502
    assert "nothing was sent" in json.loads(_text(res))["error"]
    assert not called
    # And the slot is released, or a failed build would make the feed unsendable.
    assert "xur" not in feed_actions._sending


async def test_a_send_during_another_send_s_build_is_refused() -> None:
    """The in-flight guard must cover the pre-flight build, not just the announcer.

    The build takes seconds against the live Bungie API. When the slot was only claimed
    after it, two requests arriving inside that window both passed the check and both
    posted — the exact double-post the guard exists to prevent.
    """
    building = asyncio.Event()
    release = asyncio.Event()
    builds = 0

    async def _slow_constructor(**_kwargs: object) -> HMessage:
        nonlocal builds
        builds += 1
        building.set()
        await release.wait()
        return _post()

    async def _announcer(**_kwargs: t.Any) -> None:
        return None

    _register(constructor=_slow_constructor, announcer=_announcer)

    first = asyncio.create_task(
        feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    )
    await building.wait()  # the first request is mid-build, not yet started

    second = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert second.status == 409
    assert "already in flight" in json.loads(_text(second))["error"]

    release.set()
    assert (await first).status == 200
    assert builds == 1  # the second never got as far as building


async def test_send_rejects_a_second_send_while_one_is_in_flight() -> None:
    release = asyncio.Event()

    async def _announcer(**_kwargs: t.Any) -> None:
        await release.wait()

    _register(announcer=_announcer)
    first = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert first.status == 200

    second = await feed_actions._handle_send(_as_request(_FakeRequest("xur", {})))
    assert second.status == 409
    assert "already in flight" in json.loads(_text(second))["error"]

    # Once the first finishes, the slot is released and a send is allowed again.
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "xur" not in feed_actions._sending


async def test_routes_registered_without_a_page_or_card() -> None:
    # Actions only: no page shell and no control-panel card. The buttons live on the
    # /autopost_settings rows, so a per-feed page (or a card per feed — exactly the flat
    # list that was rejected) would be a click in the way.
    app = aiohttp.web.Application()
    feed_actions.register_feed_action_routes(app)
    paths = {getattr(r.resource, "canonical", None) for r in app.router.routes()}
    assert "/feed/{name}/preview" in paths
    assert "/feed/{name}/send" in paths


async def test_preview_before_the_bot_is_up_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The message must survive to the page. An HTTPServiceUnavailable here stringified
    # to "Service Unavailable" — technically true, useless to read — because both call
    # sites report str(e) rather than letting it propagate as a response.
    monkeypatch.setattr(web, "_bot", None)
    _register()
    res = await feed_actions._handle_preview(_as_request(_FakeRequest("xur")))
    assert "still starting" in json.loads(_text(res))["error"]
