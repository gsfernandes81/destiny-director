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

"""Picking or moving a feed's channel takes effect without restarting beacon.

A followable's channel is a setting edited on anchor's Autopost Settings page, so it
can change while beacon runs. Most readers already resolve it per use; the two that
hold something *built* from it — a ``NavPages`` over the channel's history, and free
games' cached last message — used to be built once on ``StartedEvent`` and never
revisited. These cover the reconciler that fixes that, and the three properties that
keep it from being worse than the bug: it does no work when nothing changed, it never
leaves a feed briefly unavailable while rebuilding, and it pages once per broken
channel rather than once per tick.
"""

import logging
import typing as t
from unittest.mock import AsyncMock, MagicMock

import hikari as h
import lightbulb as lb
import pytest

from dd.beacon import nav, utils
from dd.beacon.extensions import free_games
from dd.common import settings

pytestmark = pytest.mark.asyncio


def _started_listener(loader: lb.Loader):
    """The StartedEvent callback a Loader just had registered on it."""
    loadable = next(
        item
        for item in loader._loadables
        if h.StartedEvent in getattr(item, "_event_types", ())
    )
    return loadable._callback._func


def _criticals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.CRITICAL]


class _FakePages:
    """Stands in for a NavPages: records the channel it was built from, and whether it
    was torn down."""

    built: t.ClassVar[list[int]] = []
    fail_for: t.ClassVar[set[int]] = set()

    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id
        self.torn_down = False

    @classmethod
    async def from_channel(
        cls, _bot: object, channel_id: int, **_kwargs: object
    ) -> "_FakePages":
        cls.built.append(channel_id)
        if channel_id in cls.fail_for:
            raise h.NotFoundError(url="", headers={}, raw_body=b"")
        return cls(channel_id)

    def teardown(self) -> None:
        self.torn_down = True


@pytest.fixture(autouse=True)
def _reset_fake_pages() -> t.Iterator[None]:
    _FakePages.built = []
    _FakePages.fail_for = set()
    yield


class _Configured:
    """A stand-in for ``settings.get_followable_channel`` a test can move.

    Stateful rather than a list of return values: the code under test reads the setting
    a variable number of times per pass, so a consuming sequence would make what a test
    asserts depend on how many reads the implementation happens to do.
    """

    def __init__(self, value: int = 0) -> None:
        self.value = value

    async def __call__(self, _feed: str) -> int:
        return self.value


async def _start(loader: lb.Loader) -> None:
    await _started_listener(loader)(MagicMock())


# --- the registry --------------------------------------------------------------------


async def test_a_tick_over_an_unchanged_channel_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The property that makes polling affordable: rebuilding reads a whole channel
    # history, so a tick that finds the channel unmoved must not call on_change at all.
    on_change = AsyncMock()
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    utils.watch_feed_channel("xur", lambda: 42, on_change)

    await utils.reconcile_feed_channels()

    on_change.assert_not_awaited()


async def test_a_changed_channel_reaches_the_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_change = AsyncMock()
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=99))
    utils.watch_feed_channel("xur", lambda: 42, on_change)

    await utils.reconcile_feed_channels()

    on_change.assert_awaited_once_with(99)


async def test_one_failing_watcher_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Watchers are independent readers; a reader that blows up must not cost the rest
    # of them their reconcile for the whole life of the process.
    survivor = AsyncMock()
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=99))
    utils.watch_feed_channel("xur", lambda: 0, AsyncMock(side_effect=RuntimeError("x")))
    utils.watch_feed_channel("ada", lambda: 0, survivor)

    with caplog.at_level(logging.ERROR):
        await utils.reconcile_feed_channels()

    survivor.assert_awaited_once_with(99)
    assert any("xur" in r.getMessage() for r in caplog.records)


# --- nav pages -----------------------------------------------------------------------


