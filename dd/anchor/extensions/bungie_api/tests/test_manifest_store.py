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

"""The manifest as it now lives: loaded out of Bungie's SQLite into the database.

Covers the load (including the signed-id → unsigned-hash conversion and the version
swap), the synchronous keyed lookup consumers read through, and the server-side
projections the derived indexes are built from. No network: the download tests live in
``test_manifest.py``; these start from an already-extracted manifest file.
"""

import datetime as dt
import json
import sqlite3

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from dd.anchor.extensions.bungie_api import manifest as m
from dd.common import schemas

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# A hash >= 2**31 exercises the signed-id conversion: Bungie's SQLite stores it under a
# negative primary key, and consumers speak the unsigned hash. Computed here
# independently of _to_unsigned_hash so a bug in that helper is actually caught.
_BIG_HASH = 3_000_000_000
_BIG_HASH_SIGNED = 3_000_000_000 - 2**32  # -1_294_967_296
_SMALL_HASH = 12_345


def _write_manifest(path, tables: dict[str, list[tuple[int, object]]]) -> str:
    """Write a Bungie-shaped manifest SQLite (``id`` signed PK, ``json`` blob)."""
    file = str(path)
    con = sqlite3.connect(file)
    try:
        for table, rows in tables.items():
            con.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, json)')
            con.executemany(
                f'INSERT INTO "{table}" (id, json) VALUES (?, ?)', list(rows)
            )
        con.commit()
    finally:
        con.close()
    return file


def _row(hash_: int, definition: dict) -> tuple[int, str]:
    signed = hash_ - 2**32 if hash_ >= 2**31 else hash_
    return (signed, json.dumps(definition | {"hash": hash_}))


async def _clear_manifest() -> None:
    """Drop every stored manifest, so each test starts from an empty store."""
    async with schemas.db_session() as session, session.begin():
        await session.execute(schemas.DestinyManifestDefinition.__table__.delete())
        await session.execute(schemas.DestinyManifestVersion.__table__.delete())


@pytest_asyncio.fixture
async def store(tmp_path):
    """A loaded, active manifest with a couple of tables in it."""
    await _clear_manifest()
    path = _write_manifest(
        tmp_path / "world_v1.content",
        {
            "DestinyInventoryItemDefinition": [
                _row(_SMALL_HASH, {"displayProperties": {"name": "small"}}),
                _row(_BIG_HASH, {"displayProperties": {"name": "big"}}),
            ],
            "DestinyVendorDefinition": [
                _row(111, {"vendorIdentifier": "ROTATOR_A"}),
                _row(222, {"vendorIdentifier": "OTHER"}),
            ],
        },
    )
    version_id = await schemas.DestinyManifestVersion.begin_load("world_v1.content")
    await m._load_tables(version_id, path)
    await schemas.DestinyManifestVersion.activate(version_id)
    yield version_id
    await _clear_manifest()


# --- the load -------------------------------------------------------------------------


async def test_load_converts_the_signed_id_to_the_unsigned_hash(store) -> None:
    with m.ManifestLookup(store) as lookup:
        items = lookup["DestinyInventoryItemDefinition"]
        # Round-trips the definition's own hash: proves the row stored under the
        # negative SQLite id is reachable by the unsigned hash consumers use.
        assert items[_BIG_HASH]["hash"] == _BIG_HASH
        assert items[_SMALL_HASH]["displayProperties"]["name"] == "small"


async def test_to_unsigned_hash_conversion() -> None:
    assert m._to_unsigned_hash(_SMALL_HASH) == _SMALL_HASH
    assert m._to_unsigned_hash(_BIG_HASH_SIGNED) == _BIG_HASH
    assert m._to_unsigned_hash(-(2**31)) == 2**31  # boundary
    assert m._to_unsigned_hash(2**31 - 1) == 2**31 - 1


async def test_a_table_absent_from_the_manifest_is_skipped_not_fatal(tmp_path) -> None:
    """Bungie's table set moves; a missing table must degrade, not fail the load."""
    await _clear_manifest()
    path = _write_manifest(
        tmp_path / "sparse.content",
        {"DestinyVendorDefinition": [_row(1, {"vendorIdentifier": "X"})]},
    )
    version_id = await schemas.DestinyManifestVersion.begin_load("sparse.content")
    await m._load_tables(version_id, path)  # must not raise
    await schemas.DestinyManifestVersion.activate(version_id)

    with m.ManifestLookup(version_id) as lookup:
        assert lookup["DestinyVendorDefinition"][1]["vendorIdentifier"] == "X"
        with pytest.raises(KeyError):
            _ = lookup["DestinyInventoryItemDefinition"][1]
    await _clear_manifest()


