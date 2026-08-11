# Copyright © 2019-present gsfernandes81

# This file is part of "dd" henceforth referred to as "destiny-director".

# destiny-director is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.

# "destiny-director" is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License along with
# destiny-director. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import asyncio as aio
import datetime as dt
import enum
import logging
import os
import re
import sys
import typing as t
from dataclasses import dataclass
from typing import Self

from sqlalchemy import (
    Index,
    bindparam,
    case,
    create_engine,
    exists,
    literal,
    or_,
    tuple_,
)
from sqlalchemy.dialects.postgresql import (
    JSONB,
    insert as pg_insert,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    Mapped,
    aliased,
    declarative_base,
    mapped_column,
    validates,
)
from sqlalchemy.sql import insert, select, text, update
from sqlalchemy.sql.expression import and_, delete, desc
from sqlalchemy.sql.functions import coalesce, func
from sqlalchemy.sql.schema import CheckConstraint, Column, UniqueConstraint
from sqlalchemy.sql.sqltypes import (
    JSON,
    VARCHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
)

from dd.common import cfg
from dd.common.utils import FriendlyValueError, check_number_of_layers, ensure_session

Base = declarative_base()


class _SessionmakerProxy:
    """Stable indirection over an ``async_sessionmaker``.

    The object identity is captured by ``@ensure_session`` at import time and by
    ``from schemas import db_session`` in the beacon extensions. Calling the proxy
    forwards to the *current* inner sessionmaker, so rebinding the inner maker (via
    ``configure_test_db``) transparently repoints every existing call site at a new
    engine without re-importing or re-decorating anything."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    def __call__(self) -> AsyncSession:
        return self._sessionmaker()

    def rebind(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker


def _build_engine() -> AsyncEngine:
    return create_async_engine(
        cfg.db_url_async, connect_args=cfg.db_connect_args, **cfg.db_engine_args
    )


db_engine: AsyncEngine = _build_engine()
db_session = _SessionmakerProxy(async_sessionmaker(db_engine, **cfg.db_session_kwargs))


def configure_test_db(engine: AsyncEngine) -> None:
    """Repoint the module-global engine and the ``db_session`` proxy at ``engine``.

    Used by the test harness to swap the production Postgres engine for a throwaway
    SQLite engine. Affects every ``@ensure_session`` method, every direct
    ``db_session()`` call site, and every module-level ``db_engine`` reader at once."""
    global db_engine
    db_engine = engine
    db_session.rebind(async_sessionmaker(engine, **cfg.db_session_kwargs))


def reset_db() -> None:
    """Restore the production engine/sessionmaker (call on test teardown)."""
    global db_engine
    db_engine = _build_engine()
    db_session.rebind(async_sessionmaker(db_engine, **cfg.db_session_kwargs))
    dispose_sync_db_engine()


#: The synchronous engine and the URL it was built for. Rebuilt (not just reused) when
#: ``db_engine`` is repointed, so ``configure_test_db`` moves both halves together.
_sync_engine: Engine | None = None
_sync_engine_url: URL | None = None


def _sync_url() -> URL:
    """``db_engine``'s URL, with the async driver swapped for its sync counterpart.

    Derived from the *live* engine rather than ``cfg.db_url`` so it follows
    ``configure_test_db`` onto the test SQLite file. Postgres needs no swap —
    ``postgresql+psycopg`` is the same driver either way.
    """
    url = db_engine.url
    return url.set(drivername="sqlite") if url.get_backend_name() == "sqlite" else url


def sync_db_engine() -> Engine:
    """A **synchronous** engine over the same database ``db_engine`` targets.

    Exists for exactly one caller: the Destiny manifest lookup
    (:mod:`dd.anchor.extensions.bungie_api.manifest`), whose consumers read it from deep
    inside synchronous model construction — ``DestinyItem.from_sale_item`` and friends
    cannot ``await``. Those reads run inside ``asyncio.to_thread``, so the blocking
    calls this engine makes never touch the event loop; the project's "no blocking I/O
    in coroutine paths" rule is kept by moving the *caller* off the loop, not by
    pretending the driver is async.

    Nothing else should use this. Every other DB access in the codebase is already
    async and belongs on ``db_session``.
    """
    global _sync_engine, _sync_engine_url
    url = _sync_url()
    if _sync_engine is None or _sync_engine_url != url:
        dispose_sync_db_engine()
        # Derived from the URL in hand, not cfg: under pytest the configured database
        # may be Postgres while the engine actually in use is the temp-file SQLite, and
        # SQLite rejects the ``-c timezone=UTC`` connect option psycopg wants.
        connect_args = (
            {} if url.get_backend_name() == "sqlite" else dict(cfg.db_connect_args)
        )
        engine_args: dict[str, t.Any] = {"pool_pre_ping": True, "pool_recycle": 3600}
        if url.get_backend_name() != "sqlite":
            # AUTOCOMMIT, because these connections are held open for the length of a
            # post build. In a read transaction that would pin the oldest visible xmin
            # for a minute at a time and block autovacuum — which matters more here than
            # anywhere else in the schema, since replacing the manifest deletes a couple
            # of hundred MB of TOASTed rows that vacuum then has to reclaim.
            engine_args["isolation_level"] = "AUTOCOMMIT"
            # A small, hard-capped pool. This sits *alongside* the async engine's
            # 5+10, and the Pi's Postgres runs with max_connections=25 (see
            # cfg.db_engine_args) — the manifest never needs more than the handful of
            # post builds in flight at once, so it does not compete for those slots.
            engine_args["pool_size"] = 2
            engine_args["max_overflow"] = 2
        _sync_engine = create_engine(url, connect_args=connect_args, **engine_args)
        _sync_engine_url = url
    return _sync_engine


def dispose_sync_db_engine() -> None:
    """Drop the synchronous engine and its pool, if one was ever built."""
    global _sync_engine, _sync_engine_url
    if _sync_engine is not None:
        _sync_engine.dispose()
    _sync_engine = None
    _sync_engine_url = None


# Sentinel used as default for session parameters in @ensure_session-decorated methods.
# The decorator always supplies a real AsyncSession before the function body runs.
_UNSET: AsyncSession = t.cast(AsyncSession, None)


async def wait_for_db(retry_interval: float = 10.0) -> None:
    """Block until the database accepts a connection, retrying every
    retry_interval seconds."""
    while True:
        try:
            async with db_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except OperationalError:
            logging.warning("Database unavailable, retrying in %ss...", retry_interval)
            await aio.sleep(retry_interval)


# Anchor with \Z (end of string) rather than $, since $ also matches just
# before a trailing newline and would let an illegal character like "ping\n"
# slip through as a valid command name.
rgx_cmd_name_is_valid = re.compile(r"^[a-z][a-z0-9_-]{1,31}\Z")
rgx_sub_cmd_name_is_valid = re.compile(r"^[a-z]{0,1}[a-z0-9_-]{0,31}\Z")
# The difference between command and sub command name validator regexes is
# that the sub command regex needs to allow blank strings to indicate and
# match blanks for commands that aren't 3 layers deep (where the last and)
# potentially the second last layer will be blank


class MirroredChannel(Base):
    """Mirror channels model

    with a cache for the list of all legacy source channgel ids only.
    Note, the src_ids cache will not remove elements from the cache even
    if the last mirror from it has been disabled"""

    __tablename__ = "mirrored_channel"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (UniqueConstraint("src_id", "dest_id", name="_mir_ids_uc"),)
    src_id = Column("src_id", BigInteger, primary_key=True)
    dest_id = Column("dest_id", BigInteger, primary_key=True)
    dest_server_id = Column("dest_server_id", BigInteger)
    legacy = Column("legacy", Boolean)
    enabled = Column("enabled", Boolean, default=True)
    role_mention_id = Column("role_mention_id", BigInteger, default=None)
    # Auto-disable is driven by a separate low-load reachability sweep, not the delivery
    # hot path: ``unreachable_since`` is stamped when a probe first finds a destination
    # unreachable / lacking send perms, cleared the moment it is reachable again; once
    # it has stayed unreachable past the grace window the pair is disabled and the date
    # below is stamped so an owner can undo a sweep by date.
    unreachable_since = Column("unreachable_since", DateTime, default=None)
    legacy_disable_for_failure_on_date = Column(
        "legacy_disable_for_failure_on_date", DateTime, default=None
    )
    _legacy_srcs_cache = set()

    def __init__(
        self,
        src_id: int,
        dest_id: int,
        dest_server_id: int,
        legacy: bool,
        enabled: bool,
        role_mention_id: int | None,
    ):
        super().__init__()
        self.src_id = int(src_id)
        self.dest_id = int(dest_id)
        self.dest_server_id = dest_server_id and int(dest_server_id)
        self.legacy = bool(legacy)
        self.enabled = bool(enabled)
        self.role_mention_id = role_mention_id and int(role_mention_id)

    @classmethod
    @ensure_session(db_session)
    async def add_mirror(
        cls,
        src_id: int,
        dest_id: int,
        dest_server_id: int,
        legacy: bool,
        enabled: bool = True,
        role_mention_id: int | None = 0,
        session: AsyncSession = _UNSET,
    ) -> None:
        src_id = int(src_id)
        dest_id = int(dest_id)
        await session.merge(
            cls(
                src_id,
                dest_id,
                dest_server_id,
                legacy,
                enabled=enabled,
                role_mention_id=role_mention_id,
            )
        )

        if legacy and src_id not in cls._legacy_srcs_cache:
            cls._legacy_srcs_cache.add(src_id)

    @classmethod
    @ensure_session(db_session)
    async def fetch_dests(
        cls,
        src_id: int,
        legacy: bool | None = True,
        enabled: bool | None = True,
        session: AsyncSession = _UNSET,
    ) -> list[int]:
        """Fetch all dests for a given src_id

        src_id -> The source channel ID
        legacy -> True: Fetch legacy only, False: Fetch non-legacy only, None: Fetch all
        enabled -> True: Fetch enabled only, False: Fetch disabled only, None: Fetch all
        """
        src_id = int(src_id)
        dests = await session.execute(
            select(cls.dest_id)
            .where(
                and_(
                    cls.src_id == src_id,
                    (cls.legacy == legacy) if legacy is not None else True,
                    (cls.enabled == enabled) if enabled is not None else True,
                )
            )
            .join(
                ServerStatistics,
                cls.dest_server_id == ServerStatistics.id,
                isouter=True,
            )
            .order_by(
                desc(coalesce(ServerStatistics.population, 10**12)),
            )
        )

        dests = dests if dests else []
        dests = [dest[0] for dest in dests]
        return dests

    @classmethod
    @ensure_session(db_session)
    async def fetch_all_channel_ids(
        cls,
        session: AsyncSession = _UNSET,
    ) -> set[int]:
        """Every distinct source + destination channel id across all mirrors.

        Scopes the beacon gateway channel cache (see
        ``dd.common.scoped_cache.ScopedChannelCacheImpl``): only channels that are a
        mirror source or destination are ever read from the cache, so only these are
        worth keeping resident. Disabled mirrors are included so a temporarily-disabled
        destination stays cache-warm for the reachability sweep.
        """
        srcs = await session.execute(select(cls.src_id).distinct())
        dests = await session.execute(select(cls.dest_id).distinct())
        return {int(r[0]) for r in srcs} | {int(r[0]) for r in dests}

    @classmethod
    @ensure_session(db_session)
    async def fetch_mirror_and_role_mention_id(
        cls,
        src_id: int,
        session: AsyncSession = _UNSET,
    ) -> dict[int, int]:
        """Fetch all dests with corresponding mirror_ping_role id for a given src_id

        src_id -> The source channel ID
        """
        src_id = int(src_id)
        mention_ids = await session.execute(
            select(cls.dest_id, cls.role_mention_id).where(and_(cls.src_id == src_id))
        )

        mention_ids = mention_ids if mention_ids else []
        mention_ids = {dest[0]: dest[1] for dest in mention_ids}
        return mention_ids

    @classmethod
    @ensure_session(db_session)
    async def fetch_srcs(
        cls,
        dest_id: int,
        legacy: bool | None = True,
        enabled: bool | None = True,
        session: AsyncSession = _UNSET,
    ) -> list[int]:
        dest_id = int(dest_id)
        srcs = (
            await session.execute(
                select(cls.src_id).where(
                    and_(
                        cls.dest_id == dest_id,
                        (cls.legacy == legacy) if legacy is not None else True,
                        (cls.enabled == enabled) if enabled is not None else True,
                    )
                )
            )
        ).fetchall()
        srcs = srcs if srcs else []
        srcs = [src[0] for src in srcs]
        return srcs

    @classmethod
    @ensure_session(db_session)
    async def get_or_fetch_all_srcs(
        cls,
        legacy: bool | None = True,
        session: AsyncSession = _UNSET,
    ) -> set[int]:
        """Fetch all srcs

        WARNING: This is function has a silent failure mode where it will return
        src_ids that may have been deleted with the removal of the last mirror
        with this src. This is intentional to avoid the overhead of clearing
        and refetching the cache on every mirror removal.

        If you need to ensure that the returned src_ids are valid, use
        fetch_all_srcs instead"""
        if legacy and cls._legacy_srcs_cache:
            return cls._legacy_srcs_cache
        else:
            srcs = await cls.fetch_all_srcs(legacy=legacy, session=session)
            if legacy:
                cls._legacy_srcs_cache = set(srcs)
            return srcs

    @classmethod
    @ensure_session(db_session)
    async def fetch_all_srcs(
        cls,
        legacy: bool | None = True,
        session: AsyncSession = _UNSET,
    ) -> set[int]:
        srcs = (
            await session.execute(select(cls.src_id).where(cls.legacy == legacy))
        ).fetchall()
        srcs = srcs if srcs else []
        srcs = [src[0] for src in srcs]
        return set(srcs)

    @classmethod
    @ensure_session(db_session)
    async def count_dests(
        cls,
        src_id: int,
        legacy_only: bool | None = True,
        session: AsyncSession = _UNSET,
    ) -> int:
        src_id = int(src_id)
        dests_count = (
            await session.execute(
                select(func.count())
                .select_from(cls)
                .where(
                    and_(
                        cls.enabled,
                        cls.src_id == src_id,
                        (cls.legacy == legacy_only)
                        if legacy_only is not None
                        else True,
                    )
                )
            )
        ).scalar_one()

        return dests_count

    @classmethod
    @ensure_session(db_session)
    async def count_total_dests(
        cls,
        legacy_only: bool | None = True,
        session: AsyncSession = _UNSET,
    ) -> int:
        dests_count = (
            await session.execute(
                select(func.count())
                .select_from(cls)
                .where(
                    (cls.legacy == legacy_only) if legacy_only is not None else True,
                )
            )
        ).scalar_one()

        return dests_count

    @classmethod
    @ensure_session(db_session)
    async def set_legacy(
        cls,
        src_id: int,
        dest_id: int,
        legacy: bool = True,
        session: AsyncSession = _UNSET,
    ) -> None:
        src_id = int(src_id)
        dest_id = int(dest_id)
        await session.execute(
            update(cls)
            .where(and_(cls.src_id == src_id, cls.dest_id == dest_id))
            .values(legacy=legacy)
        )
        if legacy:
            if src_id not in cls._legacy_srcs_cache:
                cls._legacy_srcs_cache.add(src_id)
        else:
            if src_id in cls._legacy_srcs_cache:
                cls._legacy_srcs_cache.remove(src_id)

    @classmethod
    @ensure_session(db_session)
    async def remove_mirror(
        cls, src_id: int, dest_id: int, session: AsyncSession = _UNSET
    ) -> None:
        src_id = int(src_id)
        dest_id = int(dest_id)
        await session.execute(
            update(cls)
            .where(and_(cls.src_id == src_id, cls.dest_id == dest_id, cls.enabled))
            .values(enabled=False)
        )

        # Note: We deliberately don't remove the src_id from the _all_srcs_cache
        # since we don't know if there are other mirrors with the same src_id
        # and clearing and refetching would be needlessly expensive
        # Since mirrors are mostly designed as a one to many repeater of messages
        # it is also unlikely that the last mirror with a given src_id will be
        # removed.

    @classmethod
    @ensure_session(db_session)
    async def remove_all_mirrors(
        cls, dest_id: int, session: AsyncSession = _UNSET
    ) -> None:
        dest_id = int(dest_id)
        await session.execute(
            update(cls)
            .where(and_(cls.dest_id == dest_id, cls.enabled))
            .values(enabled=False)
        )

        # Note: We deliberately don't remove the src_ids from the _all_srcs_cache
        # since we don't know if there are other mirrors with the same src_id
        # and clearing and refetching would be needlessly expensive
        # Since mirrors are mostly designed as a one to many repeater of messages
        # it is also unlikely that the last mirror with a given src_id will be
        # removed.

    @classmethod
    @ensure_session(db_session)
    async def get_legacy_mirrors_disabled_for_failure(
        cls, since: dt.datetime | None, session: AsyncSession = _UNSET
    ) -> list[tuple[int, int]]:
        """Return mirrors that have been disabled for failure

        Mirrors that have been disabled for failure since `since` are returned
        in the format (src_id, dest_id)
        """
        disabled_mirrors = await session.execute(
            select(cls.src_id, cls.dest_id).where(
                and_(
                    ~cls.enabled,
                    cls.legacy,
                    cls.legacy_disable_for_failure_on_date >= since,
                )
            )
        )
        disabled_mirrors = disabled_mirrors if disabled_mirrors else []
        disabled_mirrors = disabled_mirrors.fetchall()

        return disabled_mirrors

    @classmethod
    @ensure_session(db_session)
    async def undo_auto_disable_for_failure(
        cls,
        since: dt.datetime | None,
        session: AsyncSession = _UNSET,
    ) -> list[tuple[int, int]]:
        """Undo auto disable for failure of mirrors

        Mirrors that have been disabled for failure since `since` are re-enabled
        Mirrors are returned in the format (src_id, dest_id)
        """
        mirrors_to_enable = await cls.get_legacy_mirrors_disabled_for_failure(
            since=since, session=session
        )
        await session.execute(
            update(cls)
            # Match the disabled rows by the SAME predicate the SELECT used. Rebuilding
            # ``src_id IN (...) AND dest_id IN (...)`` from the pairs matches the
            # Cartesian product of the two id sets, so it would also re-enable innocent
            # rows that merely share a src or dest with a genuinely-disabled pair (or
            # were disabled for some *other* reason). Clear the disable stamp and the
            # unreachable clock too, so the re-enabled row starts clean (the
            # reachability sweep re-stamps it only if it is still unreachable).
            .where(
                and_(
                    ~cls.enabled,
                    cls.legacy,
                    cls.legacy_disable_for_failure_on_date >= since,
                )
            )
            .values(
                enabled=True,
                legacy_disable_for_failure_on_date=None,
                unreachable_since=None,
            )
        )

        # Add re-enabled mirrors to the cache — but ONLY if it has already been
        # populated by a full fetch. Seeding an empty cache with just this handful of
        # ids would make get_or_fetch_all_srcs (which treats a non-empty cache as
        # authoritative) return only them and silently drop every other legacy source
        # until restart. An empty cache is left empty so the next fetch reads all srcs
        # (the re-enabled pairs included, since they are now ``enabled``).
        if cls._legacy_srcs_cache:
            cls._legacy_srcs_cache.update(src_id for src_id, _ in mirrors_to_enable)

        return mirrors_to_enable

    @classmethod
    @ensure_session(db_session)
    async def fetch_reachability_candidates(
        cls,
        *,
        session: AsyncSession = _UNSET,
    ) -> list[tuple[int, int]]:
        """Enabled legacy ``(src_id, dest_id)`` pairs for the reachability sweep."""
        rows = (
            await session.execute(
                select(cls.src_id, cls.dest_id).where(and_(cls.enabled, cls.legacy))
            )
        ).fetchall()
        return [(int(src_id), int(dest_id)) for src_id, dest_id in rows]

    @classmethod
    @ensure_session(db_session)
    async def apply_reachability_sweep(
        cls,
        reachable: t.Collection[tuple[int, int]],
        unreachable: t.Collection[tuple[int, int]],
        *,
        now: dt.datetime | None = None,
        session: AsyncSession = _UNSET,
    ) -> list[tuple[int, int]]:
        """Record a reachability pass and disable pairs unreachable past the grace.

        ``reachable`` pairs have ``unreachable_since`` cleared (a recovered destination
        resets the clock). ``unreachable`` pairs get ``unreachable_since`` stamped only
        when unset, so the grace measures *continuous* unreachability rather than the
        latest probe. A pair is disabled only when it is **confirmed unreachable this
        sweep** AND its ``unreachable_since`` is older than
        ``cfg.mirror_unreachable_grace_hours`` — so a merely ambiguous (UNKNOWN) probe
        never disables a mirror. Disabled pairs are stamped so an owner can undo them.
        Returns the disabled pairs.
        """
        now = now or _utcnow()
        reachable = [(int(s), int(d)) for s, d in reachable]
        unreachable = [(int(s), int(d)) for s, d in unreachable]

        if reachable:
            await session.execute(
                update(cls)
                .where(
                    and_(
                        tuple_(cls.src_id, cls.dest_id).in_(reachable),
                        cls.unreachable_since.is_not(None),
                    )
                )
                .values(unreachable_since=None)
            )
        if not unreachable:
            return []
        await session.execute(
            update(cls)
            .where(
                and_(
                    tuple_(cls.src_id, cls.dest_id).in_(unreachable),
                    cls.unreachable_since.is_(None),
                )
            )
            .values(unreachable_since=now)
        )

        cutoff = now - dt.timedelta(hours=cfg.mirror_unreachable_grace_hours)
        stale = (
            await session.execute(
                select(cls.src_id, cls.dest_id).where(
                    and_(
                        cls.enabled,
                        cls.legacy,
                        tuple_(cls.src_id, cls.dest_id).in_(unreachable),
                        cls.unreachable_since.is_not(None),
                        cls.unreachable_since <= cutoff,
                    )
                )
            )
        ).fetchall()
        pairs = [(int(src_id), int(dest_id)) for src_id, dest_id in stale]
        if not pairs:
            return []

        await session.execute(
            update(cls)
            .where(
                and_(
                    cls.enabled,
                    cls.legacy,
                    tuple_(cls.src_id, cls.dest_id).in_(pairs),
                )
            )
            .values(enabled=False, legacy_disable_for_failure_on_date=now)
        )

        # Note: we deliberately don't remove the src_id from _legacy_srcs_cache — a
        # disabled pair may share a src with enabled ones, and its dest is filtered
        # by ``enabled`` at fetch time anyway.
        return pairs


def _utcnow() -> dt.datetime:
    # Truncated to whole seconds on purpose. Postgres TIMESTAMP and SQLite both keep
    # sub-second precision, so nothing rounds a just-now ``due_at`` UP past the current
    # second any more (MySQL DATETIME(0) used to, making an immediately-due row fail the
    # ``due_at <= now`` pick gate until the next poll). Whole seconds still store
    # exactly on every backend and second granularity is ample for the scheduler
    # (retries 180-300s, poll 45s, grace in hours), so the ledger's timestamps stay
    # comparable across a backend swap.
    return dt.datetime.now(tz=dt.UTC).replace(microsecond=0)


def _dialect_insert(cls: type[Base], session: AsyncSession):
    """Dialect-specific INSERT construct (the one that grows ``on_conflict_*``).

    Postgres and SQLite spell upserts the same way — ``ON CONFLICT … DO UPDATE/DO
    NOTHING`` with an ``excluded`` pseudo-table — but the construct is per-dialect, so
    it has to be picked from the bound engine rather than compiled per-dialect the way
    the old MySQL/SQLite ``prefix_with`` trick did.
    """
    if session.get_bind().dialect.name == "sqlite":
        return sqlite_insert(cls)
    return pg_insert(cls)


def _insert_ignore(cls: type[Base], session: AsyncSession):
    """Duplicate-PK-ignoring INSERT, portable across dialects.

    ``INSERT … ON CONFLICT DO NOTHING`` on both Postgres and SQLite (replacing the old
    MySQL ``INSERT IGNORE`` / SQLite ``INSERT OR IGNORE`` prefixes). No conflict target
    is given, so — like the prefixes it replaces — *any* unique violation is swallowed:
    a duplicate gateway event or a manual re-mirror of an already-enqueued message is a
    no-op rather than a primary-key violation.

    ``preserve_rowcount`` matters: callers read ``result.rowcount`` to learn how many
    rows survived the conflict filter, and SQLAlchemy does not capture it for a plain
    INSERT before closing the cursor — psycopg then reports ``-1`` on the closed
    cursor (SQLite happens to keep the value, which is why the SQLite lane never saw
    this). The option makes SQLAlchemy read it while the cursor is still open.
    """
    return (
        _dialect_insert(cls, session)
        .on_conflict_do_nothing()
        .execution_options(preserve_rowcount=True)
    )


class DeliveryState(enum.StrEnum):
    """Lifecycle state of a single ``mirror_delivery`` row.

    Single-worker model: there is no CLAIMED state. The one worker picks a batch,
    delivers it, and flushes every outcome *before* it picks again, so a row is never
    handed out twice in normal operation. A crash mid-batch simply leaves the row
    PENDING for the next pick to re-do — re-sending at most once (the accepted small
    crash-duplicate window).
    """

    PENDING = "PENDING"  # needs work (applied < desired, or an unapplied delete)
    DELIVERED = "DELIVERED"  # converged (applied_version == desired_version)
    FAILED = "FAILED"  # terminal; last_error_class is PERMANENT or exhausted TRANSIENT
    CANCELLED = (
        "CANCELLED"  # user cancel / delete-before-delivery / undo neutralisation
    )


class CrosspostState(enum.StrEnum):
    """Durable crosspost sub-state of a ``mirror_delivery`` row, apart from ``state``.

    A row delivered to a Discord announcement (news) channel becomes ``DELIVERED`` with
    ``crosspost_state = PENDING``; a later pick crossposts it (idempotent — Discord's
    "already crossposted" counts as success) and sets ``DONE``. Non-news destinations
    are ``NOT_APPLICABLE`` and never picked for crosspost. A run does not
    wait on crosspost — it is durable background work.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    DONE = "DONE"


