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

"""Tests for the lazy sqlite-backed manifest lookup (ManifestLookup)."""

import json
import sqlite3

import pytest

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
