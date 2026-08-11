"""Destiny 2 manifest storage in Postgres, and the lookups over it.

**The manifest lives in the database, not on local disk.** Bungie ships it as a ~43-90
MB zip containing a ~340 MB SQLite file; this module downloads that *only when Bungie
has actually published a new one*, copies the tables the bot reads
(:data:`~.constants.manifest_stored_table_names`) into
:class:`~dd.common.schemas.DestinyManifestDefinition`, and throws the download away.
Three things follow:

* A redeploy costs nothing. The manifest outlives the container, so a cold start reads
  it straight out of Postgres instead of re-downloading a third of a gigabyte.
* A Bungie outage stops being fatal. The old last line of defence was "reuse whatever is
  extracted in ``manifest/``", which is no answer on a fresh container — there was
  nothing to fall back to (``plans/manifest_backup_in_git.md``). Now there always is.
* No host keeps 340 MB of manifest around: not Railway's ephemeral disk, not the Pi's
  SD card. Only the transient download does, for the minute or two a load takes.

Currency is still Bungie's versioned filename, and still checked on **every** resolve —
a new manifest is a new name, so it can never be served from an old copy. What that
check gates is now a *load*, not a download-and-extract.

Reads come in two shapes and this module answers them differently:

* **Keyed lookups** (``manifest["DestinyInventoryItemDefinition"][hash]``) go through
  :class:`ManifestLookup`, which is **synchronous** because its callers are — the vendor
  model constructors in :mod:`.models` cannot ``await``. It reads through
  :func:`dd.common.schemas.sync_db_engine`, so those callers must run inside
  ``asyncio.to_thread`` (:func:`build_in_thread`); the blocking round-trips then happen
  off the event loop.
* **Whole-table derivations** (the item index, the weapon pool, the activity pools) do
  *not* use :class:`ManifestLookup`. They run projections server-side through the normal
  async session and ship the handful of fields they keep, rather than hundreds of
  megabytes of JSON — see :func:`scan_projection`.
"""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import typing as t
import zipfile
from dataclasses import dataclass

import aiofiles
import aiohttp
from sqlalchemy import and_, func, insert, select
from sqlalchemy.engine import Connection

from dd.common import schemas

from .constants import (
    API_MANIFEST,
    BUNGIE_NET,
    manifest_stored_table_names,
    manifest_table_names,
)

logger = logging.getLogger(__name__)

# Timeouts for the manifest fetch. The metadata call is tiny; the manifest zip
# is large (tens of MB) so it gets a much longer allowance.
_MANIFEST_META_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MANIFEST_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600)

# Serialises the resolve so concurrent callers don't race the load. `_inflight` already
# means there is normally only one resolve running at all; this is kept as the last line
# of defence, since nothing stops a caller reaching `_resolve_manifest` by another route
# in future.
_manifest_lock = asyncio.Lock()

#: The resolve currently running, if any, so a burst of callers shares one rather than
#: each making its own Bungie round-trip. Not a cache — it is dropped as soon as it
#: completes, so the next caller performs a fresh currency check.
_inflight: "asyncio.Task[ManifestVersion] | None" = None

#: Rows moved from the downloaded SQLite into Postgres per statement. Each row is a
#: whole definition — kilobytes for an item, tens of kilobytes for a vendor — so this is
#: sized by *bytes in flight* (a few MB) rather than by row count.
_LOAD_BATCH = 500


@dataclass(frozen=True)
class ManifestVersion:
    """The manifest currently loaded: its row id, and Bungie's name for it."""

    id: int
    version: str