async def test_a_corrupt_row_is_skipped_and_the_rest_of_the_table_loads(
    tmp_path,
) -> None:
    await _clear_manifest()
    path = _write_manifest(
        tmp_path / "mixed.content",
        {
            "DestinyVendorDefinition": [
                _row(1, {"vendorIdentifier": "A"}),
                (2, "{not json"),
                # Bungie declares the column BLOB and real rows do arrive as blobs.
                (3, json.dumps({"hash": 3, "vendorIdentifier": "C"}).encode()),
            ]
        },
    )
    version_id = await schemas.DestinyManifestVersion.begin_load("mixed.content")
    await m._load_tables(version_id, path)
    await schemas.DestinyManifestVersion.activate(version_id)

    with m.ManifestLookup(version_id) as lookup:
        vendors = lookup["DestinyVendorDefinition"]
        assert vendors[1]["vendorIdentifier"] == "A"
        assert vendors[3]["vendorIdentifier"] == "C"
        assert vendors.get(2) is None
    await _clear_manifest()


# --- the version swap -----------------------------------------------------------------


async def _loaded_versions() -> set[int]:
    async with schemas.db_session() as session:
        rows = (
            await session.execute(
                select(schemas.DestinyManifestDefinition.version_id).distinct()
            )
        ).scalars()
        return set(rows)


async def _load(path: str, version: str) -> int:
    version_id = await schemas.DestinyManifestVersion.begin_load(version)
    await m._load_tables(version_id, path)
    await schemas.DestinyManifestVersion.activate(version_id)
    return version_id


async def test_activating_a_new_version_makes_it_the_one_readers_get(
    store, tmp_path
) -> None:
    path = _write_manifest(
        tmp_path / "world_v2.content",
        {"DestinyVendorDefinition": [_row(999, {"vendorIdentifier": "NEW"})]},
    )
    new_id = await _load(path, "world_v2.content")

    active = await schemas.DestinyManifestVersion.active()
    assert active is not None
    assert active.version == "world_v2.content"
    assert active.id == new_id


async def test_the_replaced_manifest_stays_readable_after_it_is_replaced(
    store, tmp_path
) -> None:
    """The window that would otherwise render "Unknown Type" into a published post.

    A post build resolves a version id once and reads through it for a minute; deleting
    that manifest the moment a newer one activates would turn its mid-render lookups
    into misses, and the consumers' ``.get(hash, {})`` defaults would swallow them.
    """
    lookup = m.ManifestLookup(store)  # a post build, holding the outgoing version
    path = _write_manifest(
        tmp_path / "world_v2.content",
        {"DestinyVendorDefinition": [_row(999, {"vendorIdentifier": "NEW"})]},
    )
    await _load(path, "world_v2.content")

    assert lookup["DestinyVendorDefinition"][111]["vendorIdentifier"] == "ROTATOR_A"
    lookup.close()
    assert await _loaded_versions() == {store, (await _current_id())}


async def test_the_next_load_is_what_reclaims_the_replaced_manifest(
    store, tmp_path
) -> None:
    """Deferred, not skipped — otherwise the table grows by a manifest every week."""
    second = _write_manifest(
        tmp_path / "world_v2.content",
        {"DestinyVendorDefinition": [_row(999, {"vendorIdentifier": "TWO"})]},
    )
    third = _write_manifest(
        tmp_path / "world_v3.content",
        {"DestinyVendorDefinition": [_row(999, {"vendorIdentifier": "THREE"})]},
    )
    second_id = await _load(second, "world_v2.content")
    assert await _loaded_versions() == {store, second_id}

    third_id = await _load(third, "world_v3.content")
    # v1 is gone (reclaimed at the start of the v3 load); v2 is kept for in-flight
    # readers. Two manifests is the steady state, not three.
    assert await _loaded_versions() == {second_id, third_id}


async def _current_id() -> int:
    active = await schemas.DestinyManifestVersion.active()
    assert active is not None
    return int(active.id)


async def test_a_loading_version_is_not_served_as_active(store) -> None:
    """A load that has not finished must be invisible, not half-current."""
    await schemas.DestinyManifestVersion.begin_load("world_v2.content")
    active = await schemas.DestinyManifestVersion.active()
    assert active is not None
    assert active.version == "world_v1.content"


async def test_a_live_claim_on_a_version_blocks_a_second_loader(store) -> None:
    """Two anchor containers noticing the same new manifest must not both load it."""
    await schemas.DestinyManifestVersion.begin_load("world_v2.content")
    with pytest.raises(schemas.ManifestLoadInProgress):
        await schemas.DestinyManifestVersion.begin_load("world_v2.content")


