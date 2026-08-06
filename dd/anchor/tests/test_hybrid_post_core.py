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

"""Tests for the shared post core (reset maths, PostSpec, post_spec_nodes).

The rendering these used to assert moved to the shared golden corpus
(``dd/anchor/preview_fixtures``), which holds the Python and JavaScript renderers
to one output rather than checking either alone."""

import dataclasses
import json
import sqlite3
import typing as t

import aiosqlite
import pytest

from dd.anchor import hybrid_post_core as hpc


def test_hybrid_post_spec_has_no_autopost_hooks() -> None:
    # weekly_reset/trials no longer carry a reset-day autopost toggle, so the shared
    # spec dropped its get_autopost/set_autopost hooks entirely.
    fields = {f.name for f in dataclasses.fields(hpc.HybridPostSpec)}
    assert "get_autopost" not in fields
    assert "set_autopost" not in fields


def test_core_has_no_auto_route_handler() -> None:
    # The POST /{prefix}/auto handler that wrote the toggle is removed.
    assert not hasattr(hpc, "auto")


def test_autopostsettings_has_no_weekly_or_trials_toggle() -> None:
    from dd.common import schemas

    aps = schemas.AutoPostSettings
    for name in (
        "get_weekly_reset_enabled",
        "set_weekly_reset",
        "get_trials_enabled",
        "set_trials",
    ):
        assert not hasattr(aps, name), name
    # The generic accessors and other feeds' toggles are untouched.
    assert hasattr(aps, "get_enabled") and hasattr(aps, "get_iron_banner_enabled")


def test_postspec_cv2_factory_and_from_payload() -> None:
    direct = hpc.PostSpec.cv2("# Hi", "https://ex.com/a.png")
    assert direct.kind == "cv2"
    assert direct.body == "# Hi" and direct.image_url == "https://ex.com/a.png"

    # from_payload defaults to cv2 and coerces a blank/missing image to None.
    parsed = hpc.PostSpec.from_payload({"body": "# Hi", "image_url": ""})
    assert parsed == hpc.PostSpec.cv2("# Hi", None)
    assert hpc.PostSpec.from_payload({}) == hpc.PostSpec.cv2("", None)
    assert hpc.PostSpec.from_payload({"kind": "cv2", "body": "x"}).body == "x"


def test_postspec_from_payload_rejects_unknown_kind() -> None:
    # The embed kind (and any other) isn't renderable yet — surfaced as ValueError so a
    # route can 422 it.
    with pytest.raises(ValueError, match="Unsupported post kind"):
        hpc.PostSpec.from_payload({"kind": "embed", "title": "x"})


def test_footer_button_specs() -> None:
    from dd.common import components as c

    # Guides first, then the standard Support button.
    assert c.footer_button_specs(guides=[("Guide", "https://g.example")]) == [
        ("Guide", "https://g.example"),
        ("Support Us", c.KOFI_URL),
    ]
    # No guides -> just the Support button (e.g. Portal Ops / Weekly Reset).
    assert c.footer_button_specs() == [("Support Us", c.KOFI_URL)]
    # A row caps at 5 buttons, so at most 4 guides.
    with pytest.raises(ValueError):
        c.footer_button_specs(guides=[("a", "https://x")] * 5)


# --- post_spec_nodes ------------------------------------------------------------------


def _drop_defaults(value: t.Any) -> t.Any:
    """Strip hikari's explicit defaults so two descriptions of a post compare.

    ``build_cv2`` goes through hikari's builders, which spell out ``spoiler: false`` and
    ``disabled: false`` and hand back URL objects; ``post_spec_nodes`` writes the raw
    JSON Discord actually needs. Neither difference is a difference in the post.
    """
    defaults = {"spoiler": False, "disabled": False}
    if isinstance(value, dict):
        return {
            k: _drop_defaults(v)
            for k, v in value.items()
            if not (k in defaults and v == defaults[k])
        }
    if isinstance(value, list):
        return [_drop_defaults(v) for v in value]
    if isinstance(value, str):
        return value
    return str(value) if type(value).__name__ == "URL" else value


@pytest.mark.parametrize(
    "body,image,buttons",
    [
        ("just a body", None, ()),
        ("with an image", "https://example.com/i.png", ()),
        ("with buttons", None, (("Guide", "https://example.com/g"),)),
        (
            "everything",
            "https://example.com/i.png",
            (("Guide", "https://example.com/g"), ("Support", "https://example.com/s")),
        ),
    ],
)
def test_post_spec_nodes_matches_build_cv2(
    body: str, image: str | None, buttons: tuple
) -> None:
    """The preview's tree IS the post's tree.

    This is the pin that makes retiring the old ``.post-*`` previewer safe: rather than
    a second markup vocabulary approximating the post, the previewer renders the very
    node list ``build_cv2`` sends. If the two ever diverge, the preview stops being a
    preview — so compare them directly, on every shape a producer emits.
    """
    live, _ = hpc.build_cv2(body, image, buttons=buttons).components[0].build()
    spec = hpc.PostSpec.cv2(body, image, buttons=buttons)

    assert _drop_defaults(live) == _drop_defaults(hpc.post_spec_nodes(spec)[0])