class OutcomeKind(enum.Enum):
    """Which write-back shape a :class:`DeliveryOutcome` takes in ``flush_outcomes``."""

    SUCCESS = 1  # a send or edit succeeded
    DELETE_SUCCESS = 2  # a dest message delete succeeded (or was already gone)
    TRANSIENT = 3  # a retryable failure below the attempt cap
    TERMINAL = 4  # a permanent or attempt-cap-exhausted failure
    CANCELLED = 5  # short-circuited (cancel requested / nothing to do)
    CROSSPOST_DONE = (
        6  # crosspost succeeded (or was given up on) — crosspost_state DONE
    )
    CROSSPOST_RETRY = 7  # crosspost hit a retryable failure — back off, stay PENDING


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """One delivery attempt's result, produced by the worker, consumed by the flusher.

    Defined here (in ``dd.common``) rather than in the beacon worker so the flusher can
    type its input without ``dd.common`` importing ``dd.beacon`` (the dependency
    direction is beacon → common, never the reverse). ``version`` is the
    ``desired_version`` observed when the row was picked; the flusher's CASE guard
    compares it against the row's *current* ``desired_version`` so an edit/delete that
    raced the in-flight attempt keeps the row converging instead of marking it terminal.
    """

    kind: OutcomeKind
    src_msg_id: int
    dest_ch_id: int
    version: int
    dest_msg_id: int | None = None
    attempts: int = 0
    due_at: dt.datetime | None = None
    error_ref: str | None = None
    error_class: str | None = None
    error_msg: str | None = None
    # A fresh send to a news channel warrants a crosspost — the flusher records
    # ``crosspost_state = PENDING`` so a later pick converges it durably.
    crosspost_pending: bool = False


@dataclass(frozen=True, slots=True)
class PickedRow:
    """Frozen snapshot of a picked ``mirror_delivery`` row handed to the worker.

    A plain dataclass (not a live ORM object) so the worker can process it after the
    pick transaction closes without touching an expired/detached instance. ``state`` +
    ``crosspost_state`` tell the worker whether this pick is a delivery (state PENDING)
    or a durable crosspost (state DELIVERED, crosspost_state PENDING).
    """

    src_msg_id: int
    dest_ch_id: int
    src_ch_id: int
    dest_msg_id: int | None
    desired_version: int
    deleted: bool
    attempts: int
    state: str
    crosspost_state: str


