# Rotation data — recovery runbook (DB-JSON store)

> **Historical note.** This file used to document a rollback *to Google Sheets* for the
> `lost_sector` cutover. The Google-Sheets / `gspread` path has since been **removed
> entirely** (see the `rotation/remove-gspread` change): there is no Sheet fallback, no
> `SHEETS_*` config, and no `/rotation import_from_sheet` command. The DB-JSON store is
> now the sole source. The steps below reflect that.

## Current state (what reads from where)

- Rotation data lives in the DB: `rotation_data` table (`RotationData` in
  `dd/common/schemas.py`), edited in the **rotation editor** — the Discord-OAuth web form
  on anchor (`dd/anchor/extensions/rotation_editor.py` + `dd/anchor/web.py`). Reach it at
  `{PUBLIC_BASE_URL}/rotation`, or via `/control_panel` → **Rotation Editor**. (The former
  `/rotation edit` command was removed 2026-08-04; the control panel is the entry point.)
- **`lost_sector`** — `dd/common/lost_sector.py:load_rotation`: DB row → last-known-good
  in-memory cache → **raises** if neither is available (an absent schedule can't render).
  So the row **must exist and be valid** in every deployed environment.
- **`xur_location`** — `dd/anchor/extensions/xur.py:load_xur_locations`: DB row →
  auto-seed from the committed `dd/common/seed_data/xur_location.json` on a clean absent
  read → cache → empty map (renders raw API location names). Lower-risk: it degrades
  rather than raising.
- **`world_activity_*`** — `dd/common/legacy_activities.py`: DB row → auto-seed from
  `dd/common/seed_data/world_activity/<key>.json` → cache.

## Recovering a bad / missing row

No code change or redeploy is needed — fix the data:

1. **Bad content:** open the rotation editor → pick `<type>` → correct the document →
   save (the server re-validates against the JSON schema).
2. **Corrupt / to reset a seeded type** (`xur_location`, `world_activity_*`): delete the
   row so the next read auto-seeds from the committed seed doc —
   `railway connect Postgres` (correct environment), then
   `DELETE FROM rotation_data WHERE name = '<slug>';`. `lost_sector` has **no** seed, so
   don't delete it without a good replacement ready in the editor.
3. Both loaders cache the last-known-good doc in memory, so **restart the bots** to drop
   a cached bad doc and re-read (`railway redeploy -s anchor -y` / `-s beacon -y`), or
   wait out the process lifetime.

## Do NOT roll back

- **The `rotation_data` table + its migration** — additive; migrations auto-apply
  at boot.
- **The web editor / OAuth server** (`rotation_editor.py`, `web.py`, `web_auth.py`) —
  reverting them breaks the rotation editor and `/bungie login`. Needs `PUBLIC_BASE_URL` /
  `RAILWAY_PUBLIC_DOMAIN` set on the environment.

## Verify

- Discord: beacon `/ls today` (incl. lookahead pages) renders the correct sector; a Xûr
  post renders friendly location names (or raw API names if no row/seed).
- Discord: `/bungie login` still completes (OAuth server intact).
- Local: `make check` (lint + typecheck + test) and `uv run python -OOm dd.anchor` boots
  cleanly.
