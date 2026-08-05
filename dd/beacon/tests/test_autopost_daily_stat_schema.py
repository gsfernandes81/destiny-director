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

"""Integration test for ``AutopostDailyStat.fetch_series`` (uses the SQLite test DB)."""

import datetime as dt

import pytest
import pytest_asyncio

from dd.common import schemas
from dd.common.schemas import AutopostDailyStat, db_session

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    await schemas.destroy_all()
    await schemas.create_all()
    yield


async def _seed(rows: list[tuple[dt.date, str, str, int]]) -> None:
    # Seed via the plain ORM constructor — shorter than a record() call per row, and
    # record()'s own upsert is covered by test_record_overwrites_same_day below.
    async with db_session() as session, session.begin():
        session.add_all(
            AutopostDailyStat(date=day, feed=feed, kind=kind, count=count)
            for day, feed, kind, count in rows
        )


@pytest.mark.asyncio
async def test_fetch_series_filters_since_and_orders():
    d = dt.date(2026, 6, 20)
    await _seed(
        [
            (d, "xur", "follow", 12),
            (d, "xur", "mirror", 4),
            (d - dt.timedelta(days=1), "ada", "follow", 7),
            (d - dt.timedelta(days=10), "old", "mirror", 99),
        ]
    )

    rows = await AutopostDailyStat.fetch_series(since=d - dt.timedelta(days=2))

    # 'old' (10 days back) is excluded; rows ordered by date, then feed, then kind.
    assert rows == [
        (d - dt.timedelta(days=1), "ada", "follow", 7),
        (d, "xur", "follow", 12),
        (d, "xur", "mirror", 4),
    ]


@pytest.mark.asyncio
async def test_fetch_series_no_since_returns_all():
    d = dt.date(2026, 6, 20)
    await _seed(
        [
            (d, "xur", "follow", 3),
            (d - dt.timedelta(days=30), "ada", "mirror", 1),
        ]
    )

    rows = await AutopostDailyStat.fetch_series()

    assert rows == [
        (d - dt.timedelta(days=30), "ada", "mirror", 1),
        (d, "xur", "follow", 3),
    ]


@pytest.mark.asyncio
async def test_record_overwrites_same_day():
    """``record`` upserts on the ``(date, feed, kind)`` PK, overwriting ``count``.

    Re-running the daily snapshot corrects the value rather than doubling it (unlike
    ``CommandUsage.increment``'s ``count = count + 1``). Covers the ON CONFLICT DO
    UPDATE path on whichever dialect the suite is bound to.
    """
    d = dt.date(2026, 6, 20)

    await AutopostDailyStat.record(d, "xur", "follow", 12)
    await AutopostDailyStat.record(d, "xur", "mirror", 4)
    assert await AutopostDailyStat.fetch_series(since=d) == [
        (d, "xur", "follow", 12),
        (d, "xur", "mirror", 4),
    ]

    # Same key again → the row is corrected in place, not duplicated or summed.
    await AutopostDailyStat.record(d, "xur", "follow", 9)
    assert await AutopostDailyStat.fetch_series(since=d) == [
        (d, "xur", "follow", 9),
        (d, "xur", "mirror", 4),
    ]