class MirrorDelivery(Base):
    """Durable delivery ledger: one row per (source message, destination channel).

    Stores *intent* (``desired_version`` /
    ``deleted``), never content — content is fetched fresh from Discord at delivery
    time. An edit bumps ``desired_version``; the convergence worker converges rows where
    ``applied_version < desired_version``.
    """

    __tablename__ = "mirror_delivery"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_mirror_delivery_state_due", "state", "due_at"),  # delivery pick scan
        # Crosspost pick scan: the (rare, short-lived) DELIVERED rows awaiting a durable
        # crosspost, found without touching the far larger DELIVERED-DONE population.
        Index("ix_mirror_delivery_crosspost_due", "crosspost_state", "due_at"),
        Index("ix_mirror_delivery_created_at", "created_at"),  # prune
    )

    src_msg_id = Column("src_msg_id", BigInteger, primary_key=True)
    dest_ch_id = Column("dest_ch_id", BigInteger, primary_key=True)
    src_ch_id = Column("src_ch_id", BigInteger, nullable=False)
    dest_msg_id = Column(
        "dest_msg_id", BigInteger, nullable=True
    )  # NULL until delivered
    desired_version = Column("desired_version", Integer, nullable=False, default=1)
    applied_version = Column("applied_version", Integer, nullable=False, default=0)
    deleted = Column("deleted", Boolean, nullable=False, default=False)
    state = Column(
        "state", String(16), nullable=False, default=DeliveryState.PENDING.value
    )
    crosspost_state = Column(
        "crosspost_state",
        String(16),
        nullable=False,
        default=CrosspostState.NOT_APPLICABLE.value,
    )
    attempts = Column("attempts", Integer, nullable=False, default=0)
    due_at = Column("due_at", DateTime, nullable=False, default=_utcnow)
    last_error_ref = Column("last_error_ref", String(8), nullable=True)
    last_error_class = Column("last_error_class", String(12), nullable=True)
    last_error_msg = Column("last_error_msg", String(256), nullable=True)
    created_at = Column("created_at", DateTime, nullable=False, default=_utcnow)
    finished_at = Column("finished_at", DateTime, nullable=True)

    # Columns inserted by the enqueue INSERT…SELECT, in order (dest_msg_id + the
    # error/finished columns default to NULL).
    _ENQUEUE_COLS = (
        "src_msg_id",
        "dest_ch_id",
        "src_ch_id",
        "desired_version",
        "applied_version",
        "deleted",
        "state",
        "crosspost_state",
        "attempts",
        "due_at",
        "created_at",
    )

    @classmethod
    def _enqueue_select(cls, src_ch_id: int, src_msg_id: int, now: dt.datetime):
        """SELECT feeding the enqueue/reconcile INSERT — one candidate row per enabled
        legacy dest of ``src_ch_id`` (excluding the source channel itself)."""
        return select(
            literal(int(src_msg_id)),
            MirroredChannel.dest_id,
            literal(int(src_ch_id)),
            literal(1),  # desired_version
            literal(0),  # applied_version
            literal(False),  # deleted
            literal(DeliveryState.PENDING.value),  # state
            literal(CrosspostState.NOT_APPLICABLE.value),  # crosspost_state
            literal(0),  # attempts
            literal(now),  # due_at
            literal(now),  # created_at
        ).where(
            and_(
                MirroredChannel.src_id == int(src_ch_id),
                MirroredChannel.legacy,
                MirroredChannel.enabled,
                MirroredChannel.dest_id != int(src_ch_id),
            )
        )

    @classmethod
    @ensure_session(db_session)
    async def enqueue_send(
        cls,
        src_ch_id: int,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> int:
        """Enqueue a fresh send fan-out for ``src_msg_id``; return rows inserted.

        A single INSERT…SELECT (no read-then-write, no locks). INSERT-IGNORE makes a
        duplicate gateway event or a manual re-mirror of an already-enqueued message a
        no-op.
        """
        now = _utcnow()
        result = await session.execute(
            _insert_ignore(cls, session).from_select(
                list(cls._ENQUEUE_COLS),
                cls._enqueue_select(src_ch_id, src_msg_id, now),
            )
        )
        return result.rowcount or 0

    @classmethod
    @ensure_session(db_session)
    async def bump_for_edit(
        cls,
        src_ch_id: int,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> tuple[int, int, bool]:
        """Reconcile an edit: bump every non-deleted row's ``desired_version`` and reset
        its retry budget, then (only for an already-delivered message) insert rows for
        any dests added since the send.

        Returns ``(bumped, inserted, had_delivered_baseline)``. ``bumped + inserted``
        == 0 means this is not an enqueued message at all (nothing to do). ``bumped``
        covers every non-deleted row so a **pre-delivery** edit still refreshes the
        version the worker fetches its *current* content at — the send fetches source
        content at delivery time (see ``mirror_worker``), but a version that never moves
        lets the
        per-``(src_msg_id, version)`` source cache serve stale pre-edit content to the
        whole fan-out, silently losing the edit. Rows for dests since removed from
        ``mirrored_channel`` keep converging (bumped, not re-inserted).
        FAILED/CANCELLED/PENDING rows are (re)armed to PENDING; ``deleted=1`` rows are
        never touched.

        ``had_delivered_baseline`` is True iff a row already carries a ``dest_msg_id``
        (the message landed somewhere). The caller shows an *update* progress card and
        fresh-fans-out to newly added dests only then; before first delivery the edit is
        folded silently into the pending send. This is also why the publish/crosspost
        transition Discord reports as a ``MessageUpdateEvent`` never surfaces a phantom
        "update": at that instant nothing has been delivered, so no card is shown and no
        fresh fan-out is inserted here (which would otherwise race the create handler's
        send) — regardless of gateway event ordering.

        A row that is in-flight *right now* is re-armed to PENDING here too, but the one
        worker flushes the current batch's outcomes before it picks again, so it can't
        re-pick the row mid-flight. That in-flight outcome (stamped with the pre-bump
        version) loses the flusher's version guard and bounces the row back to PENDING
        while recording any created ``dest_msg_id`` — so the next pick *edits* the
        recorded message instead of re-sending it. No duplicate, no orphan, no lease.
        """
        now = _utcnow()
        bumped = await session.execute(
            update(cls)
            .where(and_(cls.src_msg_id == int(src_msg_id), ~cls.deleted))
            .values(
                desired_version=cls.desired_version + 1,
                state=DeliveryState.PENDING.value,
                attempts=0,
                due_at=now,
                finished_at=None,
            )
        )
        bumped_rows = bumped.rowcount or 0
        if not bumped_rows:
            # Not an enqueued message (or only deleted rows) — nothing to reconcile, and
            # crucially no fresh fan-out inserted (that is the create handler's job).
            return (0, 0, False)
        had_delivered_baseline = bool(
            (
                await session.execute(
                    select(func.count())
                    .select_from(cls)
                    .where(
                        and_(
                            cls.src_msg_id == int(src_msg_id),
                            cls.dest_msg_id.is_not(None),
                            ~cls.deleted,
                        )
                    )
                )
            ).scalar_one()
        )
        inserted_rows = 0
        if had_delivered_baseline:
            inserted = await session.execute(
                _insert_ignore(cls, session).from_select(
                    list(cls._ENQUEUE_COLS),
                    cls._enqueue_select(src_ch_id, src_msg_id, now),
                )
            )
            inserted_rows = inserted.rowcount or 0
        return (bumped_rows, inserted_rows, had_delivered_baseline)

    @classmethod
    @ensure_session(db_session)
    async def mark_deleted(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> int:
        """Flag every row for ``src_msg_id`` as delete-intent; return the deletion-work
        count (rows that actually need a Discord delete).

        Never-delivered rows (``dest_msg_id`` NULL) go straight to CANCELLED (nothing
        to delete Discord-side); delivered rows go to PENDING carrying the delete
        intent, so the worker deletes their dest message. A CANCELLED row is left alone
        ONLY when it never delivered — a CANCELLED row that still carries a
        ``dest_msg_id`` (e.g. an update run cancelled after the original send, or a
        delivered row cancelled by a permanent source-fetch failure) holds a live
        Discord message and must still be deleted, else it is orphaned forever. The
        returned count is the number of PENDING (delivered) rows — the RunView total for
        the delete card; 0 means there is nothing to delete (not mirrored, or nothing
        was ever delivered).
        """
        now = _utcnow()
        base = and_(
            cls.src_msg_id == int(src_msg_id),
            or_(
                cls.state != DeliveryState.CANCELLED.value,
                cls.dest_msg_id.is_not(None),
            ),
        )
        # Count the deletion-work rows (have a dest message) before flipping state.
        deletion_work = (
            await session.execute(
                select(func.count())
                .select_from(cls)
                .where(and_(base, cls.dest_msg_id.is_not(None)))
            )
        ).scalar_one()
        await session.execute(
            update(cls)
            .where(base)
            .values(
                deleted=True,
                state=case(
                    (cls.dest_msg_id.is_(None), DeliveryState.CANCELLED.value),
                    else_=DeliveryState.PENDING.value,
                ),
                attempts=0,
                due_at=now,
                finished_at=None,
            )
        )
        return int(deletion_work)

    @classmethod
    @ensure_session(db_session)
    async def cancel_pending(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> list[int]:
        """Cancel not-yet-delivered rows for ``src_msg_id``; return the cancelled dests.

        Only PENDING, non-deleted rows are cancelled. This stops every row the worker
        has not yet *picked*. A row already picked into the current batch is delivered
        anyway (the worker has no in-memory cancel check) and, once sent, converges to
        DELIVERED under the version guard — cancel lost the race, the message is out
        and stays recorded. So cancel is best-effort against the at-most-one in-flight
        batch. A racing delete's PENDING rows (``deleted=1``) are left to converge.
        Returns the affected ``dest_ch_id``s so the caller can mark them cancelled in
        the run view; an empty list means there was nothing to cancel.
        """
        now = _utcnow()
        where = and_(
            cls.src_msg_id == int(src_msg_id),
            cls.state == DeliveryState.PENDING.value,
            ~cls.deleted,
        )
        dest_ids = [
            int(d)
            for (d,) in (
                await session.execute(select(cls.dest_ch_id).where(where))
            ).fetchall()
        ]
        if not dest_ids:
            return []
        await session.execute(
            update(cls)
            .where(where)
            .values(
                state=DeliveryState.CANCELLED.value,
                finished_at=now,
            )
        )
        return dest_ids

    @classmethod
    @ensure_session(db_session)
    async def pick_batch(
        cls,
        batch_size: int,
        *,
        now: dt.datetime | None = None,
        session: AsyncSession = _UNSET,
    ) -> list[PickedRow]:
        """Return up to ``batch_size`` due rows needing work, biggest-server-first.

        A row needs work when ``due_at <= now`` and it is either a PENDING delivery
        (send / edit / delete) or a DELIVERED row still awaiting a durable crosspost
        (``crosspost_state = PENDING``). No lease, no ``FOR UPDATE``, no state mutation:
        the single worker picks, processes the whole batch, and flushes every outcome
        *before* it picks again, so a row is never handed out twice (a retry is bounced
        to a future ``due_at``; a crosspost is marked DONE). Biggest-server-first order
        comes from a two-hop join to ``server_statistics`` via ``mirrored_channel``
        (``dest_server_id`` no longer lives on the ledger).
        """
        now = now or _utcnow()
        rows = (
            await session.execute(
                select(
                    cls.src_msg_id,
                    cls.dest_ch_id,
                    cls.src_ch_id,
                    cls.dest_msg_id,
                    cls.desired_version,
                    cls.deleted,
                    cls.attempts,
                    cls.state,
                    cls.crosspost_state,
                )
                .join(
                    MirroredChannel,
                    and_(
                        MirroredChannel.src_id == cls.src_ch_id,
                        MirroredChannel.dest_id == cls.dest_ch_id,
                    ),
                    isouter=True,
                )
                .join(
                    ServerStatistics,
                    MirroredChannel.dest_server_id == ServerStatistics.id,
                    isouter=True,
                )
                .where(
                    and_(
                        cls.due_at <= now,
                        or_(
                            cls.state == DeliveryState.PENDING.value,
                            cls.crosspost_state == CrosspostState.PENDING.value,
                        ),
                    )
                )
                # Biggest-server-first, then oldest first. An unknown population (no
                # server_statistics row) coalesces to 10**12 so it sorts optimistically
                # among the largest, matching the fetch_dests convention. A bounded
                # ~batch-size filesort over a set already narrowed by the
                # (state, due_at) index — cheap at our volumes.
                .order_by(
                    desc(coalesce(ServerStatistics.population, 10**12)),
                    cls.created_at,
                )
                .limit(batch_size)
            )
        ).all()
        return [
            PickedRow(
                src_msg_id=int(r.src_msg_id),
                dest_ch_id=int(r.dest_ch_id),
                src_ch_id=int(r.src_ch_id),
                dest_msg_id=None if r.dest_msg_id is None else int(r.dest_msg_id),
                desired_version=int(r.desired_version),
                deleted=bool(r.deleted),
                attempts=int(r.attempts),
                state=str(r.state),
                crosspost_state=str(r.crosspost_state),
            )
            for r in rows
        ]

    @classmethod
    @ensure_session(db_session)
    async def flush_outcomes(
        cls,
        outcomes: list[DeliveryOutcome],
        *,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Write back a batch of delivery outcomes in one transaction.

        One executemany per :class:`OutcomeKind`, each a static SQL shape whose
        version/deleted guard is a CASE inside the VALUES — so one statement handles a
        raced edit/delete without a per-row read. Invariant: a dest message id, once
        created and observed, is *always* recorded (even when the guard sends the row
        back to PENDING), so re-convergence edits instead of re-sending.
        """
        if not outcomes:
            return
        now = _utcnow()
        by_kind: dict[OutcomeKind, list[DeliveryOutcome]] = {}
        for o in outcomes:
            by_kind.setdefault(o.kind, []).append(o)

        # The version/deleted guard: the row's current desired_version still matches the
        # version this attempt delivered AND no delete intent has landed. When it fails
        # (a raced edit bumped the version, or a delete landed), the row is kept
        # converging instead of being marked terminal, and — crucially — the racing
        # writer's fresh state (e.g. bump_for_edit's ``attempts=0``) is preserved rather
        # than clobbered by this stale outcome.
        guard = and_(
            cls.desired_version == bindparam("b_version"),
            ~cls.deleted,
        )
        # A CANCELLED outcome should latch even for a delete-intent row that had nothing
        # to deliver (``deleted=1`` + never sent) — the ``~deleted`` half of ``guard``
        # would send it back to PENDING forever, re-claimed and re-cancelled every poll.
        # It must NOT latch when a real delete is still outstanding (a delivered message
        # to remove) or an edit bumped the version.
        cancel_guard = and_(
            cls.desired_version == bindparam("b_version"),
            or_(~cls.deleted, cls.dest_msg_id.is_(None)),
        )
        pk = and_(
            cls.src_msg_id == bindparam("b_src"),
            cls.dest_ch_id == bindparam("b_dest"),
        )

        for kind, group in by_kind.items():
            if kind is OutcomeKind.SUCCESS:
                # ``b_crosspost`` is PENDING only for a fresh send to a news channel.
                # News-ness is version-independent and crossposting is idempotent, so
                # arm the sub-state PENDING whenever this outcome is a news send — even
                # when the version guard fails (a raced edit bumped the version), so the
                # publish intent survives that race. For any other success (an edit, or
                # a plain non-news send) PRESERVE the existing sub-state rather than
                # writing NOT_APPLICABLE: an edit landing between a news send and its
                # deferred crosspost carries crosspost_pending=False and would otherwise
                # downgrade a still-PENDING crosspost, silently dropping the publish.
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(
                        dest_msg_id=bindparam("b_dest_msg"),
                        applied_version=bindparam("b_version"),
                        # Reset the retry budget on success so a following durable
                        # crosspost gets a clean attempt count (guarded so a raced
                        # edit/delete keeps its own freshly-reset budget instead).
                        attempts=case((guard, 0), else_=cls.attempts),
                        last_error_ref=None,
                        last_error_class=None,
                        last_error_msg=None,
                        crosspost_state=case(
                            (
                                bindparam("b_crosspost")
                                == CrosspostState.PENDING.value,
                                CrosspostState.PENDING.value,
                            ),
                            else_=cls.crosspost_state,
                        ),
                        state=case(
                            (guard, DeliveryState.DELIVERED.value),
                            else_=DeliveryState.PENDING.value,
                        ),
                        finished_at=case((guard, now), else_=None),
                    )
                )
                params = [
                    {
                        "b_src": o.src_msg_id,
                        "b_dest": o.dest_ch_id,
                        "b_dest_msg": o.dest_msg_id,
                        "b_version": o.version,
                        "b_crosspost": (
                            CrosspostState.PENDING.value
                            if o.crosspost_pending
                            else CrosspostState.NOT_APPLICABLE.value
                        ),
                    }
                    for o in group
                ]
            elif kind is OutcomeKind.DELETE_SUCCESS:
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(
                        applied_version=bindparam("b_version"),
                        last_error_ref=None,
                        last_error_class=None,
                        last_error_msg=None,
                        state=DeliveryState.DELIVERED.value,
                        # Resolve any still-PENDING crosspost: the dest message is gone,
                        # so leaving it PENDING would keep pick_batch re-selecting this
                        # row to crosspost a deleted message (3 failing attempts + a
                        # health alert each) before giving up.
                        crosspost_state=CrosspostState.DONE.value,
                        finished_at=now,
                    )
                )
                params = [
                    {
                        "b_src": o.src_msg_id,
                        "b_dest": o.dest_ch_id,
                        "b_version": o.version,
                    }
                    for o in group
                ]
            elif kind is OutcomeKind.TRANSIENT:
                # State is PENDING either way (re-pickable); but the retry budget and
                # backoff are guarded so a raced edit that already re-armed the row
                # (attempts=0, due=now) is not clobbered by this stale outcome's
                # exhausted count / far-future backoff.
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(
                        attempts=case(
                            (guard, bindparam("b_attempts")), else_=cls.attempts
                        ),
                        due_at=case((guard, bindparam("b_due_at")), else_=cls.due_at),
                        state=DeliveryState.PENDING.value,
                        last_error_ref=case(
                            (guard, bindparam("b_ref")), else_=cls.last_error_ref
                        ),
                        last_error_class=case(
                            (guard, bindparam("b_class")), else_=cls.last_error_class
                        ),
                        last_error_msg=case(
                            (guard, bindparam("b_msg")), else_=cls.last_error_msg
                        ),
                    )
                )
                params = [
                    {
                        "b_src": o.src_msg_id,
                        "b_dest": o.dest_ch_id,
                        "b_version": o.version,
                        "b_attempts": o.attempts,
                        "b_due_at": o.due_at,
                        "b_ref": o.error_ref,
                        "b_class": o.error_class,
                        "b_msg": o.error_msg,
                    }
                    for o in group
                ]
            elif kind is OutcomeKind.TERMINAL:
                # Every value is guarded: when a raced edit/delete bumped the version
                # the row goes back to PENDING (not FAILED) AND keeps the racing
                # writer's reset budget/error state, so the new version gets its full
                # retry allowance instead of inheriting this attempt's exhausted count.
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(
                        attempts=case(
                            (guard, bindparam("b_attempts")), else_=cls.attempts
                        ),
                        last_error_ref=case(
                            (guard, bindparam("b_ref")), else_=cls.last_error_ref
                        ),
                        last_error_class=case(
                            (guard, bindparam("b_class")), else_=cls.last_error_class
                        ),
                        last_error_msg=case(
                            (guard, bindparam("b_msg")), else_=cls.last_error_msg
                        ),
                        state=case(
                            (guard, DeliveryState.FAILED.value),
                            else_=DeliveryState.PENDING.value,
                        ),
                        finished_at=case((guard, now), else_=None),
                    )
                )
                params = [
                    {
                        "b_src": o.src_msg_id,
                        "b_dest": o.dest_ch_id,
                        "b_version": o.version,
                        "b_attempts": o.attempts,
                        "b_ref": o.error_ref,
                        "b_class": o.error_class,
                        "b_msg": o.error_msg,
                    }
                    for o in group
                ]
            elif kind is OutcomeKind.CROSSPOST_DONE:
                # Idempotent terminal for the crosspost sub-state; unguarded (a raced
                # edit does not un-crosspost a message that is already out).
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(crosspost_state=CrosspostState.DONE.value)
                )
                params = [
                    {"b_src": o.src_msg_id, "b_dest": o.dest_ch_id} for o in group
                ]
            elif kind is OutcomeKind.CROSSPOST_RETRY:
                # Back off a transient crosspost failure; stay PENDING so a later pick
                # retries. Reuses ``attempts``/``due_at`` (free once delivered).
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(
                        attempts=bindparam("b_attempts"),
                        due_at=bindparam("b_due_at"),
                        crosspost_state=CrosspostState.PENDING.value,
                    )
                )
                params = [
                    {
                        "b_src": o.src_msg_id,
                        "b_dest": o.dest_ch_id,
                        "b_attempts": o.attempts,
                        "b_due_at": o.due_at,
                    }
                    for o in group
                ]
            else:  # OutcomeKind.CANCELLED
                stmt = (
                    update(cls.__table__)
                    .where(pk)
                    .values(
                        state=case(
                            (cancel_guard, DeliveryState.CANCELLED.value),
                            else_=DeliveryState.PENDING.value,
                        ),
                        finished_at=case((cancel_guard, now), else_=None),
                    )
                )
                params = [
                    {
                        "b_src": o.src_msg_id,
                        "b_dest": o.dest_ch_id,
                        "b_version": o.version,
                    }
                    for o in group
                ]
            # One driver executemany per kind. psycopg pipelines an executemany into a
            # single round trip on Postgres, but the statement is still applied per row.
            # Acceptable either way: it is a single transaction bounded by the
            # pick batch size, and the version-guarded CASE columns make a hand-built
            # bulk UPDATE (per-row CASE keyed on PK) materially more error-prone than
            # the per-row cost is worth. Revisit with a temp-table/VALUES join if flush
            # dominates a run's wall-clock.
            await session.execute(stmt, params)

    @classmethod
    @ensure_session(db_session)
    async def non_terminal_backlog(
        cls,
        *,
        session: AsyncSession = _UNSET,
    ) -> list[tuple[int, int, int, bool, bool]]:
        """Per-source summary of non-terminal (PENDING) rows, for recovery.

        Returns ``(src_msg_id, src_ch_id, count, any_deleted, any_unsent)`` per source
        message with work still to do, so the worker can register a synthetic recovery
        RunView (op inferred from the flags) and total per source.
        """
        rows = (
            await session.execute(
                select(
                    cls.src_msg_id,
                    func.max(cls.src_ch_id),
                    func.count(),
                    func.max(case((cls.deleted, 1), else_=0)),
                    func.max(case((cls.applied_version == 0, 1), else_=0)),
                )
                .where(cls.state == DeliveryState.PENDING.value)
                .group_by(cls.src_msg_id)
            )
        ).fetchall()
        return [
            (int(smi), int(sci), int(cnt), bool(any_del), bool(any_unsent))
            for smi, sci, cnt, any_del, any_unsent in rows
        ]

    @classmethod
    @ensure_session(db_session)
    async def non_terminal_counts(
        cls,
        src_msg_ids: t.Collection[int],
        *,
        session: AsyncSession = _UNSET,
    ) -> dict[int, int]:
        """Count non-terminal (PENDING) rows per source message.

        The ledger-authoritative completion signal: a run is durably done exactly when
        this returns 0 for its ``src_msg_id`` (all rows DELIVERED/FAILED/CANCELLED).
        Crosspost is background work and does not hold a run open, so a DELIVERED
        row still awaiting crosspost is *not* counted here. A ``src_msg_id`` with no
        non-terminal rows is simply absent from the result (callers treat missing as 0).
        """
        ids = [int(i) for i in src_msg_ids]
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(cls.src_msg_id, func.count())
                .where(
                    and_(
                        cls.src_msg_id.in_(ids),
                        cls.state == DeliveryState.PENDING.value,
                    )
                )
                .group_by(cls.src_msg_id)
            )
        ).fetchall()
        return {int(smi): int(cnt) for smi, cnt in rows}

    @classmethod
    @ensure_session(db_session)
    async def state_counts(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> dict[str, int]:
        """Per-``state`` row counts for one source message — the progress card's data.

        The card renders straight off this cheap ``GROUP BY state`` count, so there is a
        single source of truth for run progress (the ledger) instead of an in-memory
        accounting that can drift from it.
        """
        rows = (
            await session.execute(
                select(cls.state, func.count())
                .where(cls.src_msg_id == int(src_msg_id))
                .group_by(cls.state)
            )
        ).fetchall()
        return {str(state): int(count) for state, count in rows}

    @classmethod
    @ensure_session(db_session)
    async def sources_needing_source_content(
        cls,
        src_msg_ids: t.Collection[int],
        *,
        session: AsyncSession = _UNSET,
    ) -> set[int]:
        """Of ``src_msg_ids``, those with a PENDING non-deleted delivery row still open.

        Only such rows need the source message's *content* fetched at delivery time (a
        crosspost or delete doesn't), so the worker can drop the rest from its per-
        source content cache — their fan-out has resolved. A subset of the input.
        """
        if not src_msg_ids:
            return set()
        rows = await session.execute(
            select(cls.src_msg_id)
            .where(
                and_(
                    cls.src_msg_id.in_([int(s) for s in src_msg_ids]),
                    cls.state == DeliveryState.PENDING.value,
                    ~cls.deleted,
                )
            )
            .distinct()
        )
        return {int(s) for (s,) in rows}

    @classmethod
    @ensure_session(db_session)
    async def outstanding_count(
        cls,
        *,
        session: AsyncSession = _UNSET,
    ) -> int:
        """Total PENDING rows across all runs — the pre-restart 'work in progress' gate.

        A restart mid-fan-out is safe (leftover PENDING rows are re-picked on startup),
        but this still surfaces outstanding work so an owner can choose to wait.
        """
        return int(
            (
                await session.execute(
                    select(func.count()).where(cls.state == DeliveryState.PENDING.value)
                )
            ).scalar_one()
        )

    @classmethod
    @ensure_session(db_session)
    async def failure_breakdown(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> list[tuple[str, str, int, str]]:
        """FAILED rows for one source, grouped by error reference for the progress card.

        Returns ``(reference_code, error_class, count, sample_message)`` per distinct
        error, most-common first.
        """
        rows = (
            await session.execute(
                select(
                    cls.last_error_ref,
                    func.max(cls.last_error_class),
                    func.count(),
                    func.max(cls.last_error_msg),
                )
                .where(
                    and_(
                        cls.src_msg_id == int(src_msg_id),
                        cls.state == DeliveryState.FAILED.value,
                    )
                )
                .group_by(cls.last_error_ref)
                .order_by(desc(func.count()))
            )
        ).fetchall()
        return [
            (str(ref or ""), str(cls_ or ""), int(count), str(msg or ""))
            for ref, cls_, count, msg in rows
        ]

    @classmethod
    @ensure_session(db_session)
    async def op_meta(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> tuple[int, int]:
        """``(version, max_attempts)`` for a source — labels an operation-log row: the
        version its fan-out converged and the worst per-row attempt count."""
        row = (
            await session.execute(
                select(func.max(cls.desired_version), func.max(cls.attempts)).where(
                    cls.src_msg_id == int(src_msg_id)
                )
            )
        ).first()
        if row is None or row[0] is None:
            return (1, 0)
        return (int(row[0]), int(row[1] or 0))

    @classmethod
    @ensure_session(db_session)
    async def recent_runs(
        cls,
        *,
        limit: int = 50,
        within_days: int = 30,
        session: AsyncSession = _UNSET,
    ) -> list[dict[str, t.Any]]:
        """Recent mirror runs (one row per source message) for the web log view.

        Groups every delivery row created within ``within_days`` by ``src_msg_id``,
        newest first, capped at ``limit`` — the per-state and crosspost tallies the log
        list needs without shipping every delivery row. The ``created_at`` window keeps
        the scan on the prune index rather than the whole table. The datetime aggregates
        carry an explicit ``DateTime`` type so SQLite returns ``datetime`` (not an ISO
        string); the values are the ledger's naive-UTC wall clock (the web layer stamps
        them UTC on the way out).
        """
        cutoff = _utcnow() - dt.timedelta(days=within_days)

        def _sum_state(value: str):
            return func.sum(case((cls.state == value, 1), else_=0))

        def _sum_xpost(value: str):
            return func.sum(case((cls.crosspost_state == value, 1), else_=0))

        rows = (
            await session.execute(
                select(
                    cls.src_msg_id,
                    func.max(cls.src_ch_id),
                    func.count(),
                    _sum_state(DeliveryState.DELIVERED.value),
                    _sum_state(DeliveryState.FAILED.value),
                    _sum_state(DeliveryState.PENDING.value),
                    _sum_state(DeliveryState.CANCELLED.value),
                    _sum_xpost(CrosspostState.DONE.value),
                    _sum_xpost(CrosspostState.PENDING.value),
                    func.min(cls.created_at, type_=DateTime),
                    func.max(coalesce(cls.finished_at, cls.created_at), type_=DateTime),
                    func.max(cls.attempts),
                    func.max(cls.desired_version),
                )
                .where(cls.created_at >= cutoff)
                .group_by(cls.src_msg_id)
                .order_by(desc(func.min(cls.created_at)))
                .limit(int(limit))
            )
        ).fetchall()

        return [
            {
                "src_msg_id": int(smi),
                "src_ch_id": int(sci),
                "total": int(total),
                "delivered": int(delivered),
                "failed": int(failed),
                "pending": int(pending),
                "cancelled": int(cancelled),
                "crosspost_done": int(xp_done),
                "crosspost_pending": int(xp_pending),
                "started": started,
                "last_at": last_at,
                "max_attempts": int(max_attempts),
                "version": int(version),
            }
            for (
                smi,
                sci,
                total,
                delivered,
                failed,
                pending,
                cancelled,
                xp_done,
                xp_pending,
                started,
                last_at,
                max_attempts,
                version,
            ) in rows
        ]

    @classmethod
    @ensure_session(db_session)
    async def run_rows(
        cls,
        src_msg_id: int,
        *,
        limit: int = 500,
        session: AsyncSession = _UNSET,
    ) -> list[dict[str, t.Any]]:
        """Per-destination delivery rows for one source message (the log detail view).

        Ordered failures-first then by destination channel and capped at ``limit`` so a
        huge fan-out cannot produce an unbounded payload. Carries the error triple so
        the page can show *why* a destination failed, and — via a LEFT JOIN to the
        mirror config on the exact ``(src, dest)`` pair — the destination's
        ``dest_server_id`` so the web layer can build a jump-to-message link instead of
        showing a bare snowflake. The join is on both ids (the config PK) so a channel
        that is a destination for several sources can't multiply the row; it is a LEFT
        join so a since-removed mirror config just yields ``None`` (bare-id fallback).
        """
        rows = (
            await session.execute(
                select(
                    cls.dest_ch_id,
                    cls.dest_msg_id,
                    cls.state,
                    cls.crosspost_state,
                    cls.attempts,
                    cls.applied_version,
                    cls.desired_version,
                    cls.deleted,
                    cls.last_error_ref,
                    cls.last_error_class,
                    cls.last_error_msg,
                    cls.created_at,
                    cls.finished_at,
                    MirroredChannel.dest_server_id,
                )
                .outerjoin(
                    MirroredChannel,
                    and_(
                        MirroredChannel.src_id == cls.src_ch_id,
                        MirroredChannel.dest_id == cls.dest_ch_id,
                    ),
                )
                .where(cls.src_msg_id == int(src_msg_id))
                .order_by(
                    case((cls.state == DeliveryState.FAILED.value, 0), else_=1),
                    cls.dest_ch_id,
                )
                .limit(int(limit))
            )
        ).fetchall()

        return [
            {
                "dest_ch_id": int(dest_ch_id),
                "dest_msg_id": int(dest_msg_id) if dest_msg_id is not None else None,
                "dest_server_id": int(dest_server_id)
                if dest_server_id is not None
                else None,
                "state": str(state),
                "crosspost_state": str(crosspost_state),
                "attempts": int(attempts),
                "applied_version": int(applied_version),
                "desired_version": int(desired_version),
                "deleted": bool(deleted),
                "error_ref": str(error_ref) if error_ref else None,
                "error_class": str(error_class) if error_class else None,
                "error_msg": str(error_msg) if error_msg else None,
                "created_at": created_at,
                "finished_at": finished_at,
            }
            for (
                dest_ch_id,
                dest_msg_id,
                state,
                crosspost_state,
                attempts,
                applied_version,
                desired_version,
                deleted,
                error_ref,
                error_class,
                error_msg,
                created_at,
                finished_at,
                dest_server_id,
            ) in rows
        ]

    @classmethod
    @ensure_session(db_session)
    async def prune(
        cls,
        *,
        now: dt.datetime | None = None,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Prune rows older than the retention window, keeping a per-channel anchor.

        We never edit or delete a mirrored message older than ``mirror_retention_days``,
        so everything past that window is pruned — *except* the single most-recent
        DELIVERED message per destination channel, which is kept indefinitely as a
        cautious record (so we always know the last thing mirrored to a channel). Every
        row within the window is kept, including a non-latest one (a channel can hold a
        second, user-related announcement we may still need to touch).
        """
        now = now or _utcnow()
        cutoff = now - dt.timedelta(days=cfg.mirror_retention_days)

        # Old, non-DELIVERED rows are never anchors — prune them outright.
        await session.execute(
            delete(cls).where(
                and_(
                    cls.created_at < cutoff,
                    cls.state != DeliveryState.DELIVERED.value,
                )
            )
        )
        # Old DELIVERED rows: prune those superseded by a newer DELIVERED in the
        # SAME destination channel — the single latest per channel stays as the anchor.
        # SELECT-the-pks then DELETE-by-pk (not one self-referencing DELETE). This shape
        # was forced by MySQL (error 1093: the delete target may not be referenced from
        # a subquery); Postgres and SQLite both allow the single statement, but the
        # two-step form is kept — it is portable, and it bounds the delete by pk.
        newer = aliased(cls)
        superseded = (
            await session.execute(
                select(cls.src_msg_id, cls.dest_ch_id).where(
                    and_(
                        cls.created_at < cutoff,
                        cls.state == DeliveryState.DELIVERED.value,
                        exists().where(
                            and_(
                                newer.dest_ch_id == cls.dest_ch_id,
                                newer.state == DeliveryState.DELIVERED.value,
                                newer.finished_at > cls.finished_at,
                            )
                        ),
                    )
                )
            )
        ).fetchall()
        pks = [(int(smi), int(dci)) for smi, dci in superseded]
        for i in range(0, len(pks), 500):  # chunk to bound the IN-list / packet size
            await session.execute(
                delete(cls).where(
                    tuple_(cls.src_msg_id, cls.dest_ch_id).in_(pks[i : i + 500])
                )
            )


class MirrorMessageVersion(Base):
    """Per-version content snapshot of a mirrored source message.

    The delivery ledger (:class:`MirrorDelivery`) stores *intent*, never content — the
    worker fetches the source fresh at delivery time. This table is the one place we
    persist *what was actually delivered*: the worker snapshots each
    ``(src_msg_id, version)`` once, as it materializes that version on a source-cache
    miss (so one cheap write per version, not per destination), letting the web log
    re-render every version a follower saw and diff between them. There is **no
    backfill** — history starts at deploy.

    ``payload`` is a JSON snapshot in Discord's own component/embed shape:

    - ``kind == "cv2"``:  ``{"components": [raw component dicts]}`` — the re-renderable,
      diffable Components V2 tree, post item-emoji rewrite (i.e. exactly what followers
      receive).
    - ``kind == "classic"``:  ``{"content": str, "embeds": [embed dicts]}``.
    - a pathological (over-cap) post is stored as ``{"truncated": true}`` — the row
      still records that a version existed, just not its full body.

    Attachments/stickers are referenced by url inside the payload, never stored as
    binary. ``summary`` is a short label (first text line / embed title) for the run
    list; ``captured_at`` is naive-UTC.
    """

    __tablename__ = "mirror_message_version"
    __table_args__ = (
        # prune scans by captured age; also serves the versions-for-source lookup's
        # sibling ordering cheaply enough without a second index.
        Index("ix_mirror_message_version_captured_at", "captured_at"),
    )

    src_msg_id = Column("src_msg_id", BigInteger, primary_key=True)
    version = Column("version", Integer, primary_key=True)
    captured_at = Column("captured_at", DateTime, nullable=False, default=_utcnow)
    src_guild_id = Column("src_guild_id", BigInteger, nullable=True)
    kind = Column("kind", String(8), nullable=False)
    summary = Column("summary", String(200), nullable=True)
    payload = Column("payload", JSON, nullable=False)

    @classmethod
    @ensure_session(db_session)
    async def capture(
        cls,
        *,
        src_msg_id: int,
        version: int,
        src_guild_id: int | None,
        kind: str,
        summary: str | None,
        payload: dict[str, t.Any],
        session: AsyncSession = _UNSET,
    ) -> int:
        """Record one version snapshot; return rows inserted (0 if already captured).

        INSERT-IGNORE on the ``(src_msg_id, version)`` PK, so re-materializing a version
        already snapshotted (a multi-batch fan-out, a restart) is a cheap no-op rather
        than a clobber — the first capture wins.
        """
        result = await session.execute(
            _insert_ignore(cls, session).values(
                src_msg_id=int(src_msg_id),
                version=int(version),
                captured_at=_utcnow(),
                src_guild_id=int(src_guild_id) if src_guild_id is not None else None,
                kind=str(kind),
                summary=(str(summary)[:200] if summary else None),
                payload=payload,
            )
        )
        return result.rowcount or 0

    @classmethod
    @ensure_session(db_session)
    async def versions_for(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> list[dict[str, t.Any]]:
        """Captured versions of one source message, oldest first (the selector list).

        Returns the light metadata only — ``version``, ``captured_at`` (naive-UTC),
        ``summary``, ``kind`` — never the payload, so the detail JSON stays small; the
        payload is fetched per-version by the render route.
        """
        rows = (
            await session.execute(
                select(cls.version, cls.captured_at, cls.summary, cls.kind)
                .where(cls.src_msg_id == int(src_msg_id))
                .order_by(cls.version)
            )
        ).fetchall()
        return [
            {
                "version": int(version),
                "captured_at": captured_at,
                "summary": summary,
                "kind": str(kind),
            }
            for version, captured_at, summary, kind in rows
        ]

    @classmethod
    @ensure_session(db_session)
    async def latest_for(
        cls,
        src_msg_ids: t.Sequence[int],
        *,
        session: AsyncSession = _UNSET,
    ) -> dict[int, dict[str, t.Any]]:
        """Latest snapshot per source message, for the run-list summary + source link.

        One query over the given ids (the run list is capped, so the id set is small),
        reduced in Python to the highest ``version`` per ``src_msg_id``. Returns a map
        of ``src_msg_id -> {version, src_guild_id, summary, kind}`` (no payload)."""
        if not src_msg_ids:
            return {}
        ids = [int(i) for i in src_msg_ids]
        rows = (
            await session.execute(
                select(
                    cls.src_msg_id,
                    cls.version,
                    cls.src_guild_id,
                    cls.summary,
                    cls.kind,
                )
                .where(cls.src_msg_id.in_(ids))
                .order_by(cls.src_msg_id, cls.version)
            )
        ).fetchall()
        # Ordered by version ascending, so the last row seen per id is its latest.
        latest: dict[int, dict[str, t.Any]] = {}
        for src_msg_id, version, src_guild_id, summary, kind in rows:
            latest[int(src_msg_id)] = {
                "version": int(version),
                "src_guild_id": int(src_guild_id) if src_guild_id is not None else None,
                "summary": summary,
                "kind": str(kind),
            }
        return latest

    @classmethod
    @ensure_session(db_session)
    async def get_version(
        cls,
        src_msg_id: int,
        version: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> dict[str, t.Any] | None:
        """One version's full snapshot (payload included), or ``None`` if absent."""
        row = (
            await session.execute(
                select(
                    cls.version,
                    cls.captured_at,
                    cls.src_guild_id,
                    cls.kind,
                    cls.summary,
                    cls.payload,
                ).where(
                    and_(cls.src_msg_id == int(src_msg_id), cls.version == int(version))
                )
            )
        ).first()
        if row is None:
            return None
        version_, captured_at, src_guild_id, kind, summary, payload = row
        return {
            "version": int(version_),
            "captured_at": captured_at,
            "src_guild_id": int(src_guild_id) if src_guild_id is not None else None,
            "kind": str(kind),
            "summary": summary,
            "payload": payload,
        }

    @classmethod
    @ensure_session(db_session)
    async def prune(
        cls,
        *,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Drop version rows orphaned by :meth:`MirrorDelivery.prune`.

        A snapshot is kept exactly as long as its source still has any delivery row —
        so it lives through the retention window and stays for the indefinitely-kept
        latest-delivered-per-channel anchor, then goes when the last delivery row for
        that source is pruned. Run *after* ``MirrorDelivery.prune``. The subquery
        targets a different table, so this is not the self-reference the ledger prune
        splits into a read plus a delete-by-pk.
        """
        orphaned = (
            (
                await session.execute(
                    select(cls.src_msg_id).where(
                        ~exists().where(MirrorDelivery.src_msg_id == cls.src_msg_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        ids = [int(smi) for smi in orphaned]
        for i in range(0, len(ids), 500):  # chunk the IN-list / packet size
            await session.execute(
                delete(cls).where(cls.src_msg_id.in_(ids[i : i + 500]))
            )


class MirrorOperationLog(Base):
    """Append-only record of each completed mirror op (create / update / delete).

    The delivery ledger (:class:`MirrorDelivery`) only ever holds a source's *current*
    converged state, so it cannot answer "how did each individual operation do?". This
    table does: the drain watcher writes one row when an operation's fan-out drains,
    capturing that operation's own final counts + timing — the exact numbers the retired
    Discord progress card showed live. In-flight operations read from the live ledger
    (their counts *are* the current state); settled operations are read from here. No
    backfill — history starts at deploy; earlier operations render as "counts not
    recorded".

    ``op_type`` is ``"create"`` | ``"update"`` | ``"delete"``. ``version`` is the source
    version this operation converged (matches a :class:`MirrorMessageVersion` column;
    NULL if unknown). ``failure_refs`` is the op's own failure breakdown
    (``[{ref, error_class, count, sample}]``), like the ledger's per-run one.
    """

    __tablename__ = "mirror_operation_log"
    __table_args__ = (
        Index("ix_mirror_operation_log_src_msg_id", "src_msg_id"),  # per-message fetch
        Index(
            "ix_mirror_operation_log_finished_at", "finished_at"
        ),  # prune + daily agg
    )

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    src_msg_id = Column("src_msg_id", BigInteger, nullable=False)
    src_ch_id = Column("src_ch_id", BigInteger, nullable=True)
    op_type = Column("op_type", String(8), nullable=False)
    version = Column("version", Integer, nullable=True)
    started_at = Column("started_at", DateTime, nullable=False)
    finished_at = Column("finished_at", DateTime, nullable=False)
    total = Column("total", Integer, nullable=False, default=0)
    delivered = Column("delivered", Integer, nullable=False, default=0)
    failed = Column("failed", Integer, nullable=False, default=0)
    cancelled = Column("cancelled", Integer, nullable=False, default=0)
    attempts = Column("attempts", Integer, nullable=False, default=0)
    failure_refs = Column("failure_refs", JSON, nullable=True)

    @classmethod
    @ensure_session(db_session)
    async def record(
        cls,
        *,
        src_msg_id: int,
        src_ch_id: int | None,
        op_type: str,
        version: int | None,
        started_at: dt.datetime,
        finished_at: dt.datetime,
        total: int,
        delivered: int,
        failed: int,
        cancelled: int,
        attempts: int,
        failure_refs: list[dict[str, t.Any]] | None,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Append one completed-operation row (called once per drain)."""
        session.add(
            cls(
                src_msg_id=int(src_msg_id),
                src_ch_id=int(src_ch_id) if src_ch_id is not None else None,
                op_type=str(op_type),
                version=int(version) if version is not None else None,
                started_at=started_at,
                finished_at=finished_at,
                total=int(total),
                delivered=int(delivered),
                failed=int(failed),
                cancelled=int(cancelled),
                attempts=int(attempts),
                failure_refs=failure_refs,
            )
        )
        await session.flush()

    @classmethod
    @ensure_session(db_session)
    async def for_message(
        cls,
        src_msg_id: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> list[dict[str, t.Any]]:
        """Recorded operations for one source message, oldest first (detail view)."""
        rows = (
            (
                await session.execute(
                    select(cls)
                    .where(cls.src_msg_id == int(src_msg_id))
                    .order_by(cls.finished_at, cls.id)
                )
            )
            .scalars()
            .all()
        )
        return [_operation_row(r) for r in rows]

    @classmethod
    @ensure_session(db_session)
    async def recent(
        cls,
        *,
        within_days: int = 30,
        limit: int = 1000,
        session: AsyncSession = _UNSET,
    ) -> list[dict[str, t.Any]]:
        """Operations in the window, newest first, capped at ``limit``.

        The run-list op chips + the overview's per-op-type daily chart read from this;
        the cap keeps the list-poll payload bounded (the run list is capped too)."""
        cutoff = _utcnow() - dt.timedelta(days=within_days)
        rows = (
            (
                await session.execute(
                    select(cls)
                    .where(cls.finished_at >= cutoff)
                    .order_by(desc(cls.finished_at))
                    .limit(int(limit))
                )
            )
            .scalars()
            .all()
        )
        return [_operation_row(r) for r in rows]

    @classmethod
    @ensure_session(db_session)
    async def prune(
        cls,
        *,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Drop operation rows orphaned by the ledger prune (same lifetime as the
        source's delivery rows). Run after ``MirrorDelivery.prune``."""
        orphaned = (
            (
                await session.execute(
                    select(cls.src_msg_id)
                    .where(~exists().where(MirrorDelivery.src_msg_id == cls.src_msg_id))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        ids = [int(smi) for smi in orphaned]
        for i in range(0, len(ids), 500):
            await session.execute(
                delete(cls).where(cls.src_msg_id.in_(ids[i : i + 500]))
            )


def _operation_row(r: MirrorOperationLog) -> dict[str, t.Any]:
    return {
        "id": int(r.id),
        "src_msg_id": int(r.src_msg_id),
        "src_ch_id": int(r.src_ch_id) if r.src_ch_id is not None else None,
        "op_type": str(r.op_type),
        "version": int(r.version) if r.version is not None else None,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "total": int(r.total),
        "delivered": int(r.delivered),
        "failed": int(r.failed),
        "cancelled": int(r.cancelled),
        "attempts": int(r.attempts),
        "failure_refs": r.failure_refs or [],
    }


class ServerStatistics(Base):
    __tablename__ = "server_statistics"
    __mapper_args__ = {"eager_defaults": True}
    # Server id
    id = Column("id", BigInteger, primary_key=True)
    population = Column("population", BigInteger)

    # Population is set high by default since its better to prioritize
    # new servers if we don't yet know their population
    def __init__(self, id: int, population: int = 10**12):
        self.id = int(id)
        self.population = int(population)

    @classmethod
    @ensure_session(db_session)
    async def add_server(
        cls,
        id: int,
        population: int = 10**12,
        session: AsyncSession = _UNSET,
    ) -> None:
        id = int(id)
        await session.merge(cls(id, population))

    @classmethod
    @ensure_session(db_session)
    async def add_servers_in_batch(
        cls,
        ids: list[int],
        populations: list[int],
        session: AsyncSession = _UNSET,
    ) -> None:
        ids = [int(id) for id in ids]
        populations = [int(population) for population in populations]
        if len(ids) != len(populations):
            raise ValueError("ids and populations must be of the same length")

        if not (ids and populations):
            return

        await session.execute(
            insert(cls),
            [
                {"id": id, "population": population}
                for id, population in zip(ids, populations, strict=True)
            ],
        )

    @classmethod
    @ensure_session(db_session)
    async def fetch_server_ids(
        cls,
        session: AsyncSession = _UNSET,
    ) -> list[int]:
        ids = await session.execute(select(cls.id))
        ids = ids if ids else []
        ids = [id[0] for id in ids]
        return ids

    @classmethod
    @ensure_session(db_session)
    async def fetch_server_populations(
        cls, session: AsyncSession = _UNSET
    ) -> list[tuple[int, int]]:
        """Returns tuples of server id to population, ordered by id.

        The ORDER BY is explicit because Postgres returns rows in physical order, and
        an UPDATE moves the updated tuple to the end of the heap — so an unordered
        SELECT would silently reshuffle the dashboard's population list after every
        population refresh.
        """
        populations = (
            await session.execute(select(cls.id, cls.population).order_by(cls.id))
        ).fetchall()
        populations = populations if populations else []
        return populations

    @classmethod
    @ensure_session(db_session)
    async def update_population(
        cls,
        id: int,
        population: int,
        session: AsyncSession = _UNSET,
    ) -> None:
        id = int(id)
        await session.execute(
            update(cls).where(cls.id == id).values(population=population)
        )

    @classmethod
    @ensure_session(db_session)
    async def update_population_in_batch(
        cls,
        ids: list[int],
        populations: list[int],
        session: AsyncSession = _UNSET,
    ) -> None:
        ids = [int(id) for id in ids]
        populations = [int(population) for population in populations]
        await session.execute(
            update(cls),
            [
                {"id": id, "population": population}
                for id, population in zip(ids, populations, strict=True)
            ],
        )


class CommandUsage(Base):
    """Per-command, per-day invocation counts for user-facing slash commands.

    Daily buckets keep growth bounded (commands × days) while supporting both
    all-time totals and time-windowed queries. Writes use an ON CONFLICT upsert with an
    atomic ``count = count + 1`` so concurrent increments are race-free with no
    in-memory buffering.
    """

    __tablename__ = "command_usage"
    __mapper_args__ = {"eager_defaults": True}

    command_name = Column("command_name", String(length=128), primary_key=True)
    date = Column("date", Date, primary_key=True)  # daily bucket, UTC
    count = Column("count", BigInteger, nullable=False, default=0)

    @classmethod
    @ensure_session(db_session)
    async def increment(
        cls, command_name: str, *, session: AsyncSession = _UNSET
    ) -> None:
        today = dt.datetime.now(tz=dt.UTC).date()
        stmt = _dialect_insert(cls, session).values(
            command_name=command_name, date=today, count=1
        )
        # Conflict target is the composite PK. The SET expression reads the *existing*
        # row's count (not stmt.excluded.count, which is the literal 1 being inserted),
        # keeping the increment atomic in one statement.
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[cls.command_name, cls.date],
                set_={"count": cls.count + 1},
            )
        )

    @classmethod
    @ensure_session(db_session)
    async def fetch_totals(
        cls, *, since: dt.date | None = None, session: AsyncSession = _UNSET
    ) -> list[tuple[str, int]]:
        q = select(cls.command_name, func.sum(cls.count).label("total"))
        if since is not None:
            q = q.where(cls.date >= since)
        q = q.group_by(cls.command_name).order_by(desc("total"))
        return [(name, int(total)) for name, total in (await session.execute(q)).all()]

    @classmethod
    @ensure_session(db_session)
    async def fetch_daily(
        cls, *, since: dt.date, session: AsyncSession = _UNSET
    ) -> list[tuple[str, dt.date, int]]:
        """Per-command daily counts on/after ``since`` as ``(name, date, count)`` rows.

        Ordered by name then date. The presentation layer derives both windowed totals
        (for trend deltas) and per-day series (for sparklines) from these rows, so the
        windowing math stays in pure, testable Python rather than SQL.
        """
        q = (
            select(cls.command_name, cls.date, cls.count)
            .where(cls.date >= since)
            .order_by(cls.command_name, cls.date)
        )
        return [(n, d, int(c)) for n, d, c in (await session.execute(q)).all()]


class AutopostDailyStat(Base):
    """Per-feed, per-day snapshot of active autopost destination counts.

    One row per ``(date, feed, kind)`` where ``kind`` is ``"follow"`` (native Discord
    channel-follows) or ``"mirror"`` (legacy mirrored channels). A daily task snapshots
    the current ``MirroredChannel`` reach into this table, so the series is robust to
    later removals (a deleted ``MirroredChannel`` row leaves the historical snapshot
    intact). Writes are an ON CONFLICT upsert that **overwrites** ``count`` — unlike
    ``CommandUsage.increment``'s ``count = count + 1`` — so re-running the snapshot on
    the same day corrects the value rather than doubling it.
    """

    __tablename__ = "autopost_daily_stat"
    __mapper_args__ = {"eager_defaults": True}

    date = Column("date", Date, primary_key=True)  # daily bucket, UTC
    feed = Column("feed", String(length=32), primary_key=True)  # followable feed slug
    kind = Column("kind", String(length=8), primary_key=True)  # "follow" | "mirror"
    count = Column("count", BigInteger, nullable=False, default=0)

    @classmethod
    @ensure_session(db_session)
    async def record(
        cls,
        date: dt.date,
        feed: str,
        kind: str,
        count: int,
        *,
        session: AsyncSession = _UNSET,
    ) -> None:
        stmt = _dialect_insert(cls, session).values(
            date=date, feed=feed, kind=kind, count=count
        )
        # Conflict target is the composite PK. ``excluded`` is the row the INSERT tried
        # to add (MySQL spelled it ``inserted``), so this overwrites with the new count.
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[cls.date, cls.feed, cls.kind],
                set_={"count": stmt.excluded.count},
            )
        )

    @classmethod
    @ensure_session(db_session)
    async def fetch_series(
        cls, *, since: dt.date | None = None, session: AsyncSession = _UNSET
    ) -> list[tuple[dt.date, str, str, int]]:
        """Daily snapshot rows on/after ``since`` as ``(date, feed, kind, count)``.

        Ordered by date, then feed, then kind. The web layer re-buckets this daily
        series into weekly/monthly resolutions in the browser, so the aggregation math
        stays out of SQL.
        """
        q = select(cls.date, cls.feed, cls.kind, cls.count)
        if since is not None:
            q = q.where(cls.date >= since)
        q = q.order_by(cls.date, cls.feed, cls.kind)
        return [(d, f, k, int(c)) for d, f, k, c in (await session.execute(q)).all()]


class UserCommand(Base):
    __tablename__ = "user_command"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("l1_name", "l2_name", "l3_name", name="_ln_name_uc"),
        # Make sure if l3_name is empty then response_type is 0
        # ie either l3_name can be empty or response_type can be non 0
        CheckConstraint("l3_name = '' OR response_type <> 0"),
        # Make sure if l2_name is empty, the so is l3
        CheckConstraint("(l2_name = '' AND l3_name = '') OR (l2_name <> '')"),
    )
    id: Mapped[int] = mapped_column("id", Integer, primary_key=True)
    l1_name: Mapped[str] = mapped_column("l1_name", String(length=32), nullable=True)
    l2_name: Mapped[str] = mapped_column("l2_name", String(length=32), nullable=True)
    l3_name: Mapped[str] = mapped_column("l3_name", String(length=32), nullable=True)
    # command_name can include spaces, must match rgx_command_name_is_valid
    description: Mapped[str] = mapped_column(
        "description", String(length=256), nullable=True
    )
    response_type: Mapped[int] = mapped_column("response_type", Integer, nullable=True)
    # response_types are as follows:
    # 0: No response, ie this is a command group
    # 1: Plain text, respondes directly with response_data column text
    # 2: Message copy, fetches a message and responds with a copy of its content,
    #    embeds, components and attachments. response_data must be a Discord message
    #    link (…/channels/<guild_id>/<channel_id>/<message_id>); the channel_id and
    #    message_id are taken from the last two path segments. Note: the bot must be
    #    able to fetch the message, so check it is accessible before adding to db
    # 3: Embed, responds by parsing response data, parsing the same as
    #    json, and passing it to hikari.Embed(...). This embed is sent
    #    as a response
    response_data: Mapped[str] = mapped_column(Text, nullable=True)

    def __init__(
        self,
        l1_name: str,
        l2_name: str = "",
        l3_name: str = "",
        *,
        description: str,
        response_type: int,
        response_data: str = "",
    ):
        self.l1_name = str(l1_name)
        self.l2_name = str(l2_name)
        self.l3_name = str(l3_name)
        self.description = str(description)
        self.response_type = int(response_type)
        self.response_data = str(response_data)

    def __repr__(self) -> str:
        return " -> ".join(
            ln_name for ln_name in [self.l1_name, self.l2_name, self.l3_name] if ln_name
        )

    @validates("l1_name", "l2_name", "l3_name")
    def command_name_validator(self, key: str, value: str) -> str:
        """Restrict to valid discord command names"""
        value = str(value)
        if (
            key == "l1_name"
            and rgx_cmd_name_is_valid.match(value)
            or key in ["l2_name", "l3_name"]
            and rgx_sub_cmd_name_is_valid.match(value)
        ):
            return value
        else:
            raise FriendlyValueError(
                "Command names must start with a letter, be all lowercase, and only "
                + "contain letter, numbers, dashes (-) and underscores (_) and must "
                + "not be longer than 32 characters. Spaces cannot be used."
            )

    @classmethod
    @ensure_session(db_session)
    async def fetch_commands(cls, session: AsyncSession = _UNSET) -> list[UserCommand]:
        commands = (
            await session.execute(select(cls).where(cls.response_type != 0))
        ).fetchall()
        commands = commands if commands else []
        commands = [command[0] for command in commands]
        return commands

    @classmethod
    @ensure_session(db_session)
    async def fetch_command_groups(
        cls, session: AsyncSession = _UNSET
    ) -> list[UserCommand]:
        commands = (
            await session.execute(
                select(cls)
                .where(cls.response_type == 0)
                .order_by(cls.l1_name, cls.l2_name, cls.l3_name)
            )
        ).fetchall()
        commands = commands if commands else []
        commands = [command[0] for command in commands]
        return commands

    @classmethod
    @ensure_session(db_session)
    async def fetch_command(
        cls, *ln_names: str, session: AsyncSession = _UNSET
    ) -> UserCommand | None:
        check_number_of_layers(ln_names)

        # Pad ln_names with "" up to len 3
        ln_names = list(ln_names)
        ln_names.extend([""] * (3 - len(ln_names)))

        return (
            await session.execute(
                select(cls).where(
                    and_(
                        cls.l1_name == ln_names[0],
                        cls.l2_name == ln_names[1],
                        cls.l3_name == ln_names[2],
                        cls.response_type != 0,
                    )
                )
            )
        ).scalar()

    @classmethod
    @ensure_session(db_session)
    async def fetch_command_group(
        cls, *ln_names: str, session: AsyncSession = _UNSET
    ) -> UserCommand | None:
        if len(ln_names) >= 3:
            raise FriendlyValueError(
                "Discord does not support slash command groups more than "
                + "2 layers deep"
            )
        elif len(ln_names) == 0:
            raise ValueError("Too few ln_names provided, need at least 1")

        # Pad ln_names with "" up to len 3
        ln_names = list(ln_names)
        ln_names.extend([""] * (2 - len(ln_names)))

        return (
            await session.execute(
                select(cls).where(
                    and_(
                        cls.l1_name == ln_names[0],
                        cls.l2_name == ln_names[1],
                        cls.response_type == 0,
                    )
                )
            )
        ).scalar()

    @classmethod
    @ensure_session(db_session)
    async def _autocomplete(
        cls,
        l1_name: str = "",
        l2_name: str = "",
        l3_name: str = "",
        session: AsyncSession = _UNSET,
    ) -> list[UserCommand]:
        completions = (
            await session.execute(
                select(cls).where(
                    (cls.l1_name + cls.l2_name + cls.l3_name).startswith(
                        l1_name + l2_name + l3_name
                    )
                )
            )
        ).fetchall()
        completions = completions if completions else []
        completions = [completion[0] for completion in completions]
        return completions

    @classmethod
    @ensure_session(db_session)
    async def add_command(
        cls,
        *ln_names: str,  # Layer n names
        description: str,
        response_type: int,
        response_data: str,
        session: AsyncSession = _UNSET,
    ) -> UserCommand:
        check_number_of_layers(ln_names)
        await cls.check_parent_command_groups_exist(*ln_names, session=session)

        # Check if there is an existing command with the same name
        existing_command = await cls.fetch_command(*ln_names, session=session)
        if existing_command:
            raise FriendlyValueError(
                f"Command {' -> '.join(filter(lambda n: n != '', ln_names))} "
                "already exists"
            )

        self = cls(
            *ln_names,
            description=description,
            response_type=response_type,
            response_data=response_data,
        )
        session.add(self)
        return self

    @classmethod
    @ensure_session(db_session)
    async def add_command_group(
        cls, *ln_names, description, session: AsyncSession = _UNSET
    ) -> UserCommand:
        return await cls.add_command(
            *ln_names,
            description=description,
            response_type=0,  # Response type 0 for command groups
            session=session,
            response_data=None,
        )

    @classmethod
    @ensure_session(db_session)
    async def check_parent_command_groups_exist(
        cls,
        l1_name: str,
        l2_name: str = "",
        l3_name: str = "",
        session: AsyncSession = _UNSET,
    ) -> bool:
        """Check if the parent command groups exist

        Note, this is different from the command existing (response_type must be 0)

        raises FriendlyValueError if command groups specified do not exist"""

        if l2_name:
            # Only check l1_name if l3_name command is provided
            l1_exists = (
                await session.execute(
                    select(cls.id).where(
                        # Check whether l1_name exists with a 0 response type
                        # since 0 response types signify a command group
                        and_(cls.l1_name == l1_name, cls.response_type == 0)
                    )
                )
            ).scalar()
            # scalar only returns false (None) when no rows are found

            if not l1_exists:
                raise FriendlyValueError(
                    f"{l1_name} is not an existing command group",
                )

        if l3_name:
            # Only check if l2_name exists if l3_name command is provided
            l2_exists = (
                await session.execute(
                    select(cls.id).where(
                        and_(
                            # Check whether l1_name -> l2_name exists with a 0 response
                            # type since 0 response types signify a command group
                            cls.l1_name == l1_name,
                            cls.l2_name == l2_name,
                            cls.response_type == 0,
                        )
                    )
                )
            ).scalar()
            # scalar only returns false (None) when no rows are found

            if not l2_exists:
                raise FriendlyValueError(
                    f"{l1_name} -> {l2_name} is not an existing command group",
                )

        # Return true if command groups exist
        return True

    @classmethod
    @ensure_session(db_session)
    async def fetch_subcommands(
        cls, l1_name, l2_name: str = "", session: AsyncSession = _UNSET
    ) -> list[UserCommand]:
        return (
            await session.execute(
                select(cls).where(
                    and_(
                        cls.l1_name == l1_name,
                        # The below is to handle subcommands of command groups
                        # at the top layer where l2_name will not be specified
                        # when trying to fetch subcommands
                        (cls.l2_name == l2_name) if l2_name else True,
                        cls.response_type != 0,
                    )
                )
            )
        ).fetchall()

    @classmethod
    @ensure_session(db_session)
    async def delete_command(
        cls,
        l1_name: str,
        l2_name: str = "",
        l3_name: str = "",
        fetch_deleted: bool = True,
        session: AsyncSession = _UNSET,
    ) -> UserCommand:
        commands_to_delete = (
            (
                await session.execute(
                    select(cls).where(
                        and_(
                            cls.l1_name == l1_name,
                            cls.l2_name == l2_name,
                            cls.l3_name == l3_name,
                            cls.response_type != 0,
                        )
                    )
                )
            ).scalar()
            if fetch_deleted  # Do not fetch if fetch_deleted is False
            else []
        )

        await session.execute(
            delete(cls).where(
                and_(
                    cls.l1_name == l1_name,
                    cls.l2_name == l2_name,
                    cls.l3_name == l3_name,
                    cls.response_type != 0,
                )
            )
        )
        return commands_to_delete

    @classmethod
    @ensure_session(db_session)
    async def delete_command_group(
        cls,
        l1_name: str,
        l2_name: str = "",
        cascade: bool = False,
        fetch_deleted: bool = True,
        session: AsyncSession = _UNSET,
    ) -> list[UserCommand]:
        subcommands = await cls.fetch_subcommands(l1_name, l2_name, session=session)
        if subcommands and not cascade:
            # Handle the case where subcommands are found and we aren't supposed
            # to cascade delete
            raise FriendlyValueError(
                f"Command group {l1_name}{(' -> ' + l2_name) if l2_name else ''} "
                + "still has subcommands"
            )
        else:
            # If cascade delete is not specified then the below will only delete the
            # command group since we already know that there are no subcommands as per
            # the above branch
            # If cascade delete is True, delete all with matching l1 & if specified l2
            # names

            deleted = (
                (
                    await session.execute(
                        select(cls).where(
                            and_(
                                cls.l1_name == l1_name,
                                (cls.l2_name == l2_name) if l2_name else True,
                            )
                        )
                    )
                ).fetchall()
                if fetch_deleted  # Do not fetch if fetch_delted is False
                else []
            )
            await session.execute(
                delete(cls).where(
                    and_(
                        cls.l1_name == l1_name,
                        (cls.l2_name == l2_name) if l2_name else True,
                    )
                )
            )
            deleted = deleted if deleted else []
            deleted = [item[0] for item in deleted]
            return deleted

    @property
    def is_command_group(self) -> bool:
        return self.response_type == 0

    @property
    def is_subcommand_or_subgroup(self) -> bool:
        return self.depth > 1

    @property
    def depth(self) -> int:
        return len(self.ln_names)

    @property
    def ln_names(self) -> list[str]:
        return [
            ln_name for ln_name in [self.l1_name, self.l2_name, self.l3_name] if ln_name
        ]


class AutoPostSettings(Base):
    __tablename__ = "auto_post_settings"
    __mapper_args__ = {"eager_defaults": True}

    name = Column("name", VARCHAR(32), primary_key=True)
    enabled = Column(
        "enabled",
        Boolean,
        default=True,
    )
    # Optional free-text payload for settings that need a value, not just an on/off
    # flag (e.g. ``eververse_image_url`` stores the default-image URL edited from the
    # autopost settings webpage). NULL for the plain boolean toggle rows.
    value = Column("value", String(512), nullable=True, default=None)

    def __init__(
        self,
        name: str,
        enabled=False,
        value: str | None = None,
    ):
        self.name = name
        self.enabled = enabled
        self.value = value

    @classmethod
    @ensure_session(db_session)
    async def get_enabled(
        cls, auto_post_name: str, session: AsyncSession = _UNSET
    ) -> bool | None:
        enabled = (
            await session.execute(select(cls.enabled).where(cls.name == auto_post_name))
        ).scalar()
        return enabled

    @classmethod
    @ensure_session(db_session)
    async def set_enabled(
        cls, auto_post_name: str, enabled: bool, session: AsyncSession = _UNSET
    ) -> None:
        currently_enabled = await cls.get_enabled(auto_post_name, session=session)

        if currently_enabled == enabled:
            return
        elif currently_enabled is None:
            await session.execute(
                insert(cls).values({cls.name: auto_post_name, cls.enabled: enabled})
            )
        else:
            await session.execute(
                update(cls)
                .values({cls.enabled: enabled})
                .where(cls.name == auto_post_name)
            )

    @classmethod
    @ensure_session(db_session)
    async def get_all_rows(
        cls, session: AsyncSession = _UNSET
    ) -> dict[str, tuple[bool | None, str | None]]:
        """Every row as ``{name: (enabled, value)}`` — one query, for a startup preload.

        Used by :mod:`dd.common.settings` to warm its process-local cache rather than
        issuing one query per setting.
        """
        rows = (await session.execute(select(cls.name, cls.enabled, cls.value))).all()
        return {name: (enabled, value) for name, enabled, value in rows}

    @classmethod
    @ensure_session(db_session)
    async def get_value(
        cls, auto_post_name: str, session: AsyncSession = _UNSET
    ) -> str | None:
        return (
            await session.execute(select(cls.value).where(cls.name == auto_post_name))
        ).scalar()

    @classmethod
    @ensure_session(db_session)
    async def set_value(
        cls, auto_post_name: str, value: str | None, session: AsyncSession = _UNSET
    ) -> None:
        # Upsert on the name PK, touching only the ``value`` column so an existing row's
        # ``enabled`` flag is preserved (value-only slugs like ``eververse_image_url``
        # never have their ``enabled`` read by a producer).
        row_exists = (
            await session.execute(select(cls.name).where(cls.name == auto_post_name))
        ).scalar()
        if row_exists is None:
            await session.execute(
                insert(cls).values({cls.name: auto_post_name, cls.value: value})
            )
        else:
            await session.execute(
                update(cls).values({cls.value: value}).where(cls.name == auto_post_name)
            )

    @classmethod
    async def get_eververse_enabled(cls) -> bool | None:
        return await cls.get_enabled("eververse")

    @classmethod
    async def set_eververse(cls, enabled: bool) -> None:
        return await cls.set_enabled("eververse", enabled)

    @classmethod
    async def get_eververse_image_url(cls) -> str | None:
        return await cls.get_value("eververse_image_url")

    @classmethod
    async def set_eververse_image_url(cls, url: str | None) -> None:
        return await cls.set_value("eververse_image_url", url)

    @classmethod
    async def get_lost_sector_enabled(cls) -> bool | None:
        return await cls.get_enabled("lost_sector")

    @classmethod
    async def set_lost_sector(cls, enabled: bool) -> None:
        return await cls.set_enabled("lost_sector", enabled)

    @classmethod
    async def get_lost_sector_details_enabled(cls) -> bool | None:
        return await cls.get_enabled("lost_sector_details")

    @classmethod
    async def set_lost_sector_details(cls, enabled: bool) -> None:
        return await cls.set_enabled("lost_sector_details", enabled)

    @classmethod
    async def get_xur_enabled(cls) -> bool | None:
        return await cls.get_enabled("xur")

    @classmethod
    async def set_xur(cls, enabled: bool) -> None:
        return await cls.set_enabled("xur", enabled)

    @classmethod
    async def get_xur_default_image_enabled(cls) -> bool | None:
        return await cls.get_enabled("xur_default_image")

    @classmethod
    async def set_xur_default_image_enabled(cls, enabled: bool) -> None:
        return await cls.set_enabled("xur_default_image", enabled)

    @classmethod
    async def get_ada_enabled(cls) -> bool | None:
        return await cls.get_enabled("ada")

    @classmethod
    async def set_ada(cls, enabled: bool) -> None:
        return await cls.set_enabled("ada", enabled)

    @classmethod
    async def get_portal_ops_enabled(cls) -> bool | None:
        return await cls.get_enabled("portal_ops")

    @classmethod
    async def set_portal_ops(cls, enabled: bool) -> None:
        return await cls.set_enabled("portal_ops", enabled)

    # NB: weekly_reset and trials intentionally have NO enabled/disabled accessors.
    # Those two are hybrid-post producers driven entirely by the web form's
    # Create/Publish buttons — no reset-day autopost cron to gate, so no toggle row.

    @classmethod
    async def get_iron_banner_enabled(cls) -> bool | None:
        return await cls.get_enabled("iron_banner")

    @classmethod
    async def set_iron_banner(cls, enabled: bool) -> None:
        return await cls.set_enabled("iron_banner", enabled)


class RotationData(Base):
    """Whole-document JSON store for rotation-based posts (one row per post type).

    The PK ``name`` is the post-type slug (``lost_sector``, future
    ``dares_of_eternity``…) — the same slug used by :class:`AutoPostSettings` and
    ``settings.FOLLOWABLE_SLUGS`` — so a post type is addressed identically across its
    enabled-flag, channel and data. ``data`` is the full JSON document validated by
    :mod:`dd.common.rotation_schema`; integrity lives at the app layer (schema +
    attrs construction + the tolerant loader), not in a relational shape, so new post
    types need a new *row*, never a migration.
    """

    __tablename__ = "rotation_data"
    __mapper_args__ = {"eager_defaults": True}

    name = Column("name", VARCHAR(32), primary_key=True)
    data = Column("data", JSON, nullable=False)
    updated_at = Column("updated_at", DateTime, default=None)

    @classmethod
    @ensure_session(db_session)
    async def get_data(
        cls, name: str, session: AsyncSession = _UNSET
    ) -> dict[str, t.Any] | None:
        """Return the stored JSON document for ``name``, or ``None`` if absent."""
        return (
            await session.execute(select(cls.data).where(cls.name == name))
        ).scalar()

    @classmethod
    @ensure_session(db_session)
    async def set_data(
        cls, name: str, data: dict[str, t.Any], session: AsyncSession = _UNSET
    ) -> None:
        """Upsert the whole JSON document for ``name``, stamping ``updated_at``."""
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        exists = (
            await session.execute(select(cls.name).where(cls.name == name))
        ).scalar()
        if exists is None:
            await session.execute(
                insert(cls).values(
                    {cls.name: name, cls.data: data, cls.updated_at: now}
                )
            )
        else:
            await session.execute(
                update(cls)
                .values({cls.data: data, cls.updated_at: now})
                .where(cls.name == name)
            )


class AppEmojiCache(Base):
    """Per-bot cache of lazily-uploaded application emojis for Destiny item icons.

    Application emojis are per-app and only render inline in messages the owning app
    posts, so the composite PK ``(app_id, name)`` scopes each row to one bot's store.
    ``last_used`` drives LRU eviction (safe: a deleted emoji's CDN image persists, so
    posted messages keep rendering); ``icon_url`` lets the store detect a name reused
    for a different icon. See :mod:`dd.common.emoji_store`.
    """

    __tablename__ = "app_emoji_cache"
    __mapper_args__ = {"eager_defaults": True}

    app_id = Column("app_id", BigInteger, primary_key=True)
    name = Column("name", VARCHAR(32), primary_key=True)
    emoji_id = Column("emoji_id", BigInteger, nullable=False)
    icon_url = Column("icon_url", VARCHAR(256), nullable=False, default="")
    last_used = Column("last_used", DateTime, nullable=False)

    __table_args__ = (
        Index("ix_app_emoji_lru", "app_id", "last_used"),
        Index("ix_app_emoji_emoji_id", "emoji_id"),
    )

    @classmethod
    @ensure_session(db_session)
    async def all_for_app(
        cls, app_id: int, session: AsyncSession = _UNSET
    ) -> list[Self]:
        """All cached rows for one application store."""
        return list(
            (await session.execute(select(cls).where(cls.app_id == app_id))).scalars()
        )

    @classmethod
    @ensure_session(db_session)
    async def get_by_emoji_id(
        cls, emoji_id: int, session: AsyncSession = _UNSET
    ) -> Self | None:
        """The row for a rendered emoji id, across every app's store (or ``None``).

        Emoji ids are Discord snowflakes (globally unique), so at most one row matches
        even though both bots write this table. Used by the beacon mirror to tell an
        anchor *item* emoji (rewrite to our own) from any other emoji (leave alone).
        """
        return (
            await session.execute(select(cls).where(cls.emoji_id == emoji_id))
        ).scalar_one_or_none()

    @classmethod
    @ensure_session(db_session)
    async def upsert(
        cls,
        app_id: int,
        name: str,
        emoji_id: int,
        icon_url: str,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Insert or refresh a cache row, stamping ``last_used`` = now.

        Uses a portable select-then-write (like :meth:`RotationData.set_data`) so the
        SQLite test engine and prod Postgres behave identically.
        """
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        exists_ = (
            await session.execute(
                select(cls.name).where(and_(cls.app_id == app_id, cls.name == name))
            )
        ).scalar()
        if exists_ is None:
            await session.execute(
                insert(cls).values(
                    {
                        cls.app_id: app_id,
                        cls.name: name,
                        cls.emoji_id: emoji_id,
                        cls.icon_url: icon_url,
                        cls.last_used: now,
                    }
                )
            )
        else:
            await session.execute(
                update(cls)
                .where(and_(cls.app_id == app_id, cls.name == name))
                .values(
                    {cls.emoji_id: emoji_id, cls.icon_url: icon_url, cls.last_used: now}
                )
            )

    @classmethod
    @ensure_session(db_session)
    async def touch(
        cls, app_id: int, names: list[str], session: AsyncSession = _UNSET
    ) -> None:
        """Bump ``last_used`` = now for the given names (LRU recency)."""
        if not names:
            return
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        await session.execute(
            update(cls)
            .where(and_(cls.app_id == app_id, cls.name.in_(names)))
            .values({cls.last_used: now})
        )

    @classmethod
    @ensure_session(db_session)
    async def oldest(
        cls, app_id: int, limit: int, session: AsyncSession = _UNSET
    ) -> list[str]:
        """The ``limit`` least-recently-used emoji names for this app (LRU victims)."""
        return list(
            (
                await session.execute(
                    select(cls.name)
                    .where(cls.app_id == app_id)
                    .order_by(cls.last_used.asc())
                    .limit(limit)
                )
            ).scalars()
        )

    @classmethod
    @ensure_session(db_session)
    async def remove(
        cls, app_id: int, name: str, session: AsyncSession = _UNSET
    ) -> None:
        """Delete a single cache row."""
        await session.execute(
            delete(cls).where(and_(cls.app_id == app_id, cls.name == name))
        )


class BungieCredentials(Base):
    __tablename__ = "bungie_credentials"
    __mapper_args__ = {"eager_defaults": True}

    id = Column("id", Integer, primary_key=True)
    api_key = cfg.bungie_api_key
    client_id = cfg.bungie_client_id
    client_secret = cfg.bungie_client_secret
    refresh_token = Column("refresh_token", VARCHAR(1024), default=None)
    refresh_token_expires = Column("refresh_token_expires", DateTime, default=None)

    def __init__(
        self,
        id: int = 1,
        refresh_token=None,
        refresh_token_expires=None,
    ):
        self.id = id
        self.refresh_token = refresh_token
        self.refresh_token_expires = refresh_token_expires

    @classmethod
    @ensure_session(db_session)
    async def get_credentials(cls, id=1, session: AsyncSession = _UNSET) -> Self:
        return (await session.execute(select(cls).where(cls.id == id))).scalar()

    @classmethod
    @ensure_session(db_session)
    async def set_refresh_token(
        cls,
        id=1,
        refresh_token=None,
        refresh_token_expires=None,
        session: AsyncSession = _UNSET,
    ) -> None:
        refresh_token_expires = dt.datetime.now() + dt.timedelta(
            seconds=refresh_token_expires * 0.8  # 20% Factor of Safety
        )

        self = (await session.execute(select(cls.id).where(cls.id == id))).scalar()

        if self:
            await session.execute(
                update(cls)
                .where(cls.id == id)
                .values(
                    {
                        cls.refresh_token: refresh_token,
                        cls.refresh_token_expires: refresh_token_expires,
                    }
                )
            )
        else:
            await session.execute(
                insert(cls).values(
                    {
                        cls.id: id,
                        cls.refresh_token: refresh_token,
                        cls.refresh_token_expires: refresh_token_expires,
                    }
                )
            )


class Cv2Draft(Base):
    """A handoff session for the web Components-V2 builder.

    The in-Discord ``/post components`` builder kept its whole state in one ephemeral
    message. The web builder can't: the Discord command and the browser are different
    processes, so the command writes a draft row and replies with a link, and the page
    reads it back. The row also carries **what to do on publish** — post to a channel,
    edit an existing message, or send a copy.

    That makes the target a property of the row, which is the safety property worth
    stating: publishing reads ``target_channel_id``/``target_message_id`` from here and
    never from the publishing request, so no request can point an existing draft
    somewhere new. A target still has to be *chosen* by someone — for the web-originated
    "custom post" flow that someone is the browser, once, at creation — but whoever
    chooses it must vet it before this row is written (see
    ``dd.anchor.extensions.cv2_builder_page._handle_new``, which validates through the
    settings page's own channel checker), because nothing downstream re-checks it.

    **This is not an auth token.** ``id`` is a UUID for tidy, non-enumerable URLs, but
    every anchor HTTP surface is already gated by Discord OAuth to bot owners
    (:mod:`dd.anchor.extensions.web_auth`), and :meth:`get_for_user` additionally
    requires the row's ``created_by`` to match the logged-in id. Possession of the link
    grants nothing on its own.

    ``nodes`` is the raw CV2 node list (:mod:`dd.anchor.cv2_nodes`) — the same JSON the
    REST API accepts — autosaved as the author edits, so a closed tab or an expired
    session loses nothing. :meth:`prune` drops rows older than **14 days**; drafts are
    scratch space, not history. It runs at boot and then daily on a cron, both in
    ``dd.anchor.extensions.cv2_builder_page`` — the boot pass alone was enough when a
    draft cost someone typing a Discord command, but the web flow mints one per button
    press and a long-lived process would never sweep them.
    """

    __tablename__ = "cv2_draft"
    __mapper_args__ = {"eager_defaults": True}

    # Publish actions. Mirrors the three surfaces in dd/anchor/extensions/posts.py.
    ACTION_POST = "post"  # /post components   -> send to target_channel_id
    ACTION_EDIT = "edit"  # Edit post          -> edit target_message_id in place
    ACTION_COPY = "copy"  # Copy post          -> send a copy to target_channel_id
    ACTIONS = frozenset({ACTION_POST, ACTION_EDIT, ACTION_COPY})

    id = Column("id", VARCHAR(36), primary_key=True)
    created_by = Column("created_by", BigInteger, nullable=False)
    action = Column("action", VARCHAR(8), nullable=False, default=ACTION_POST)
    nodes = Column("nodes", JSON, nullable=False)
    guild_id = Column("guild_id", BigInteger, nullable=True)
    target_channel_id = Column("target_channel_id", BigInteger, nullable=True)
    target_message_id = Column("target_message_id", BigInteger, nullable=True)
    # Set once the draft has been posted/edited, so the page can show "already
    # published" instead of silently double-posting on a browser back-button.
    published_message_id = Column("published_message_id", BigInteger, nullable=True)
    created_at = Column("created_at", DateTime, nullable=False)
    updated_at = Column("updated_at", DateTime, nullable=False)

    __table_args__ = (Index("ix_cv2_draft_created_at", "created_at"),)

    @classmethod
    @ensure_session(db_session)
    async def create(
        cls,
        id: str,
        created_by: int,
        action: str,
        nodes: list[dict[str, t.Any]],
        guild_id: int | None = None,
        target_channel_id: int | None = None,
        target_message_id: int | None = None,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Insert a new draft. ``id`` is caller-supplied (a UUID4 hex string)."""
        if action not in cls.ACTIONS:
            raise FriendlyValueError(f"Unknown draft action {action!r}")
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        await session.execute(
            insert(cls).values(
                {
                    cls.id: id,
                    cls.created_by: created_by,
                    cls.action: action,
                    cls.nodes: nodes,
                    cls.guild_id: guild_id,
                    cls.target_channel_id: target_channel_id,
                    cls.target_message_id: target_message_id,
                    cls.created_at: now,
                    cls.updated_at: now,
                }
            )
        )

    @classmethod
    @ensure_session(db_session)
    async def get_for_user(
        cls, id: str, user_id: int, session: AsyncSession = _UNSET
    ) -> Self | None:
        """The draft, but only if ``user_id`` created it.

        Scoping the read to the creator means one owner can't stumble into another's
        in-progress draft via a shared or guessed link — the OAuth middleware has
        already established *that* they are an owner, this establishes *which* one.
        """
        return (
            await session.execute(
                select(cls).where(and_(cls.id == id, cls.created_by == user_id))
            )
        ).scalar_one_or_none()

    @classmethod
    @ensure_session(db_session)
    async def save_nodes(
        cls,
        id: str,
        user_id: int,
        nodes: list[dict[str, t.Any]],
        session: AsyncSession = _UNSET,
    ) -> None:
        """Autosave the node list, stamping ``updated_at`` (creator-scoped)."""
        await session.execute(
            update(cls)
            .values(
                {
                    cls.nodes: nodes,
                    cls.updated_at: dt.datetime.now(dt.UTC).replace(tzinfo=None),
                }
            )
            .where(and_(cls.id == id, cls.created_by == user_id))
        )

    @classmethod
    @ensure_session(db_session)
    async def mark_published(
        cls,
        id: str,
        user_id: int,
        message_id: int,
        session: AsyncSession = _UNSET,
    ) -> None:
        """Record the message this draft produced (creator-scoped)."""
        await session.execute(
            update(cls)
            .values(
                {
                    cls.published_message_id: message_id,
                    cls.updated_at: dt.datetime.now(dt.UTC).replace(tzinfo=None),
                }
            )
            .where(and_(cls.id == id, cls.created_by == user_id))
        )

    @classmethod
    @ensure_session(db_session)
    async def prune(
        cls, older_than_days: int = 14, session: AsyncSession = _UNSET
    ) -> int:
        """Delete drafts older than ``older_than_days``; returns the row count.

        Keyed on ``created_at``, not ``updated_at``, even though :meth:`save_nodes`
        stamps the latter and a draft still being autosaved is by definition not stale.
        ``ix_cv2_draft_created_at`` is the index that exists; keying on ``updated_at``
        would want its own (and a migration to add it) to avoid a scan. At a 14-day
        retention the difference is a draft someone opened a fortnight ago and has been
        editing ever since — and the cost of getting that wrong is re-doing an edit, not
        losing anything published.
        """
        cutoff = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(
            days=older_than_days
        )
        result = await session.execute(delete(cls).where(cls.created_at < cutoff))
        return int(result.rowcount or 0)


class ManifestLoadInProgress(RuntimeError):
    """Another process is already loading this manifest version — back off and read
    whatever is currently active instead of loading a second copy."""


class DestinyManifestVersion(Base):
    """One row per Destiny manifest Bungie has published *and we have loaded*.

    ``version`` is Bungie's own versioned filename
    (``world_sql_content_<hash>.content``) — the same string the on-disk cache used to
    be keyed by, and the reason invalidation
    is free: a new manifest is a new name, so it can never be confused with the copy we
    hold.

    ``state`` is the load barrier. Rows are inserted ``loading`` and flipped to
    ``active`` only once every definition row is committed, so a load that dies part-way
    (the container is redeployed mid-import) leaves rows no reader will ever look at
    rather than a half-populated manifest that reads as current.

    **A replaced manifest is not deleted when it is replaced — it is deleted when the
    *next* load starts.** Activating a new version only demotes the old one to
    ``superseded``. A reader resolves a version id once and then reads through it for
    the length of a post build; deleting its rows underneath it would turn a
    mid-render lookup into a miss, and the consumers are full of
    ``.get(hash, {})`` defaults that would quietly render "Unknown Type" into a
    published post rather than fail. Deferring the delete to the next load makes that
    window a week wide instead of milliseconds. The cost is one superseded manifest's
    worth of rows kept around; the steady state is two, which is what Postgres would
    have held in dead tuples anyway.

    The ``version`` unique constraint doubles as the cross-process lock. Two anchor
    containers that both notice a new manifest will both try to claim it; exactly one
    INSERT wins and the loser gets :class:`ManifestLoadInProgress` and falls back to the
    manifest already in place. A claim that is never completed (the winner was killed)
    is reclaimable after :data:`LOAD_LEASE`.
    """

    __tablename__ = "destiny_manifest_version"
    __mapper_args__ = {"eager_defaults": True}

    id = Column("id", Integer, primary_key=True, autoincrement=True)
    version = Column("version", VARCHAR(160), nullable=False, unique=True)
    state = Column("state", VARCHAR(16), nullable=False, default="loading")
    created_at = Column("created_at", DateTime, nullable=False)
    activated_at = Column("activated_at", DateTime, default=None)

    LOADING: t.ClassVar[str] = "loading"
    ACTIVE: t.ClassVar[str] = "active"
    SUPERSEDED: t.ClassVar[str] = "superseded"

    #: How long another process's unfinished claim on a version is respected before it
    #: is treated as wreckage and taken over. Comfortably longer than a real load (a
    #: download plus ~65k rows, minutes at worst) and shorter than the gap between
    #: manifests (a week or two), so it can only ever reclaim a genuinely dead load.
    LOAD_LEASE: t.ClassVar[dt.timedelta] = dt.timedelta(minutes=30)

    @classmethod
    @ensure_session(db_session)
    async def active(cls, session: AsyncSession = _UNSET) -> Self | None:
        """The manifest readers should be reading, or ``None`` if none is loaded."""
        return (
            await session.execute(select(cls).where(cls.state == cls.ACTIVE))
        ).scalar_one_or_none()

    @classmethod
    @ensure_session(db_session)
    async def begin_load(cls, version: str, session: AsyncSession = _UNSET) -> int:
        """Claim ``version`` for loading and return its id.

        First prunes: every version that is neither active nor a live claim goes, rows
        and all. That is where a superseded manifest is actually deleted, and where the
        wreckage of a load that was killed mid-import is cleared — re-importing on top
        of it would collide on the definition primary key.

        Raises :class:`ManifestLoadInProgress` if another process holds a live claim on
        this version, which is what the ``version`` unique constraint buys: whoever
        loses the INSERT race backs off instead of loading a second copy.
        """
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        reclaimable = (
            await session.execute(
                select(cls.id).where(
                    and_(
                        cls.state != cls.ACTIVE,
                        or_(
                            cls.version != version,
                            cls.created_at < now - cls.LOAD_LEASE,
                        ),
                    )
                )
            )
        ).scalars()
        for stale_id in list(reclaimable):
            await session.execute(
                delete(DestinyManifestDefinition).where(
                    DestinyManifestDefinition.version_id == stale_id
                )
            )
            await session.execute(delete(cls).where(cls.id == stale_id))

        try:
            row = (
                await session.execute(
                    insert(cls)
                    .values(
                        {
                            cls.version: version,
                            cls.state: cls.LOADING,
                            cls.created_at: now,
                        }
                    )
                    .returning(cls.id)
                )
            ).scalar_one()
        except IntegrityError as error:
            raise ManifestLoadInProgress(
                f"another process is already loading manifest {version!r}"
            ) from error
        return int(row)

    @classmethod
    @ensure_session(db_session)
    async def activate(cls, version_id: int, session: AsyncSession = _UNSET) -> None:
        """Promote ``version_id`` and demote whatever it replaces.

        One transaction, so readers never see two active manifests and never see zero —
        the outgoing manifest stays *readable* (see the class docstring: its rows
        survive until the next load) right up to the commit that replaces it.
        """
        await session.execute(
            update(cls)
            .values({cls.state: cls.SUPERSEDED})
            .where(and_(cls.id != version_id, cls.state == cls.ACTIVE))
        )
        await session.execute(
            update(cls)
            .values(
                {
                    cls.state: cls.ACTIVE,
                    cls.activated_at: dt.datetime.now(dt.UTC).replace(tzinfo=None),
                }
            )
            .where(cls.id == version_id)
        )

    @classmethod
    @ensure_session(db_session)
    async def discard(cls, version_id: int, session: AsyncSession = _UNSET) -> None:
        """Delete a version and its definitions — the cleanup for a failed load."""
        await session.execute(
            delete(DestinyManifestDefinition).where(
                DestinyManifestDefinition.version_id == version_id
            )
        )
        await session.execute(delete(cls).where(cls.id == version_id))


class DestinyManifestDefinition(Base):
    """One manifest definition row: ``(version, table, hash) -> the definition JSON``.

    This is the manifest itself, flattened out of Bungie's per-table SQLite into one
    table keyed by the table name it came from. Storing it here rather than on disk is
    what makes a redeploy free (the manifest outlives the container, so a cold start
    downloads nothing) and what gives the bot an answer while Bungie is unreachable —
    the failure mode ``plans/manifest_in_postgres.md`` was written to close.

    ``hash`` is Bungie's **unsigned** 32-bit hash, not the signed ``id`` SQLite stored
    it under; consumers speak unsigned hashes, so the conversion happens once at load
    rather than on every lookup.

    ``definition`` is JSONB on Postgres and JSON (text) on SQLite. JSONB costs a little
    more space than the raw text but parses once at load instead of once per read, which
    is what makes the projection scans (the item index, the weapon pool, the activity
    pools) cheap enough to run server-side — they select five fields out of a table
    whose rows are kilobytes each, and shipping whole rows over the wire is precisely
    what this move is meant to stop.
    """

    __tablename__ = "destiny_manifest_definition"

    version_id = Column("version_id", Integer, primary_key=True)
    table_name = Column("table_name", VARCHAR(64), primary_key=True)
    hash = Column("hash", BigInteger, primary_key=True)
    definition = Column(
        "definition", JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )


_LOCAL_DB_HOSTS = frozenset({None, "", "localhost", "127.0.0.1", "::1"})


def _db_is_local() -> bool:
    """Whether the *active* engine targets SQLite or a local Postgres host.

    Gates destructive schema ops (and the ``TEST_USE_POSTGRES`` test path) so they can
    never wipe a shared dev/prod database. ``configure_test_db`` swaps ``db_engine``,
    so this reflects whatever backend is currently in use."""
    url = db_engine.url
    return url.get_backend_name() == "sqlite" or url.host in _LOCAL_DB_HOSTS


def _assert_schema_destroy_allowed() -> None:
    """Refuse to drop the schema of a non-local database unless explicitly forced."""
    if _db_is_local() or os.getenv("ALLOW_REMOTE_SCHEMA_DESTROY"):
        return
    raise RuntimeError(
        f"Refusing to drop the schema of a non-local database "
        f"(host={db_engine.url.host!r}). This guard stops tests / "
        "`make destroy-schemas` from wiping the dev/prod DB. Set "
        "ALLOW_REMOTE_SCHEMA_DESTROY=1 only if you truly mean it."
    )


async def destroy_all() -> None:
    _assert_schema_destroy_allowed()
    await wait_for_db()

    async with db_engine.begin() as conn:
        logging.info(f"Dropping tables: {list(Base.metadata.tables.keys())}")
        await conn.run_sync(Base.metadata.drop_all)

    await destroy_migration_metadata()


async def destroy_migration_metadata() -> None:
    """Drop the migration tool's own bookkeeping table.

    Alembic stores the applied revision in ``alembic_version``; leaving it behind
    after a ``destroy-all`` would make the next ``alembic upgrade head`` believe the
    (now absent) schema is already at head."""
    async with db_engine.begin() as conn:
        logging.info("Dropping table: alembic_version")
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def create_all() -> None:
    await wait_for_db()

    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logging.info(f"Created tables: {list(Base.metadata.tables.keys())}")


if __name__ == "__main__":
    if "--destroy-all" in sys.argv:
        aio.run(destroy_all())

    if "--create-all" in sys.argv:
        aio.run(create_all())
