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

"""A fake Bungie manifest endpoint, and real manifest zips to serve from it.

Shared by the two test modules that drive the manifest resolver — the resolve-semantics
suite (``dd/anchor/tests/test_manifest.py``) and the download-and-load mechanics
(``dd/anchor/extensions/bungie_api/tests/test_manifest.py``). The zips contain a *real*
SQLite file in Bungie's shape, so the production extract and load paths run unmodified.
"""

import io
import json
import sqlite3
import tempfile
import typing as t
import zipfile
from pathlib import Path

from dd.anchor.extensions.bungie_api import manifest as manifest_module
from dd.common import schemas

#: ``{table: [(signed id, definition JSON)]}``. The JSON may be ``bytes``: Bungie
#: declares the column BLOB and real rows do come back as blobs, so fixtures exercise
#: both.
ManifestRows = dict[str, list[tuple[int, t.Any]]]

#: Bungie serves the manifest under this path; the filename after it is the version.
FRAGMENT_PREFIX = "/common/destiny2_content/sqlite/en/"

#: A couple of rows in two of the allowlisted tables, enough to prove the load ran.
DEFAULT_TABLES: ManifestRows = {
    "DestinyInventoryItemDefinition": [
        (12_345, json.dumps({"hash": 12_345, "displayProperties": {"name": "small"}}))
    ],
    "DestinyVendorDefinition": [
        (111, json.dumps({"hash": 111, "vendorIdentifier": "ROTATOR_A"}))
    ],
}


def manifest_sqlite_bytes(tables: ManifestRows | None = None) -> bytes:
    """A Bungie-shaped manifest SQLite (``id`` signed PK, ``json`` blob), as bytes."""
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "manifest.content"
        con = sqlite3.connect(path)
        try:
            for table, rows in (tables or DEFAULT_TABLES).items():
                con.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, json)')
                con.executemany(f'INSERT INTO "{table}" (id, json) VALUES (?, ?)', rows)
            con.commit()
        finally:
            con.close()
        return path.read_bytes()


def manifest_zip(version: str, tables: ManifestRows | None = None) -> bytes:
    """The zip Bungie serves: one member, named exactly ``version``.

    The name matters — the loader opens ``<workdir>/<version>`` after extracting, so a
    member under any other name is a load that finds nothing.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(version, manifest_sqlite_bytes(tables))
    return buffer.getvalue()


async def load_store(
    tmp_path: Path, tables: ManifestRows, version: str = "test.content"
) -> int:
    """Load ``tables`` into the test database as the active manifest; returns its id.

    Skips the download entirely and drives the real loader against a manifest SQLite
    written here, which is how anything that *reads* the manifest gets a fixture.
    """
    await clear_store()
    path = tmp_path / version
    con = sqlite3.connect(path)
    try:
        for table, rows in tables.items():
            con.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, json)')
            con.executemany(f'INSERT INTO "{table}" (id, json) VALUES (?, ?)', rows)
        con.commit()
    finally:
        con.close()

    version_id = await schemas.DestinyManifestVersion.begin_load(version)
    await manifest_module._load_tables(version_id, str(path))
    await schemas.DestinyManifestVersion.activate(version_id)
    return version_id


async def clear_store() -> None:
    """Drop every stored manifest, so a test starts from an empty store."""
    manifest_module._inflight = None
    async with schemas.db_session() as session, session.begin():
        await session.execute(schemas.DestinyManifestDefinition.__table__.delete())
        await session.execute(schemas.DestinyManifestVersion.__table__.delete())


def pin_manifest(monkeypatch: t.Any, version_id: int, version: str = "test") -> None:
    """Make every ``ensure_manifest`` call resolve to ``version_id``, without Bungie."""

    async def _resolved(_api_key: str) -> manifest_module.ManifestVersion:
        return manifest_module.ManifestVersion(version_id, version)

    monkeypatch.setattr(manifest_module, "ensure_manifest", _resolved)


class _FakeContent:
    """``response.content``: yields the body in deliberately small pieces.

    The requested chunk size is recorded but not honoured, so a body far smaller than
    1 MiB still drives several iterations of the write loop — what we actually want to
    prove is that the loop reassembles the payload byte-for-byte.
    """

    def __init__(self, body: bytes, requested: list[int]) -> None:
        self._body = body
        self._requested = requested

    async def iter_chunked(self, size: int) -> t.AsyncIterator[bytes]:
        self._requested.append(size)
        for start in range(0, len(self._body), 7):
            yield self._body[start : start + 7]


class _FakeResponse:
    def __init__(self, payload: t.Any, body: bytes, requested: list[int]) -> None:
        self._payload = payload
        self.content = _FakeContent(body, requested)

    async def json(self) -> t.Any:
        return self._payload

    async def read(self) -> bytes:
        raise AssertionError("the manifest download must stream, not buffer via read()")

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def install_fake_bungie(
    monkeypatch: t.Any,
    *,
    current: str | None,
    zip_bytes: bytes | None = None,
    chunk_sizes: list[int] | None = None,
) -> list[str]:
    """Patch ``aiohttp.ClientSession`` in the manifest module. Returns the URLs called.

    ``current`` is the filename Bungie reports as current; ``None`` makes every request
    fail, standing in for an outage. ``zip_bytes`` overrides the served payload (to
    drive the failure paths), and ``chunk_sizes`` collects what the download asked
    ``iter_chunked`` for.
    """
    calls: list[str] = []
    requested = chunk_sizes if chunk_sizes is not None else []

    class _FakeSession:
        """Stands in for both sessions the resolver opens (metadata, then download)."""

        def __init__(self, *_a: object, **_kw: object) -> None:
            if current is None:
                raise OSError("Bungie unreachable")

        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def get(self, url: str, **_kw: t.Any) -> _FakeResponse:
            calls.append(url)
            payload = {
                "Response": {
                    "mobileWorldContentPaths": {"en": FRAGMENT_PREFIX + str(current)}
                }
            }
            body = (
                b""
                if url == manifest_module.API_MANIFEST
                else (
                    zip_bytes
                    if zip_bytes is not None
                    else manifest_zip(t.cast(str, current))
                )
            )
            return _FakeResponse(payload, body, requested)

    monkeypatch.setattr(manifest_module.aiohttp, "ClientSession", _FakeSession)
    return calls
