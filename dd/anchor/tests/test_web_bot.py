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

"""The single live-bot stash every anchor web route reads: the two accessors, the
middleware that turns a handler's ``BotNotReady`` into one shared 503, and how
:func:`web.start` composes that middleware with the auth gate rather than displacing
it. Each route's own use of the accessors is covered in that route's test module."""

import json
import typing as t

import aiohttp.web
import pytest

from dd.anchor import web
from dd.anchor.extensions import web_auth

pytestmark = pytest.mark.asyncio


class _FakeRequest:
    """Minimal request stand-in — the middleware only logs the method and path."""

    def __init__(self, path: str = "/bot/stop", method: str = "POST") -> None:
        self.path = path
        self.method = method


def _as_request(req: _FakeRequest) -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, req)


@pytest.fixture(autouse=True)
def _unstashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "the bot is not up yet".

    The stash is process-wide module state, so without this a test that stashes one
    would decide the answer for whatever runs after it.
    """
    monkeypatch.setattr(web, "_bot", None)


async def test_get_bot_is_none_before_the_stash() -> None:
    # The tolerant accessor: a caller that can degrade (a preview without guild emoji,
    # a panel row that stays a raw snowflake) asks with this one and gets None.
    assert web.get_bot() is None


async def test_require_bot_refuses_with_a_sentence_worth_showing() -> None:
    with pytest.raises(web.BotNotReady) as excinfo:
        web.require_bot()
    # The message is the whole point of BotNotReady over HTTPServiceUnavailable, whose
    # stringification ("Service Unavailable") drops the part telling you what to do.
    assert "try again" in str(excinfo.value)


async def test_stash_bot_serves_both_accessors() -> None:
    sentinel = t.cast(t.Any, object())
    web.stash_bot(sentinel)
    assert web.get_bot() is sentinel
    assert web.require_bot() is sentinel


async def test_middleware_answers_bot_not_ready_with_the_shared_503() -> None:
    async def _handler(_request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
        web.require_bot()
        raise AssertionError("require_bot should have raised")

    resp = await web._bot_not_ready_middleware(_as_request(_FakeRequest()), _handler)

    assert resp.status == 503
    # JSON, because every page here reads `data.error` off a failed fetch.
    assert resp.content_type == "application/json"
    body = t.cast(aiohttp.web.Response, resp).text
    assert json.loads(body or "")["error"] == web.BOT_STARTING_MSG


async def test_middleware_lets_a_normal_response_through() -> None:
    async def _handler(_request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
        return aiohttp.web.json_response({"ok": True})

    resp = await web._bot_not_ready_middleware(_as_request(_FakeRequest()), _handler)
    assert resp.status == 200


async def test_middleware_does_not_swallow_other_failures() -> None:
    # It converts exactly one exception. A genuine bug must still surface as a 500 —
    # reporting it as "the bot is still starting" would send the operator to wait out a
    # startup that finished hours ago.
    async def _handler(_request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
        raise RuntimeError("something else entirely")

    with pytest.raises(RuntimeError):
        await web._bot_not_ready_middleware(_as_request(_FakeRequest()), _handler)


async def test_start_nests_the_converter_inside_the_auth_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Port 0 so the kernel picks a free one — this exercises the real start() path,
    # registrars and all, without claiming cfg.port.
    monkeypatch.setattr(web, "_runner", None)
    try:
        await web.start(port=0)
        runner = web._runner
        assert runner is not None
        # aiohttp treats middlewares[0] as outermost: the auth gate still runs first and
        # the converter only ever sees a request it admitted.
        assert runner.app.middlewares == [
            web_auth._auth_middleware,
            web._bot_not_ready_middleware,
        ]
    finally:
        await web.stop()


async def test_start_still_refuses_when_no_registrar_contributed_a_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fail-closed guard asks whether web_auth loaded. The converter is installed
    # from inside start(), AFTER that question is asked — so it must not be able to
    # answer it on web_auth's behalf and serve the editor unauthenticated.
    monkeypatch.setattr(web, "_runner", None)
    monkeypatch.setattr(web, "_route_registrars", [])
    with pytest.raises(RuntimeError, match="no middleware"):
        await web.start(port=0)
    assert web._runner is None
