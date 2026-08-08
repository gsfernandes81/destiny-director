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

"""One-shot copy of every row from the old MySQL database into the new Postgres one.

    python -m dd.common.db_transfer --source mysql://user:pass@host:3306/db
    python -m dd.common.db_transfer --source mysql://… --execute

DRY RUN BY DEFAULT. Without ``--execute`` it connects to both databases, counts both
sides and prints what it would copy, and writes nothing. That is the shape the last
data migration in this repo used (``conduction/modules/migration.py``, 2023: a hidden
owner-only command with ``dry_run=True`` that rolled the transaction back), and it is
the right default for something run once, by hand, against production.

THIS IS AN OPERATOR SCRIPT, NOT PART OF EITHER BOT. It is never imported at runtime and
its MySQL driver lives in the ``dev`` dependency group, so it does not reach the
deployed image at all. Run it from a machine that can see both databases, with both
bots stopped.

WHY IT COPIES THROUGH SQLALCHEMY RATHER THAN DUMPING SQL. The obvious alternatives are
``mysqldump`` piped through a translator, or pgloader. Both have to solve the type
mapping themselves — MySQL spells booleans ``tinyint(1)``, its ``JSON`` is not
Postgres's ``JSONB``, and ``DATETIME`` and ``TIMESTAMP`` differ on precision and range.
None of that arises here, because the SAME SQLAlchemy models describe both databases:
the commit that moved this project to Postgres (3a6b9d4) changed no ``Column``
definition, only the dialect-specific INSERT construct. So both engines are bound to
one ``Base.metadata``, SQLAlchemy does every conversion on the way through, and the
mapping cannot drift from the models because it *is* the models.

The one thing that is genuinely checked rather than assumed: ``DateTime`` columns here
carry no ``timezone=True``, so both sides are naive and both are UTC by convention
(``_utcnow``). Values move across unchanged. If a column ever gains ``timezone=True``,
revisit this — MySQL's driver dropped tzinfo, so the old rows have none to preserve.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import typing as t

from sqlalchemy import Table, delete, func, insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from . import cfg
from .schemas import Base

logger = logging.getLogger(__name__)

#: Rows held in memory at once. The largest tables here are the mirror log and the
#: per-message version history, which are wide (JSON payloads) rather than deep, so this
#: is sized for bytes rather than for row count.
BATCH = 500


class TransferError(Exception):
    """Refusal or mismatch. Always raised before anything is written."""


class TableReport(t.NamedTuple):
    """What one table looked like before, and what happened to it."""

    table: str
    source_rows: int
    dest_rows_before: int
    copied: int
    dest_rows_after: int

    @property
    def ok(self) -> bool:
        return self.dest_rows_after == self.source_rows + self.dest_rows_before


async def _count(session, table: Table) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(table))).scalar_one()
    )


async def _assert_schema_present(engine: AsyncEngine) -> None:
    """Every table the models declare must already exist on the destination.

    This script does NOT create the schema, deliberately. ``alembic upgrade head`` owns
    that, and having two things able to create tables is how they drift. A missing table
    here means the destination was never migrated, which is a different mistake with a
    different fix, so it gets its own message rather than a driver-level error 200 lines
    later.

    ASKED OF THE INSPECTOR, NOT BY PROBING WITH A DOOMED SELECT. The obvious version of
    this runs ``SELECT … LIMIT 0`` per table and catches the failure — and it works on
    SQLite, which is why it survived until it was pointed at Postgres in review.
    Postgres ABORTS the surrounding transaction on any failed statement, so the first
    missing
    table would poison every count that follows with "current transaction is aborted",
    and the real message would never be reached. The inspector reads catalogue metadata
    and raises nothing.
    """
    async with engine.connect() as conn:
        present = set(
            await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        )
    missing = [t.name for t in Base.metadata.sorted_tables if t.name not in present]
    if missing:
        raise TransferError(
            f"the destination ({engine.dialect.name}) is missing {len(missing)} "
            f"table(s): {', '.join(missing)}.\n"
            "Run `alembic upgrade head` against it first — this script fills a schema, "
            "it does not create one."
        )


async def _reset_sequences(session, dialect: str) -> list[str]:
    """Advance Postgres sequences past the ids that were just inserted.

    THE STEP THAT IS SILENT UNTIL IT IS NOT. Copying rows with explicit primary keys
    does not move the sequence that backs them, so it stays at 1 and the FIRST insert
    the bot makes after cutover collides on a key that already exists. The failure lands
    minutes-to-days later, in the bot, looking nothing like a migration problem.

    Which columns need it is asked of Postgres (``pg_get_serial_sequence``) rather than
    inferred from the models: a column is backed by a sequence because of how the
    migration declared it, and that is the authority. Columns with no sequence return
    NULL and are skipped.
    """
    if dialect != "postgresql":
        return []
    reset = []
    for table in Base.metadata.sorted_tables:
        for column in table.primary_key.columns:
            seq = (
                await session.execute(
                    select(func.pg_get_serial_sequence(table.name, column.name))
                )
            ).scalar_one_or_none()
            if not seq:
                continue
            # coalesce+false: an empty table must leave the sequence such that the next
            # value is 1, not 2 — hence is_called=false when there is nothing to skip.
            # The column and table names are interpolated because an identifier cannot
            # be a bind parameter; both come from the models, never from input. The
            # sequence name itself IS bound, since setval takes it as a value.
            col, tbl = column.name, table.name
            await session.execute(
                text(
                    f"SELECT setval(:seq, COALESCE((SELECT MAX({col}) FROM {tbl}), 1), "
                    f"(SELECT MAX({col}) IS NOT NULL FROM {tbl}))"
                ),
                {"seq": seq},
            )
            reset.append(f"{table.name}.{column.name}")
    return reset


async def transfer(
    source_engine: AsyncEngine,
    dest_engine: AsyncEngine,
    *,
    execute: bool = False,
    truncate: bool = False,
) -> list[TableReport]:
    """Copy every table from ``source_engine`` to ``dest_engine``.

    Engines rather than URLs, so the tests can drive this with two SQLite databases and
    the CLI keeps the "is this really MySQL/Postgres" question to itself.

    ONE TRANSACTION FOR THE WHOLE COPY on the destination. Thirteen tables with no
    foreign keys between them could be committed independently, and it would even be
    faster — but a half-copied database is the worst state to be in at 2am, and the
    tables are small enough that holding one transaction costs nothing worth having.
    A dry run reaches the same rollback by a different route.
    """
    src_sessions = async_sessionmaker(source_engine, expire_on_commit=False)
    dst_sessions = async_sessionmaker(dest_engine, expire_on_commit=False)
    dest_dialect = dest_engine.dialect.name
    reports: list[TableReport] = []

    await _assert_schema_present(dest_engine)

    async with src_sessions() as src, dst_sessions() as dst:
        # NO `async with dst.begin()`. The session opens its transaction implicitly on
        # the first statement — the counts below — so an explicit begin() afterwards
        # raises "a transaction is already begun". Commit and rollback are therefore
        # spelled out at the end of the block, which also makes the dry run's rollback
        # a visible decision rather than an exception thrown to escape a context
        # manager.
        try:
            # Survey BOTH sides before writing anything, so a refusal below costs
            # nothing and the dry run prints the numbers the real run will act on.
            survey = [
                (table, await _count(src, table), await _count(dst, table))
                for table in Base.metadata.sorted_tables
            ]

            occupied = [(tbl.name, d) for tbl, _s, d in survey if d]
            if occupied and not truncate:
                raise TransferError(
                    "the destination is not empty: "
                    + ", ".join(f"{n} has {d} row(s)" for n, d in occupied)
                    + ".\nRefusing to append — a second run over a populated database "
                    "would double every row whose primary key is generated rather than "
                    "natural. Pass --truncate to empty these tables first, or point at "
                    "a fresh one."
                )

            if truncate:
                # reversed(sorted_tables) is the delete-safe order. There are no foreign
                # keys today, so it makes no difference — it is here so that adding one
                # later does not turn this into a puzzle.
                for table in reversed(Base.metadata.sorted_tables):
                    await dst.execute(delete(table))

            for table, source_rows, dest_before in survey:
                copied = 0
                if execute:
                    # yield_per streams the source in batches rather than materialising
                    # a whole table; .mappings() gives plain dicts, which is what
                    # executemany wants and skips building ORM instances that would
                    # be thrown away.
                    result = await src.stream(
                        select(table).execution_options(yield_per=BATCH)
                    )
                    async for batch in result.mappings().partitions(BATCH):
                        rows = [dict(row) for row in batch]
                        if not rows:
                            continue
                        await dst.execute(insert(table), rows)
                        copied += len(rows)

                dest_after = (
                    await _count(dst, table) if execute else dest_before + source_rows
                )
                reports.append(
                    TableReport(
                        table.name,
                        source_rows,
                        0 if truncate else dest_before,
                        copied,
                        dest_after,
                    )
                )
                logger.info(
                    "%-24s source=%-8d copied=%-8d dest=%d",
                    table.name,
                    source_rows,
                    copied,
                    dest_after,
                )

            if execute:
                for name in await _reset_sequences(dst, dest_dialect):
                    logger.info("sequence advanced: %s", name)
                await dst.commit()
            else:
                # The dry run's transaction has still read both sides, and under
                # --truncate it has issued the DELETEs too. Rolling back rather than
                # committing keeps the promise in the module docstring literally true:
                # --execute is the only way this writes.
                await dst.rollback()
        except Exception:
            await dst.rollback()
            raise

    return reports


def _format(reports: list[TableReport], *, execute: bool) -> str:
    head = (
        f"{'table':26} {'source':>8} {'dest before':>12} "
        f"{'copied':>8} {'dest after':>11}"
    )
    lines = [head, "-" * len(head)]
    for r in reports:
        flag = "" if r.ok else "   <-- MISMATCH"
        lines.append(
            f"{r.table:26} {r.source_rows:>8} {r.dest_rows_before:>12} "
            f"{r.copied:>8} {r.dest_rows_after:>11}{flag}"
        )
    lines.append("-" * len(head))
    lines.append(
        f"{'TOTAL':26} {sum(r.source_rows for r in reports):>8} "
        f"{sum(r.dest_rows_before for r in reports):>12} "
        f"{sum(r.copied for r in reports):>8} "
        f"{sum(r.dest_rows_after for r in reports):>11}"
    )
    if not execute:
        lines.append("")
        lines.append("DRY RUN — nothing was written. Re-run with --execute.")
    return "\n".join(lines)


def _source_url(raw: str) -> str:
    """Accept the URL Railway hands out and force the async driver onto it.

    ``cfg`` cannot be reused for this: it now REJECTS a mysql:// URL outright, which is
    correct for the bots and useless here. So this is the one place in the codebase that
    still knows how to spell a MySQL DSN, and it is deliberately not shared.
    """
    scheme, sep, remainder = raw.partition("://")
    if not sep:
        raise TransferError(f"--source is not a URL: {raw!r}")
    backend = scheme.split("+", 1)[0].lower()
    if backend not in {"mysql", "mariadb"}:
        raise TransferError(
            f"--source must be a mysql:// URL, got {backend}://. This script only "
            "moves MySQL -> Postgres; there is no reverse and no same-engine mode."
        )
    return f"mysql+asyncmy://{remainder}"


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dd.common.db_transfer",
        description="Copy every row from the old MySQL database into Postgres.",
    )
    parser.add_argument(
        "--source", required=True, help="MySQL URL to read from (never written to)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually write. Without this the run is a dry run and rolls back.",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="empty the destination tables first. Required to re-run over a populated "
        "destination.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True
    )

    # The destination is cfg's, so it is the same URL the bots will use and cannot be
    # typed differently here by accident. cfg has already rejected anything that is not
    # postgresql:// or sqlite:// by the time this line runs.
    if not cfg.db_url_async.startswith("postgresql"):
        raise TransferError(
            "the destination (DATABASE_URL/DATABASE_PRIVATE_URL) is not Postgres: "
            f"{cfg.db_url_async.split('://')[0]}://…"
        )

    source_engine = create_async_engine(_source_url(args.source), pool_pre_ping=True)
    dest_engine = create_async_engine(
        cfg.db_url_async, connect_args=cfg.db_connect_args, **cfg.db_engine_args
    )
    try:
        reports = await transfer(
            source_engine,
            dest_engine,
            execute=args.execute,
            truncate=args.truncate,
        )
    finally:
        await source_engine.dispose()
        await dest_engine.dispose()

    print(_format(reports, execute=args.execute))
    bad = [r for r in reports if not r.ok]
    if bad:
        print(
            f"\n{len(bad)} table(s) do not reconcile. The transaction was committed; "
            "investigate before starting the bots.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except TransferError as e:
        print(f"db_transfer: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
