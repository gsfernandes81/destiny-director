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

"""Integration tests for the web mirror-log ledger queries.

``MirrorDelivery.recent_runs`` / ``run_rows`` back the anchor ``/mirror-logs`` page.
Exercises the per-source aggregation (state + crosspost tallies, run timing), the
created_at window + limit, the failures-first detail ordering, and — the cross-dialect
gotcha the design flagged — that the MIN/MAX datetime aggregates come back as
``datetime`` objects (via the explicit ``type_=DateTime``), not ISO strings.
"""

import datetime as dt

import pytest
import pytest_asyncio

from dd.common import schemas
from dd.common.schemas import (
    CrosspostState,
    DeliveryState,
    MirrorDelivery,
    MirroredChannel,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    await schemas.destroy_all()
    await schemas.create_all()
    MirroredChannel._legacy_srcs_cache.clear()
    yield


async def _insert(rows: list[dict]) -> None:
    async with schemas.db_session() as session, session.begin():
        for r in rows:
            session.add(MirrorDelivery(**r))


def _row(
    src_msg_id: int,
    dest_ch_id: int,
    *,
    state: str,
    created_at: dt.datetime,
    src_ch_id: int = 1,
    crosspost_state: str = CrosspostState.NOT_APPLICABLE.value,
    dest_msg_id: int | None = None,
    desired_version: int = 1,
    applied_version: int = 1,
    attempts: int = 0,
    finished_at: dt.datetime | None = None,
    deleted: bool = False,
    last_error_ref: str | None = None,
    last_error_class: str | None = None,
    last_error_msg: str | None = None,
) -> dict:
    return dict(
        src_msg_id=src_msg_id,
        dest_ch_id=dest_ch_id,
        src_ch_id=src_ch_id,
        state=state,
        created_at=created_at,
        crosspost_state=crosspost_state,
        dest_msg_id=dest_msg_id,
        desired_version=desired_version,
        applied_version=applied_version,
        attempts=attempts,
        due_at=created_at,
        finished_at=finished_at,
        deleted=deleted,
        last_error_ref=last_error_ref,
        last_error_class=last_error_class,
        last_error_msg=last_error_msg,
    )


# -- recent_runs -------------------------------------------------------------


async def test_recent_runs_aggregates_and_orders_newest_first() -> None:
    now = schemas._utcnow()
    older = now - dt.timedelta(hours=6)
    D = DeliveryState.DELIVERED.value
    F = DeliveryState.FAILED.value
    await _insert(
        [
            # Run A (older): 3 delivered, 1 crosspost DONE + 1 PENDING.
            _row(
                100,
                1,
                state=D,
                created_at=older,
                dest_msg_id=11,
                crosspost_state=CrosspostState.DONE.value,
                finished_at=older,
            ),
            _row(
                100,
                2,
                state=D,
                created_at=older,
                dest_msg_id=12,
                crosspost_state=CrosspostState.PENDING.value,
                finished_at=older,
            ),
            _row(100, 3, state=D, created_at=older, dest_msg_id=13, finished_at=older),
            # Run B (newer): 1 delivered, 1 failed.
            _row(200, 1, state=D, created_at=now, dest_msg_id=21, finished_at=now),
            _row(
                200,
                2,
                state=F,
                created_at=now,
                finished_at=now,
                last_error_ref="PERM01",
            ),
        ]
    )

    runs = await MirrorDelivery.recent_runs(within_days=30)
    by_id = {r["src_msg_id"]: r for r in runs}

    assert [r["src_msg_id"] for r in runs] == [200, 100]  # newest run first
    assert (by_id[100]["total"], by_id[100]["delivered"], by_id[100]["failed"]) == (
        3,
        3,
        0,
    )
    assert by_id[100]["crosspost_done"] == 1
    assert by_id[100]["crosspost_pending"] == 1
    assert (by_id[200]["delivered"], by_id[200]["failed"], by_id[200]["pending"]) == (
        1,
        1,
        0,
    )


async def test_recent_runs_datetime_aggregates_are_datetimes() -> None:
    # The MIN/MAX over created_at/finished_at must return datetime, not an ISO string —
    # this is what the type_=DateTime on the aggregates guards (SQLite would otherwise
    # hand back a string and the web layer's UTC stamping would break).
    now = schemas._utcnow()
    await _insert(
        [
            _row(
                1,
                1,
                state=DeliveryState.DELIVERED.value,
                created_at=now,
                finished_at=now,
            )
        ]
    )

    (run,) = await MirrorDelivery.recent_runs(within_days=30)

    assert isinstance(run["started"], dt.datetime)
    assert isinstance(run["last_at"], dt.datetime)


async def test_recent_runs_respects_window_and_limit() -> None:
    now = schemas._utcnow()
    await _insert(
        [
            _row(1, 1, state=DeliveryState.DELIVERED.value, created_at=now),
            _row(
                2,
                1,
                state=DeliveryState.DELIVERED.value,
                created_at=now - dt.timedelta(hours=1),
            ),
            # Outside the 30-day window — must be excluded.
            _row(
                3,
                1,
                state=DeliveryState.DELIVERED.value,
                created_at=now - dt.timedelta(days=40),
            ),
        ]
    )

    windowed = await MirrorDelivery.recent_runs(within_days=30)
    assert {r["src_msg_id"] for r in windowed} == {1, 2}  # old run excluded

    limited = await MirrorDelivery.recent_runs(within_days=30, limit=1)
    assert [r["src_msg_id"] for r in limited] == [1]  # newest only


# -- run_rows ----------------------------------------------------------------


async def test_run_rows_failures_first_with_error_detail() -> None:
    now = schemas._utcnow()
    await _insert(
        [
            _row(
                500,
                10,
                state=DeliveryState.DELIVERED.value,
                created_at=now,
                dest_msg_id=99,
            ),
            _row(
                500,
                20,
                state=DeliveryState.FAILED.value,
                created_at=now,
                last_error_ref="PERM01",
                last_error_class="PERMANENT",
                last_error_msg="Missing Access",
            ),
            _row(500, 30, state=DeliveryState.PENDING.value, created_at=now),
        ]
    )

    rows = await MirrorDelivery.run_rows(500)

    assert rows[0]["state"] == "FAILED"  # failures sort first
    assert rows[0]["error_ref"] == "PERM01"
    assert rows[0]["error_class"] == "PERMANENT"
    assert rows[0]["error_msg"] == "Missing Access"
    delivered = next(r for r in rows if r["state"] == "DELIVERED")
    assert delivered["dest_msg_id"] == 99
    pending = next(r for r in rows if r["state"] == "PENDING")
    assert pending["dest_msg_id"] is None  # never delivered → no dest message
    # No mirror config for these dests → dest_server_id is None (bare-id fallback).
    assert all(r["dest_server_id"] is None for r in rows)


async def test_run_rows_dest_server_id_from_mirror_config() -> None:
    now = schemas._utcnow()
    await _insert([_row(600, 42, state=DeliveryState.DELIVERED.value, created_at=now)])
    # A matching mirror config row supplies the destination's guild id for the link.
    async with schemas.db_session() as session, session.begin():
        session.add(
            MirroredChannel(
                src_id=1,
                dest_id=42,
                dest_server_id=9001,
                legacy=True,
                enabled=True,
                role_mention_id=None,
            )
        )

    (row,) = await MirrorDelivery.run_rows(600)
    assert row["dest_server_id"] == 9001  # joined on the exact (src, dest) pair


async def test_run_rows_caps_at_limit() -> None:
    now = schemas._utcnow()
    await _insert(
        [
            _row(700, dest, state=DeliveryState.DELIVERED.value, created_at=now)
            for dest in range(1, 6)
        ]
    )

    rows = await MirrorDelivery.run_rows(700, limit=3)
    assert len(rows) == 3  # capped
