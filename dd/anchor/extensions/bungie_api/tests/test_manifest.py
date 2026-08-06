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

"""Tests for the manifest download and the lazy sqlite-backed lookup (ManifestLookup).

No network: the download tests drive ``_get_latest_manifest`` against a fake aiohttp
session serving an in-memory zip.
"""

import io
import json
import sqlite3
import zipfile

import pytest

from dd.anchor.extensions.bungie_api import manifest as manifest_module
from dd.anchor.extensions.bungie_api.manifest import ManifestLookup, _to_signed_id

# A hash >= 2**31 exercises the unsigned->signed id conversion; the manifest sqlite
# stores it under the signed primary key (computed here independently of _to_signed_id
# so a bug in that helper is actually caught).
_BIG_HASH = 3_000_000_000
_BIG_HASH_SIGNED = 3_000_000_000 - 2**32  # -1_294_967_296
_SMALL_HASH = 12_345


def _make_manifest(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE DestinyInventoryItemDefinition (id INTEGER PRIMARY KEY, json)"
        )
        con.execute(
            "CREATE TABLE DestinyVendorDefinition (id INTEGER PRIMARY KEY, json)"
        )
        con.executemany(
            "INSERT INTO DestinyInventoryItemDefinition (id, json) VALUES (?, ?)",
            [
                (_SMALL_HASH, json.dumps({"hash": _SMALL_HASH, "name": "small"})),
                (_BIG_HASH_SIGNED, json.dumps({"hash": _BIG_HASH, "name": "big"})),
            ],
        )
        con.executemany(
            "INSERT INTO DestinyVendorDefinition (id, json) VALUES (?, ?)",
            [
                (111, json.dumps({"hash": 111, "vendorIdentifier": "ROTATOR_A"})),
                (222, json.dumps({"hash": 222, "vendorIdentifier": "OTHER"})),
            ],
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def manifest(tmp_path):
    path = str(tmp_path / "world.content")
    _make_manifest(path)
    lookup = ManifestLookup(path)
    yield lookup
    lookup.close()


def test_to_signed_id_conversion():
    assert _to_signed_id(_SMALL_HASH) == _SMALL_HASH
    assert _to_signed_id(_BIG_HASH) == _BIG_HASH_SIGNED
    assert _to_signed_id(2**31) == -(2**31)  # boundary
    assert _to_signed_id(2**31 - 1) == 2**31 - 1


def test_keyed_lookup_small_and_big_hash(manifest):
    items = manifest["DestinyInventoryItemDefinition"]
    assert items[_SMALL_HASH]["name"] == "small"
    # Round-trips the returned item's own hash — proves the signed-id lookup fetched
    # the correct row for a hash >= 2**31.
    big = items[_BIG_HASH]
    assert big["name"] == "big"
    assert big["hash"] == _BIG_HASH


def test_get_missing_returns_default(manifest):
    items = manifest["DestinyInventoryItemDefinition"]
    assert items.get(99_999) is None
    assert items.get(99_999, "fallback") == "fallback"


def test_getitem_missing_raises_keyerror(manifest):
    with pytest.raises(KeyError):
        _ = manifest["DestinyInventoryItemDefinition"][99_999]


def test_unknown_table_raises_keyerror(manifest):
    with pytest.raises(KeyError):
        _ = manifest["NotARealTable"]


def test_contains(manifest):
    assert "DestinyInventoryItemDefinition" in manifest
    assert "NotARealTable" not in manifest
    items = manifest["DestinyInventoryItemDefinition"]
    assert _SMALL_HASH in items
    assert 99_999 not in items


def test_values_iterates_whole_table(manifest):
    vendor_hashes = {v["hash"] for v in manifest["DestinyVendorDefinition"].values()}
    assert vendor_hashes == {111, 222}
    # The rotator-discovery filter (eververse) reads vendorIdentifier off the values.
    rotators = [
        v["hash"]
        for v in manifest["DestinyVendorDefinition"].values()
        if v.get("vendorIdentifier", "").startswith("ROTATOR")
    ]
    assert rotators == [111]


# --- hashes_by_field_prefix (the sqlite-side replacement for that .values() scan) ---


def _prefix_table(path: str, rows: list[tuple[int, object]]) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE DestinyVendorDefinition (id INTEGER PRIMARY KEY, json)"
        )
        con.executemany(
            "INSERT INTO DestinyVendorDefinition (id, json) VALUES (?, ?)", rows
        )
        con.commit()
    finally:
        con.close()


