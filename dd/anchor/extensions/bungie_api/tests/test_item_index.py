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

# Pure search/resolution logic over an injected in-memory index (no manifest download).

import json

import pytest

from dd.anchor.extensions.bungie_api import item_index
from dd.anchor.tests.manifest_fixtures import clear_store, load_store, pin_manifest

# The pure search/resolution tests need no database; the three _build tests do, and are
# marked individually rather than marking the module.


@pytest.fixture
def index():
    item_index._index = {
        "chroma rush": [
            {
                "name": "Chroma Rush",
                "hash": 100,
                "type": "Auto Rifle",
                "item_type": 3,
                "icon": "i",
                "collectible": True,
            },
            {
                "name": "Chroma Rush",
                "hash": 50,
                "type": "Auto Rifle",
                "item_type": 3,
                "icon": "i",
                "collectible": False,
            },
        ],
        "wild hunt vest": [
            {
                "name": "Wild Hunt Vest",
                "hash": 200,
                "type": "Hunter Armor",
                "item_type": 2,
                "icon": "i",
                "collectible": True,
            },
        ],
    }
    item_index._index_version = 1
    yield
    item_index._index = None
    item_index._index_version = None


def test_resolve_prefers_collectible_type_match(index):
    # Two "Chroma Rush" entries; the collectible reissue (hash 100) wins.
    assert (
        item_index.resolve_light_gg_url("Chroma Rush (Auto Rifle)")
        == "https://www.light.gg/db/items/100/"
    )


def test_resolve_unknown_name_is_none(index):
    assert item_index.resolve_light_gg_url("Nonexistent (Shotgun)") is None


def test_resolve_prefers_newer_season_over_hash():
    # Two collectible, type-matching "Recluse" copies: the reissue is a NEWER season but
    # a LOWER hash. Season number must win over the hash tiebreak (review finding #10).
    common = {"name": "Recluse", "type": "Submachine Gun", "item_type": 3, "icon": "i"}
    item_index._index = {
        "recluse": [
            {**common, "hash": 900, "collectible": True, "season": 6},  # original
            {**common, "hash": 100, "collectible": True, "season": 23},  # reissue
        ],
    }
    item_index._index_version = 1
    try:
        assert (
            item_index.resolve_light_gg_url("Recluse (Submachine Gun)")
            == "https://www.light.gg/db/items/100/"
        )
    finally:
        item_index._index = None
        item_index._index_version = None


def test_search_weapon_kind(index):
    res = item_index.search("chroma", kind="weapon")
    assert res and res[0]["name"] == "Chroma Rush"
    assert res[0]["url"] == "https://www.light.gg/db/items/100/"
    # deduped by (name, type): only one Chroma Rush entry.
    assert len(res) == 1


def test_search_kind_filter(index):
    assert item_index.search("chroma", kind="armor") == []
    assert item_index.search("wild", kind="armor")[0]["name"] == "Wild Hunt Vest"


def test_cold_index_degrades_gracefully():
    item_index._index = None
    assert not item_index.ready()
    assert item_index.search("anything") == []
    assert item_index.resolve_light_gg_url("Chroma Rush (Auto Rifle)") is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_build_joins_season_numbers(tmp_path, monkeypatch):
    # An item's seasonHash resolves to the season's seasonNumber; an item with no
    # seasonHash falls back to -1.
    version_id = await load_store(
        tmp_path,
        {
            "DestinyInventoryItemDefinition": [
                _item(
                    100,
                    {
                        "seasonHash": 555,
                        "itemType": 3,
                        "itemTypeDisplayName": "Hand Cannon",
                        "displayProperties": {"name": "Fatebringer", "icon": "i"},
                    },
                ),
                _item(
                    200,
                    {
                        "itemType": 3,
                        "itemTypeDisplayName": "Scout Rifle",
                        "displayProperties": {"name": "Jade Rabbit", "icon": "i"},
                    },
                ),
            ],
            "DestinySeasonDefinition": [_item(555, {"seasonNumber": 15})],
        },
    )
    pin_manifest(monkeypatch, version_id)

    index = await item_index._build("key")

    assert index["fatebringer"][0]["season"] == 15
    assert index["jade rabbit"][0]["season"] == -1  # no seasonHash → sentinel
    await clear_store()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_build_survives_a_manifest_with_no_season_table(tmp_path, monkeypatch):
    """The season table isn't in every manifest; without it every item gets -1 rather
    than the build failing (the collectible + hash tiebreak then decides recency)."""
    version_id = await load_store(
        tmp_path,
        {
            "DestinyInventoryItemDefinition": [
                _item(
                    100,
                    {
                        "seasonHash": 555,
                        "itemType": 3,
                        "itemTypeDisplayName": "Hand Cannon",
                        "displayProperties": {"name": "Fatebringer", "icon": "i"},
                    },
                )
            ]
        },
    )
    pin_manifest(monkeypatch, version_id)

    index = await item_index._build("key")

    assert index["fatebringer"][0]["season"] == -1
    await clear_store()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_build_keeps_only_weapons_and_armour_and_needs_a_name(
    tmp_path, monkeypatch
):
    # The item-type filter runs in the database; a nameless row is dropped here. The
    # hash comes from the manifest's own primary key rather than the definition body,
    # so an item whose JSON omits "hash" is still indexed correctly — the old build read
    # it out of the JSON and had to skip such a row.
    version_id = await load_store(
        tmp_path,
        {
            "DestinyInventoryItemDefinition": [
                _item(
                    100,
                    {
                        "itemType": 3,
                        "itemTypeDisplayName": "Auto Rifle",
                        "displayProperties": {"name": "Chroma Rush", "icon": "i"},
                        "collectibleHash": 1,
                    },
                ),
                # A mod: not a weapon or armour, filtered out in SQL.
                (
                    300,
                    json.dumps(
                        {
                            "hash": 300,
                            "itemType": 19,
                            "displayProperties": {"name": "A Mod"},
                        }
                    ),
                ),
                # Named armour, but its body carries no "hash" key.
                (
                    200,
                    json.dumps(
                        {
                            "itemType": 2,
                            "itemTypeDisplayName": "Hunter Armor",
                            "displayProperties": {
                                "name": "Wild Hunt Vest",
                                "icon": "i",
                            },
                        }
                    ),
                ),
                # No display name at all.
                (
                    400,
                    json.dumps({"hash": 400, "itemType": 3, "displayProperties": {}}),
                ),
            ]
        },
    )
    pin_manifest(monkeypatch, version_id)

    index = await item_index._build("key")

    assert index["chroma rush"][0]["hash"] == 100
    assert index["wild hunt vest"][0]["hash"] == 200
    assert "a mod" not in index
    assert len(index) == 2
    await clear_store()


def _item(hash_: int, body: dict) -> tuple[int, str]:
    return (hash_, json.dumps(body | {"hash": hash_}))