async def test_nav_pages_rebuild_when_the_channel_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _Configured(42)
    monkeypatch.setattr(settings, "get_followable_channel", configured)
    loader = lb.Loader()
    holder = nav.setup_nav_pages(loader, feed="xur", pages_cls=_FakePages)
    await _start(loader)
    first = holder.pages
    assert isinstance(first, _FakePages)
    assert holder.channel_id == 42

    configured.value = 99
    await utils.reconcile_feed_channels()

    assert _FakePages.built == [42, 99]
    assert holder.channel_id == 99
    assert holder.pages is not first
    # Build-then-swap-then-tear-down: the old instance is released only once it has
    # been replaced, or every rebuild would leak its history listener + lookahead task.
    assert first.torn_down


async def test_nav_pages_are_left_alone_when_the_channel_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    loader = lb.Loader()
    holder = nav.setup_nav_pages(loader, feed="xur", pages_cls=_FakePages)
    await _start(loader)
    built = holder.pages

    await utils.reconcile_feed_channels()
    await utils.reconcile_feed_channels()

    assert _FakePages.built == [42]
    assert holder.pages is built


async def test_a_feed_configured_after_boot_becomes_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The headline case: beacon booted with nothing configured, so its command answered
    # "unavailable". Picking a channel on the settings page used to need a restart.
    configured = _Configured(0)
    monkeypatch.setattr(settings, "get_followable_channel", configured)
    loader = lb.Loader()
    holder = nav.setup_nav_pages(loader, feed="xur", pages_cls=_FakePages)
    await _start(loader)
    assert holder.pages is None
    assert holder.unavailable == utils.FEED_UNCONFIGURED

    configured.value = 42
    await utils.reconcile_feed_channels()

    assert isinstance(holder.pages, _FakePages)
    assert holder.unavailable is None
    assert holder.channel_id == 42


async def test_an_unconfigured_feed_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # channel_id 0 against a configured 0 is agreement, not a pending rebuild — a feed
    # nobody has set up must cost nothing per tick.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=0))
    loader = lb.Loader()
    holder = nav.setup_nav_pages(loader, feed="xur", pages_cls=_FakePages)
    await _start(loader)

    await utils.reconcile_feed_channels()
    await utils.reconcile_feed_channels()

    assert _FakePages.built == []
    assert holder.channel_id == 0


