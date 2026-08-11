"""Bungie.net API integration for the anchor bot.

Handles the Destiny 2 manifest (stored in Postgres), OAuth token management, and the
authenticated vendor/profile API calls used to build the Xûr and Eververse posts.

This package is the discovered lightbulb extension: it owns ``loader`` and the
``/bungie`` command group, and re-exports the public surface (models, OAuth helpers,
manifest helpers, constants) so importers keep using
``dd.anchor.extensions.bungie_api.<symbol>`` unchanged.
"""

import asyncio

import hikari as h
import lightbulb as lb

from dd.anchor import web
from dd.common import schemas

from . import client
from .constants import (
    ADA_VENDOR_HASH,
    ARMOR_TYPE_NAMES,
    DESTINY_CLASS_TYPE_IDS,
    DESTINY_CLASSES_ENUM,
    EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX,
    EVERVERSE_SILVER_ROTATOR_PREFIX,
    VENDOR_NOT_FOUND_ERROR_CODE,
    XUR_STRANGE_GEAR_VENDOR_HASH,
    XUR_VENDOR_HASH,
    likely_emoji_name,
)
from .manifest import (
    Field,
    ManifestLookup,
    build_in_thread,
    ensure_manifest,
    hashes_by_field_prefix,
    prewarm_manifest,
    scan_projection,
)
from .models import (
    APIOffline,
    DestinyArmor,
    DestinyCollectible,
    DestinyItem,
    DestinyMembership,
    DestinyPresentationNode,
    DestinyVendor,
    DestinyWeapon,
    VendorNotFound,
)
from .oauth import (
    APIOfflineException,
    OAuthStateManager,
    check_bungie_api_online,
    get_webserver_runner,
    oauth_url,
    refresh_api_tokens,
    register_oauth_routes,
    webserver_runner_preparation,
)

__all__ = [
    "client",
    "ADA_VENDOR_HASH",
    "ARMOR_TYPE_NAMES",
    "DESTINY_CLASSES_ENUM",
    "DESTINY_CLASS_TYPE_IDS",
    "EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX",
    "EVERVERSE_SILVER_ROTATOR_PREFIX",
    "VENDOR_NOT_FOUND_ERROR_CODE",
    "XUR_STRANGE_GEAR_VENDOR_HASH",
    "XUR_VENDOR_HASH",
    "likely_emoji_name",
    "Field",
    "ManifestLookup",
    "build_in_thread",
    "ensure_manifest",
    "hashes_by_field_prefix",
    "prewarm_manifest",
    "scan_projection",
    "APIOffline",
    "APIOfflineException",
    "DestinyArmor",
    "DestinyCollectible",
    "DestinyItem",
    "DestinyMembership",
    "DestinyPresentationNode",
    "DestinyVendor",
    "DestinyWeapon",
    "VendorNotFound",
    "OAuthStateManager",
    "check_bungie_api_online",
    "get_webserver_runner",
    "oauth_url",
    "refresh_api_tokens",
    "register_oauth_routes",
    "webserver_runner_preparation",
    "loader",
]

# Serve the Bungie OAuth callback from the anchor's persistent web app (replaces the
# transient per-/bungie-login server). Registered at extension-import time, before the
# gateway reaches StartedEvent where the web app is built and started.
web.register_routes(register_oauth_routes)


loader = lb.Loader()

# No commands live here any more: `/bungie login` and `/bungie account_numbers` moved to
# the web control panel (dd/anchor/extensions/bungie_account.py, `/bungie`). Login in
# particular was a poor fit for Discord — it printed a URL and then blocked for up to 15
# minutes polling for the token, where on the web the redirect back IS the completion
# signal. The loader stays because load_extensions_strict requires one — and, now, for
# the manifest prewarm below.


# Strong references to the background prewarm task: the event loop keeps only a weak ref
# to a bare create_task(), so without this it can be garbage-collected — and cancelled —
# mid-download. Same trap, same fix, as rotation_editor's `_warm_tasks`.
_prewarm_tasks: set["asyncio.Task[None]"] = set()


@loader.listener(h.StartedEvent)
async def _prewarm_manifest_on_start(_event: h.StartedEvent) -> None:
    """Pull the manifest at boot so no request has to wear the download.

    Here rather than in a producer because the manifest is not any one feature's: xur,
    eververse, ada, portal_ops, the weekly-reset option pools and the item index all
    resolve it. It *was* already being pulled at boot — as a side effect of
    ``rotation_editor``'s item-index warm — which is exactly the problem: nothing said
    so, and the guarantee every other consumer now leans on rested on which extension
    happened to be loaded. That warm still runs and coalesces onto this one rather than
    downloading twice.

    Fire-and-forget: ``StartedEvent`` listeners run before the bot is fully up, and on
    the one boot in a fortnight where Bungie has shipped a new manifest this downloads
    and loads it, which takes minutes. Every other boot it is two queries.
    """
    task = asyncio.create_task(prewarm_manifest(schemas.BungieCredentials.api_key))
    _prewarm_tasks.add(task)
    task.add_done_callback(_prewarm_tasks.discard)