async def prewarm_manifest(api_key: str) -> None:
    """Make sure the current manifest is in the database, ignoring failures.

    Run once at ``StartedEvent``. On a database that already holds the current manifest
    this is a single metadata round-trip and one indexed SELECT — the common case, since
    Bungie ships a manifest every week or two and the bot restarts far more often than
    that. When Bungie *has* shipped a new one this is where the download and load
    happen, so no request wears them.

    Failures are logged and swallowed: a bot that cannot reach Bungie at boot must still
    start, and every consumer already resolves the manifest itself.

    Note this is not the *only* thing that warms the manifest at boot — ``item_index``'s
    own warm (``rotation_editor``) resolves it too, and coalesces onto this one rather
    than loading twice. What this adds is ownership: the warm belongs to the module that
    owns the manifest, instead of being a side effect of whichever consumer happens to
    be loaded.
    """
    if not api_key:
        # Matches item_index.warm: no key is a configuration state, not a failure, and
        # it must not log a warning on every boot of an environment that has none.
        return
    try:
        current = await ensure_manifest(api_key)
    except Exception:
        logger.warning("Manifest prewarm failed; it will be resolved on first use")
        return
    logger.info("Manifest prewarmed: %s", current.version)


async def ensure_manifest(api_key: str) -> ManifestVersion:
    """The manifest readers should be reading, loading it first if it is not current.

    Concurrent callers are **coalesced onto one resolve**: several pages that each need
    the manifest (a weekly-reset form load alone touches the option indexes and the
    weapon pool) share a single metadata round-trip and, when a new manifest has
    shipped, a single load.

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


async def manifest_lookup(api_key: str) -> "ManifestLookup":
    """A keyed lookup over the current manifest. See :class:`ManifestLookup`."""
    return ManifestLookup((await ensure_manifest(api_key)).id)


async def _current_version_fragment(api_key: str) -> str:
    """Bungie's path fragment for the manifest that is current *right now*."""
    async with (
        aiohttp.ClientSession(timeout=_MANIFEST_META_TIMEOUT) as session,
        session.get(API_MANIFEST, headers={"X-API-Key": api_key}) as response,
    ):
        return (await response.json())["Response"]["mobileWorldContentPaths"]["en"]


async def _resolve_manifest(api_key: str) -> ManifestVersion:
    async with _manifest_lock:
        # Ask Bungie which manifest is current — on every resolve, deliberately. This
        # round-trip IS the currency check, and it is what makes the stored copy safe to
        # reuse: Bungie versions the manifest in its own filename, so a new one cannot
        # be mistaken for the one we hold. Skipping the check on a timer was tried and
        # reverted — it bounded staleness at hours, and the window it opened (a mid-week
        # hotfix landing between the check and the post that reads it) is exactly when
        # the definitions matter. The cost it saved is a small JSON GET; the cost that
        # actually hurts is the download-and-load, which this still avoids.
        try:
            fragment = await _current_version_fragment(api_key)
        except Exception:
            # Bungie unreachable. The stored manifest is stale at worst — its content
            # changes every week or two — and is enormously better than failing every
            # autocomplete pool and post derivation until Bungie comes back. Unlike the
            # old on-disk fallback this also answers on a *cold* process, which is the
            # case that used to have nothing to fall back to. No state is written here,
            # so the next call re-checks rather than committing to the stale copy.
            active = await schemas.DestinyManifestVersion.active()
            if active is not None:
                logger.warning(
                    "Could not reach Bungie to check the manifest version; reusing %s",
                    active.version,
                    exc_info=True,
                )
                return ManifestVersion(int(active.id), str(active.version))
            raise

        version = fragment.split("/")[-1]
        active = await schemas.DestinyManifestVersion.active()
        if active is not None and active.version == version:
            return ManifestVersion(int(active.id), str(active.version))

        try:
            version_id = await _download_and_load(fragment, version)
        except schemas.ManifestLoadInProgress:
            # Another anchor container got there first. Wait for nothing — read what is
            # currently active and pick the new manifest up on a later resolve, once its
            # load has finished. Only a database with no manifest at all has to fail.
            if active is not None:
                logger.info(
                    "Manifest %s is being loaded elsewhere; reading %s meanwhile",
                    version,
                    active.version,
                )
                return ManifestVersion(int(active.id), str(active.version))
            raise
        return ManifestVersion(version_id, version)


