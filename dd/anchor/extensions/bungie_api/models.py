"""Destiny domain models parsed from Bungie API / manifest data.

Membership, items (weapons/armor), collectibles, presentation nodes, and vendors.
These are pure parsers — the HTTP fetching lives in :mod:`.client`. The ``from_api``
/ ``request_from_api`` / ``get_character_id`` methods are kept as thin deprecated
wrappers (fetch via ``client`` then parse) so existing call sites keep working;
prefer ``client.fetch_*`` + the ``from_*_response`` / ``parse_*`` parsers.
"""

import typing as t
from pprint import pformat

from .constants import (
    BUNGIE_NET,
    DESTINY_CLASS_TYPE_IDS,
    DESTINY_CLASSES_ENUM,
    DESTINY_ITEM_TYPE_ARMOR,
    DESTINY_ITEM_TYPE_WEAPON,
    likely_emoji_name,
)


class VendorNotFound(Exception):
    def __init__(self, message, api_response=None):
        super().__init__(message)
        self.message = message
        self.api_response = api_response

    def __str__(self) -> str:
        return super().__str__() + "\n" + pformat(self.api_response)


class APIOffline(Exception):
    def __init__(self, api_response):
        self.message = "The Bungie API is currently offline"
        super().__init__(self.message)
        self.api_response = api_response

    def __str__(self) -> str:
        return self.message + "\n" + pformat(self.api_response)


class MissingResponseField(Exception):
    def __init__(
        self,
        field_name: str,
        api_response: dict[str, t.Any],
        request_details: str = "",
    ):
        self.message = (
            f"The expected field '{field_name}' was not found in the API response"
        )
        super().__init__(self.message)
        self.api_response = api_response
        self.request_details = request_details

    def __str__(self) -> str:
        return (
            self.message
            + "\n"
            + self.request_details
            + "\n"
            + pformat(self.api_response)
        )


class DestinyMembership:
    @classmethod
    async def from_api(
        cls,
        session: t.Any,
        access_token: str,
    ) -> t.Self:
        """Deprecated thin wrapper — prefer ``client.fetch_memberships(...)`` +
        :meth:`from_api_response`."""
        from .client import fetch_memberships

        return cls.from_api_response(await fetch_memberships(session, access_token))

    @classmethod
    def from_api_response(cls, response) -> t.Self:
        destiny_memberships = response["destinyMemberships"]
        primary_membership_id = response["primaryMembershipId"]

        primary_membership_type = None
        for membership in destiny_memberships:
            if membership["membershipId"] == primary_membership_id:
                primary_membership_type = membership["membershipType"]
                break

        if primary_membership_type is None:
            raise ValueError(
                "Could not find primary destiny membership type for this bungie account"
            )

        return cls(primary_membership_id, primary_membership_type)

    def __init__(
        self,
        membership_id: int,
        membership_type: int,
    ):
        self.membership_id = int(membership_id)
        self.membership_type = int(membership_type)

    def __repr__(self):
        return f"Destiny Membership: {self.membership_id} ({self.membership_type})"

    async def get_character_id(
        self,
        session: t.Any,
        access_token: str,
        character_class: str = "Hunter",
    ):
        """Deprecated thin wrapper — prefer ``client.fetch_profile(...)`` +
        :meth:`parse_character_id`."""
        from .client import fetch_profile

        profile = await fetch_profile(
            session, access_token, self.membership_type, self.membership_id
        )
        return self.parse_character_id(profile, character_class)

    def parse_character_id(
        self, profile_response: dict[str, t.Any], character_class: str = "Hunter"
    ) -> int:
        """Resolve the character id for ``character_class`` from a profile response."""
        character_id_by_class = {}
        for character_id in profile_response["profile"]["data"]["characterIds"]:
            class_id = profile_response["characters"]["data"][character_id]["classType"]
            character_id_by_class[DESTINY_CLASS_TYPE_IDS[class_id]] = character_id

        return character_id_by_class[character_class]


