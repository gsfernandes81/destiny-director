"""Destiny 2 manifest download, caching, and in-memory table building."""

import asyncio
import json
import os
import sqlite3
import typing as t
import zipfile
from pathlib import Path

import aiofiles
import aiohttp

from .constants import API_MANIFEST, BUNGIE_NET, manifest_table_names

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
_manifest_lock = asyncio.Lock()


async def _get_latest_manifest(api_key: str) -> str:
    async with _manifest_lock:
        # Prep the manifest directory
        Path("manifest").mkdir(exist_ok=True)

        # Get the latest manifest url from the API
        async with (
            aiohttp.ClientSession(timeout=_MANIFEST_META_TIMEOUT) as session,
            session.get(API_MANIFEST, headers={"X-API-Key": api_key}) as response,
        ):
            manifest_url_fragment = (await response.json())["Response"][
                "mobileWorldContentPaths"
            ]["en"]

        manifest_url_filename = manifest_url_fragment.split("/")[-1]
        # Check if the manifest is already downloaded (a concurrent caller that waited
        # on the lock finds the freshly-extracted file here and returns, no re-fetch)
        if os.path.exists("manifest/" + manifest_url_filename):
            return "manifest/" + manifest_url_filename

        manifest_url = BUNGIE_NET + manifest_url_fragment

        async with (
            aiohttp.ClientSession(timeout=_MANIFEST_DOWNLOAD_TIMEOUT) as session,
            session.get(manifest_url) as response,
        ):
            manifest_zip = await response.read()

        async with aiofiles.open("manifest.zip", "wb") as file:
            await file.write(manifest_zip)

        # Cleanup manifest directory
        for file in os.listdir("manifest"):
            os.remove("manifest/" + file)

        def _extract():
            # Extract the newly downloaded manifest
            with zipfile.ZipFile("manifest.zip", "r") as zip_ref:
                zip_ref.extractall("manifest")

        await asyncio.get_event_loop().run_in_executor(None, _extract)

        manifest_path = "manifest/" + os.listdir("manifest")[0]
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
    ``DestinyInventoryItemDefinition`` being loaded whole. Iteration (``values`` /
    ``items`` / ``iter``) streams and caches the entire table on first use, for the one
    table that needs it (``DestinyVendorDefinition``). ``KeyError`` on a missing hash
    matches the old ``dict[hash]`` behaviour (fail-fast call sites still fail)."""

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
    eververse post; consumers only read specific hashes (plus one ``.values()`` scan of
    ``DestinyVendorDefinition``), so rows are now read on demand. A post drops its
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
    materialising every table. Only the ``DestinyVendorDefinition`` scan and the exact
    hashes a post references are ever loaded, collapsing the per-post memory spike."""
    return ManifestLookup(manifest_path)
