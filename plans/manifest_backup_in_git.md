# Keep a fallback Destiny manifest in the repo — stub

## Status: deferred note (2026-08-05). Not scoped.

Spun out of the manifest currency work in `dd/anchor/extensions/bungie_api/manifest.py`.
That change made the resolver ask Bungie which manifest is current on **every** resolve,
and added a `StartedEvent` prewarm so no request wears the download. Both leave one hole
open, and this records it rather than pretending it is closed.

## The hole

The resolver's last line of defence when Bungie is unreachable is "reuse whatever is
already extracted in `manifest/`". That is a good answer on a warm process and **no
answer at all on a cold one**: a fresh Railway deploy has an empty `manifest/`, so a
Bungie outage that overlaps a deploy leaves every manifest-backed path failing outright
— `/weekly_reset`'s option pools, the weapon index, and the Xûr / Eververse / Ada /
Portal Ops post constructors. There is nothing to fall back *to*.

It is a narrow window (an outage AND a restart) but it is exactly the window in which
someone is most likely to be restarting the bot — because something is already wrong.

## What the work is

Ship a manifest with the code, as the floor under the disk cache:

- A known-good extracted manifest committed under something like `manifest_fallback/`,
  which `_downloaded_manifests()` (or a sibling) falls back to when `manifest/` is
  empty and Bungie cannot be reached.
- It is **never** written to and never mistaken for current — it is only reachable on
  the failure path, and the resolver should log loudly when it is used, because posts
  built from it can silently carry last-quarter's definitions.

## Open questions before scoping

- **Size.** The extracted world content sqlite is hundreds of MB. That is not a thing to
  put in git history directly — it wants Git LFS, or a trimmed manifest containing only
  the tables the bot actually reads (`manifest_table_names` in `constants.py` is already
  the allowlist, and `_LazyManifestTable` means only a handful of rows are ever touched).
  **A trimmed manifest is the more interesting option**: it might be small enough to
  commit plainly, and it doubles as a test fixture — the manifest is currently untestable
  without a network, which is why `dd/anchor/tests/test_manifest.py` fakes the transport
  rather than the data.
- **How does it get refreshed?** A committed manifest rots. Either a periodic chore (a
  CI job that opens a PR when Bungie ships a new one) or an explicit "this is a floor,
  not a mirror" acceptance that it may be a season behind. The second is fine for its
  actual job — keeping the bot answering during an outage — as long as the logging is
  honest about it.
- **Does it belong in the repo or on the Railway volume?** A persistent volume that
  survives deploys would solve the same problem without the size question. Worth
  checking whether the anchor service has one and whether `manifest/` could live on it —
  that may make this whole plan unnecessary, and is the cheaper thing to try first.

## Related

- `invalidate_manifest_cache()` exists (deletes the extracted copy so the next resolve
  re-downloads) and has **no caller** — it is the escape hatch for a current-but-corrupt
  manifest, which the filename-based currency check cannot see. If this work happens, a
  "refresh the manifest" control on the control panel is the natural place to wire it,
  alongside whatever surfaces which manifest is in use.
