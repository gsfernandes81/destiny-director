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

"""Integration test for ``CommandUsage.fetch_daily`` (uses the SQLite test DB)."""

import datetime as dt

import pytest
import pytest_asyncio

from dd.common import schemas
from dd.common.schemas import CommandUsage, db_session

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    await schemas.destroy_all()
    await schemas.create_all()
    yield


async def _seed(rows: list[tuple[str, dt.date, int]]) -> None:
    # Seed via the ORM constructor rather than CommandUsage.increment: increment only
    # ever writes today's bucket with +1, and these cases need arbitrary dates/counts.
    # (increment's own upsert is covered by test_increment_upserts_in_place below.)
    async with db_session() as session, session.begin():
        session.add_all(
            CommandUsage(command_name=name, date=day, count=count)
            for name, day, count in rows
        )


@pytest.mark.asyncio
async def test_fetch_daily_filters_since_and_orders():
    d = dt.date(2026, 6, 20)
    await _seed(
        [
            ("xur", d, 5),
            ("xur", d - dt.timedelta(days=1), 3),
            ("ada", d, 2),
            ("old", d - dt.timedelta(days=10), 99),
        ]
    )

    rows = await CommandUsage.fetch_daily(since=d - dt.timedelta(days=2))

    # 'old' (10 days back) is excluded; rows ordered by name then date ascending.
    assert rows == [
        ("ada", d, 2),
        ("xur", d - dt.timedelta(days=1), 3),
        ("xur", d, 5),
    ]


@pytest.mark.asyncio
async def test_increment_upserts_in_place():
    """``increment`` inserts the day's bucket, then bumps it — no duplicate PK row.

    Covers the ON CONFLICT DO UPDATE path on whichever dialect the suite is bound to
    (SQLite by default, real Postgres under TEST_USE_POSTGRES).
    """
    today = dt.datetime.now(tz=dt.UTC).date()

    await CommandUsage.increment("xur")
    assert await CommandUsage.fetch_daily(since=today) == [("xur", today, 1)]

    await CommandUsage.increment("xur")
    await CommandUsage.increment("xur")
    await CommandUsage.increment("ada")

    assert await CommandUsage.fetch_daily(since=today) == [
        ("ada", today, 1),
        ("xur", today, 3),
    ]
