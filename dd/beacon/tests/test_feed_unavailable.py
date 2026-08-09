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

"""How beacon's commands behave when a feed's source channel is unusable.

The rule they all follow: register regardless, answer the user clearly, and log
CRITICAL so the owners are paged — a source channel is ours to fix, and a user who
hits one can do nothing about it. None of this touches mirroring, which runs off
``MirroredChannel`` rows and is always on.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import hikari as h
import lightbulb as lb
import pytest

from dd.beacon import nav, utils
from dd.beacon.extensions import autoposts, free_games
from dd.common import settings

pytestmark = pytest.mark.asyncio


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.respond = AsyncMock()
    ctx.defer = AsyncMock()
    return ctx


def _started_listener(loader: lb.Loader):
    """The StartedEvent callback a Loader just had registered on it.

    Reaches into lightbulb's private loadable list because there is no public
    accessor: the alternative is standing up a real client and dispatching, which is
    far more machinery than "call the listener" deserves.
    """
    loadable = next(
        item
        for item in loader._loadables
        if h.StartedEvent in getattr(item, "_event_types", ())
    )
    return loadable._callback._func


def _invoke(command: lb.SlashCommand, *args: object, **kwargs: object):
    """Call a command's @lb.invoke callback directly, past the DI wrapper."""
    return type(command).invoke._func(command, *args, **kwargs)


def _responded_embed(ctx: MagicMock) -> h.Embed:
    ctx.respond.assert_awaited_once()
    (embed,) = ctx.respond.await_args.args
    assert isinstance(embed, h.Embed)
    return embed


def _criticals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.CRITICAL]


# --- the shared source-open helper ---------------------------------------------------
#
# Every reader (navigators, /free games) goes through open_feed_source, so these cover
# the two unusable states once; the per-reader tests below then only assert that each
# reader records the reason where its command looks for it.


async def test_open_feed_source_returns_what_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    opened = object()
    open_source = AsyncMock(return_value=opened)

    assert await utils.open_feed_source("xur", open_source) == (opened, None)
    open_source.assert_awaited_once_with(42)


async def test_open_feed_source_alerts_on_an_unset_channel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=0))
    open_source = AsyncMock()

    with caplog.at_level(logging.CRITICAL):
        result = await utils.open_feed_source("xur", open_source)

    assert result == (None, utils.FEED_UNCONFIGURED)
    open_source.assert_not_awaited()  # never "open" channel 0
    assert _criticals(caplog)


async def test_open_feed_source_can_stay_quiet_about_an_unset_channel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A reader that re-opens its source on later events passes alert_when_unset=False:
    # the state was already paged for at import, and re-paging for an unchanged state
    # adds nothing. The *reason* is still recorded — the command must still answer.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=0))

    with caplog.at_level(logging.CRITICAL):
        result = await utils.open_feed_source(
            "free_games", AsyncMock(), alert_when_unset=False
        )

    assert result == (None, utils.FEED_UNCONFIGURED)
    assert not _criticals(caplog)


@pytest.mark.parametrize("error_cls", [h.NotFoundError, h.ForbiddenError])
async def test_open_feed_source_alerts_on_an_unreachable_channel(
    error_cls: type[h.NotFoundError] | type[h.ForbiddenError],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Deleted (404) and no-longer-visible (403) are the same story to a reader, and
    # both are alerted even when the unset state isn't: this one is new information.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    error = error_cls(url="", headers={}, raw_body=b"")

    with caplog.at_level(logging.CRITICAL):
        result = await utils.open_feed_source(
            "free_games",
            AsyncMock(side_effect=error),
            alert_when_unset=False,
        )

    assert result == (None, utils.FEED_UNREACHABLE)
    assert _criticals(caplog)


# --- /autopost <feed> ----------------------------------------------------------------


async def test_follow_control_refuses_an_unconfigured_feed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The important half is what does NOT happen: no mirror row is written against
    # channel 0, which would be a subscription that can never deliver anything.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=0))
    enable = AsyncMock()
    monkeypatch.setattr(autoposts, "_enable_autopost", enable)

    command_cls = autoposts.follow_control_command_maker("xur", "Xur auto posts")
    command = command_cls()
    command.option = 1
    command.ping_role = None
    ctx = _ctx()

    with caplog.at_level(logging.CRITICAL):
        await _invoke(command, ctx, bot=MagicMock())

    enable.assert_not_awaited()
    assert "isn't available" in (_responded_embed(ctx).title or "")
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)


