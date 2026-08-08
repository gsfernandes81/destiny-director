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

# The "See more on Kyber's Corner!" link button on text (response_type 1) user commands.
# Its url is the ``default_url`` DB setting, which — unlike the DEFAULT_URL env var it
# replaced — defaults to "" and is clearable on the settings page. hikari does no url
# validation, so a blank setting would put ``"url": ""`` in the payload and Discord
# would reject the whole response, breaking every text user command at once. These pin
# the caller-side guard: button when configured, no components at all when blank.

import typing as t

import hikari as h
import pytest

from dd.beacon.extensions import user_commands as uc
from dd.common import settings
from dd.common.schemas import UserCommand

LABEL = "See more on Kyber's Corner!"


class _FakeContext:
    """Captures the one ``respond`` call the text handler makes."""

    def __init__(self) -> None:
        self.content: t.Any = None
        self.kwargs: dict[str, t.Any] = {}

    async def respond(self, content: t.Any = None, **kwargs: t.Any) -> None:
        self.content = content
        self.kwargs = kwargs


async def _run(monkeypatch: pytest.MonkeyPatch, default_url: str) -> _FakeContext:
    """Invoke a response_type 1 handler with ``default_url`` as the stored setting."""

    async def _get_default_url() -> str:
        return default_url

    monkeypatch.setattr(settings, "get_default_url", _get_default_url)

    # Response text with no urls in it, so the redirect-following branch stays inert
    # and the handler never reaches the network.
    cmd = UserCommand(
        "kyber", description="d", response_type=1, response_data="  hello there  "
    )
    handler = uc._user_command_response_func_builder(cmd)
    ctx = _FakeContext()
    await handler(None, t.cast(t.Any, ctx))
    return ctx


@pytest.mark.asyncio
async def test_blank_default_url_responds_without_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A blank setting is a supported state (fresh install, or cleared on the settings
    # page), so the response must still go out — minus the button.
    ctx = await _run(monkeypatch, "")

    assert ctx.content == "hello there"
    assert ctx.kwargs["components"] is h.UNDEFINED


@pytest.mark.asyncio
async def test_configured_default_url_attaches_the_link_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _run(monkeypatch, "https://kyber3000.com")

    assert ctx.content == "hello there"
    (row,) = ctx.kwargs["components"]
    assert isinstance(row, h.impl.MessageActionRowBuilder)
    (button,) = row.components
    assert isinstance(button, h.impl.LinkButtonBuilder)
    assert button.url == "https://kyber3000.com"
    assert button.label == LABEL


def test_a_blank_url_would_reach_discord_unvalidated() -> None:
    # Why the guard has to live at the call site: hikari's builder accepts "" happily
    # and serialises it, so nothing downstream of here would catch it — Discord is the
    # first thing to object, by rejecting the entire response.
    row = h.impl.MessageActionRowBuilder().add_link_button("", label=LABEL)
    payload, _attachments = row.build()

    assert payload["components"][0]["url"] == ""
