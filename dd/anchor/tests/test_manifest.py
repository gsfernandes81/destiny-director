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

# Manifest resolution: the currency check, the reuse it enables, and the
# Bungie-unreachable fallback. No network — aiohttp.ClientSession is faked out and the
# manifest is loaded into the test database.
#
# The behaviour under test is specifically the thing a TTL cache broke once: reuse must
# be keyed on Bungie's versioned filename, never on elapsed time, so a manifest that
# ships mid-week is picked up on the next resolve rather than hours later.
#
# The fallback is the reason this whole move happened. It used to mean "reuse whatever
# is extracted in manifest/", which answered on a warm process and not at all on a cold
# one — a fresh container had nothing to fall back to. Now the manifest outlives the
# container, so test_falls_back_* holds on a process that has never seen Bungie.

import asyncio

import pytest
import pytest_asyncio

from dd.anchor.extensions.bungie_api import manifest as m
from dd.common import schemas

from .manifest_fixtures import install_fake_bungie

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture(autouse=True)
async def _empty_store():
    """No stored manifest, and no resolve left in flight from a previous test."""
    m._inflight = None
    await _clear()
    yield
    m._inflight = None
    await _clear()


async def _clear() -> None:
    async with schemas.db_session() as session, session.begin():
        await session.execute(schemas.DestinyManifestDefinition.__table__.delete())
        await session.execute(schemas.DestinyManifestVersion.__table__.delete())


async def _loaded_version() -> str | None:
    active = await schemas.DestinyManifestVersion.active()
    return None if active is None else str(active.version)


async def test_reuses_the_loaded_manifest_when_it_is_the_current_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_bungie(monkeypatch, current="world_v1.content")
    assert (await m.ensure_manifest("key")).version == "world_v1.content"

    m._inflight = None
    calls.clear()
    resolved = await m.ensure_manifest("key")

    assert resolved.version == "world_v1.content"
    # One call — the currency check. The download is what reuse saves, not the check.
    assert len(calls) == 1


async def test_a_newer_manifest_is_loaded_rather_than_served_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The regression a time-based cache introduced: one manifest loaded, a different one
    # live at Bungie. Elapsed time must not decide this.
    install_fake_bungie(monkeypatch, current="world_v1.content")
    await m.ensure_manifest("key")

    m._inflight = None
    install_fake_bungie(monkeypatch, current="world_v2.content")
    assert (await m.ensure_manifest("key")).version == "world_v2.content"
    assert await _loaded_version() == "world_v2.content"


async def test_raises_when_bungie_is_unreachable_and_nothing_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_bungie(monkeypatch, current=None)

    with pytest.raises(OSError):
        await m.ensure_manifest("key")


async def test_falls_back_to_the_stored_manifest_when_bungie_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback answers, and does not stick — the next resolve re-checks.

    Note what makes this different from the on-disk version it replaces: the process
    doing the falling back never successfully talked to Bungie *in this test's second
    half*, and on a real deploy would not have talked to it at all. The manifest is in
    the database, so a cold start during an outage still has one.
    """
    install_fake_bungie(monkeypatch, current="world_v1.content")
    await m.ensure_manifest("key")

    m._inflight = None
    install_fake_bungie(monkeypatch, current=None)
    assert (await m.ensure_manifest("key")).version == "world_v1.content"

    m._inflight = None
    install_fake_bungie(monkeypatch, current="world_v2.content")
    assert (await m.ensure_manifest("key")).version == "world_v2.content"


async def test_a_failed_load_leaves_nothing_the_next_resolve_would_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load that dies part-way must not be readable as the current manifest.

    The on-disk equivalent of this was a partial extract leaving a file with exactly the
    name Bungie had just given us — indistinguishable from a good copy, and handed to
    every consumer as a truncated sqlite for the life of the process. The version row's
    ``loading`` state is what replaces that: nothing is current until every row is in.
    """
    calls = install_fake_bungie(monkeypatch, current="world_v1.content")

    async def _die(*_a: object, **_kw: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(m, "_load_tables", _die)
    with pytest.raises(OSError):
        await m.ensure_manifest("key")

    assert await _loaded_version() is None

    # And the next resolve genuinely re-loads rather than reusing the wreckage.
    m._inflight = None
    monkeypatch.undo()
    calls.clear()
    install_fake_bungie(monkeypatch, current="world_v1.content")
    assert (await m.ensure_manifest("key")).version == "world_v1.content"


async def test_concurrent_callers_share_one_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several pages needing the manifest at once must not each call Bungie."""
    install_fake_bungie(monkeypatch, current="world_v1.content")
    await m.ensure_manifest("key")

    m._inflight = None
    calls = install_fake_bungie(monkeypatch, current="world_v1.content")
    results = await asyncio.gather(*(m.ensure_manifest("key") for _ in range(5)))

    assert [r.version for r in results] == ["world_v1.content"] * 5
    assert len(calls) == 1


async def test_a_cancelled_caller_does_not_cancel_the_shared_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_bungie(monkeypatch, current="world_v1.content")
    await m.ensure_manifest("key")

    m._inflight = None
    install_fake_bungie(monkeypatch, current="world_v1.content")
    first = asyncio.create_task(m.ensure_manifest("key"))
    second = asyncio.create_task(m.ensure_manifest("key"))
    await asyncio.sleep(0)  # let both attach to the same resolve
    first.cancel()

    assert (await second).version == "world_v1.content"


async def test_prewarm_swallows_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bot that cannot reach Bungie at boot must still start."""
    install_fake_bungie(monkeypatch, current=None)
    await m.prewarm_manifest("key")  # must not raise


async def test_prewarm_is_a_no_op_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_bungie(monkeypatch, current="world_v1.content")
    await m.prewarm_manifest("")
    assert calls == []
