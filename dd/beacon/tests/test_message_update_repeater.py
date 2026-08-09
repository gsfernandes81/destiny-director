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

"""Unit tests for ``message_update_repeater_impl``'s watcher gate (no DB / Discord).

Reconciling an edit always bumps the ledger, but the drain watcher (and the fresh
fan-out to newly-added dests) is gated on ``bump_for_edit``'s delivered-baseline flag.
This is the guard that stops the publish/crosspost transition — Discord reports it as a
MessageUpdateEvent while nothing has been delivered yet — from surfacing a phantom
"update" run. These pin: no watcher when the ledger write gave up, no watcher before
first delivery, and exactly one UPDATE watcher once a baseline exists.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from dd.beacon.extensions import mirror
from dd.beacon.mirror_core import MirrorOperationType, RunView

pytestmark = pytest.mark.asyncio


def _wire(
    monkeypatch: pytest.MonkeyPatch, *, write_result: object
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Stub the ledger write, drain watcher, worker; return (bot, watcher, worker)."""
    monkeypatch.setattr(
        mirror, "_ledger_write_with_retry", AsyncMock(return_value=write_result)
    )
    watcher = MagicMock()  # start_drain_watcher is synchronous (it spawns a task)
    monkeypatch.setattr(mirror, "start_drain_watcher", watcher)
    worker = MagicMock()
    monkeypatch.setattr(mirror, "mirror_worker", worker)

    bot = MagicMock()
    bot.rest.fetch_message = AsyncMock(return_value=MagicMock())
    return bot, watcher, worker


async def _run(bot: MagicMock) -> None:
    await mirror.message_update_repeater_impl(MagicMock(), bot)


async def test_no_watcher_when_ledger_write_gave_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, watcher, worker = _wire(monkeypatch, write_result=None)
    await _run(bot)
    watcher.assert_not_called()
    worker.nudge.assert_not_called()


async def test_no_watcher_when_not_an_enqueued_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bump_for_edit found nothing to reconcile: (bumped, inserted, baseline) all falsy.
    bot, watcher, worker = _wire(monkeypatch, write_result=(0, 0, False))
    await _run(bot)
    watcher.assert_not_called()
    worker.nudge.assert_not_called()


async def test_no_watcher_before_first_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rows bumped, but nothing delivered yet — the edit folds into the pending send and
    # no watcher runs (this is exactly the publish-transition state).
    bot, watcher, worker = _wire(monkeypatch, write_result=(3, 0, False))
    await _run(bot)
    watcher.assert_not_called()
    worker.nudge.assert_not_called()


async def test_starts_single_update_watcher_once_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, watcher, worker = _wire(monkeypatch, write_result=(11, 1, True))
    await _run(bot)

    watcher.assert_called_once()
    view = watcher.call_args.args[0]
    assert isinstance(view, RunView)
    assert view.op is MirrorOperationType.UPDATE
    worker.nudge.assert_called_once()


async def test_ledger_write_is_the_edit_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = AsyncMock(return_value=(1, 0, True))
    monkeypatch.setattr(mirror, "_ledger_write_with_retry", write)
    monkeypatch.setattr(mirror, "start_drain_watcher", MagicMock())
    monkeypatch.setattr(mirror, "mirror_worker", MagicMock())
    bot = MagicMock()
    bot.rest.fetch_message = AsyncMock(return_value=MagicMock())

    await _run(bot)

    # The single durable write is tagged "edit" (its retry alerts read this label).
    assert write.await_args is not None
    assert write.await_args.args[0] == "edit"
