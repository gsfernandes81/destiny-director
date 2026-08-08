"""Destiny 2 manifest download, caching, and in-memory table building.

**The cache is the extracted file on disk**, keyed by the versioned filename Bungie
itself hands out. That makes invalidation fall out for free — a new manifest is a new
name, so it can never be served from an old copy — at the price of one small metadata
round-trip per resolve, which :func:`prewarm_manifest` keeps off the first request.
"""

import asyncio
import json
import logging
import os
import sqlite3
import typing as t
import zipfile
from pathlib import Path

import aiofiles
import aiohttp

from .constants import API_MANIFEST, BUNGIE_NET, manifest_table_names

logger = logging.getLogger(__name__)

# Timeouts for the manifest fetch. The metadata call is tiny; the manifest zip
# is large (hundreds of MB) so it gets a much longer allowance.
_MANIFEST_META_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MANIFEST_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600)

# Serialises the download+extract so concurrent callers don't race the shared
# ``manifest.zip`` / ``manifest/`` paths. Without it, two extensions prewarming the
# manifest at once (e.g. weekly_reset + trials on StartedEvent) each download over the
# same file, one wipes ``manifest/`` while the other is extracting, and both
# ``extractall`` concurrently — corrupting the sqlite ("database disk image is
# malformed"). The first caller downloads; the rest wait and hit the cached path below.
# `_inflight` now means there is normally only one resolve running at all; this is kept
# as the last line of defence, since nothing stops a caller reaching `_resolve_manifest`
# by another route in future.
_manifest_lock = asyncio.Lock()

#: The resolve currently running, if any, so a burst of callers shares one rather than
#: each making its own Bungie round-trip. Not a cache — it is dropped as soon as it
#: completes, so the next caller performs a fresh currency check.
_inflight: "asyncio.Task[str] | None" = None

#: Where the extracted manifest and its download live. Named rather than spelled out at
#: each use because both deferred plans that touch this (a committed fallback manifest,
#: and moving the cache onto a Railway volume — see plans/manifest_backup_in_git.md)
#: turn on being able to point these somewhere else.
_MANIFEST_DIR = "manifest"
_MANIFEST_ZIP = "manifest.zip"


def _downloaded_manifests() -> list[str]:
    """Every extracted manifest on disk, newest first.

    Normally one — a download wipes the directory first — but ordered so ``[0]`` means
    something if an interrupted extract ever leaves two behind.
    """
    try:
        names = os.listdir(_MANIFEST_DIR)
    except FileNotFoundError:
        return []
    paths = [os.path.join(_MANIFEST_DIR, name) for name in names]
    return sorted(
        (p for p in paths if os.path.isfile(p)),
        key=os.path.getmtime,
        reverse=True,
    )


async def prewarm_manifest(api_key: str) -> None:
    """Resolve (and if needed download) the manifest, ignoring failures.

    Run once at ``StartedEvent``. The download is hundreds of MB and the extract is
    slow, so a process that does this lazily makes whichever request arrives first —
    typically a weekly-reset form load or a vendor post — wear the whole cost. Doing it
    at startup means the manifest is on disk before anything asks, and every later
    resolve is a small metadata round-trip plus a local sqlite open.

    Failures are logged and swallowed: a bot that cannot reach Bungie at boot must still
    start, and every consumer already resolves the manifest itself.

    Note this is not the *only* thing that warms the manifest at boot — ``item_index``'s
    own warm (``rotation_editor``) resolves it too, and coalesces onto this one rather
    than downloading twice. What this adds is ownership: the warm now belongs to the
    module that owns the manifest, instead of being a side effect of whichever consumer
    happens to be loaded.
    """
    if not api_key:
        # Matches item_index.warm: no key is a configuration state, not a failure, and
        # it must not log a warning on every boot of an environment that has none.
        return
    try:
        path = await _get_latest_manifest(api_key)
    except Exception:
        logger.warning("Manifest prewarm failed; it will be resolved on first use")
        return
    logger.info("Manifest prewarmed: %s", path)


