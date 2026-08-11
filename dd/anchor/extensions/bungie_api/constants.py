"""Static Bungie.net API endpoints, manifest tables, and Destiny enums.

Pure module-level constants (plus the tiny ``likely_emoji_name`` helper) with no
project dependencies — everything else in the package imports from here.
"""

BUNGIE_NET = "https://www.bungie.net"
API_ROOT = BUNGIE_NET + "/Platform"

API_GET_MEMBERSHIPS = API_ROOT + "/User/GetMembershipsForCurrentUser/"
API_MANIFEST = API_ROOT + "/Destiny2/Manifest/"
API_OAUTH = (
    BUNGIE_NET
    + "/en/OAuth/Authorize?client_id={client_id}&response_type=code&state={state}"
)
API_OAUTH_GET_TOKEN = API_ROOT + "/App/OAuth/token/"
API_PROFILE = (
    API_ROOT + "/Destiny2/{membership_type}/Profile/{membership_id}/?components=100,200"
)
API_VENDORS = API_ROOT + "/Destiny2/Vendors/"
API_VENDORS_AUTHENTICATED = (
    API_ROOT
    + "/Destiny2/{membershipType}"
    + "/Profile/{destinyMembershipId}"
    + "/Character/{characterId}"
    + "/Vendors/{vendorHash}"
    + "/?components={components}"
)
# Bungie manifest vendor hashes (keys into DestinyVendorDefinition).
XUR_VENDOR_HASH = 2190858386
XUR_STRANGE_GEAR_VENDOR_HASH = 3751514131

# Ada-1 (Gunsmith / armour synthesis). Rotates weekly at the Tuesday reset, selling a
# handful of shaders and one Legendary armour set from the World Loot Pool.
ADA_VENDOR_HASH = 350061650

# Eververse "daily bright dust" rotator vendors are identified in the manifest by a
# ``vendorIdentifier`` starting with this prefix (e.g.
# ``EVERVERSE_BRIGHT_DUST_ROTATOR_EXOTIC_GHOSTS``). Several of these rotate the daily
# exotic/legendary cosmetics sold for Bright Dust.
EVERVERSE_BRIGHT_DUST_ROTATOR_PREFIX = "EVERVERSE_BRIGHT_DUST_ROTATOR"

# The Silver analogue of the above: the daily featured cosmetics sold for Silver (e.g.
# ``EVERVERSE_SILVER_ROTATOR_EXOTIC_GHOSTS``). Same shape as the bright-dust rotators.
EVERVERSE_SILVER_ROTATOR_PREFIX = "EVERVERSE_SILVER_ROTATOR"

# Bungie API ErrorCode returned when a vendor is not currently available.
VENDOR_NOT_FOUND_ERROR_CODE = 1627

ARMOR_TYPE_NAMES = (
    "Helmet",
    "Gauntlets",
    "Chest Armor",
    "Leg Armor",
    "Hunter Cloak",
    "Titan Mark",
    "Warlock Bond",
)

DESTINY_CLASSES_ENUM = ("Titan", "Hunter", "Warlock")

DESTINY_CLASS_TYPE_IDS = {1: "Hunter", 0: "Titan", 2: "Warlock"}

components = (
    # "300,"  # DestinyComponentType.ItemInstances
    "302,"  # DestinyComponentType.ItemPerks
    "304,"  # DestinyComponentType.ItemStats
    # "305,"  # DestinyComponentType.ItemSockets
    # "306,"  # DestinyComponentType.ItemTalentGrids
    # "307,"  # DestinyComponentType.ItemCommonData
    # "308,"  # DestinyComponentType.ItemPlugStates
    # "310,"  # DestinyComponentType.ItemReusablePlugs
    "400,"  # DestinyComponentType.Vendors
    "402"  # DestinyComponentType.VendorSales
)


#: Tables reachable by hash through ``ManifestLookup`` — the keyed lookups the vendor
#: post constructors make.
manifest_table_names = [
    # "DestinyClassDefinition",
    # "DestinyPlaceDefinition",
    # "DestinyPlugSetDefinition",
    "DestinySandboxPerkDefinition",
    "DestinyStatDefinition",
    # "DestinyStatGroupDefinition",
    "DestinyEquipmentSlotDefinition",
    "DestinyCollectibleDefinition",
    "DestinyDestinationDefinition",
    "DestinyInventoryItemDefinition",
    "DestinyPresentationNodeDefinition",
    "DestinyVendorDefinition",
]

#: Tables nothing looks up by hash, but whose *whole contents* are scanned to derive an
#: index: seasons (item recency, ``item_index``) and activities (the weekly-reset strike
#: and conquest pools, ``weekly_reset._scan_activities``).
manifest_scanned_table_names = [
    "DestinySeasonDefinition",
    "DestinyActivityDefinition",
    "DestinyActivityTypeDefinition",
]

#: Everything the loader copies out of Bungie's SQLite into Postgres. Anything absent
#: here simply does not exist as far as the bot is concerned, so a new consumer that
#: needs a new table has to add it *and* trigger a reload — this is the allowlist that
#: keeps the stored manifest to the ~11 tables we read instead of Bungie's ~50.
manifest_stored_table_names = manifest_table_names + manifest_scanned_table_names


DESTINY_ITEM_TYPE_WEAPON = 3
DESTINY_ITEM_TYPE_ARMOR = 2


def likely_emoji_name(name: str) -> str:
    return name.replace(" ", "_").replace("-", "_").lower()
