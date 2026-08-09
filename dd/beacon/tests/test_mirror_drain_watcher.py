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

"""Unit tests for the card-free drain watcher.

The watcher polls the ledger until a run drains, then runs ``_log_run_summary`` (the
failure escalation / ALERTS push — the one behaviour that must never regress) and
``_record_operation`` (the durable row ``/mirror-logs`` reads). These pin: both fire on
drain, no finalisation on an empty ledger, the lifetime cap suppresses both, the
escalation runs before the best-effort op-log write, and that a second event for a live
source coalesces into one watcher while a finished watcher is replaced.

The Discord result line these used to assert is gone with the log channel — the
mirror-logs page is now the only place a run is read back.
"""

import asyncio as aio
from time import perf_counter
from unittest.mock import AsyncMock, MagicMock

import pytest

from dd.beacon.extensions import mirror
from dd.beacon.mirror_core import MirrorOperationType, RunCounts, RunView
from dd.common.schemas import DeliveryState

pytestmark = pytest.mark.asyncio

SRC = 31337


def _view() -> RunView:
    view = RunView(
        op=MirrorOperationType.SEND,
        src_ch_id=1,
        src_msg_id=SRC,
        start_time=perf_counter(),
    )
    view.counts = RunCounts()
    return view


@pytest.fixture(autouse=True)
def _quiet_op_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the op-log write; the tests that care about it override this."""
    monkeypatch.setattr(mirror, "_record_operation", AsyncMock())


async def test_summarises_and_records_on_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mirror.MirrorDelivery,
        "state_counts",
        AsyncMock(return_value={DeliveryState.DELIVERED.value: 3}),
    )
    summary = MagicMock()
    record = AsyncMock()
    monkeypatch.setattr(mirror, "_log_run_summary", summary)
    monkeypatch.setattr(mirror, "_record_operation", record)

    await mirror._run_drain_watcher(_view())

    summary.assert_called_once()  # failure escalation / summary path
    record.assert_awaited_once()  # the row /mirror-logs reads


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

    await mirror._run_drain_watcher(_view())

    summary.assert_called_once()  # finalised only after rows appeared


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
    record = AsyncMock()
    monkeypatch.setattr(mirror, "_log_run_summary", summary)
    monkeypatch.setattr(mirror, "_record_operation", record)

    await mirror._run_drain_watcher(_view())

    summary.assert_not_called()  # a stuck run is not summarised
    record.assert_not_awaited()  # and leaves no settled row


async def test_failure_escalation_runs_before_the_op_log_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ordering is the invariant: the escalation must already have fired by the time the
    # best-effort op-log write is attempted, so nothing about that write can cost us a
    # page. (_record_operation swallows its own failures internally — see its docstring
    # — so the watcher itself has no suppression to assert against here.)
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
    order: list[str] = []
    monkeypatch.setattr(
        mirror,
        "_log_run_summary",
        MagicMock(side_effect=lambda _v: order.append("push")),
    )

    async def _record(_v: RunView) -> None:
        order.append("oplog")

    monkeypatch.setattr(mirror, "_record_operation", _record)

    await mirror._run_drain_watcher(_view())

    assert order == ["push", "oplog"]


async def test_start_coalesces_while_live(monkeypatch: pytest.MonkeyPatch) -> None:
    mirror._watchers.pop(SRC, None)

    async def fake_watcher(_view: RunView) -> None:
        await aio.sleep(3600)

    monkeypatch.setattr(mirror, "_run_drain_watcher", fake_watcher)

    mirror.start_drain_watcher(_view())
    first = mirror._watchers[SRC]
    mirror.start_drain_watcher(_view())  # coalesces — no new task
    second = mirror._watchers[SRC]

    try:
        assert first is second  # the live watcher was reused, not superseded
        assert not first.cancelled()  # coalescing never cancels (no freeze surface)
    finally:
        first.cancel()
        mirror._watchers.pop(SRC, None)


async def test_finished_watcher_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    mirror._watchers.pop(SRC, None)

    async def instant(_view: RunView) -> None:
        return

    monkeypatch.setattr(mirror, "_run_drain_watcher", instant)
    mirror.start_drain_watcher(_view())
    first = mirror._watchers.get(SRC)
    if first is not None:
        await first
        await aio.sleep(0)  # let the done-callback evict it

    async def long(_view: RunView) -> None:
        await aio.sleep(3600)

    monkeypatch.setattr(mirror, "_run_drain_watcher", long)
    mirror.start_drain_watcher(_view())  # spawns fresh
    second = mirror._watchers[SRC]

    try:
        assert second is not first  # a finished watcher is replaced, not coalesced into
    finally:
        second.cancel()
        mirror._watchers.pop(SRC, None)