async def _get_latest_manifest(api_key: str) -> str:
    """The path to the current manifest, downloading it if we do not have it.

    Concurrent callers are **coalesced onto one resolve**: several pages that each need
    the manifest (a weekly-reset form load alone touches the option indexes and the
    weapon pool) share a single metadata round-trip and, on a cold volume, a single
    download. Previously they queued on the lock and each made its own Bungie call once
    it got in — harmless but wasteful, and it multiplied the wait for the last in line.

    ``shield`` so a caller that gives up (a cancelled request) does not cancel the
    resolve that everyone else is waiting on.
    """
    global _inflight
    task = _inflight
    if task is None or task.done():
        # No await between the check and the assignment, so this cannot interleave: a
        # burst of callers all see the same task.
        task = asyncio.create_task(_resolve_manifest(api_key))
        _inflight = task
    return await asyncio.shield(task)


async def _resolve_manifest(api_key: str) -> str:
    async with _manifest_lock:
        # Prep the manifest directory
        Path(_MANIFEST_DIR).mkdir(exist_ok=True)

        # Ask Bungie which manifest is current — on every resolve, deliberately. This
        # round-trip IS the currency check, and it is what makes the extracted file on
        # disk safe to reuse: Bungie versions the manifest in its own filename, so a new
        # one cannot be mistaken for the copy we hold. Skipping the check on a timer was
        # tried and reverted — it bounded staleness at hours, and the window it opened
        # (a mid-week hotfix landing between the check and the post that reads it) is
        # exactly when the definitions matter. The cost it saved is a small JSON GET;
        # the cost that actually hurts is the download, which this still avoids, and
        # which `prewarm_manifest` moves off the first request entirely.
        try:
            async with (
                aiohttp.ClientSession(timeout=_MANIFEST_META_TIMEOUT) as session,
                session.get(API_MANIFEST, headers={"X-API-Key": api_key}) as response,
            ):
                manifest_url_fragment = (await response.json())["Response"][
                    "mobileWorldContentPaths"
                ]["en"]
        except Exception:
            # Bungie unreachable. A manifest already on disk is stale at worst — its
            # content changes every week or two — and is enormously better than failing
            # every autocomplete pool and post derivation until Bungie comes back. No
            # state is written here, so the next call re-checks rather than committing
            # to the stale copy.
            existing = _downloaded_manifests()
            if existing:
                logger.warning(
                    "Could not reach Bungie to check the manifest version; reusing %s",
                    existing[0],
                    exc_info=True,
                )
                return existing[0]
            raise

        manifest_url_filename = manifest_url_fragment.split("/")[-1]
        manifest_path = os.path.join(_MANIFEST_DIR, manifest_url_filename)
        # Check if the manifest is already downloaded (a concurrent caller that waited
        # on the lock finds the freshly-extracted file here and returns, no re-fetch)
        if os.path.exists(manifest_path):
            return manifest_path

        manifest_url = BUNGIE_NET + manifest_url_fragment

        # Stream the zip straight to disk in 1 MiB chunks. ``await response.read()``
        # would buffer the whole 43-90 MB payload, and aiohttp builds that by
        # accumulating chunks in a list and then ``b"".join()``-ing them — both are
        # alive at the join, so the peak is ~2x the payload before the file write even
        # starts. Chunking holds one chunk at a time (and is ~5x faster).
        async with (
            aiohttp.ClientSession(timeout=_MANIFEST_DOWNLOAD_TIMEOUT) as session,
            session.get(manifest_url) as response,
            aiofiles.open(_MANIFEST_ZIP, "wb") as file,
        ):
            async for chunk in response.content.iter_chunked(1 << 20):
                await file.write(chunk)

        # Cleanup manifest directory
        for stale in _downloaded_manifests():
            os.remove(stale)

        def _extract():
            # Extract the newly downloaded manifest
            with zipfile.ZipFile(_MANIFEST_ZIP, "r") as zip_ref:
                zip_ref.extractall(_MANIFEST_DIR)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _extract)
        except Exception:
            # A partial extract is the one failure the currency check above cannot see:
            # it leaves a file with exactly the name Bungie just gave us, so the next
            # resolve finds it, believes it, and hands every consumer a truncated sqlite
            # ("database disk image is malformed") for the rest of the process's life.
            # The realistic cause is running out of disk part-way through ~340MB. Clean
            # up what we wrote so the next resolve downloads again instead.
            logger.warning("Manifest extract failed; discarding the partial copy")
            for partial in _downloaded_manifests():
                os.remove(partial)
            raise

        # The extracted sqlite is all we need, so drop the zip — it is another 43-90 MB
        # of SD card on the Pi / of Railway's ephemeral disk, kept for nothing. Only
        # after a *successful* extract: a failing ``extractall`` raises above this line,
        # leaving the download in place for the next attempt (and for inspection).
        os.remove(_MANIFEST_ZIP)

        # The name Bungie just gave us, not whatever `listdir` happens to return first.
        return manifest_path


