"""Developer utility for looking up Destiny item ids by name.

Not loaded by the bot. Resolves an item name to its inventory-item hashes using
the downloaded Bungie manifest, for ad-hoc debugging.
"""

import asyncio
from pathlib import Path
from pprint import pprint

from dd.anchor.extensions import bungie_api as b
from dd.common import schemas

FILE_PATH = Path(__file__).parent.parent.parent / "getprofile.json"
ITEM_NAME = "Ferropotent Robes"


async def item_ids_from_name(item_name: str) -> list[str]:
    """Every inventory-item hash whose display name matches, as strings.

    A projection rather than a walk over the item table: the name is the only field
    this compares, so it is the only field worth transferring.
    """
    rows = await b.scan_projection(
        schemas.BungieCredentials.api_key,
        "DestinyInventoryItemDefinition",
        [b.Field("displayProperties.name")],
    )

    wanted = item_name.lower().strip()
    item_ids: list[str] = []
    for item_id, name in rows:
        if wanted == (name or "").lower().strip():
            print(f"Found Item ID: {item_id}, Name: {name}")
            item_ids.append(str(item_id))

    return item_ids


async def search_file(path: Path, item_name: str) -> dict[int, str]:
    """Get the line number of an int in a file."""

    item_ids = await item_ids_from_name(item_name)

    with path.open("r", encoding="utf-8") as f:
        data = f.readlines()

    results = {}
    for line_number, line in enumerate(data):
        if any(str(item_id) in line for item_id in item_ids):
            results[line_number] = line

    return results


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(search_file(FILE_PATH, ITEM_NAME))
    pprint(results)