def _vendor(id_: int, identifier: str | None) -> tuple[int, str]:
    defn: dict = {"hash": id_}
    if identifier is not None:
        defn["vendorIdentifier"] = identifier
    return (id_, json.dumps(defn))


@pytest.fixture
def prefix_manifest(tmp_path):
    path = str(tmp_path / "prefix.content")
    _prefix_table(
        path,
        [
            _vendor(1, "ROTATOR_ALPHA"),
            _vendor(2, "ROTATOR"),  # exactly the prefix
            _vendor(3, "ROTATORX"),  # prefix + non-separator
            _vendor(4, "rotator_alpha"),  # wrong case
            _vendor(5, "OTHER_ROTATOR"),  # prefix not at position 0
            _vendor(6, ""),
            _vendor(7, None),  # field absent
        ],
    )
    lookup = ManifestLookup(path)
    yield lookup
    lookup.close()


def test_hashes_by_field_prefix_is_exact_case_sensitive_and_ordered(prefix_manifest):
    table = prefix_manifest["DestinyVendorDefinition"]
    # Rowid order preserved; case-sensitive; matches at position 0 only; no wildcards.
    assert table.hashes_by_field_prefix("vendorIdentifier", "ROTATOR") == [1, 2, 3]
    assert table.hashes_by_field_prefix("vendorIdentifier", "ROTATOR_") == [1]
    assert table.hashes_by_field_prefix("vendorIdentifier", "rotator") == [4]
    assert table.hashes_by_field_prefix("vendorIdentifier", "NOPE") == []


def test_hashes_by_field_prefix_is_not_a_like_pattern(prefix_manifest):
    table = prefix_manifest["DestinyVendorDefinition"]
    # "ROTATOR_" under LIKE would be "ROTATOR" + any one char, matching ROTATORX too;
    # and "%" would match everything. Neither is a wildcard here.
    assert 3 not in table.hashes_by_field_prefix("vendorIdentifier", "ROTATOR_")
    assert table.hashes_by_field_prefix("vendorIdentifier", "%") == []


def test_hashes_by_field_prefix_empty_prefix_matches_every_row(prefix_manifest):
    # "".startswith("") is True even for the row with no vendorIdentifier at all, which
    # is what the coalesce to '' buys.
    table = prefix_manifest["DestinyVendorDefinition"]
    assert table.hashes_by_field_prefix("vendorIdentifier", "") == [1, 2, 3, 4, 5, 6, 7]


def test_hashes_by_field_prefix_handles_blob_and_corrupt_rows(tmp_path):
    path = str(tmp_path / "mixed.content")
    _prefix_table(
        path,
        [
            _vendor(1, "ROTATOR_A"),
            # The manifest column is declared BLOB and real rows arrive as blobs.
            (2, json.dumps({"hash": 2, "vendorIdentifier": "ROTATOR_B"}).encode()),
            # A corrupt row must be skipped, not abort the query with "malformed JSON"
            # — the .values() path skipped it via json.loads' except.
            (3, "{not json"),
            _vendor(4, "ROTATOR_C"),
        ],
    )
    with ManifestLookup(path) as lookup:
        table = lookup["DestinyVendorDefinition"]
        assert table.hashes_by_field_prefix("vendorIdentifier", "ROTATOR") == [1, 2, 4]


def test_hashes_by_field_prefix_parses_nothing_into_the_row_cache(prefix_manifest):
    # The whole point: the fat vendor rows are never materialised, so the query must
    # leave the memoisation cache untouched (and not mark the table fully loaded).
    table = prefix_manifest["DestinyVendorDefinition"]
    table.hashes_by_field_prefix("vendorIdentifier", "ROTATOR")
    assert table._cache == {}
    assert table._all_loaded is False


def test_hashes_by_field_prefix_reads_a_non_identifier_field(prefix_manifest):
    # The field name is a bound JSON-path parameter, not string-interpolated SQL.
    table = prefix_manifest["DestinyVendorDefinition"]
    assert table.hashes_by_field_prefix("nosuchfield", "x") == []


def test_memoises_repeated_lookup(manifest):
    items = manifest["DestinyInventoryItemDefinition"]
    first = items[_SMALL_HASH]
    assert items[_SMALL_HASH] is first  # cached object returned, not re-parsed


def test_context_manager_closes(tmp_path):
    path = str(tmp_path / "world.content")
    _make_manifest(path)
    with ManifestLookup(path) as m:
        assert m["DestinyInventoryItemDefinition"][_SMALL_HASH]["name"] == "small"
    # Connection is closed on exit; a query for an *uncached* hash (not memoised inside
    # the block) must actually reach the closed connection and fail.
    with pytest.raises(sqlite3.ProgrammingError):
        _ = m["DestinyInventoryItemDefinition"][_BIG_HASH]