_MANIFEST_TABLE_NAMES = frozenset(manifest_table_names)


def _to_signed_id(hash_: int) -> int:
    """Convert a Bungie unsigned 32-bit hash to the signed ``id`` primary key sqlite
    stores it under (``id = hash`` for ``hash < 2**31`` else ``hash - 2**32``)."""
    h = int(hash_)
    return h - 2**32 if h >= 2**31 else h


class _LazyManifestTable:
    """Dict-like view over one manifest table, backed by the sqlite file on demand.

    Keyed access (``table[hash]`` / ``table.get(hash)``) runs an indexed ``WHERE id=?``
    lookup and memoises the parsed row, so only the handful of hashes a post actually
    touches are materialised — this is what stops the ~39k-row
    ``DestinyInventoryItemDefinition`` being loaded whole. ``KeyError`` on a missing
    hash matches the old ``dict[hash]`` behaviour (fail-fast call sites still fail).

    Whole-table *searches* go through :meth:`hashes_by_field_prefix`, which pushes the
    predicate into sqlite and returns bare hashes — nothing is parsed into Python. The
    dict-compatible iteration methods (``values`` / ``items`` / ``iter``) do still
    stream *and cache* the whole table, which for ``DestinyVendorDefinition`` costs
    hundreds of MB (its ``itemList`` runs to thousands of entries per vendor); they are
    kept only for dict parity and have **no production caller** — eververse's rotator
    discovery, the one path that used to need them, is a
    :meth:`hashes_by_field_prefix` call now. Don't add one back."""

    def __init__(self, con: sqlite3.Connection, table: str) -> None:
        self._con = con
        self._table = table
        # hash -> parsed json, or None to memoise a confirmed miss
        self._cache: dict[int, dict[str, t.Any] | None] = {}
        self._all_loaded = False

    def __getitem__(self, hash_: int) -> dict[str, t.Any]:
        key = int(hash_)
        if key in self._cache:
            cached = self._cache[key]
            if cached is None:
                raise KeyError(hash_)
            return cached
        row = self._con.execute(
            f'SELECT json FROM "{self._table}" WHERE id=?', (_to_signed_id(key),)
        ).fetchone()
        if row is None:
            self._cache[key] = None
            raise KeyError(hash_)
        parsed: dict[str, t.Any] = json.loads(row[0])
        self._cache[key] = parsed
        return parsed

    def get(self, hash_: int, default: t.Any = None) -> t.Any:
        try:
            return self[hash_]
        except KeyError:
            return default

    def __contains__(self, hash_: object) -> bool:
        try:
            self[t.cast(int, hash_)]
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def hashes_by_field_prefix(self, field: str, prefix: str) -> list[int]:
        """Hashes of every row whose top-level ``field`` string starts with ``prefix``.

        Equivalent to ``[d["hash"] for d in self.values() if
        d.get(field, "").startswith(prefix)]`` — same hashes, same (rowid) order — but
        the predicate runs *inside* sqlite, so no row is ever parsed into Python. That
        matters for ``DestinyVendorDefinition``: eververse needs two scalars per vendor
        and the ``.values()`` route paid for every nested ``itemList`` to get them.

        Implementation notes, all of them load-bearing for the equivalence:

        * ``json_extract`` is JSON1, which is compiled into CPython's bundled sqlite on
          every platform this ships to (Alpine amd64, ``linux/arm/v6``, the dev boxes),
          so it needs no extension loading.
        * The prefix test is ``substr(...) = ?``, **not** ``LIKE``: sqlite's ``LIKE``
          is ASCII-case-insensitive and treats ``_`` and ``%`` in the pattern as
          wildcards, and the identifiers we match on (``EVERVERSE_BRIGHT_DUST_ROTATOR``)
          are full of underscores. ``substr`` is an exact, case-sensitive,
          wildcard-free prefix compare, which is what ``str.startswith`` does.
        * ``CAST(json AS TEXT)`` because the manifest declares the column ``BLOB`` and
          some rows really do come back as blobs, which ``json_extract`` rejects.
        * ``coalesce(..., '')`` mirrors ``.get(field, "")`` for a row missing the field,
          and the ``json_valid`` guard (in a ``CASE``, whose branch evaluation is
          guaranteed, unlike ``AND`` short-circuiting) skips a corrupt row exactly as
          :meth:`_load_all`'s ``json.loads`` ``try``/``except`` does.

        Nothing is memoised: no row is parsed, so there is nothing to cache.
        """
        rows = self._con.execute(
            f"SELECT json_extract(CAST(json AS TEXT), '$.hash') FROM \"{self._table}\" "
            "WHERE CASE WHEN json_valid(CAST(json AS TEXT)) THEN "
            "substr(coalesce(json_extract(CAST(json AS TEXT), ?), ''), 1, ?) = ? "
            "ELSE 0 END",
            (f"$.{field}", len(prefix), prefix),
        )
        return [int(hash_) for (hash_,) in rows if hash_ is not None]

    def _load_all(self) -> None:
        if self._all_loaded:
            return
        for (raw,) in self._con.execute(f'SELECT json FROM "{self._table}"'):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                continue
            self._cache[int(parsed["hash"])] = parsed
        self._all_loaded = True

    def values(self) -> list[dict[str, t.Any]]:
        self._load_all()
        return [v for v in self._cache.values() if v is not None]

    def items(self) -> list[tuple[int, dict[str, t.Any]]]:
        self._load_all()
        return [(k, v) for k, v in self._cache.items() if v is not None]

    def __iter__(self) -> t.Iterator[int]:
        self._load_all()
        return (k for k, v in self._cache.items() if v is not None)


