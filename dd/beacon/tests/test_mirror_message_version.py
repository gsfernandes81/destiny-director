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

"""Integration tests for the mirror version-snapshot store.

``MirrorMessageVersion`` backs the web mirror-log render/diff view. Exercises the
capture INSERT-IGNORE (first-write-wins), the versions-for-source metadata listing
(oldest first, no payload), the single-version payload fetch, and the orphan prune that
piggybacks on the ledger prune (a snapshot survives exactly as long as its source keeps
a delivery row — JSON round-trips through both the SQLite and Postgres lanes).
"""

import datetime as dt

import pytest
import pytest_asyncio

from dd.common import schemas
from dd.common.schemas import DeliveryState, MirrorDelivery, MirrorMessageVersion

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    await schemas.destroy_all()
    await schemas.create_all()
    yield


def _cv2_payload(text: str) -> dict:
    return {"components": [{"type": 17, "components": [{"type": 10, "content": text}]}]}


async def _delivery_row(src_msg_id: int, dest_ch_id: int = 1) -> None:
    now = schemas._utcnow()
    async with schemas.db_session() as session, session.begin():
        session.add(
            MirrorDelivery(
                src_msg_id=src_msg_id,
                dest_ch_id=dest_ch_id,
                src_ch_id=1,
                state=DeliveryState.DELIVERED.value,
                created_at=now,
                due_at=now,
            )
        )


async def test_capture_is_insert_ignore_first_write_wins() -> None:
    inserted = await MirrorMessageVersion.capture(
        src_msg_id=100,
        version=1,
        src_guild_id=42,
        kind="cv2",
        summary="Weekly reset",
        payload=_cv2_payload("hello"),
    )
    assert inserted == 1
    # Re-capturing the same (src, version) is a no-op that keeps the original payload.
    again = await MirrorMessageVersion.capture(
        src_msg_id=100,
        version=1,
        src_guild_id=42,
        kind="cv2",
        summary="CLOBBER",
        payload=_cv2_payload("overwritten"),
    )
    assert again == 0
    got = await MirrorMessageVersion.get_version(100, 1)
    assert got is not None
    assert got["summary"] == "Weekly reset"  # first write survived
    assert got["payload"] == _cv2_payload("hello")
    assert got["src_guild_id"] == 42
    assert got["kind"] == "cv2"
    assert isinstance(got["captured_at"], dt.datetime)


async def test_versions_for_lists_metadata_oldest_first_without_payload() -> None:
    for v in (3, 1, 2):  # insert out of order to prove the ORDER BY version
        await MirrorMessageVersion.capture(
            src_msg_id=200,
            version=v,
            src_guild_id=None,
            kind="classic",
            summary=f"v{v}",
            payload={"content": f"body {v}", "embeds": []},
        )
    versions = await MirrorMessageVersion.versions_for(200)
    assert [x["version"] for x in versions] == [1, 2, 3]
    assert [x["summary"] for x in versions] == ["v1", "v2", "v3"]
    assert all("payload" not in x for x in versions)  # listing stays light
    assert all(isinstance(x["captured_at"], dt.datetime) for x in versions)


async def test_get_version_absent_returns_none() -> None:
    assert await MirrorMessageVersion.get_version(999, 1) is None


async def test_prune_drops_only_orphaned_snapshots() -> None:
    # src 300 keeps a live delivery row; src 400 has none (orphaned).
    await _delivery_row(300)
    await MirrorMessageVersion.capture(
        src_msg_id=300,
        version=1,
        src_guild_id=1,
        kind="cv2",
        summary="kept",
        payload=_cv2_payload("kept"),
    )
    await MirrorMessageVersion.capture(
        src_msg_id=400,
        version=1,
        src_guild_id=1,
        kind="cv2",
        summary="orphan",
        payload=_cv2_payload("orphan"),
    )

    await MirrorMessageVersion.prune()

    assert await MirrorMessageVersion.get_version(300, 1) is not None  # source lives
    assert await MirrorMessageVersion.get_version(400, 1) is None  # orphan pruned


async def test_capture_version_resolves_guild_from_channel() -> None:
    # A REST-fetched source message has guild_id=None, so _capture_version must resolve
    # the source guild from its channel (for the web log's channel/message links).
    import typing as t
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import hikari as h

    from dd.beacon.mirror_worker import MirrorWorker
    from dd.common.bot import CachedFetchBot
    from dd.hmessage import HMessage

    worker = MirrorWorker()
    msg = t.cast(h.Message, SimpleNamespace(guild_id=None, channel_id=555))
    bot = SimpleNamespace(
        fetch_channel=AsyncMock(return_value=SimpleNamespace(guild_id=42424242)),
        entity_factory=SimpleNamespace(serialize_embed=None),
    )
    await worker._capture_version(
        msg, 600, 1, HMessage(content="hi"), t.cast(CachedFetchBot, bot)
    )

    got = await MirrorMessageVersion.get_version(600, 1)
    assert got is not None
    assert got["src_guild_id"] == 42424242  # resolved from channel, not msg.guild_id
    bot.fetch_channel.assert_awaited_once_with(555)


async def test_summary_is_capped_to_column_width() -> None:
    await MirrorMessageVersion.capture(
        src_msg_id=500,
        version=1,
        src_guild_id=None,
        kind="classic",
        summary="x" * 500,
        payload={"content": "", "embeds": []},
    )
    got = await MirrorMessageVersion.get_version(500, 1)
    assert got is not None
    assert len(got["summary"]) == 200  # trimmed to the varchar(200) width
