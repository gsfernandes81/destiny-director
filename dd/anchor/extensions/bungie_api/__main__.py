"""Standalone smoke test: fetch Xûr and log the weapons/armor on sale.

Run with ``uv run python -OOm dd.anchor.extensions.bungie_api`` (requires a populated
``.env`` and a prior Bungie OAuth login).
"""

import asyncio
import logging
import typing as t
from functools import partial

import aiohttp

from dd.common import schemas

from .client import fetch_vendor
from .constants import XUR_VENDOR_HASH
from .manifest import ManifestLookup, build_in_thread
from .models import DestinyMembership, DestinyVendor
from .oauth import refresh_api_tokens, webserver_runner_preparation

logger = logging.getLogger(__name__)


def _build_vendor(
    response: dict[str, t.Any], manifest_table: ManifestLookup
) -> DestinyVendor:
    """The synchronous half of a vendor fetch. Runs off the loop; see
    :func:`~.manifest.build_in_thread`."""
    return DestinyVendor.from_vendors_api_response(
        response=response, manifest_table=manifest_table
    )


async def main():
    runner = webserver_runner_preparation()
    access_token = await refresh_api_tokens(runner)

    async with aiohttp.ClientSession() as session:
        destiny_membership = await DestinyMembership.from_api(session, access_token)
        character_id = await destiny_membership.get_character_id(session, access_token)

    for vendor_hash in [XUR_VENDOR_HASH]:
        response = await fetch_vendor(
            access_token,
            destiny_membership.membership_type,
            destiny_membership.membership_id,
            character_id,
            vendor_hash,
        )
        vendor = await build_in_thread(
            schemas.BungieCredentials.api_key, partial(_build_vendor, response)
        )
        logger.info("%s", vendor)
        for item in vendor.sale_items:
            if item.is_armor or item.is_weapon:
                logger.info("%s", item)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
