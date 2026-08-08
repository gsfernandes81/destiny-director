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

"""Unit tests for ``handle_waiting_for_crosspost``'s terminal-on-permanent behaviour.

A permanent fetch error (e.g. 403 Missing Access on the source message) must make the
crosspost wait give up immediately instead of retrying — and re-logging a full
traceback — roughly every 30s forever. Transient errors must still retry.

Also covers the source channel's name being resolved once for the whole wait rather
than per retry — the retry loop runs for up to 12h and ``followable_name`` rebuilds the
entire feed→channel dict on every call.
"""

import asyncio
import typing as t
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import hikari as h
import pytest

from dd.beacon.extensions import mirror

pytestmark = pytest.mark.asyncio

SRC_CHANNEL = 864935509229699084
MSG_ID = 1519386646641119484


def _forbidden(code: int = 50001) -> h.ForbiddenError:
    return h.ForbiddenError(
        url="https://x", headers={}, raw_body="", message="Missing Access", code=code
    )


def _fake_bot(fetch_side_effect: object) -> MagicMock:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.fetch_message = AsyncMock(side_effect=fetch_side_effect)
    bot.wait_for = AsyncMock()
    return bot


async def _run(bot: MagicMock) -> None:
    msg = SimpleNamespace(channel_id=SRC_CHANNEL, id=MSG_ID)
    channel = SimpleNamespace(id=SRC_CHANNEL)
    # Cap at 1s: the old buggy code would sleep ~30s before its first retry, so a
    # prompt return proves the loop did not spin.
    await asyncio.wait_for(
        mirror.handle_waiting_for_crosspost(
            t.cast("h.Message", msg),
            t.cast("mirror.CachedFetchBot", bot),
            t.cast("h.TextableChannel", channel),
            wait_for_crosspost=True,
        ),
        timeout=1.0,
    )


async def test_permanent_error_returns_without_looping_or_alerting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = AsyncMock()
    monkeypatch.setattr(mirror, "discord_error_logger", alert)

    bot = _fake_bot(_forbidden())
    await _run(bot)

    bot.rest.fetch_message.assert_awaited_once()  # did not loop
    bot.wait_for.assert_not_awaited()  # never reached the crosspost wait
    alert.assert_not_awaited()  # no Discord alert flood on the permanent path


async def test_transient_error_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())  # instant backoff
    monkeypatch.setattr(mirror, "discord_error_logger", AsyncMock())

    # A transient ConnectionError once, then an already-crossposted message so the
    # wait branch is skipped and the loop breaks.
    crossposted = SimpleNamespace(
        channel_id=SRC_CHANNEL, id=MSG_ID, flags=h.MessageFlag.CROSSPOSTED
    )
    bot = _fake_bot([ConnectionError("boom"), crossposted])
    await _run(bot)

    assert bot.rest.fetch_message.await_count == 2  # retried after the transient error
    bot.wait_for.assert_not_awaited()  # already crossposted -> no wait


async def test_followable_name_resolved_once_per_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two loop iterations (one transient failure, then success) plus the permanent-path
    # and give-up log lines must all share a single name lookup: the name cannot change
    # mid-wait, and each lookup rebuilds every followable's channel id.
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())  # instant backoff
    monkeypatch.setattr(mirror, "discord_error_logger", AsyncMock())
    lookups = MagicMock(return_value="lost_sector")
    monkeypatch.setattr(mirror.settings, "followable_name", lookups)

    crossposted = SimpleNamespace(
        channel_id=SRC_CHANNEL, id=MSG_ID, flags=h.MessageFlag.CROSSPOSTED
    )
    bot = _fake_bot([ConnectionError("boom"), crossposted])
    await _run(bot)

    assert bot.rest.fetch_message.await_count == 2  # the loop did iterate twice
    lookups.assert_called_once_with(id=SRC_CHANNEL)


async def test_transient_gives_up_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A sustained *transient* fetch failure must stop at the wait ceiling instead of
    # retrying (and re-alerting) forever, like the crosspost wait_for's own timeout.
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())  # instant backoff
    alert = AsyncMock()
    monkeypatch.setattr(mirror, "discord_error_logger", alert)
    # A fake clock that advances past the (tiny) ceiling so the deadline elapses after
    # one transient retry rather than the test looping forever.
    ticks = iter([0.0, 5.0, 10.0, 15.0])
    monkeypatch.setattr(mirror, "perf_counter", lambda: next(ticks, 999.0))
    monkeypatch.setattr(mirror, "_CROSSPOST_WAIT_CEILING_SECONDS", 10)

    bot = _fake_bot(ConnectionError("boom"))  # always transient
    await _run(bot)  # returns (gives up) instead of hanging

    assert bot.rest.fetch_message.await_count == 1  # one attempt, then the ceiling hit
    assert alert.await_count == 1  # one alert, not a flood
    bot.wait_for.assert_not_awaited()  # never reached the publish wait
