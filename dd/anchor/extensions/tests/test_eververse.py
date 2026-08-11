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

"""Pure-helper tests for the eververse daily bright-dust offerings.

No DB / network / Discord — only the manifest-driven pure functions, exercised with
hand-built fixtures that mirror the live manifest shapes (verified against dev data).
"""

import json

import pytest
import pytest_asyncio

from dd.anchor.extensions.bungie_api import (
    EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX,
    EVERVERSE_SILVER_ROTATOR_PREFIX,
)
from dd.anchor.extensions.bungie_api.manifest import (
    ManifestLookup,
    hashes_by_field_prefix,
)
from dd.anchor.extensions.bungie_api.models import DestinyItem
from dd.anchor.extensions.eververse import (
    _eververse_line,
    _eververse_type_group,
    _exotic_ornament_target_name,
    _group_eververse_offerings,
)
from dd.anchor.tests.manifest_fixtures import clear_store, load_store, pin_manifest

# Vendor rows as (id, vendorDefinition). ``_rotator_hashes`` used to scan
# ``.values()`` in Python and now pushes the prefix test into sqlite, so the fixture is
# a real manifest sqlite (the shape production actually hands it) and every case below
# is asserted against the old ``.values()`` filter, kept verbatim as the reference
# implementation in ``_rotator_hashes_via_values``.
#
# The last four rows are the ones that separate a genuine ``str.startswith`` from the
# obvious-looking ``LIKE 'EVERVERSE_BRIGHT_DUST_ROTATOR%'``: sqlite's LIKE is
# ASCII-case-insensitive and reads ``_`` in the pattern as "any one character", and
# these identifiers are nothing but underscores.
_VENDOR_ROWS: list[tuple[int, dict]] = [
    (
        10,
        {
            "hash": 10,
            "vendorIdentifier": "EVERVERSE_BRIGHT_DUST_ROTATOR_EXOTIC_GHOSTS",
        },
    ),
    (
        20,
        {
            "hash": 20,
            "vendorIdentifier": "EVERVERSE_BRIGHT_DUST_ROTATOR_LEGENDARY_SHADERS",
        },
    ),
    (30, {"hash": 30, "vendorIdentifier": "EVERVERSE_WEEKLY_OFFERINGS"}),
    (40, {"hash": 40, "vendorIdentifier": ""}),
    (50, {"hash": 50}),  # missing vendorIdentifier entirely
    (60, {"hash": 60, "vendorIdentifier": "EVERVERSE_SILVER_ROTATOR_EXOTIC_GHOSTS"}),
    # Exactly the prefix, no suffix — startswith matches; 'PREFIX_%' would not.
    (70, {"hash": 70, "vendorIdentifier": "EVERVERSE_BRIGHT_DUST_ROTATOR"}),
    # Prefix immediately followed by a non-separator — startswith matches.
    (80, {"hash": 80, "vendorIdentifier": "EVERVERSE_BRIGHT_DUST_ROTATORX"}),
    # Same length and letters, wrong separators — LIKE's '_' wildcard would match.
    (90, {"hash": 90, "vendorIdentifier": "EVERVERSEXBRIGHTXDUSTXROTATOR_GHOSTS"}),
    # Lowercase — LIKE is case-insensitive, startswith is not.
    (100, {"hash": 100, "vendorIdentifier": "eververse_bright_dust_rotator_ghosts"}),
    # A vendorIdentifier carrying LIKE's other wildcard, for good measure.
    (110, {"hash": 110, "vendorIdentifier": "EVERVERSE%ROTATOR"}),
]

#: Ids of rows written as blobs rather than text — the manifest column is declared BLOB
#: and real rows do come back that way, which bare ``json_extract`` rejects.
_BLOB_ROW_IDS = frozenset({20, 60})
#: Id of a row holding unparseable JSON: the ``.values()`` path skips it via
#: ``json.loads``' ``except``, and the sqlite path must skip it too rather than
#: aborting the whole query with "malformed JSON".
_CORRUPT_ROW_ID = 120


def _rotator_hashes_via_values(manifest_table, prefix: str) -> list[int]:
    """The pre-pushdown implementation, kept as the equivalence reference."""
    return [
        vendor_def["hash"]
        for vendor_def in manifest_table["DestinyVendorDefinition"].values()
        if vendor_def.get("vendorIdentifier", "").startswith(prefix)
    ]