async def test_a_dead_claim_is_reclaimed_after_its_lease(store, tmp_path) -> None:
    """A load killed mid-import leaves rows; the next attempt must be able to take over
    rather than collide with them on the definition primary key."""
    path = _write_manifest(
        tmp_path / "world_v2.content",
        {"DestinyVendorDefinition": [_row(999, {"vendorIdentifier": "NEW"})]},
    )
    first = await schemas.DestinyManifestVersion.begin_load("world_v2.content")
    await m._load_tables(first, path)  # ... and now the process "dies"
    await _age_claim(first)

    second = await schemas.DestinyManifestVersion.begin_load("world_v2.content")
    await m._load_tables(second, path)  # must not raise
    await schemas.DestinyManifestVersion.activate(second)

    with m.ManifestLookup(second) as lookup:
        assert lookup["DestinyVendorDefinition"][999]["vendorIdentifier"] == "NEW"


async def _age_claim(version_id: int) -> None:
    """Backdate a claim past its lease, as an abandoned load would be."""
    stale = dt.datetime.now(dt.UTC).replace(tzinfo=None) - (
        schemas.DestinyManifestVersion.LOAD_LEASE + dt.timedelta(minutes=1)
    )
    async with schemas.db_session() as session, session.begin():
        await session.execute(
            update(schemas.DestinyManifestVersion)
            .values(created_at=stale)
            .where(schemas.DestinyManifestVersion.id == version_id)
        )


# --- the keyed lookup -----------------------------------------------------------------


async def test_get_missing_returns_default(store) -> None:
    with m.ManifestLookup(store) as lookup:
        items = lookup["DestinyInventoryItemDefinition"]
        assert items.get(99_999) is None
        assert items.get(99_999, "fallback") == "fallback"


async def test_getitem_missing_raises_keyerror(store) -> None:
    with m.ManifestLookup(store) as lookup, pytest.raises(KeyError):
        _ = lookup["DestinyInventoryItemDefinition"][99_999]


async def test_unknown_table_raises_keyerror(store) -> None:
    with m.ManifestLookup(store) as lookup, pytest.raises(KeyError):
        _ = lookup["NotARealTable"]


async def test_contains(store) -> None:
    with m.ManifestLookup(store) as lookup:
        assert "DestinyInventoryItemDefinition" in lookup
        assert "NotARealTable" not in lookup
        items = lookup["DestinyInventoryItemDefinition"]
        assert _SMALL_HASH in items
        assert 99_999 not in items


async def test_memoises_repeated_lookup(store) -> None:
    with m.ManifestLookup(store) as lookup:
        items = lookup["DestinyInventoryItemDefinition"]
        first = items[_SMALL_HASH]
        assert items[_SMALL_HASH] is first  # cached object, not a second round-trip


async def test_values_iterates_the_whole_table(store) -> None:
    with m.ManifestLookup(store) as lookup:
        assert {v["hash"] for v in lookup["DestinyVendorDefinition"].values()} == {
            111,
            222,
        }


async def test_close_makes_further_reads_fail_rather_than_reconnect(store) -> None:
    lookup = m.ManifestLookup(store)
    assert lookup["DestinyInventoryItemDefinition"][_SMALL_HASH]["hash"] == _SMALL_HASH
    lookup.close()
    # An *uncached* hash must actually reach the connection — and find it gone. A silent
    # reconnect would leak a pooled connection per read after close.
    with pytest.raises(m.ManifestClosed):
        _ = lookup["DestinyInventoryItemDefinition"][_BIG_HASH]


async def test_build_in_thread_runs_off_the_event_loop(store, monkeypatch) -> None:
    """The blessed construction path: sync manifest reads, none of them on the loop."""
    import threading

    monkeypatch.setattr(
        m, "ensure_manifest", lambda _key: _resolved(m.ManifestVersion(store, "v1"))
    )
    loop_thread = threading.get_ident()
    seen: dict[str, object] = {}

    def build(lookup: m.ManifestLookup) -> str:
        seen["thread"] = threading.get_ident()
        return lookup["DestinyInventoryItemDefinition"][_SMALL_HASH][
            "displayProperties"
        ]["name"]

    assert await m.build_in_thread("key", build) == "small"
    assert seen["thread"] != loop_thread


async def _resolved(value):
    return value


# --- prefix search --------------------------------------------------------------------