# --- navigator commands --------------------------------------------------------------


async def test_navigator_command_answers_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = nav.NavPagesHolder("xur", nav.NavPages, {})
    holder.unavailable = utils.FEED_UNREACHABLE
    command_cls = nav.make_navigator_command(
        holder, name="xur", description="d", feed="xur"
    )
    ctx = _ctx()

    await _invoke(command_cls(), ctx)

    embed = _responded_embed(ctx)
    assert "Xûr" in (embed.title or "")
    assert utils.FEED_UNREACHABLE in (embed.description or "")


async def test_setup_nav_pages_records_an_unconfigured_feed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=0))
    loader = lb.Loader()
    holder = nav.setup_nav_pages(loader, feed="xur")

    with caplog.at_level(logging.CRITICAL):
        await _started_listener(loader)(MagicMock())

    assert holder.pages is None
    assert holder.unavailable == utils.FEED_UNCONFIGURED
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)


async def test_setup_nav_pages_records_an_unreachable_channel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))

    class _Pages:
        @classmethod
        async def from_channel(cls, *_args: object, **_kwargs: object) -> object:
            raise h.NotFoundError(url="", headers={}, raw_body=b"")

    loader = lb.Loader()
    holder = nav.setup_nav_pages(
        loader,
        feed="xur",
        pages_cls=_Pages,
    )
    with caplog.at_level(logging.CRITICAL):
        await _started_listener(loader)(MagicMock())

    assert holder.pages is None
    assert holder.unavailable == utils.FEED_UNREACHABLE
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)


# --- /free games ---------------------------------------------------------------------


async def test_free_games_answers_when_it_has_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(free_games, "last_message_in_channel", None)
    monkeypatch.setattr(free_games, "unavailable_reason", utils.FEED_UNCONFIGURED)
    ctx = _ctx()

    command = free_games.FreeGames()
    await _invoke(command, ctx)

    assert "Free Games" in (_responded_embed(ctx).title or "")


async def test_free_games_records_an_unset_channel_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=0))
    monkeypatch.setattr(free_games, "unavailable_reason", None)

    with caplog.at_level(logging.CRITICAL):
        await free_games.refresh_message_for_command(MagicMock())

    assert free_games.unavailable_reason == utils.FEED_UNCONFIGURED
    # Refreshing happens repeatedly (every delete of the repeated message), so this
    # path must not re-page for a state resolve_followable_channel already alerted on.
    assert not _criticals(caplog)


async def test_free_games_records_an_unreachable_channel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    monkeypatch.setattr(free_games, "unavailable_reason", None)
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(
        side_effect=h.ForbiddenError(url="", headers={}, raw_body=b"")
    )

    with caplog.at_level(logging.CRITICAL):
        await free_games.refresh_message_for_command(bot)

    assert free_games.unavailable_reason == utils.FEED_UNREACHABLE
    assert _criticals(caplog)


# --- registration ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_portal_ops_registers_its_commands_with_no_channel_configured() -> None:
    # Regression: this module used to register nothing at all when its channel was
    # unset, so `/portal ops` and `/autopost portal_ops` silently did not exist —
    # indistinguishable, from the outside, from the bot being broken. The test suite
    # imports extensions with no followable configured, so this asserts the state the
    # rest of the file is about.
    from dd.beacon.extensions import portal_ops

    assert portal_ops.FOLLOWABLE_CHANNEL == 0
    assert set(portal_ops.portal_command_group.subcommands) == {"ops"}