@pytest_asyncio.fixture
async def vendor_manifest(tmp_path, monkeypatch):
    """The vendor rows, loaded into the store as the active manifest.

    The corrupt row is still fed in: it is skipped at *load* time now (the JSON never
    reaches the database) rather than being stepped over by every query, so neither the
    pushdown nor the reference implementation can see it.
    """
    rows = [
        (
            id_,
            json.dumps(defn).encode() if id_ in _BLOB_ROW_IDS else json.dumps(defn),
        )
        for id_, defn in _VENDOR_ROWS
    ]
    rows.append((_CORRUPT_ROW_ID, "{not json"))
    version_id = await load_store(tmp_path, {"DestinyVendorDefinition": rows})
    pin_manifest(monkeypatch, version_id)
    yield version_id
    await clear_store()


async def _rotators(prefix: str) -> list[int]:
    return await hashes_by_field_prefix(
        "key", "DestinyVendorDefinition", "vendorIdentifier", prefix
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rotator_hashes_filters_by_bright_dust_prefix(vendor_manifest):
    assert await _rotators(EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX) == [10, 20, 70, 80]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rotator_hashes_filters_by_silver_prefix(vendor_manifest):
    assert await _rotators(EVERVERSE_SILVER_ROTATOR_PREFIX) == [60]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "prefix",
    [
        EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX,
        EVERVERSE_SILVER_ROTATOR_PREFIX,
        "EVERVERSE",
        "EVERVERSE_BRIGHT_DUST_ROTATOR_EXOTIC_GHOSTS",
        "SOMETHING_ELSE",
        "eververse",
        "EVERVERSE%",
    ],
)
async def test_rotator_hashes_matches_the_values_scan(vendor_manifest, prefix):
    # The whole point of the pushdown: the same hashes, for every prefix. Compared as
    # sets — the pushdown orders by hash where the values() scan yields whatever order
    # the rows come back in, and the one caller treats the result as a set.
    with ManifestLookup(vendor_manifest) as lookup:
        reference = _rotator_hashes_via_values(lookup, prefix)
    pushed = await _rotators(prefix)
    assert set(pushed) == set(reference)
    assert pushed == sorted(pushed)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rotator_hashes_empty_when_none_match(vendor_manifest):
    assert await _rotators("NOTHING_STARTS_WITH_THIS") == []


def _item_manifest_with(entry: dict) -> dict:
    return {"DestinyInventoryItemDefinition": {999: entry}}


def _item() -> DestinyItem:
    return DestinyItem(
        name="Test Ornament",
        hash_=999,
        rarity="Exotic",
        class_="Hunter",
        bucket="",
        item_type=19,
        item_type_friendly_name="Hunter Ornament",
    )


def test_exotic_armor_ornament_target_resolved():
    manifest = _item_manifest_with(
        {
            "traitIds": ["item.ornament.armor"],
            "displayProperties": {
                "description": (
                    "Equip this ornament to change the appearance of "
                    "Celestial Nighthawk. Once you get an ornament, it's unlocked."
                )
            },
        }
    )
    assert _exotic_ornament_target_name(_item(), manifest) == "Celestial Nighthawk"


def test_exotic_weapon_ornament_target_resolved():
    manifest = _item_manifest_with(
        {
            "traitIds": ["item.ornament.weapon"],
            "displayProperties": {
                "description": (
                    "Equip this weapon ornament to change the appearance of "
                    "Heartshadow. Once you get an ornament, it's unlocked."
                )
            },
        }
    )
    assert _exotic_ornament_target_name(_item(), manifest) == "Heartshadow"


def test_non_ornament_exotic_returns_none():
    # A ghost shell / ship is exotic but not an ornament: no base item, no suffix.
    manifest = _item_manifest_with(
        {
            "traitIds": ["item.ghost"],
            "displayProperties": {
                "description": "For Ghosts who get to the heart of the matter."
            },
        }
    )
    assert _exotic_ornament_target_name(_item(), manifest) is None


def test_ornament_without_matching_description_returns_none():
    manifest = _item_manifest_with(
        {
            "traitIds": ["item.ornament.armor"],
            "displayProperties": {"description": "A mysterious ornament."},
        }
    )
    assert _exotic_ornament_target_name(_item(), manifest) is None


def test_missing_manifest_entry_returns_none():
    assert (
        _exotic_ornament_target_name(_item(), {"DestinyInventoryItemDefinition": {}})
        is None
    )


