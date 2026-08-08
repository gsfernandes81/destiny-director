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

"""Unit tests for the card-free drain watcher that replaces the progress card.

The watcher polls the ledger until a run drains, then runs ``_log_run_summary`` (the
failure escalation / ALERTS push — the one behaviour that must NOT regress when the card
goes away) and posts a single never-edited result line. These pin: summary + line on
drain, no finalisation on an empty ledger, the lifetime cap (no summary), that the
failure push fires even if the cosmetic line send throws, and that a second event for a
live source coalesces (one watcher) while a finished watcher is replaced.
"""

import asyncio as aio
from time import perf_counter
from unittest.mock import AsyncMock, MagicMock

import hikari as h
import pytest

from dd.beacon.extensions import mirror
from dd.beacon.mirror_core import MirrorOperationType, RunCounts, RunView
from dd.common.schemas import DeliveryState

pytestmark = pytest.mark.asyncio

SRC = 31337
# The log channel must be explicitly *inert* (no fetch at all) when unconfigured — see
# dd.common.settings — so every test here that expects a result line configures one.
_LOG_CHANNEL_ID = 555


@pytest.fixture(autouse=True)
def _log_channel_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mirror.settings, "get_log_channel_id", AsyncMock(return_value=_LOG_CHANNEL_ID)
    )


def _view() -> RunView:
    view = RunView(
        op=MirrorOperationType.SEND,
        src_ch_id=1,
        src_msg_id=SRC,
        start_time=perf_counter(),
    )
    view.counts = RunCounts()
    return view


def _bot_with_log_channel() -> tuple[MagicMock, AsyncMock]:
    send = AsyncMock()
    channel = MagicMock(spec=h.TextableGuildChannel)
    channel.send = send
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot, send


async def test_summary_and_result_line_on_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mirror.MirrorDelivery,
        "state_counts",
        AsyncMock(return_value={DeliveryState.DELIVERED.value: 3}),
    )
    summary = MagicMock()
    monkeypatch.setattr(mirror, "_log_run_summary", summary)
    bot, send = _bot_with_log_channel()

    await mirror._run_drain_watcher(bot, _view())

    summary.assert_called_once()  # failure escalation / summary path still fires
    send.assert_awaited_once()  # one result line posted
    assert send.await_args is not None
    assert "✅" in send.await_args.args[0]  # clean run


async def test_does_not_finalize_on_empty_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty count reads is_complete (no PENDING) but total == 0 — the watcher must
    # keep polling and only finalise once real rows drain.
    monkeypatch.setattr(
        mirror.MirrorDelivery,
        "state_counts",
        AsyncMock(side_effect=[{}, {DeliveryState.DELIVERED.value: 2}]),
    )
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())  # instant poll
    summary = MagicMock()
    monkeypatch.setattr(mirror, "_log_run_summary", summary)
    bot, send = _bot_with_log_channel()

    await mirror._run_drain_watcher(bot, _view())

    summary.assert_called_once()  # finalised only after rows appeared
    send.assert_awaited_once()


async def test_lifetime_cap_incomplete_does_not_summarise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mirror, "_WATCHER_MAX_LIFETIME", -1)  # already past
    monkeypatch.setattr(
        mirror.MirrorDelivery,
        "state_counts",
        AsyncMock(return_value={DeliveryState.PENDING.value: 3}),  # never completes
    )
    summary = MagicMock()
    monkeypatch.setattr(mirror, "_log_run_summary", summary)
    bot, send = _bot_with_log_channel()

    await mirror._run_drain_watcher(bot, _view())

    summary.assert_not_called()  # a stuck run is not summarised
    send.assert_not_awaited()  # and no result line


async def test_failure_push_survives_a_broken_result_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure escalation must fire even if posting the cosmetic line throws.
    monkeypatch.setattr(
        mirror.MirrorDelivery,
        "state_counts",
        AsyncMock(
            return_value={
                DeliveryState.DELIVERED.value: 8,
                DeliveryState.FAILED.value: 2,
            }
        ),
    )
    summary = MagicMock()
    monkeypatch.setattr(mirror, "_log_run_summary", summary)
    bot = MagicMock()
    channel = MagicMock(spec=h.TextableGuildChannel)
    channel.send = AsyncMock(side_effect=RuntimeError("log channel gone"))
    bot.fetch_channel = AsyncMock(return_value=channel)

    await mirror._run_drain_watcher(bot, _view())  # must not raise

    summary.assert_called_once()  # push fired before (and despite) the send failure


async def test_result_line_flags_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mirror.MirrorDelivery,
        "state_counts",
        AsyncMock(
            return_value={
                DeliveryState.DELIVERED.value: 8,
                DeliveryState.FAILED.value: 2,
            }
        ),
    )
    monkeypatch.setattr(mirror, "_log_run_summary", MagicMock())
    bot, send = _bot_with_log_channel()

    await mirror._run_drain_watcher(bot, _view())

    assert send.await_args is not None
    line = send.await_args.args[0]
    assert "⚠️" in line and "2 failed" in line


async def test_start_coalesces_while_live(monkeypatch: pytest.MonkeyPatch) -> None:
    mirror._watchers.pop(SRC, None)

    async def fake_watcher(_bot: object, _view: RunView, **_kw: object) -> None:
        await aio.sleep(3600)

    monkeypatch.setattr(mirror, "_run_drain_watcher", fake_watcher)

    mirror.start_drain_watcher(MagicMock(), _view())
    first = mirror._watchers[SRC]
    mirror.start_drain_watcher(MagicMock(), _view())  # coalesces — no new task
    second = mirror._watchers[SRC]

    try:
        assert first is second  # the live watcher was reused, not superseded
        assert not first.cancelled()  # coalescing never cancels (no freeze surface)
    finally:
        first.cancel()
        mirror._watchers.pop(SRC, None)


async def test_finished_watcher_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    mirror._watchers.pop(SRC, None)

    async def instant(_bot: object, _view: RunView, **_kw: object) -> None:
        return

    monkeypatch.setattr(mirror, "_run_drain_watcher", instant)
    mirror.start_drain_watcher(MagicMock(), _view())
    first = mirror._watchers.get(SRC)
    if first is not None:
        await first
        await aio.sleep(0)  # let the done-callback evict it

    async def long(_bot: object, _view: RunView, **_kw: object) -> None:
        await aio.sleep(3600)

    monkeypatch.setattr(mirror, "_run_drain_watcher", long)
    mirror.start_drain_watcher(MagicMock(), _view())  # spawns fresh
    second = mirror._watchers[SRC]

    try:
        assert second is not first  # a finished watcher is replaced, not coalesced into
    finally:
        second.cancel()
        mirror._watchers.pop(SRC, None)
