# Move the Destiny manifest into Postgres

## Status: in progress (2026-08-11). Slice 1 landing; later slices listed below.

## Why

The manifest is the only piece of state the bot keeps on **local disk**, and every
problem it has follows from that:

- **Every cold start pays for it again.** A Railway deploy starts with an empty
  `manifest/`, so the process downloads Bungie's 43–90 MB zip and extracts ~340 MB
  before anything manifest-backed can answer. On the Pi B+ that is a long time on an
  SD card; on Railway it is ephemeral disk written and thrown away on the next deploy.
- **A Bungie outage that overlaps a restart takes the bot's manifest-backed surface
  down completely.** `_resolve_manifest`'s fallback is "reuse whatever is already in
  `manifest/`", which is no answer on a cold process — there is nothing to fall back
  to. This is the hole `plans/manifest_backup_in_git.md` was opened for; a manifest
  that lives in Postgres closes it without committing a fixture to git or paying for a
  Railway volume.
- **Disk cost.** ~340 MB extracted (plus the zip mid-download) on every host that runs
  anchor.

Postgres is already there, already shared, already backed up, and already survives
deploys. Moving the manifest into it removes the disk entirely, makes a redeploy free
(no download at all unless Bungie has actually shipped a new manifest), and turns the
big full-table scans into server-side projections that ship kilobytes instead of
hundreds of megabytes.

## What reads the manifest today

Two shapes of access, and they want different answers.

**1. Keyed lookups, synchronously, deep inside model construction.**
`ManifestLookup` (`dd/anchor/extensions/bungie_api/manifest.py`) is a `dict` subclass
handing out `_LazyManifestTable` views; `models.py` does `manifest_table["Destiny…"][hash]`
all through `DestinyItem.from_sale_item`, `with_stats`, `with_perks`,
`DestinyCollectible.from_collectible_hash`, `DestinyPresentationNode.from_node_hash` and
`DestinyVendor.from_vendors_api_response`. All of it is **sync**, and the entry points
that matter (`from_vendors_api_response`) are sync end to end. Tables:
`DestinyInventoryItemDefinition`, `DestinyEquipmentSlotDefinition`, `DestinyStatDefinition`,
`DestinySandboxPerkDefinition`, `DestinyCollectibleDefinition`,
`DestinyPresentationNodeDefinition`, `DestinyDestinationDefinition`, `DestinyVendorDefinition`
(the `manifest_table_names` allowlist in `constants.py`).

**2. Whole-table scans, already async, that only want a projection.**
- `item_index._build_sync` — every `DestinyInventoryItemDefinition` row, keeping five
  fields, plus `DestinySeasonDefinition` for `seasonNumber`.
- `hybrid_post_core.iter_weapon_items` — every item row, keeping five fields.
- `weekly_reset._scan_activities` — `DestinyActivityDefinition` +
  `DestinyActivityTypeDefinition`, keeping names.

Note (2) reads three tables that are **not** in `manifest_table_names`:
`DestinySeasonDefinition`, `DestinyActivityDefinition`, `DestinyActivityTypeDefinition`.
The Postgres load has to cover them too, so the allowlist has to grow to the real set.

One more consumer: `eververse._rotator_hashes` → `_LazyManifestTable.hashes_by_field_prefix`,
a prefix match on `vendorIdentifier` that today runs inside sqlite and parses no rows.
That has to keep its "no rows parsed" property on the Postgres side.

Only **anchor** reads the manifest. Beacon does not touch it.

## Design

(filled in as the slices land — see the commits on this branch)