async def _download_and_load(fragment: str, version: str) -> int:
    """Download Bungie's manifest zip and copy the tables we read into Postgres.

    Everything on disk is confined to one temp directory that is removed on the way out,
    success or failure — the bot keeps no manifest files of its own any more.

    The version row is only flipped to ``active`` once every table is committed, so a
    load interrupted half-way (the container is redeployed mid-import) leaves rows that
    no reader will ever look at, rather than a partial manifest that reads as current.
    """
    version_id = await schemas.DestinyManifestVersion.begin_load(version)
    workdir = tempfile.mkdtemp(prefix="dd-manifest-")
    try:
        zip_path = os.path.join(workdir, "manifest.zip")
        # Stream the zip straight to disk in 1 MiB chunks. ``await response.read()``
        # would buffer the whole payload, and aiohttp builds that by accumulating chunks
        # in a list and then ``b"".join()``-ing them — both are alive at the join, so
        # the peak is ~2x the payload before the file write even starts.
        async with (
            aiohttp.ClientSession(timeout=_MANIFEST_DOWNLOAD_TIMEOUT) as session,
            session.get(BUNGIE_NET + fragment) as response,
            aiofiles.open(zip_path, "wb") as file,
        ):
            async for chunk in response.content.iter_chunked(1 << 20):
                await file.write(chunk)

        def _extract() -> str:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(workdir)
            return os.path.join(workdir, version)

        sqlite_path = await asyncio.to_thread(_extract)
        # The extracted SQLite is all the load needs, so drop the zip now rather than at
        # the end: it is tens of MB of temp disk held for the whole import otherwise.
        os.remove(zip_path)

        await _load_tables(version_id, sqlite_path)
        await schemas.DestinyManifestVersion.activate(version_id)
        return version_id
    except Exception:
        # Leave nothing half-loaded behind. (A load killed outright — SIGKILL, a
        # redeploy — cannot run this, which is why `begin_load` clears an earlier
        # attempt at the same version and `activate` clears every other one.)
        await schemas.DestinyManifestVersion.discard(version_id)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _load_tables(version_id: int, sqlite_path: str) -> None:
    connection = sqlite3.connect(
        f"file:{sqlite_path}?mode=ro", uri=True, check_same_thread=False
    )
    try:
        loaded = {
            table: await _load_table(version_id, connection, table)
            for table in manifest_stored_table_names
        }
    finally:
        connection.close()
    logger.info(
        "Manifest load: %d definitions across %d tables (%s)",
        sum(loaded.values()),
        len([name for name, count in loaded.items() if count]),
        ", ".join(f"{name}={count}" for name, count in loaded.items()),
    )


async def _load_table(
    version_id: int, connection: sqlite3.Connection, table: str
) -> int:
    """Copy one manifest table into Postgres; returns the rows written.

    Every SQLite read *and* the JSON parse happen in a worker thread: a batch is several
    MB of JSON and parsing it on the event loop would stall the bot for the length of
    the load. Committing per batch (rather than one transaction for the table) bounds
    how much uncommitted work a failure throws away and keeps the write-ahead log from
    growing to the size of the manifest.
    """

    def _open() -> sqlite3.Cursor | None:
        try:
            return connection.execute(f'SELECT id, json FROM "{table}"')
        except sqlite3.Error:
            return None

    cursor = await asyncio.to_thread(_open)
    if cursor is None:
        # Bungie's table set moves over time and not every manifest carries every table
        # (DestinySeasonDefinition is the one that has historically been absent). A
        # missing table degrades the consumer that reads it — see item_index's season
        # fallback — rather than failing the whole load.
        logger.warning(
            "Manifest table %r is absent from this manifest; skipping", table
        )
        return 0

    def _next_batch() -> tuple[list[dict[str, t.Any]], int]:
        """The next batch as insert dicts, plus how many raw rows it consumed.

        The two numbers differ when a row's JSON is unparseable, which must skip the row
        and not end the table — hence the raw count as the loop's termination signal.
        """
        raw = cursor.fetchmany(_LOAD_BATCH)
        rows: list[dict[str, t.Any]] = []
        for row_id, blob in raw:
            try:
                definition = json.loads(blob)
            except (ValueError, TypeError):
                continue
            rows.append(
                {
                    "version_id": version_id,
                    "table_name": table,
                    "hash": _to_unsigned_hash(row_id),
                    "definition": definition,
                }
            )
        return rows, len(raw)

    loaded = 0
    while True:
        rows, consumed = await asyncio.to_thread(_next_batch)
        if not consumed:
            return loaded
        if not rows:
            continue
        async with schemas.db_session() as session, session.begin():
            await session.execute(insert(schemas.DestinyManifestDefinition), rows)
        loaded += len(rows)