@pytest.mark.parametrize(
    "image,kinds",
    [
        # Text, then the gallery — the order build_cv2 sends. Placement used to be the
        # previewer's business; it is the post's now.
        ("https://ex.com/a.png?x=1&y", [10, 12]),
        (None, [10]),
        # Matching the renderer, which refuses a non-http(s) media URL — better an
        # absent image in the preview than one the post will not carry.
        ("javascript:alert(1)", [10]),
        ("ftp://example.com/i.png", [10]),
    ],
)
def test_post_spec_nodes_places_the_image_and_rejects_bad_urls(
    image: str | None, kinds: list[int]
) -> None:
    spec = hpc.PostSpec.cv2("# Title", image)
    assert [c["type"] for c in hpc.post_spec_nodes(spec)[0]["components"]] == kinds


# --- resolve_weapon -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["²", "³", "①", "⑵"])
def test_resolve_weapon_survives_a_non_decimal_digit(value: str) -> None:
    """A digit `int()` refuses must not reach it.

    ``str.isdigit()`` is true for superscripts and enclosed forms; ``int()`` only takes
    decimal ones. The gap used to raise ValueError out of the weekly-reset and trials
    forms, where a free-typed weapon name is the intended fallback — so the interesting
    assertion is that these resolve to a plain name rather than blowing up.
    """
    assert hpc.resolve_weapon(value, []) == hpc.WeaponRef(name=value)


def test_resolve_weapon_still_matches_a_real_hash() -> None:
    items: list[hpc.WeaponItem] = [
        ("Null Composure", 222, "Fusion Rifle", 3, "legendary")
    ]
    assert hpc.resolve_weapon("222", items) == hpc.WeaponRef(
        "Null Composure", 222, hpc.api.likely_emoji_name("Fusion Rifle")
    )
    # Arabic-Indic digits are decimal, so int() takes them — but nobody types those
    # meaning a manifest hash, so they stay a name.
    assert hpc.resolve_weapon("٢٢٢", items) == hpc.WeaponRef(name="٢٢٢")


# --- iter_weapon_items ----------------------------------------------------------------
#
# The scan reads the item table in fetchmany batches instead of one fetchall(), which
# used to materialise every raw JSON string in the manifest's largest table before a
# single one was parsed. Batching is only worth shipping if it is output-identical, so
# the pre-fix body is kept verbatim below and the two are asserted equal.


async def _iter_weapon_items_via_fetchall(cursor) -> list[hpc.WeaponItem]:
    """The pre-fix implementation, kept as the equivalence reference."""
    item_by_key: dict[tuple[str, str], hpc.WeaponItem] = {}
    await cursor.execute("SELECT json FROM DestinyInventoryItemDefinition")
    for (row,) in await cursor.fetchall():
        defn = json.loads(row)
        item_type = defn.get("itemType")
        if item_type not in (2, 3) or defn.get("redacted"):
            continue
        rarity = (defn.get("inventory") or {}).get("tierTypeName", "")
        if rarity in ("", "Common", "Basic"):
            continue
        name = (defn.get("displayProperties") or {}).get("name")
        if not name:
            continue
        type_name = defn.get("itemTypeDisplayName", "")
        hash_ = int(defn["hash"])
        key = (name.lower(), type_name.lower())
        existing = item_by_key.get(key)
        if existing is None or hash_ > existing[1]:
            item_by_key[key] = (name, hash_, type_name, item_type, rarity)
    return sorted(item_by_key.values(), key=lambda e: e[0].lower())


def _item_defn(
    hash_: int,
    name: str | None = "Weapon",
    *,
    item_type: int = 3,
    rarity: str = "Legendary",
    type_name: str = "Hand Cannon",
    redacted: bool = False,
) -> dict:
    defn: dict = {
        "hash": hash_,
        "itemType": item_type,
        "itemTypeDisplayName": type_name,
        "inventory": {"tierTypeName": rarity},
    }
    if name is not None:
        defn["displayProperties"] = {"name": name}
    if redacted:
        defn["redacted"] = True
    return defn


