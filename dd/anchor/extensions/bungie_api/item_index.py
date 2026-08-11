"""A name → item index over the Destiny manifest, for the rotation editor.

Powers weapon/armor name autocomplete and light.gg link resolution. Built once from the
stored manifest (weapon + armor items only) and held in memory; consumers read it
synchronously. Everything degrades gracefully when the index isn't warm yet or no Bungie
API key is configured — autocomplete returns nothing and link resolution returns
``None`` rather than blocking a request on the manifest.

The build is a **projection**, not a scan: the seven fields an entry keeps are selected
in the database, so what crosses the wire is those fields for the ~7k weapons and
armour, not ~39k definitions of a few kilobytes each. That is the shape this index
always wanted — it discards almost everything it reads — and it is only affordable now
that the manifest is somewhere that can do the discarding.
"""

import asyncio
import logging
import typing as t

from .constants import DESTINY_ITEM_TYPE_ARMOR, DESTINY_ITEM_TYPE_WEAPON
from .manifest import Field, ManifestVersion, ensure_manifest, scan_projection

logger = logging.getLogger(__name__)

LIGHT_GG_URL = "https://www.light.gg/db/items/{}/"

# Built index: name.lower() -> list of entries. Only the latest manifest is kept.
_index: dict[str, list[dict[str, t.Any]]] | None = None
_index_version: int | None = None
_build_lock = asyncio.Lock()

_ITEM_FIELDS = (
    Field("itemType", "int"),
    Field("displayProperties.name"),
    Field("itemTypeDisplayName"),
    Field("displayProperties.icon"),
    Field("collectibleHash", "int"),
    Field("seasonHash", "int"),
)


def _plain_name(value: str) -> str:
    """The bare item name from a stored value like ``Chroma Rush (Auto Rifle)``."""
    return value.split(" (")[0].strip()


async def _season_numbers(api_key: str) -> dict[int, int]:
    """``seasonHash → seasonNumber`` from the manifest, or ``{}`` if unavailable.

    ``seasonNumber`` is monotonic across releases (unlike item hashes, which aren't
    assigned in release order), so it's the authoritative "which of two same-named
    weapons is newer" key. The season table isn't in every manifest, and one that lacks
    it simply loads no rows, so a missing table degrades to no season data — the
    resolver then falls back to the collectible + hash tiebreak."""
    rows = await scan_projection(
        api_key, "DestinySeasonDefinition", [Field("seasonNumber", "int")]
    )
    return {int(hash_): int(number) for hash_, number in rows if number is not None}


async def _build(api_key: str) -> dict[str, list[dict[str, t.Any]]]:
    """Project the manifest item table into a name index."""
    seasons = await _season_numbers(api_key)
    rows = await scan_projection(
        api_key,
        "DestinyInventoryItemDefinition",
        _ITEM_FIELDS,
        only={"itemType": (DESTINY_ITEM_TYPE_WEAPON, DESTINY_ITEM_TYPE_ARMOR)},
    )
    index: dict[str, list[dict[str, t.Any]]] = {}
    for (
        item_hash,
        item_type,
        raw_name,
        type_name,
        icon,
        collectible_hash,
        season_hash,
    ) in rows:
        name = (raw_name or "").strip()
        if not name:
            continue
        index.setdefault(name.lower(), []).append(
            {
                "name": name,
                "hash": int(item_hash),
                "type": type_name or "",
                "item_type": item_type,
                "icon": icon or "",
                "collectible": bool(collectible_hash),
                # -1 sorts below any real season, so items without a season fall back
                # to the hash tiebreak (exactly the prior behaviour for them).
                "season": seasons.get(season_hash, -1),
            }
        )
    return index


async def warm(api_key: str) -> None:
    """Build (or rebuild on a manifest update) the index. Safe to call repeatedly.

    Loads the manifest if it isn't already stored (slow, once per manifest Bungie
    publishes) then projects it. A no-op without an API key. Call from a background task
    so requests never block on it.
    """
    global _index, _index_version
    if not api_key:
        return
    async with _build_lock:
        try:
            version: ManifestVersion = await ensure_manifest(api_key)
        except Exception:
            logger.exception("item_index: could not fetch the manifest")
            return
        if version.id == _index_version and _index is not None:
            return
        try:
            index = await _build(api_key)
        except Exception:
            # Never let a manifest-read failure kill the warm task uncaught — that
            # surfaces only as a stray "Task exception was never retrieved" log and
            # leaves any previously-built index in place. Log and bail; a later manifest
            # update (or restart) re-attempts the build.
            logger.exception("item_index: could not build the index from the manifest")
            return
        _index, _index_version = index, version.id
        logger.info("item_index: built (%d names)", len(index))


def ready() -> bool:
    return _index is not None


def resolve_light_gg_url(value: str) -> str | None:
    """Best-effort light.gg URL for a weapon value (``Name (Type)``), or ``None``.

    Prefers a weapon whose type matches the ``(Type)`` hint and that is collectible
    (in-game obtainable), then the newest reissue by season number (falling back to the
    highest hash when season data is unavailable)."""
    if _index is None:
        return None
    name = _plain_name(value)
    entries = _index.get(name.lower())
    if not entries:
        return None
    type_hint = value[len(name) :].strip(" ()").lower()

    def score(entry: dict[str, t.Any]) -> tuple:
        entry_type = (entry["type"] or "").lower()
        return (
            entry["item_type"] == DESTINY_ITEM_TYPE_WEAPON,
            bool(entry_type) and type_hint.startswith(entry_type),
            entry["collectible"],
            # seasonNumber is the authoritative recency key (item hashes aren't
            # chronological); hash is only the final fallback when seasons tie / are -1.
            entry.get("season", -1),
            entry["hash"],
        )

    return LIGHT_GG_URL.format(max(entries, key=score)["hash"])


def search(
    query: str, kind: str | None = None, limit: int = 20
) -> list[dict[str, t.Any]]:
    """Name-substring search for autocomplete. ``kind`` filters to ``weapon``/``armor``.

    Returns ``{name, type, hash, url, icon}`` dicts, prefix matches and collectibles
    first, deduped by (name, type)."""
    if _index is None:
        return []
    q = query.lower().strip()
    if not q:
        return []
    want = {"weapon": DESTINY_ITEM_TYPE_WEAPON, "armor": DESTINY_ITEM_TYPE_ARMOR}.get(
        kind
    )

    matches: list[dict[str, t.Any]] = []
    for name_lower, entries in _index.items():
        if q not in name_lower:
            continue
        for entry in entries:
            if want is not None and entry["item_type"] != want:
                continue
            matches.append(entry)

    matches.sort(
        key=lambda e: (
            not e["name"].lower().startswith(q),
            not e["collectible"],
            e["name"],
        )
    )

    seen: set[tuple[str, str]] = set()
    results: list[dict[str, t.Any]] = []
    for entry in matches:
        key = (entry["name"], entry["type"])
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "name": entry["name"],
                "type": entry["type"],
                "hash": entry["hash"],
                "url": LIGHT_GG_URL.format(entry["hash"]),
                "icon": entry["icon"],
            }
        )
        if len(results) >= limit:
            break
    return results