@pytest_asyncio.fixture
async def prefix_store(tmp_path, monkeypatch):
    await _clear_manifest()
    path = _write_manifest(
        tmp_path / "prefix.content",
        {
            "DestinyVendorDefinition": [
                _row(1, {"vendorIdentifier": "ROTATOR_ALPHA"}),
                _row(2, {"vendorIdentifier": "ROTATOR"}),  # exactly the prefix
                _row(3, {"vendorIdentifier": "ROTATORX"}),  # prefix + non-separator
                _row(4, {"vendorIdentifier": "rotator_alpha"}),  # wrong case
                _row(5, {"vendorIdentifier": "OTHER_ROTATOR"}),  # not at position 0
                _row(6, {"vendorIdentifier": ""}),
                _row(7, {}),  # field absent
            ]
        },
    )
    version_id = await _load(path, "prefix.content")
    monkeypatch.setattr(
        m, "ensure_manifest", lambda _key: _resolved(m.ManifestVersion(version_id, "p"))
    )
    yield version_id
    await _clear_manifest()


async def test_hashes_by_field_prefix_is_exact_and_case_sensitive(
    prefix_store,
) -> None:
    assert await _prefix("ROTATOR") == [1, 2, 3]
    assert await _prefix("ROTATOR_") == [1]
    assert await _prefix("rotator") == [4]
    assert await _prefix("NOPE") == []


async def test_hashes_by_field_prefix_is_not_a_like_pattern(prefix_store) -> None:
    # "ROTATOR_" under LIKE would be "ROTATOR" + any one char, matching ROTATORX too;
    # and "%" would match everything. Neither is a wildcard here.
    assert 3 not in await _prefix("ROTATOR_")
    assert await _prefix("%") == []


async def test_hashes_by_field_prefix_empty_prefix_matches_every_row(
    prefix_store,
) -> None:
    # "".startswith("") is True even for the row with no vendorIdentifier at all, which
    # is what the coalesce to '' buys.
    assert await _prefix("") == [1, 2, 3, 4, 5, 6, 7]


async def test_hashes_by_field_prefix_reads_a_field_that_is_not_there(
    prefix_store,
) -> None:
    assert await _prefix("x", field="nosuchfield") == []


async def _prefix(prefix: str, field: str = "vendorIdentifier") -> list[int]:
    return await m.hashes_by_field_prefix(
        "key", "DestinyVendorDefinition", field, prefix
    )


# --- projections ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def projection_store(tmp_path, monkeypatch):
    await _clear_manifest()
    path = _write_manifest(
        tmp_path / "projection.content",
        {
            "DestinyInventoryItemDefinition": [
                _row(
                    10,
                    {
                        "itemType": 3,
                        "redacted": False,
                        "displayProperties": {"name": "Rifle", "icon": "/r.png"},
                        "itemTypeDisplayName": "Auto Rifle",
                        "inventory": {"tierTypeName": "Legendary"},
                    },
                ),
                _row(
                    11,
                    {
                        "itemType": 2,
                        "displayProperties": {"name": "Helm"},
                        "inventory": {"tierTypeName": "Exotic"},
                    },
                ),
                _row(12, {"itemType": 19, "displayProperties": {"name": "A mod"}}),
            ],
            "DestinyActivityDefinition": [
                _row(
                    20,
                    {
                        "displayProperties": {"name": "A Strike"},
                        "activityModeTypes": [3, 18],
                    },
                ),
            ],
        },
    )
    version_id = await schemas.DestinyManifestVersion.begin_load("projection.content")
    await m._load_tables(version_id, path)
    await schemas.DestinyManifestVersion.activate(version_id)
    monkeypatch.setattr(
        m, "ensure_manifest", lambda _key: _resolved(m.ManifestVersion(version_id, "p"))
    )
    yield version_id
    await _clear_manifest()


async def test_scan_projection_returns_typed_columns(projection_store) -> None:
    rows = await m.scan_projection(
        "key",
        "DestinyInventoryItemDefinition",
        [
            m.Field("itemType", "int"),
            m.Field("displayProperties.name"),
            m.Field("inventory.tierTypeName"),
        ],
    )
    by_hash = {row[0]: row[1:] for row in rows}
    # Ints arrive as ints and nested paths resolve — on both dialects, which is the
    # thing Field.kind exists to guarantee.
    assert by_hash[10] == (3, "Rifle", "Legendary")
    assert by_hash[11] == (2, "Helm", "Exotic")
    # An absent path is None, not an exception and not "".
    assert by_hash[12] == (19, "A mod", None)


async def test_scan_projection_filters_in_the_database(projection_store) -> None:
    rows = await m.scan_projection(
        "key",
        "DestinyInventoryItemDefinition",
        [m.Field("itemType", "int"), m.Field("displayProperties.name")],
        only={"itemType": (2, 3)},
    )
    assert {row[0] for row in rows} == {10, 11}


async def test_scan_projection_reads_a_json_array_as_text(projection_store) -> None:
    rows = await m.scan_projection(
        "key",
        "DestinyActivityDefinition",
        [m.Field("activityModeTypes", "json")],
    )
    assert json.loads(rows[0][1]) == [3, 18]