class ManifestLookup(dict[str, t.Any]):
    """Lazy, sqlite-backed drop-in for the old ``dict[table -> dict[hash -> json]]``.

    Opens the manifest sqlite once (read-only) and hands out per-table
    :class:`_LazyManifestTable` views. Previously every row of every table was parsed
    into a nested dict (hundreds of MB, ~1 GB peak on the item table) on each xur /
    eververse post; consumers only read specific hashes (plus eververse's rotator
    search, which is answered in sqlite and parses nothing — see
    :meth:`_LazyManifestTable.hashes_by_field_prefix`), so rows are now read on
    demand and no table is ever materialised whole. A post drops its
    reference when done and the connection + memoised rows are freed (the sqlite
    connection also finalises itself on GC); :meth:`close` / the context-manager
    protocol are available for explicit cleanup.

    Subclasses ``dict`` (as an empty base) purely so it satisfies the existing
    ``manifest_table: dict[str, t.Any]`` annotations across the consumers without a
    wide type-churn; all access goes through the overridden ``__getitem__`` / ``get`` /
    ``__contains__``, and nothing reads the empty base (no ``len`` / ``keys`` / truthy
    checks — verified), so the base staying empty is safe."""

    def __init__(self, path: str) -> None:
        super().__init__()
        # Read-only (we never write the manifest) and same-thread-tolerant (created
        # here, read from the event-loop thread).
        self._con = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, check_same_thread=False
        )
        self._tables: dict[str, _LazyManifestTable] = {}

    def __getitem__(self, table_name: str) -> t.Any:
        if table_name not in _MANIFEST_TABLE_NAMES:
            raise KeyError(table_name)
        table = self._tables.get(table_name)
        if table is None:
            table = _LazyManifestTable(self._con, table_name)
            self._tables[table_name] = table
        return table

    # Signature mirrors ``dict.get`` (positional-only) so the dict-subclass override is
    # type-compatible; returns the lazy table view for a known table, else ``default``.
    def get(self, key: object, default: t.Any = None, /) -> t.Any:
        try:
            return self[t.cast(str, key)]
        except KeyError:
            return default

    def __contains__(self, table_name: object) -> bool:
        return table_name in _MANIFEST_TABLE_NAMES

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "ManifestLookup":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


async def _build_manifest_dict(manifest_path: str) -> ManifestLookup:
    """Return a lazy, sqlite-backed lookup over the manifest tables.

    Name/signature preserved so callers are untouched; it now returns a
    :class:`ManifestLookup` (a drop-in for the old nested dict) instead of eagerly
    materialising every table. Only the exact hashes a post references are ever
    loaded (eververse's vendor search stays in sqlite), collapsing the per-post
    memory spike."""
    return ManifestLookup(manifest_path)
