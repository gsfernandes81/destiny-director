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

"""Unit tests for ``_post_run_summary_line`` — the one-line Discord confirmation a
drained mirror run posts to the log channel. Must be explicitly inert (never even
attempt a fetch) when no log channel is configured, not just accidentally silent
because ``fetch_channel(0)`` happens to raise into the surrounding suppress."""

from time import perf_counter
from unittest.mock import AsyncMock, MagicMock

import hikari as h
import pytest

from dd.beacon.extensions import mirror
from dd.beacon.mirror_core import MirrorOperationType, RunCounts, RunView

pytestmark = pytest.mark.asyncio


def _view() -> RunView:
    view = RunView(
        op=MirrorOperationType.SEND,
        src_ch_id=1,
        src_msg_id=99,
        start_time=perf_counter(),
    )
    view.counts = RunCounts(delivered=1, failed=0, cancelled=0)
    return view


async def test_no_fetch_when_log_channel_unconfigured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mirror.settings, "get_log_channel_id", AsyncMock(return_value=0)
    )
    bot = AsyncMock()

    await mirror._post_run_summary_line(bot, _view(), None)

    bot.fetch_channel.assert_not_awaited()


async def test_posts_when_log_channel_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mirror.settings, "get_log_channel_id", AsyncMock(return_value=123)
    )
    # spec= makes this MagicMock pass the isinstance(log_channel, TextableGuildChannel)
    # gate without needing a real hikari channel instance.
    channel = MagicMock(spec=h.TextableGuildChannel)
    channel.send = AsyncMock()
    bot = AsyncMock()
    bot.fetch_channel.return_value = channel

    await mirror._post_run_summary_line(bot, _view(), None)

    bot.fetch_channel.assert_awaited_once_with(123)
    channel.send.assert_awaited_once()