class DestinyItem:
    @classmethod
    def from_sale_item(
        cls,
        sale_item: dict[str, t.Any],
        # reusable_plugs: dict,
        stats: dict[str, t.Any],
        perks: dict[str, t.Any],
        manifest_table: dict[str, t.Any],
    ):
        hash_ = sale_item["itemHash"]

        manifest_entry = manifest_table["DestinyInventoryItemDefinition"][hash_]

        name: str = manifest_entry["displayProperties"]["name"]
        icon_path: str | None = manifest_entry["displayProperties"].get("icon")
        rarity: str = manifest_entry["inventory"].get("tierTypeName", "Unknown Rarity")
        class_type_id: int = manifest_entry["classType"]
        class_: str = (
            DESTINY_CLASSES_ENUM[class_type_id]
            if class_type_id < len(DESTINY_CLASSES_ENUM)
            else "Unknown"
        )
        bucket_hash = manifest_entry["inventory"]["bucketTypeHash"]
        bucket_entry: dict[str, t.Any] | None = manifest_table[
            "DestinyEquipmentSlotDefinition"
        ].get(bucket_hash)
        bucket: str = ""
        if bucket_entry:
            bucket = (
                bucket_entry["displayProperties"]
                .get("name", "Unknown Slot")
                .replace("Armor", "")
                .strip()
            )

        item_type: int = manifest_entry["itemType"]
        item_type_friendly_name: str = manifest_entry.get(
            "itemTypeDisplayName", "Unknown Type"
        )

        collectible_set_name = (
            (
                DestinyCollectible.from_collectible_hash(
                    manifest_entry["collectibleHash"], manifest_table
                )
                .parent_nodes[0]
                .name
            )
            if "collectibleHash" in manifest_entry
            else None
        )

        costs_data = sale_item.get("costs", [])
        costs = {}
        for cost in costs_data:
            item_hash = cost.get("itemHash", 0)
            quantity = cost.get("quantity", 0)
            if item_hash:
                item_name = (
                    manifest_table["DestinyInventoryItemDefinition"]
                    .get(item_hash, {})
                    .get("displayProperties", {})
                    .get("name", "")
                )

            else:
                item_name = ""
            if item_name:
                costs[item_name] = quantity

        subclass = cls.get_appropriate_subclass(item_type)
        item = subclass(
            name=name,
            hash_=hash_,
            rarity=rarity,
            class_=class_,
            bucket=bucket,
            item_type=item_type,
            item_type_friendly_name=item_type_friendly_name,
            collectible_set_name=collectible_set_name,
            costs=costs,
            icon_path=icon_path,
        )
        item = item.with_stats(stats, manifest_table)
        item = item.with_perks(perks, manifest_table)

        return item

    def __init__(
        self,
        name: str,
        hash_: int,
        rarity: str,
        class_: str,
        bucket: str,
        item_type: int,
        item_type_friendly_name: str,
        collectible_set_name: str | None = None,
        costs: dict[str, int] | None = None,
        icon_path: str | None = None,
    ):
        if costs is None:
            costs = {}
        self.name = name
        self.hash = hash_
        self.rarity = rarity
        self.class_ = class_
        self.bucket = bucket
        self.item_type = item_type
        self.item_type_friendly_name = item_type_friendly_name
        self.collectible_set_name = collectible_set_name
        self.costs = costs
        self.icon_path = icon_path
        self._stats: dict[str, t.Any] = {}
        self._perks: t.Any = []

    def __repr__(self):
        return (
            f"{self.name}\n"
            + f" - Rarity: {self.rarity}\n"
            + f" - Type: {self.item_type_friendly_name}\n"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DestinyItem):
            return NotImplemented
        return self.hash == other.hash

    def __hash__(self):
        return hash(self.hash)

    @staticmethod
    def get_appropriate_subclass(item_type: int) -> "type[DestinyItem]":
        if item_type == DESTINY_ITEM_TYPE_WEAPON:
            return DestinyWeapon
        elif item_type == DESTINY_ITEM_TYPE_ARMOR:
            return DestinyArmor
        else:
            return DestinyItem

    @property
    def is_armor(self) -> bool:
        return self.item_type == DESTINY_ITEM_TYPE_ARMOR

    @property
    def is_weapon(self) -> bool:
        return self.item_type == DESTINY_ITEM_TYPE_WEAPON

    @property
    def is_catalyst(self) -> bool:
        return "catalyst" in self.name.lower()

    @property
    def is_exotic(self) -> bool:
        return self.rarity == "Exotic"

    @property
    def is_legendary(self) -> bool:
        return self.rarity == "Legendary"

    @property
    def lightgg_url(self) -> str:
        return f"https://light.gg/db/items/{self.hash}"

    @property
    def icon_url(self) -> str | None:
        """Absolute Bungie.net URL of the item's icon, or ``None`` if it has none."""
        return f"{BUNGIE_NET}{self.icon_path}" if self.icon_path else None

    @property
    def expected_emoji_name(self) -> str:
        return likely_emoji_name(self.item_type_friendly_name)

    def with_reusable_plugs(
        self, plugs: dict[str, list[t.Any]], manifest_table: dict[str, t.Any]
    ):
        return self

    def with_stats(
        self,
        stats: dict[str, t.Any],
        manifest_table: dict[str, t.Any],
    ) -> t.Self:
        self._stats = {}

        if not stats:
            return self

        if "stats" in stats:
            stats = stats["stats"]

        for stat_group in stats.values():
            stat_hash = stat_group["statHash"]
            stat_value = stat_group["value"]

            stat_name = (
                manifest_table["DestinyStatDefinition"]
                .get(int(stat_hash), {})
                .get("displayProperties", {})
                .get("name")
            )
            if stat_name:
                self._stats[stat_name] = stat_value

        return self

    @property
    def stats(self) -> dict[str, t.Any]:
        return self._stats

    def with_perks(
        self,
        perks: dict[str, t.Any],
        manifest_table: dict[str, t.Any],
    ) -> t.Self:
        self._perks = []

        if not perks:
            return self

        perk_groups: t.Any = perks.get("perks", perks)

        for perk_group in perk_groups:
            perk_entry = manifest_table["DestinySandboxPerkDefinition"][
                perk_group["perkHash"]
            ]
            perk_name = perk_entry["displayProperties"]["name"]

            if not perk_name:
                continue

            self._perks.append(perk_name)

        return self

    @property
    def perks(self) -> list[str]:
        return self._perks