def _weapon_pool_rows() -> list[dict]:
    """Rows covering every filter, dedupe and ordering branch of the scan.

    Deliberately longer than one fetchmany batch (200) so the batching loop is
    exercised across boundaries, with the interesting rows sprinkled through it rather
    than all in the first batch.
    """
    rows: list[dict] = []
    # Bulk filler: distinct names, so none of them dedupe against each other.
    for i in range(500):
        rows.append(_item_defn(1_000 + i, f"Filler {i:03d}"))
    # Dropped: wrong itemType (armour=2 and weapon=3 are the keepers).
    rows.insert(5, _item_defn(50, "Dropped Emblem", item_type=14))
    rows.insert(210, _item_defn(51, "Dropped Mod", item_type=19))
    # Dropped: redacted / dummy / white / green.
    rows.insert(7, _item_defn(52, "Dropped Redacted", redacted=True))
    rows.insert(215, _item_defn(53, "Dropped Basic", rarity="Basic"))
    rows.insert(220, _item_defn(54, "Dropped Common", rarity="Common"))
    rows.insert(225, _item_defn(55, "Dropped Rarityless", rarity=""))
    # Dropped: no name at all, and an empty name.
    rows.insert(9, _item_defn(56, None))
    rows.insert(230, _item_defn(57, ""))
    # Kept: armour, and a differing-case duplicate that must dedupe with the newest
    # hash winning — split across batches so the batching cannot hide a dedupe bug.
    rows.insert(11, _item_defn(60, "Alpha Lupi", item_type=2, type_name="Chest Armor"))
    rows.insert(300, _item_defn(70, "alpha lupi", item_type=2, type_name="chest armor"))
    rows.insert(400, _item_defn(65, "ALPHA LUPI", item_type=2, type_name="CHEST ARMOR"))
    # Kept: same name, different type — a distinct pool entry, not a dedupe.
    rows.insert(310, _item_defn(80, "Ill Omen", type_name="Hand Cannon"))
    rows.insert(320, _item_defn(81, "Ill Omen", type_name="Sniper Rifle"))
    return rows


@pytest.fixture
def item_manifest(tmp_path):
    path = str(tmp_path / "world.content")
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE DestinyInventoryItemDefinition (id INTEGER PRIMARY KEY, json)"
        )
        con.executemany(
            "INSERT INTO DestinyInventoryItemDefinition (id, json) VALUES (?, ?)",
            [(i, json.dumps(defn)) for i, defn in enumerate(_weapon_pool_rows())],
        )
        con.commit()
    finally:
        con.close()
    return path


@pytest.mark.asyncio
async def test_iter_weapon_items_matches_the_fetchall_reference(item_manifest) -> None:
    async with aiosqlite.connect(item_manifest) as con:
        batched = await hpc.iter_weapon_items(await con.cursor())
        reference = await _iter_weapon_items_via_fetchall(await con.cursor())
    assert batched == reference
    assert batched  # a fixture that filtered everything out would prove nothing


@pytest.mark.asyncio
async def test_iter_weapon_items_filters_dedupes_and_sorts(item_manifest) -> None:
    async with aiosqlite.connect(item_manifest) as con:
        items = await hpc.iter_weapon_items(await con.cursor())
    names = [item[0] for item in items]
    assert not any(name.startswith("Dropped") for name in names)
    assert "" not in names
    # 500 filler + one deduped Alpha Lupi + two Ill Omens (distinct types).
    assert len(items) == 503
    # Dedupe is case-insensitive on (name, type) and the newest hash wins; the name is
    # the winning row's own, not the first-seen one.
    assert [i for i in items if i[0].lower() == "alpha lupi"] == [
        ("alpha lupi", 70, "chest armor", 2, "Legendary")
    ]
    assert sorted({i[1] for i in items if i[0] == "Ill Omen"}) == [80, 81]
    assert names == sorted(names, key=str.lower)


@pytest.mark.asyncio
async def test_iter_weapon_items_batches_rather_than_fetching_everything() -> None:
    """The fetchall() the fix removed must not come back."""
    calls: list[str] = []

    class _Cursor:
        def __init__(self) -> None:
            self._batches = [[(json.dumps(_item_defn(1, "Solo")),)], []]

        async def execute(self, _sql: str) -> None:
            calls.append("execute")

        async def fetchmany(self, size: int) -> list:
            calls.append(f"fetchmany({size})")
            return self._batches.pop(0)

        async def fetchall(self) -> list:
            raise AssertionError("iter_weapon_items must not call fetchall()")

    items = await hpc.iter_weapon_items(_Cursor())
    assert [i[0] for i in items] == ["Solo"]
    assert calls == ["execute", "fetchmany(200)", "fetchmany(200)"]