async def test_an_unreachable_channel_is_retried_quietly_and_self_heals(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A failed open leaves channel_id 0, so the reconciler tries again — a transient
    # Discord blip at swap time must not cost the feed until the next restart. The
    # retries stay quiet: a permanently deleted channel would otherwise page the owners
    # once a minute, forever.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    _FakePages.fail_for = {42}
    loader = lb.Loader()
    holder = nav.setup_nav_pages(loader, feed="xur", pages_cls=_FakePages)

    with caplog.at_level(logging.CRITICAL):
        await _start(loader)
        assert len(_criticals(caplog)) == 1

        await utils.reconcile_feed_channels()
        await utils.reconcile_feed_channels()

    assert _FakePages.built == [42, 42, 42]
    assert len(_criticals(caplog)) == 1

    _FakePages.fail_for = set()
    await utils.reconcile_feed_channels()

    assert isinstance(holder.pages, _FakePages)
    assert holder.unavailable is None


async def test_a_newly_broken_channel_pages_once_more(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Quiet retries are per channel, not forever: moving the feed to a *different*
    # broken channel is new information and has to reach the owners.
    configured = _Configured(42)
    monkeypatch.setattr(settings, "get_followable_channel", configured)
    _FakePages.fail_for = {99}
    loader = lb.Loader()
    nav.setup_nav_pages(loader, feed="xur", pages_cls=_FakePages)
    await _start(loader)

    configured.value = 99
    with caplog.at_level(logging.CRITICAL):
        await utils.reconcile_feed_channels()

    assert len(_criticals(caplog)) == 1


async def test_reopen_before_the_bot_starts_is_a_no_op() -> None:
    # The reconciler loop starts on StartedEvent alongside the builds, so a tick can in
    # principle land before one; there is nothing to fetch a channel with yet.
    holder = nav.NavPagesHolder("xur", _FakePages, {})

    await holder.reopen(42)

    assert _FakePages.built == []
    assert holder.pages is None


# --- /free games ---------------------------------------------------------------------


def _channel_with_history(channel_id: int, message_ids: list[int]) -> MagicMock:
    channel = MagicMock(spec=h.GuildTextChannel)
    channel.id = channel_id

    async def _history() -> t.AsyncIterator[MagicMock]:
        for message_id in message_ids:
            message = MagicMock()
            message.id = message_id
            yield message

    channel.fetch_history = _history
    return channel


async def test_free_games_rereads_history_when_the_channel_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The message listeners already watch whichever channel is configured now, but they
    # only bring news of a *new* message — so moving the feed to a channel that already
    # has posts left /free games repeating the old one's.
    configured = _Configured(42)
    monkeypatch.setattr(settings, "get_followable_channel", configured)
    monkeypatch.setattr(free_games, "last_message_in_channel", None)
    monkeypatch.setattr(free_games, "last_message_in_channel_id", 0)
    monkeypatch.setattr(free_games, "last_message_channel_id", 0)
    monkeypatch.setattr(free_games, "unavailable_reason", None)

    bot = MagicMock()
    bot.fetch_channel = AsyncMock(
        side_effect=[
            _channel_with_history(42, [1001]),
            _channel_with_history(99, [2002]),
        ]
    )
    monkeypatch.setattr(free_games, "_bot", bot)
    utils.watch_feed_channel(
        "free_games",
        lambda: free_games.last_message_channel_id,
        free_games._on_channel_change,
    )

    await free_games.refresh_message_for_command(bot)
    assert free_games.last_message_in_channel_id == 1001

    configured.value = 99
    await utils.reconcile_feed_channels()

    assert free_games.last_message_in_channel_id == 2002
    assert free_games.last_message_channel_id == 99


async def test_free_games_drops_the_old_message_when_the_new_channel_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Otherwise /free games would go on repeating a post out of a channel it no longer
    # reads — worse than saying it has nothing, because it looks right.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=99))
    monkeypatch.setattr(free_games, "last_message_in_channel", MagicMock())
    monkeypatch.setattr(free_games, "last_message_in_channel_id", 1001)
    monkeypatch.setattr(free_games, "last_message_channel_id", 42)
    monkeypatch.setattr(free_games, "unavailable_reason", None)

    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=_channel_with_history(99, []))

    await free_games.refresh_message_for_command(bot)

    assert free_games.last_message_in_channel is None
    assert free_games.last_message_in_channel_id == 0
    # A reachable, empty channel is not a fault — /free games has to say which of the
    # three it is, and "the bot is still starting up" (the None fallback) is neither
    # true nor actionable.
    assert free_games.unavailable_reason == utils.FEED_EMPTY
    # Recorded regardless, or an empty-but-reachable channel would have its history
    # re-read on every single tick.
    assert free_games.last_message_channel_id == 99


async def test_free_games_keeps_its_message_when_the_same_channel_is_refreshed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # refresh_message_for_command also runs whenever the repeated message is deleted.
    # That path predates this change and must keep behaving as it did.
    monkeypatch.setattr(settings, "get_followable_channel", AsyncMock(return_value=42))
    cached = MagicMock()
    monkeypatch.setattr(free_games, "last_message_in_channel", cached)
    monkeypatch.setattr(free_games, "last_message_in_channel_id", 1001)
    monkeypatch.setattr(free_games, "last_message_channel_id", 42)
    monkeypatch.setattr(free_games, "unavailable_reason", None)

    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=_channel_with_history(42, []))

    await free_games.refresh_message_for_command(bot)

    assert free_games.last_message_in_channel is cached
    assert free_games.last_message_in_channel_id == 1001
