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

"""Tests for :class:`Cv2Draft` — the web builder's Discord→browser handoff row.

The load-bearing property is **creator scoping**: the draft id travels in a URL, so
every read and write must refuse a different owner's id even though the OAuth
middleware has already admitted them as *an* owner.
"""

import datetime as dt
import uuid

import pytest

from dd.common.schemas import Cv2Draft, db_session
from dd.common.utils import FriendlyValueError

pytestmark = pytest.mark.integration

OWNER = 1234567890
OTHER_OWNER = 9876543210

NODES = [{"type": 10, "content": "# Hello"}]


def _id() -> str:
    return uuid.uuid4().hex


async def _create(draft_id: str, **kwargs) -> None:
    defaults: dict = {
        "created_by": OWNER,
        "action": Cv2Draft.ACTION_POST,
        "nodes": NODES,
    }
    defaults.update(kwargs)
    await Cv2Draft.create(id=draft_id, **defaults)


@pytest.mark.asyncio
async def test_create_and_read_back():
    draft_id = _id()
    await _create(draft_id, target_channel_id=42, guild_id=7)

    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None
    assert draft.nodes == NODES
    assert draft.action == Cv2Draft.ACTION_POST
    assert draft.target_channel_id == 42
    assert draft.guild_id == 7
    assert draft.published_message_id is None


@pytest.mark.asyncio
async def test_another_owner_cannot_read_the_draft():
    """The OAuth gate proves you are *an* owner; this proves you are *the* one."""
    draft_id = _id()
    await _create(draft_id)

    assert await Cv2Draft.get_for_user(draft_id, OTHER_OWNER) is None


@pytest.mark.asyncio
async def test_another_owner_cannot_overwrite_the_nodes():
    draft_id = _id()
    await _create(draft_id)

    hijacked = [{"type": 10, "content": "hijack"}]
    await Cv2Draft.save_nodes(draft_id, OTHER_OWNER, hijacked)

    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None
    assert draft.nodes == NODES


@pytest.mark.asyncio
async def test_save_nodes_round_trips_and_bumps_updated_at():
    draft_id = _id()
    await _create(draft_id)
    before = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert before is not None
    first_updated = before.updated_at

    new_nodes = [{"type": 17, "components": [{"type": 10, "content": "edited"}]}]
    await Cv2Draft.save_nodes(draft_id, OWNER, new_nodes)

    after = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert after is not None
    assert after.nodes == new_nodes
    assert after.updated_at >= first_updated


@pytest.mark.asyncio
async def test_mark_published_records_the_message():
    draft_id = _id()
    await _create(draft_id)

    await Cv2Draft.mark_published(draft_id, OWNER, 555)

    draft = await Cv2Draft.get_for_user(draft_id, OWNER)
    assert draft is not None
    assert draft.published_message_id == 555


@pytest.mark.asyncio
async def test_unknown_action_is_rejected():
    with pytest.raises(FriendlyValueError):
        await Cv2Draft.create(
            id=_id(), created_by=OWNER, action="launch_missiles", nodes=NODES
        )


async def _backdate(draft_id: str, days: float) -> None:
    """Set a draft's ``created_at`` explicitly, so prune tests turn on the cutoff rather
    than on how long the test itself took to run."""
    when = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days)
    async with db_session() as session, session.begin():
        draft = await Cv2Draft.get_for_user(draft_id, OWNER, session=session)
        assert draft is not None
        draft.created_at = when


@pytest.mark.asyncio
async def test_prune_drops_only_stale_drafts():
    fresh, stale = _id(), _id()
    await _create(fresh)
    await _create(stale)

    # Backdate one row past the retention window.
    await _backdate(stale, 45)

    removed = await Cv2Draft.prune(older_than_days=30)

    assert removed >= 1
    assert await Cv2Draft.get_for_user(stale, OWNER) is None
    assert await Cv2Draft.get_for_user(fresh, OWNER) is not None


@pytest.mark.asyncio
async def test_prune_retains_for_fourteen_days_by_default():
    """The default IS the policy — the scheduled sweep in ``cv2_builder_page`` calls
    ``prune()`` with no arguments, so a drifting default silently changes retention."""
    keep, drop = _id(), _id()
    await _create(keep)
    await _create(drop)
    await _backdate(keep, 13)
    await _backdate(drop, 15)

    await Cv2Draft.prune()

    assert await Cv2Draft.get_for_user(drop, OWNER) is None
    assert await Cv2Draft.get_for_user(keep, OWNER) is not None
