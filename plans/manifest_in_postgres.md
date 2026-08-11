# Move the Destiny manifest into Postgres

## Status: done (2026-08-11). Kept for the measurements and the open levers at the end.

Everything described below has landed. What is left in this file that is *not* history:
the storage/throughput measurements (§ Numbers), the two levers not pulled (§ Levers),
and the deploy note (§ Rolling this out).

## Why

The manifest was the only piece of state the bot kept on **local disk**, and every
problem it had followed from that:

- **Every cold start paid for it again.** A Railway deploy starts with an empty
  `manifest/`, so the process downloaded Bungie's 43–90 MB zip and extracted ~340 MB
  before anything manifest-backed could answer — ephemeral disk written, then thrown
  away on the next deploy.
- **A Bungie outage overlapping a restart took the manifest-backed surface down
  completely.** The resolver's fallback was "reuse whatever is already in `manifest/`",
  which is no answer on a cold process — there was nothing to fall back to. This is the
  hole the (now deleted) `plans/manifest_backup_in_git.md` was opened for; a manifest
  in Postgres closes it without committing a fixture to git or paying for a volume.
- **Disk cost.** ~340 MB extracted (plus the zip mid-download) on every host running
  anchor.

Postgres is already there, already shared, already backed up, and already survives
deploys. Moving the manifest into it removes the disk entirely, makes a redeploy free
(no download at all unless Bungie has actually shipped a new manifest), and turns the
big full-table scans into server-side projections that ship kilobytes instead of
hundreds of megabytes.

## What reads the manifest

Two shapes of access, and they want different answers. This is the inventory the move
was planned against; both shapes are described as they were *before* it.

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

One more consumer: `eververse._rotator_hashes`, a prefix match on `vendorIdentifier`
that ran inside sqlite and parsed no rows. It had to keep that "no rows parsed" property
on the Postgres side, and does — as a module-level async `hashes_by_field_prefix`, since
its caller runs it on the event loop between two Bungie fetches.

Only **anchor** reads the manifest. Beacon does not touch it.

## Design

**Storage.** `destiny_manifest_version` (one row per manifest loaded: Bungie's versioned
filename, a state, timestamps) and `destiny_manifest_definition`
(`(version_id, table_name, hash) -> definition`). The hash stored is Bungie's *unsigned*
32-bit hash, converted once at load from the signed `id` its SQLite keys rows under,
because consumers speak unsigned. `definition` is JSONB on Postgres, JSON on SQLite —
see § Numbers for why JSONB and not text.

**Loading.** A resolve asks Bungie which manifest is current (as before, on every
resolve — that check is what makes reuse safe). If it is the one already active, nothing
happens. Otherwise the zip is streamed into a temp directory, extracted, copied table by
table into Postgres in 500-row batches, and the temp directory is removed. The version
row is `loading` until every table is in, so an interrupted load leaves rows nothing
will read rather than a partial manifest that reads as current.

**Two reader shapes.** Keyed lookups keep the synchronous `ManifestLookup` and run inside
`build_in_thread` (`asyncio.to_thread`), so their blocking round-trips stay off the event
loop. Whole-table derivations use `scan_projection`, which selects the four or five
fields they keep — with the item-type filter pushed down — instead of transferring
definitions. `hashes_by_field_prefix` does the same for eververse's rotator discovery.

### Two decisions that are easy to get wrong

**A replaced manifest is reclaimed at the start of the *next* load, not when it is
replaced.** A post build resolves a version id once and reads through it for a minute.
Deleting its rows the moment a newer manifest activates would turn mid-render lookups
into misses — and the consumers are full of `.get(hash, {})` defaults that would swallow
them and render "Unknown Type" into a published post rather than fail. Deferring makes
that window a week wide. The cost is one superseded manifest kept around: two on disk,
steady state, which is what Postgres would have held in dead tuples anyway.

**The `version` unique constraint is the cross-process lock.** The old `asyncio.Lock`
and the on-disk filename were per-container; Postgres is shared. Two anchor containers
both noticing a new manifest race a single INSERT; the loser gets
`ManifestLoadInProgress` and reads whatever is already active. An abandoned claim (the
winner was killed mid-load) is reclaimable after `LOAD_LEASE` — 30 minutes, comfortably
longer than a load and far shorter than the gap between manifests.

## Numbers

Measured on Postgres 16 against a live manifest, loading the eleven allowlisted tables
(65,316 rows, 262 MB of raw JSON) through this repo's own pure-Python psycopg:

| | JSONB | TEXT |
|---|---|---|
| stored | 134 MB (166 MB relation) | 114 MB (140 MB relation) |
| point lookup by `(version, table, hash)` | 0.42 ms | 0.20 ms |
| weapon-pool projection over the item table | **0.83 s** | **10.8 s** |
| load, whole table set | 24 s | 8.5 s |

TEXT is smaller and faster to read a single row, and loses decisively on the projections
this move exists to make cheap — 10.8 s is Postgres re-parsing 198 MB of item JSON on
every scan. JSONB parses once, at load.

Load mechanism is *not* the bottleneck: for the item table, `COPY` text (10.4 s), `COPY`
binary (10.2 s) and `executemany` (10.5 s) are within noise of each other, because the
cost is server-side JSONB parsing and WAL, not client-side formatting. We use batched
`executemany` through SQLAlchemy Core: one code path that works identically on Postgres
and on the SQLite the test suite runs against, at no measurable cost in throughput.

## Levers

Neither is pulled, and neither needs a migration to pull later — a reload rebuilds the
table from scratch in well under a minute.

- **Prune fields we never read.** Projecting the item definitions down to the dozen
  fields the bot touches takes them from 198.6 MB raw to **28.2 MB** (stored 27.7 MB,
  load 1.1 s, projection 0.15 s). Not done, because pruning fails *silently*: `models.py`
  reads with defaults everywhere (`.get("itemTypeDisplayName", "Unknown Type")`), so a
  field pruned by mistake renders a wrong string into a published post instead of
  raising. The currently-commented-out `_plugs_to_stats` / `_add_intrinsic_stats` paths
  want `investmentStats` and `stats.stats`, and would be landmines for whoever uncomments
  them. Pull this if storage ever bites.
- **`COPY` instead of `executemany`.** Constant client memory and no batch sizing, at the
  price of a Postgres-only code path the SQLite test suite cannot exercise. See the
  numbers above for why it is not worth it yet.

## Rolling this out

The rollback is `git revert` + redeploy, and it is genuinely cheap: the manifest is
regenerable, so the reverted code simply downloads it again on boot exactly as it used
to. Nothing needs migrating back — the two tables can sit unused (or be dropped by
reverting the Alembic revision).

Worth watching on the first deploy: the initial load is a download plus ~65k rows into
an empty table, so anchor's first boot after this takes a couple of minutes longer than
usual before the weekly-reset pickers and item autocomplete answer. Every boot after
that is a metadata GET and one SELECT.

## What this does *not* cover

A brand-new database **and** a Bungie outage at the same time still has no manifest —
there is nothing to fall back to, and the resolve raises. That is the one sliver of
`plans/manifest_backup_in_git.md` (now deleted) this does not close, and it is
acceptable: it needs both a first-ever deploy and an outage to coincide, where the old
design failed on *every* deploy that coincided with one.

Not covered either: the Pi. It runs beacon, which never reads the manifest, so its
Postgres does not carry these tables. If anchor is ever deployed there, the load's
client-side cost on ARMv6 needs looking at before it is.

