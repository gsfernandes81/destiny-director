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

"""Alembic environment.

The URL comes from ``dd.common.cfg`` (never from alembic.ini) so migrations always
run against exactly the database the bots use, and the target metadata is the
SQLAlchemy models in ``dd.common.schemas`` — the single source of schema truth.

``cfg`` validates the environment at *import* time, so alembic needs the same populated
``.env`` as everything else here; the ``migration-*`` Makefile targets supply it with
``uv run --env-file .env``.

The engine is async (psycopg / aiosqlite, per the configured URL) and the migrations
themselves run through ``connection.run_sync`` — alembic's operations are sync-only."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from dd.common import cfg
from dd.common.schemas import Base

config = context.config

# `configure_logging = False` is how an in-process caller (the equivalence test) keeps
# alembic from re-configuring the root logger out from under it.
if config.config_file_name is not None and config.attributes.get(
    "configure_logging", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# The URL: an explicit override first (`alembic -x db_url=…`, or `sqlalchemy.url` set
# programmatically by a caller such as the migration tests), otherwise the one the
# bots themselves use. alembic.ini deliberately carries no URL of its own.
_url = (
    context.get_x_argument(as_dictionary=True).get("db_url")
    or config.get_main_option("sqlalchemy.url", None)
    or cfg.db_url_async
)
_is_sqlite = _url.startswith("sqlite")
# `%` is configparser's interpolation character; a password containing one would
# otherwise blow up when alembic reads the option back.
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Filter two model constructs the database can never reflect back.

    Both would otherwise show up in EVERY autogenerate run (and permanently fail
    ``alembic check``) even against a database that exactly matches the models:

    * CHECK constraints — the ones in ``schemas.py`` are unnamed, so Postgres invents
      names (``user_command_check``, ``…_check1``) that alembic, which matches check
      constraints *by name*, can never pair with the metadata ones. Consequence: a
      change to a CHECK constraint has to be hand-written into a revision.
    * A UNIQUE constraint over exactly the primary-key columns (``_mir_ids_uc``) —
      Postgres folds it into the primary key, so nothing separate comes back from
      reflection."""
    if type_ == "check_constraint":
        return False
    if type_ == "unique_constraint" and not reflected:
        pk = tuple(c.name for c in obj.table.primary_key.columns)
        if pk and tuple(c.name for c in obj.columns) == pk:
            return False
    return True


def _context_kwargs() -> dict[str, object]:
    """Options shared by the offline and online paths.

    ``render_as_batch`` is SQLite-only: SQLite cannot ALTER most things in place, so
    alembic emits its copy-and-move table rebuild instead. On Postgres it would just
    produce needless churn. ``compare_type``/``compare_server_default`` make
    autogenerate notice column type and default changes, which it ignores by default."""
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "render_as_batch": _is_sqlite,
        "include_object": _include_object,
    }


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (``alembic upgrade head --sql``)."""
    context.configure(
        url=_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_context_kwargs(),
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_context_kwargs())

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database over an async engine.

    NullPool because this process opens one connection, runs the migrations and
    exits — pooling buys nothing and only risks a connection outliving the loop."""
    # cfg's connect args are the Postgres session settings (timezone=UTC); SQLite
    # neither needs nor accepts them.
    engine = create_async_engine(
        _url,
        poolclass=pool.NullPool,
        connect_args={} if _is_sqlite else cfg.db_connect_args,
    )

    async with engine.connect() as connection:
        if not _is_sqlite:
            # beacon and anchor boot together and BOTH run `alembic upgrade head`
            # (docker-entrypoint.sh). This transaction-scoped advisory lock (an
            # arbitrary constant, ours alone) serialises them: the loser waits, then
            # finds the schema already at head and does nothing, instead of colliding
            # mid-DDL. Released automatically by the commit below.
            await connection.exec_driver_sql("SELECT pg_advisory_xact_lock(19980401)")
        await connection.run_sync(_do_run_migrations)
        # Explicit: alembic's own begin_transaction() is a no-op when the connection is
        # already in one (which the advisory lock above starts), so the commit has to
        # happen here or the migrations would roll back when the connection closes.
        await connection.commit()

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