def _to_unsigned_hash(row_id: int) -> int:
    """Bungie's unsigned 32-bit hash from the signed ``id`` its SQLite stores it under.

    The inverse of the conversion the on-disk lookup used to do on *every* read: the
    manifest's primary key is ``hash`` reinterpreted as a signed 32-bit int, and
    consumers speak unsigned hashes, so this happens once at load instead.
    """
    value = int(row_id)
    return value + 2**32 if value < 0 else value


# ── Reading: keyed lookups ────────────────────────────────────────────────────────────

_MANIFEST_TABLE_NAMES = frozenset(manifest_table_names)

#: Tables small enough to fetch whole on first touch instead of a row at a time. Between
#: them they are a fraction of a megabyte, and they are the *most* frequently keyed —
#: ``DestinyStatDefinition`` alone is read once per stat per item. Fetching them in one
#: query each turns what would be hundreds of round trips per post into four, which is
#: what keeps a threaded post build short. Everything else (items, collectibles,
#: presentation nodes, vendors) stays strictly on-demand: those are the tables whose
#: rows are kilobytes each and whose totals run to hundreds of MB.
_PRELOADED_TABLES = frozenset(
    {
        "DestinyStatDefinition",
        "DestinyEquipmentSlotDefinition",
        "DestinyDestinationDefinition",
        "DestinySandboxPerkDefinition",
    }
)


class ManifestClosed(RuntimeError):
    """Raised when a closed :class:`ManifestLookup` is read from."""


