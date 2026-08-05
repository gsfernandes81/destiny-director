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

"""Integration test for the daily autopost-reach snapshot split.

Exercises ``_snapshot_autopost_reach`` against the test DB. ``AutopostDailyStat.record``
is stubbed to collect the rows the task would write, so this stays a test of the
follow/mirror split rather than of the upsert (which
``test_autopost_daily_stat_schema.py`` covers directly).
"""

import datetime as dt

import pytest
import pytest_asyncio

from dd.beacon.extensions.statistics import _snapshot_autopost_reach
from dd.common import schemas
from dd.common.schemas import MirroredChannel

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    await schemas.destroy_all()
    await schemas.create_all()
    MirroredChannel._legacy_srcs_cache.clear()
    yield


@pytest.mark.asyncio
async def test_snapshot_splits_follows_and_mirrors_per_feed(monkeypatch):
    guild_id = 99
    xur_src, ada_src = 100, 200

    # xur: 2 follows (non-legacy) + 1 mirror (legacy); one follow disabled (excluded).
    await MirroredChannel.add_mirror(xur_src, 1, guild_id, legacy=False)
    await MirroredChannel.add_mirror(xur_src, 2, guild_id, legacy=False)
    await MirroredChannel.add_mirror(xur_src, 3, guild_id, legacy=True)
    await MirroredChannel.add_mirror(xur_src, 4, guild_id, legacy=False)
    # Disable dest 4 so it drops out of the enabled-only count.
    async with schemas.db_session() as session, session.begin():
        from sqlalchemy import update

        await session.execute(
            update(MirroredChannel)
            .where(MirroredChannel.src_id == xur_src, MirroredChannel.dest_id == 4)
            .values(enabled=False)
        )

    # ada: 1 mirror only, no follows.
    await MirroredChannel.add_mirror(ada_src, 5, guild_id, legacy=True)

    recorded: list[tuple[dt.date, str, str, int]] = []

    async def _fake_record(date, feed, kind, count, *, session=schemas._UNSET):
        recorded.append((date, feed, kind, count))

    monkeypatch.setattr(schemas.AutopostDailyStat, "record", _fake_record)

    await _snapshot_autopost_reach({"xur": xur_src, "ada": ada_src})

    today = dt.datetime.now(tz=dt.UTC).date()
    assert set(recorded) == {
        (today, "xur", "follow", 2),  # dest 4 disabled → not counted
        (today, "xur", "mirror", 1),
        (today, "ada", "follow", 0),
        (today, "ada", "mirror", 1),
    }