def _item_of(
    class_: str,
    type_name: str,
    name: str = "X",
    hash_: int = 1,
    cost: int = 100,
    rarity: str = "Legendary",
    currency: str = "Bright Dust",
) -> DestinyItem:
    return DestinyItem(
        name=name,
        hash_=hash_,
        rarity=rarity,
        class_=class_,
        bucket="",
        item_type=2,
        item_type_friendly_name=type_name,
        costs={currency: cost},
    )


def test_eververse_type_group_buckets_class_specific_and_types():
    # Class-specific armor ornaments → one Armor Ornaments group regardless of type.
    assert _eververse_type_group(_item_of("Titan", "Titan Ornament"))[1:] == (
        "armor",
        "Armor Ornaments",
    )
    # Class-agnostic items → curated type group, or pluralised type name as fallback.
    assert _eververse_type_group(_item_of("Unknown", "Weapon Ornament"))[1:] == (
        "weapon",
        "Weapon Ornaments",
    )
    assert _eververse_type_group(_item_of("Unknown", "Ghost Shell"))[1:] == (
        "ghost",
        "Ghosts",
    )
    assert _eververse_type_group(_item_of("Unknown", "Ship"))[1:] == (
        "sparrow",
        "Ships & Sparrows",
    )
    assert _eververse_type_group(_item_of("Unknown", "Shader")) == (4, "", "Shaders")
    # "Emote" and "Multiplayer Emote" both merge into one "Emotes" group.
    assert _eververse_type_group(_item_of("Unknown", "Emote"))[2] == "Emotes"
    assert (
        _eververse_type_group(_item_of("Unknown", "Multiplayer Emote"))[2] == "Emotes"
    )


def test_group_eververse_offerings_orders_groups_and_sorts_items():
    items = [
        _item_of("Unknown", "Shader", name="Zeta Shader", hash_=10),
        _item_of("Hunter", "Hunter Ornament", name="Hrafnagud", hash_=11),
        _item_of("Titan", "Titan Ornament", name="Arcturus Engine", hash_=12),
        _item_of("Unknown", "Ghost Shell", name="Drilldown Shell", hash_=13),
    ]
    groups = _group_eververse_offerings(items)
    headers = [header for _emoji, header, _items in groups]
    # Curated rank: Armor Ornaments, then Ghosts, then the fallback Shaders group.
    assert headers == ["Armor Ornaments", "Ghosts", "Shaders"]
    armor = next(items for _e, h, items in groups if h == "Armor Ornaments")
    # Items within a group are sorted by name.
    assert [i.name for i in armor] == ["Arcturus Engine", "Hrafnagud"]


def test_eververse_line_armor_ornament_puts_class_emoji_with_target():
    # Targets are resolved up front now (see _ornament_targets), so the renderer takes
    # a plain {hash: exotic} map and does no manifest lookup of its own.
    targets = {7: "Hallowfire Heart"}
    armor = _item_of(
        "Titan",
        "Titan Ornament",
        name="Arcturus Engine",
        hash_=7,
        cost=1500,
        rarity="Exotic",
    )
    line = _eververse_line(armor, targets)
    assert line.startswith("• [**Arcturus Engine**](")
    assert "— 1500 (:titan: Hallowfire Heart)" in line


def test_eververse_line_subtypes_and_classless_armor():
    # Armor ornament with no resolvable target → class emoji alone in parens.
    assert _eververse_line(
        _item_of("Hunter", "Hunter Ornament", cost=300), None
    ).endswith("(:hunter:)")
    # Ships/sparrows get a subtype label (sparrows are the "Vehicle" item type).
    assert _eververse_line(_item_of("Unknown", "Ship", cost=2000), None).endswith(
        "· Ship"
    )
    assert _eververse_line(_item_of("Unknown", "Vehicle", cost=2500), None).endswith(
        "· Sparrow"
    )


def test_eververse_line_silver_currency_uses_silver_cost():
    # The Silver section renders the Silver cost (not Bright Dust) via the currency arg.
    ship = _item_of(
        "Unknown", "Ship", name="Threat Display", cost=600, currency="Silver"
    )
    line = _eververse_line(ship, None, "Silver")
    assert line.startswith("• [**Threat Display**](")
    assert "— 600" in line
    assert line.endswith("· Ship")
