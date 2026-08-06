# Rename `beacon` → `director`, `anchor` → `editor`

**Status: stub — intent recorded, not yet planned in detail.**

We intend to rename the two bots to match their actual roles:

- `dd.beacon` → `dd.director` — the public-facing bot *is* Destiny Director.
- `dd.anchor` → `dd.editor` — the authoring side: web UI (rotation/post editing),
  CV2 builder, Bungie API ingestion.

Rationale: `anchor` says nothing about its role ("secondary bot, but larger than it
sounds"); `director`/`editor` describe both bots in plain English and pair naturally
(film: director + editor; also literally what the web UI is).

Scope when planned out (non-exhaustive): `dd/beacon/` and `dd/anchor/` package dirs and
imports, Makefile targets (`run-*-local`, `deploy-*`), CLAUDE.md / docs / skills
(`run-anchor-web`), CI, conventional-commit scopes, Railway service names (out-of-band),
and eventually the Discord display name for DDv1 (e.g. "DD Editor").