# --- _get_latest_manifest: chunked download + zip cleanup -----------------------------
#
# The download streams the zip to disk in chunks instead of ``await response.read()``
# (which buffers the whole 43-90 MB payload, twice over at aiohttp's internal join), and
# the zip is deleted once extraction succeeds. Nothing here touches the network.

_CONTENT_NAME = "world_sql_content_deadbeef.content"
_FRAGMENT = f"/common/destiny2_content/sqlite/en/{_CONTENT_NAME}"
_SQLITE_BYTES = b"SQLite format 3\x00" + bytes(range(256)) * 40


def _zip_bytes(name: str = _CONTENT_NAME, payload: bytes = _SQLITE_BYTES) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


class _FakeContent:
    """``response.content``: yields the body in deliberately small pieces.

    The requested chunk size is recorded but not honoured, so a body far smaller than
    1 MiB still drives several iterations of the write loop — what we actually want to
    prove is that the loop reassembles the payload byte-for-byte.
    """

    def __init__(self, body: bytes, requested: list[int]) -> None:
        self._body = body
        self._requested = requested

    async def iter_chunked(self, size: int):
        self._requested.append(size)
        for start in range(0, len(self._body), 7):
            yield self._body[start : start + 7]


class _FakeResponse:
    def __init__(self, payload: object, body: bytes, requested: list[int]) -> None:
        self._payload = payload
        self.content = _FakeContent(body, requested)

    async def json(self) -> object:
        return self._payload

    async def read(self) -> bytes:
        raise AssertionError("the manifest download must stream, not buffer via read()")

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def fake_bungie(monkeypatch, tmp_path):
    """Patch aiohttp so ``_get_latest_manifest`` runs entirely against fixtures.

    Yields a mutable config dict: set ``zip_bytes`` to control the served payload, read
    ``chunk_sizes`` to see what the download asked ``iter_chunked`` for.
    """
    cfg: dict = {"zip_bytes": _zip_bytes(), "chunk_sizes": []}
    meta = {"Response": {"mobileWorldContentPaths": {"en": _FRAGMENT}}}

    class _FakeSession:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def get(self, url: str, headers: object = None) -> _FakeResponse:
            body = b"" if url == manifest_module.API_MANIFEST else cfg["zip_bytes"]
            return _FakeResponse(meta, body, cfg["chunk_sizes"])

    monkeypatch.setattr(manifest_module.aiohttp, "ClientSession", _FakeSession)
    # The function works in relative paths ("manifest/", "manifest.zip").
    monkeypatch.chdir(tmp_path)
    return cfg


@pytest.mark.asyncio
async def test_download_streams_the_zip_to_disk_and_extracts_it(fake_bungie, tmp_path):
    path = await manifest_module._get_latest_manifest("api-key")

    assert path == f"manifest/{_CONTENT_NAME}"
    # Byte-for-byte through the chunked write loop, not merely "a file exists".
    assert (tmp_path / path).read_bytes() == _SQLITE_BYTES
    # 1 MiB chunks — small enough to bound the peak, large enough that the per-chunk
    # overhead is noise.
    assert fake_bungie["chunk_sizes"] == [1 << 20]


@pytest.mark.asyncio
async def test_zip_is_deleted_after_a_successful_extract(fake_bungie, tmp_path):
    await manifest_module._get_latest_manifest("api-key")
    assert not (tmp_path / "manifest.zip").exists()


@pytest.mark.asyncio
async def test_zip_is_retained_when_extraction_fails(fake_bungie, tmp_path):
    fake_bungie["zip_bytes"] = b"this is not a zip file at all"

    with pytest.raises(zipfile.BadZipFile):
        await manifest_module._get_latest_manifest("api-key")

    # Kept for the next attempt / for inspection — and it is the downloaded bytes, so
    # the failure happened at extraction, not at the write.
    assert (tmp_path / "manifest.zip").read_bytes() == b"this is not a zip file at all"


@pytest.mark.asyncio
async def test_cached_manifest_short_circuits_the_download(fake_bungie, tmp_path):
    first = await manifest_module._get_latest_manifest("api-key")
    fake_bungie["chunk_sizes"].clear()
    # Serving garbage now proves nothing is re-downloaded on the cached path.
    fake_bungie["zip_bytes"] = b"never read"

    assert await manifest_module._get_latest_manifest("api-key") == first
    assert fake_bungie["chunk_sizes"] == []
    assert (tmp_path / first).read_bytes() == _SQLITE_BYTES
