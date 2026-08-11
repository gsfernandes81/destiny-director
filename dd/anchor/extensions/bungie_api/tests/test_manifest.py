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

"""Download-and-load mechanics: streaming the zip, extracting it, and cleaning up.

The resolve *semantics* (currency, reuse, the Bungie-unreachable fallback, coalescing)
live in ``dd/anchor/tests/test_manifest.py``; the keyed lookup and the projections live
in ``test_manifest_store.py``. This module is about what happens on disk and in the
temp directory while a load runs — and, mostly, about the fact that nothing is left
there afterwards.

No network: ``aiohttp.ClientSession`` is faked out and serves a real zip containing a
real Bungie-shaped SQLite, so the production extract and load paths run unmodified.
"""

import os
import tempfile
import zipfile

import pytest
import pytest_asyncio

from dd.anchor.extensions.bungie_api import manifest as m
from dd.anchor.tests.manifest_fixtures import install_fake_bungie, manifest_zip
from dd.common import schemas

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_VERSION = "world_sql_content_deadbeef.content"


@pytest_asyncio.fixture(autouse=True)
async def _empty_store():
    m._inflight = None
    await _clear()
    yield
    m._inflight = None
    await _clear()


async def _clear() -> None:
    async with schemas.db_session() as session, session.begin():
        await session.execute(schemas.DestinyManifestDefinition.__table__.delete())
        await session.execute(schemas.DestinyManifestVersion.__table__.delete())


def _temp_entries() -> set[str]:
    """Whatever is sitting in the system temp dir right now."""
    return set(os.listdir(tempfile.gettempdir()))


async def test_the_download_streams_and_the_rows_land_in_the_database() -> None:
    """End to end, with nothing faked but the transport."""
    chunk_sizes: list[int] = []
    with pytest.MonkeyPatch.context() as patch:
        install_fake_bungie(patch, current=_VERSION, chunk_sizes=chunk_sizes)
        version = await m.ensure_manifest("api-key")

    assert version.version == _VERSION
    # 1 MiB chunks — small enough to bound the peak, large enough that the per-chunk
    # overhead is noise. (``_FakeResponse.read`` raises, so a buffered download fails.)
    assert chunk_sizes == [1 << 20]

    with m.ManifestLookup(version.id) as lookup:
        assert lookup["DestinyInventoryItemDefinition"][12_345] == {
            "hash": 12_345,
            "displayProperties": {"name": "small"},
        }


async def test_a_successful_load_leaves_no_temp_files_behind() -> None:
    """The whole point of the move: no host keeps a manifest, not even briefly."""
    before = _temp_entries()
    with pytest.MonkeyPatch.context() as patch:
        install_fake_bungie(patch, current=_VERSION)
        await m.ensure_manifest("api-key")

    assert not {e for e in _temp_entries() - before if e.startswith("dd-manifest-")}


async def test_a_failed_extract_leaves_no_temp_files_and_no_version() -> None:
    before = _temp_entries()
    with pytest.MonkeyPatch.context() as patch:
        install_fake_bungie(
            patch, current=_VERSION, zip_bytes=b"this is not a zip file at all"
        )
        with pytest.raises(zipfile.BadZipFile):
            await m.ensure_manifest("api-key")

    assert not {e for e in _temp_entries() - before if e.startswith("dd-manifest-")}
    assert await schemas.DestinyManifestVersion.active() is None


async def test_a_failed_load_discards_its_half_written_rows() -> None:
    """A version that never activated must take its definitions with it.

    Otherwise every failed load leaves a manifest's worth of orphaned rows behind, and
    the reclaim at the start of the *next* load is the only thing that would notice.
    """
    with pytest.MonkeyPatch.context() as patch:
        install_fake_bungie(patch, current=_VERSION)

        real_load = m._load_tables

        async def _half_load(version_id: int, path: str) -> None:
            await real_load(version_id, path)
            raise OSError(28, "No space left on device")

        patch.setattr(m, "_load_tables", _half_load)
        with pytest.raises(OSError):
            await m.ensure_manifest("api-key")

    async with schemas.db_session() as session:
        rows = await session.execute(
            schemas.select(schemas.DestinyManifestDefinition.version_id)
        )
        assert list(rows) == []


async def test_the_zip_member_must_be_named_for_the_version() -> None:
    """The loader opens ``<workdir>/<version>``; anything else is a load that finds
    nothing, and it must fail loudly rather than activate an empty manifest."""
    with pytest.MonkeyPatch.context() as patch:
        install_fake_bungie(
            patch, current=_VERSION, zip_bytes=manifest_zip("some_other_name.content")
        )
        with pytest.raises(Exception):  # noqa: B017 — sqlite3 / OSError, both fine
            await m.ensure_manifest("api-key")

    assert await schemas.DestinyManifestVersion.active() is None