class _LazyManifestTable:
    """Dict-like view over one manifest table, backed by Postgres on demand.

    Keyed access (``table[hash]`` / ``table.get(hash)``) runs a primary-key lookup and
    memoises the parsed row, so only the handful of hashes a post actually touches are
    materialised — this is what stops the ~39k-row ``DestinyInventoryItemDefinition``
    being pulled over the wire whole. ``KeyError`` on a missing hash matches the old
    ``dict[hash]`` behaviour (fail-fast call sites still fail).

    Tables in :data:`_PRELOADED_TABLES` are the exception: they are small and keyed
    constantly, so the first touch fetches the table whole and every later read is a
    dictionary hit.

    Whole-table *searches* are not here at all — :func:`hashes_by_field_prefix` pushes
    the predicate into the database and returns bare hashes. The dict-compatible
    iteration methods (``values`` / ``items`` / ``iter``) do fetch *and cache* the whole
    table, which for ``DestinyVendorDefinition`` costs hundreds of MB (its ``itemList``
    runs to thousands of entries per vendor); they are kept only for dict parity and for
    the preload above, and have **no production caller** on the big tables. Don't add
    one: over a network connection they are worse than they ever were on local SQLite.
    """

    def __init__(self, lookup: "ManifestLookup", table: str) -> None:
        self._lookup = lookup
        self._table = table
        # hash -> definition, or None to memoise a confirmed miss
        self._cache: dict[int, dict[str, t.Any] | None] = {}
        self._all_loaded = False

    def _scoped(self, *columns: t.Any) -> t.Any:
        definitions = schemas.DestinyManifestDefinition
        return select(*columns).where(
            and_(
                definitions.version_id == self._lookup.version_id,
                definitions.table_name == self._table,
            )
        )

    def __getitem__(self, hash_: int) -> dict[str, t.Any]:
        key = int(hash_)
        if self._table in _PRELOADED_TABLES:
            self._load_all()
        if key in self._cache:
            cached = self._cache[key]
            if cached is None:
                raise KeyError(hash_)
            return cached
        if self._all_loaded:
            # Nothing left to ask the database for.
            self._cache[key] = None
            raise KeyError(hash_)
        definitions = schemas.DestinyManifestDefinition
        row = self._lookup.execute(
            self._scoped(definitions.definition).where(definitions.hash == key)
        ).first()
        if row is None:
            self._cache[key] = None
            raise KeyError(hash_)
        parsed: dict[str, t.Any] = row[0]
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
        definitions = schemas.DestinyManifestDefinition
        for hash_, definition in self._lookup.execute(
            self._scoped(definitions.hash, definitions.definition)
        ):
            self._cache[int(hash_)] = definition
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
    """Lazy, Postgres-backed drop-in for the old ``dict[table -> dict[hash -> json]]``.

    Hands out per-table :class:`_LazyManifestTable` views over one manifest version.
    Consumers read specific hashes (plus eververse's rotator search, which is answered
    in SQL and transfers no definitions), so rows are fetched on demand and no table is
    ever materialised whole. A post drops its reference when done; :meth:`close` / the
    context-manager protocol return the connection to the pool explicitly.

    **This is synchronous, and that is deliberate.** Its callers — the vendor model
    constructors in :mod:`.models` — are synchronous several frames deep and cannot
    ``await``; making them async would mean async ``__init__``s. Instead the reads block
    on a real connection, and the callers run inside ``asyncio.to_thread``
    (:func:`build_in_thread`), so nothing blocking ever runs on the event loop. Calling
    into a lookup *from* the loop works but stalls it — don't.

    Subclasses ``dict`` (as an empty base) purely so it satisfies the existing
    ``manifest_table: dict[str, t.Any]`` annotations across the consumers without a wide
    type-churn; all access goes through the overridden ``__getitem__`` / ``get`` /
    ``__contains__``, and nothing reads the empty base (no ``len`` / ``keys`` / truthy
    checks — verified), so the base staying empty is safe.
    """

    def __init__(self, version_id: int) -> None:
        super().__init__()
        self.version_id = version_id
        # Connected lazily, so the (blocking) checkout happens on whichever thread first
        # reads — normally the worker thread, never the event loop.
        self._connection: Connection | None = None
        self._closed = False
        self._tables: dict[str, _LazyManifestTable] = {}

    def execute(self, statement: t.Any) -> t.Any:
        if self._closed:
            raise ManifestClosed("this ManifestLookup has been closed")
        if self._connection is None:
            self._connection = schemas.sync_db_engine().connect()
        return self._connection.execute(statement)

    def __getitem__(self, table_name: str) -> t.Any:
        if table_name not in _MANIFEST_TABLE_NAMES:
            raise KeyError(table_name)
        table = self._tables.get(table_name)
        if table is None:
            table = _LazyManifestTable(self, table_name)
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
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "ManifestLookup":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


async def build_in_thread[T](api_key: str, build: t.Callable[[ManifestLookup], T]) -> T:
    """Run ``build`` against a fresh :class:`ManifestLookup`, off the event loop.

    The one blessed way to do manifest-backed model construction. ``build`` is ordinary
    synchronous code — ``DestinyVendor.from_vendors_api_response`` and everything under
    it — and every manifest read it makes is a blocking database round-trip, so it runs
    in a worker thread and the lookup is closed (its connection returned to the pool)
    before the coroutine resumes.

    The manifest is resolved *before* the thread starts, so the metadata round-trip and
    any load stay async and stay coalesced across callers.
    """
    version = await ensure_manifest(api_key)

    def _run() -> T:
        with ManifestLookup(version.id) as lookup:
            return build(lookup)

    return await asyncio.to_thread(_run)


# ── Reading: whole-table projections ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Field:
    """One JSON path inside a definition, and the type it should come back as.

    The ``kind`` is not decoration. A bare JSON extraction has a different Python type
    on Postgres than on SQLite (``->>`` is always text; ``json_extract`` hands back a
    native int for a number and an *unquoted* string for a string, which no JSON parser
    would accept back), so every projected field names its type and SQLAlchemy's typed
    accessor makes the two dialects agree. ``"json"`` is for arrays and objects: it
    arrives as JSON text on both, for the caller to parse.
    """

    path: str
    kind: t.Literal["text", "int", "bool", "json"] = "text"

    def expression(self) -> t.Any:
        parts = self.path.split(".")
        column = schemas.DestinyManifestDefinition.definition
        indexed = column[parts[0]] if len(parts) == 1 else column[tuple(parts)]
        if self.kind == "int":
            return indexed.as_integer()
        if self.kind == "bool":
            return indexed.as_boolean()
        # "text" and "json" both come back as a string; only the caller differs.
        return indexed.as_string()