class DestinyWeapon(DestinyItem):
    def __init__(self, *, perks: tuple[tuple[str, ...], ...] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._perks = perks

    @staticmethod
    def _plugs_to_perks(
        plugs_array: dict[str, t.Any], manifest_table: dict[str, t.Any]
    ) -> tuple[tuple[str, ...], ...]:
        # CAUTION: This cannot yet differentiate between masterworks, kill trackets and
        #          actual perks
        if "plugs" in plugs_array:
            plugs_array = plugs_array["plugs"]

        perks = []
        for plugs in plugs_array.values():
            perks_in_array_segment = []
            for plug in plugs:
                plug_hash = plug["plugItemHash"]
                plug_json = manifest_table["DestinyInventoryItemDefinition"][plug_hash]
                perks_in_array_segment.append(plug_json["displayProperties"]["name"])
            perks.append(tuple(perks_in_array_segment))

        return tuple(perks)

    def with_reusable_plugs(
        self, plugs: dict[str, list[t.Any]], manifest_table: dict[str, t.Any]
    ):
        self._perks = self._plugs_to_perks(plugs, manifest_table)
        return self

    def __repr__(self):
        perks: tuple[tuple[str, ...], ...] = self._perks or ()
        return super().__repr__() + (
            f" - Perks: {self._perks_representation(perks)}\n" if perks else ""
        )

    @staticmethod
    def _perks_representation(perks: tuple[tuple[str, ...], ...]) -> str:
        _perks = []
        for perk_group in perks:
            _perks.append(" / ".join(perk_group))
        return " + ".join(_perks)


class DestinyArmor(DestinyItem):
    _plugs: dict[str, t.Any]
    _armor_v2_to_v3_stats_mapping = {
        "Mobility": "Weapons",
        "Resilience": "Health",
        "Recovery": "Class",
        "Discipline": "Grenade",
        "Intellect": "Super",
        "Strength": "Melee",
    }
    _armor_v3_to_v2_stats_mapping = {
        v: k for k, v in _armor_v2_to_v3_stats_mapping.items()
    }
    _tracked_stats = list(_armor_v2_to_v3_stats_mapping.keys()) + list(
        _armor_v2_to_v3_stats_mapping.values()
    )

    def __init__(
        self,
        *,
        stats: dict[str, int] | None = None,
        **kwargs,
    ):
        if stats is None:
            stats = {}
        super().__init__(**kwargs)
        self._intrinsic_stats_added = False
        self._stats = {}
        self._plugs = {}

        for stat in self._armor_v2_to_v3_stats_mapping.values():
            self._stats[stat] = stats.get(stat, 0) + stats.get(
                self._armor_v3_to_v2_stats_mapping[stat], 0
            )

    @staticmethod
    def _get_stat_name(manifest_table: dict[str, t.Any], hash_: int):
        return (
            manifest_table["DestinyStatDefinition"]
            .get(int(hash_), {})
            .get("displayProperties", {})
            .get("name")
        )

    def _add_intrinsic_stats(self, manifest_table: dict[str, t.Any]):
        if self._intrinsic_stats_added:
            return

        manifest_entry: dict[str, t.Any] = manifest_table[
            "DestinyInventoryItemDefinition"
        ][self.hash]

        stats: dict[str, t.Any] = manifest_entry.get("stats", {})
        stats = stats.get("stats", {})

        for stat_hash, stat_dict in stats.items():
            stat_name = self._get_stat_name(manifest_table, stat_hash)
            stat_value = stat_dict["value"]
            if stat_name and stat_name in self.stats:
                self.stats[stat_name] += stat_value

        self._intrinsic_stats_added = True

    def _plugs_to_stats(
        self,
        # ``plugs`` is the raw reusable-plugs JSON. It is shaped as:
        #   {"plugs": {<int-as-str>: [{"canInsert"/"enabled"/"plugItemHash": ...}]}}
        plugs: dict[str, t.Any],
        manifest_table: dict[str, t.Any],
    ) -> None:
        plugs = plugs["plugs"]

        for plug in plugs.values():
            for plug_dict in plug:
                plug_item_hash = plug_dict["plugItemHash"]

                # Pull the plug json from the manifest and work with that to calculate
                # the stats
                plug_json = manifest_table["DestinyInventoryItemDefinition"][
                    plug_item_hash
                ]
                if plug_json["itemType"] == 19:
                    plug_stats: list[dict[str, t.Any]] = plug_json["investmentStats"]
                    for stat_value in plug_stats:
                        stat_hash = stat_value["statTypeHash"]
                        stat_value = stat_value["value"]
                        stat_name = self._get_stat_name(manifest_table, stat_hash)
                        if stat_name and stat_name in self.stats:
                            self.stats[stat_name] += stat_value

    def with_reusable_plugs(
        self, plugs: dict[str, list[t.Any]], manifest_table: dict[str, t.Any]
    ):
        self._plugs = plugs
        # self._plugs_to_stats(plugs, manifest_table)
        # self._add_intrinsic_stats(manifest_table)
        return self

    @property
    def armor_set_name(self) -> str | None:
        if not self.is_armor or self.is_exotic:
            return None
        return self.collectible_set_name

    @property
    def stats(self) -> dict[str, t.Any]:
        self._stats = {
            name: self._stats.get(name, 0)
            for name in self._armor_v2_to_v3_stats_mapping.values()
        }
        return self._stats

    @stats.setter
    def stats(self, stats: dict[str, t.Any]):
        self._stats = {
            name: stats.get(name, 0)
            for name in self._armor_v2_to_v3_stats_mapping.values()
        }

    @property
    def stat_total(self) -> int:
        return sum(self.stats.values())

    def __repr__(self):
        return (
            super().__repr__()
            + (f" - Armor Set: {self.armor_set_name}\n" if self.armor_set_name else "")
            + (
                (
                    " - Stats:\n"
                    + "\n".join(
                        f"   * {stat_name}: {stat_value}"
                        for stat_name, stat_value in self.stats.items()
                    )
                    + "\n"
                    + f"   * Total: {self.stat_total}"
                )
                if self.stats
                else ""
            )
            + "\n"
        )


class DestinyCollectible:
    @classmethod
    def from_collectible_hash(
        cls, collectible_hash: int, manifest_table: dict[str, t.Any]
    ):
        return cls(
            manifest_table["DestinyCollectibleDefinition"][collectible_hash],
            manifest_table,
        )

    def __init__(
        self, collectible_json: dict[str, t.Any], manifest_table: dict[str, t.Any]
    ):
        self._json = collectible_json
        self.name = collectible_json.get("displayProperties", {}).get("name")
        self.description = collectible_json.get("displayProperties", {}).get(
            "description"
        )
        self.hash = collectible_json.get("hash")
        self.collectible_index = collectible_json.get("index")
        self.collectible_item_hash = collectible_json.get("itemHash")
        parent_node_hashes = collectible_json.get("parentNodeHashes") or []
        self.parent_nodes = [
            DestinyPresentationNode.from_node_hash(hash_, manifest_table)
            for hash_ in parent_node_hashes
        ]


class DestinyPresentationNode:
    @classmethod
    def from_node_hash(cls, node_hash: int, manifest_table: dict[str, t.Any]):
        return cls(
            manifest_table["DestinyPresentationNodeDefinition"][node_hash],
            manifest_table,
        )

    def __init__(self, node_json: dict[str, t.Any], manifest_table: dict[str, t.Any]):
        self._json = node_json
        self.name = node_json.get("displayProperties", {}).get("name")
        self.hash = node_json.get("hash")


class DestinyVendor:
    # ``request_from_api`` (fetch + parse in one await) is gone. It could not be used
    # any more: the parse reads the manifest through a synchronous connection and so has
    # to run in a worker thread, which means it cannot be fused with the fetch. Callers
    # do ``client.fetch_vendor(...)`` then ``manifest.build_in_thread(...)`` around
    # :meth:`from_vendors_api_response` — the split this method's own docstring already
    # said to prefer.

    @classmethod
    def from_vendors_api_response(
        cls,
        response: dict[str, t.Any],
        manifest_table: dict[str, t.Any] | None = None,
        manifest_entry: dict[str, t.Any] | None = None,
    ) -> t.Self:
        hash_ = response["vendor"]["data"]["vendorHash"]
        if manifest_entry is None:
            if manifest_table is None:
                raise ValueError(
                    "Either manifest_table or manifest_entry must be provided"
                )
            manifest_entry = manifest_table["DestinyVendorDefinition"][hash_]

        if manifest_entry is None:
            raise RuntimeError("manifest_entry could not be resolved")

        name = manifest_entry.get("displayProperties", {}).get("name")

        _locations_list = manifest_entry.get("locations")
        _location_index = response["vendor"]["data"]["vendorLocationIndex"]

        if (
            manifest_table is not None
            and _locations_list
            and _location_index < len(_locations_list)
        ):
            _destination_hash = _locations_list[_location_index]["destinationHash"]
            location = manifest_table["DestinyDestinationDefinition"][
                _destination_hash
            ]["displayProperties"]["name"]
        else:
            location = None

        _sale_items: dict[str, t.Any] = response["sales"]["data"]
        # _plugs_for_sale_items: dict = response["itemComponents"]["reusablePlugs"][
        #     "data"
        # ]
        _stats_for_sale_items: dict[str, t.Any] = response["itemComponents"]["stats"][
            "data"
        ]
        _perks_for_sale_items: dict[str, t.Any] = response["itemComponents"]["perks"][
            "data"
        ]

        destiny_items_for_sale = []
        if manifest_table is not None:
            for _sale_item_key in _sale_items:
                # _plugs_for_sale_item = _plugs_for_sale_items.get(_sale_item_key, {})
                _destiny_item_for_sale = DestinyItem.from_sale_item(
                    sale_item=_sale_items[_sale_item_key],
                    # reusable_plugs=_plugs_for_sale_item,
                    stats=_stats_for_sale_items.get(_sale_item_key, {}),
                    perks=_perks_for_sale_items.get(_sale_item_key, {}),
                    manifest_table=manifest_table,
                )
                destiny_items_for_sale.append(_destiny_item_for_sale)

        return cls(
            name=name,
            hash_=hash_,
            location=location,
            sale_items=destiny_items_for_sale,
        )

    def __init__(
        self,
        name: str,
        hash_: int,
        location: str | None = None,
        sale_items: list[DestinyItem] | None = None,
    ):
        if sale_items is None:
            sale_items = []
        self.name = name
        self.hash_ = hash_
        self.location = location
        self.sale_items = sale_items

    def __repr__(self):
        repr_ = f"{self.name}" + (f" - {self.location}" if self.location else "")
        repr_ += "\n" + "\n".join(f" - {item}" for item in self.sale_items)
        return repr_

    # Implement addition of vendors to add their sale items
    # Keeping all other properties of self
    def __add__(self, other: "DestinyVendor") -> "DestinyVendor":
        return DestinyVendor(
            name=self.name,
            hash_=self.hash_,
            location=self.location,
            sale_items=self.sale_items + other.sale_items,
        )