async def scan_projection(
    api_key: str,
    table: str,
    fields: t.Sequence[Field],
    *,
    only: t.Mapping[str, t.Sequence[t.Any]] | None = None,
) -> list[tuple[t.Any, ...]]:
    """Every row of ``table`` as ``(hash, *fields)``, projected in the database.

    ``only`` restricts the rows to those whose named field (which must be one of
    ``fields``) holds one of the given values — an ``IN`` pushed down alongside the
    projection, so the ~39k-row item table is cut to the ~7k weapons and armour before
    anything crosses the wire.

    This is how every derived index is built: the item index, the weapon pool and the
    weekly-reset activity pools each keep four or five fields out of definitions that
    are kilobytes each. Selecting them server-side is the difference between shipping a
    few hundred KB and shipping the table — which is what the SQLite implementation
    this replaces had to do, and what made those scans worth caching so hard.
    """
    definitions = schemas.DestinyManifestDefinition
    version = await ensure_manifest(api_key)
    by_path = {field.path: field for field in fields}

    predicates = [
        definitions.version_id == version.id,
        definitions.table_name == table,
    ]
    for path, values in (only or {}).items():
        predicates.append(by_path[path].expression().in_(list(values)))

    statement = select(
        definitions.hash, *(field.expression() for field in fields)
    ).where(and_(*predicates))
    async with schemas.db_session() as session:
        return [tuple(row) for row in await session.execute(statement)]


async def hashes_by_field_prefix(
    api_key: str, table: str, field: str, prefix: str
) -> list[int]:
    """Hashes of every ``table`` row whose top-level ``field`` starts with ``prefix``.

    Equivalent to ``[d["hash"] for d in table.values() if
    d.get(field, "").startswith(prefix)]`` — same hashes — but the predicate runs inside
    the database, so no definition is transferred. That matters for
    ``DestinyVendorDefinition``: eververse's rotator discovery needs one scalar per
    vendor, and reading the rows to get it paid for every nested ``itemList`` (thousands
    of entries per vendor) as well.

    Implementation notes, all of them load-bearing for the equivalence:

    * The prefix test is ``substr(...) = ?``, **not** ``LIKE``: ``LIKE`` treats ``_``
      and ``%`` in the pattern as wildcards (and is ASCII-case-insensitive on SQLite),
      and the identifiers matched on (``EVERVERSE_BRIGHT_DUST_ROTATOR``) are full of
      underscores. ``substr`` is an exact, case-sensitive, wildcard-free prefix compare,
      which is what ``str.startswith`` does. An empty prefix takes a zero-length
      ``substr``, i.e. ``'' == ''``, matching every row — as ``startswith("")`` does.
    * ``coalesce(..., '')`` mirrors ``.get(field, "")`` for a row missing the field.
    * Results are ordered by hash. The SQLite implementation this replaces returned
      rowid order — the same set in a different order; the caller treats it as a set.

    Async, and deliberately not a :class:`ManifestLookup` method: its caller runs it on
    the event loop between two Bungie fetches, not inside a threaded post build.
    """
    definitions = schemas.DestinyManifestDefinition
    version = await ensure_manifest(api_key)
    value = func.coalesce(definitions.definition[field].as_string(), "")
    statement = (
        select(definitions.hash)
        .where(
            and_(
                definitions.version_id == version.id,
                definitions.table_name == table,
                func.substr(value, 1, len(prefix)) == prefix,
            )
        )
        .order_by(definitions.hash)
    )
    async with schemas.db_session() as session:
        return [int(hash_) for (hash_,) in await session.execute(statement)]
