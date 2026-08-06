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

"""``alembic upgrade head``, run from inside each bot's startup hook.

Why in-process rather than before the process starts: the migration used to be ordered
ahead of the bot by a shell entrypoint (``alembic upgrade head && python -OO -m
dd.beacon``). Under a supervisord PID 1 there is no such ordering to lean on —
supervisord has no dependency mechanism at all, and its programs are spawned
milliseconds apart regardless of ``priority``. So instead of working around the missing
ordering, the requirement is deleted: the bot runs the migration itself on
``StartingEvent``, after ``wait_for_db`` and before anything touches a table, and the
ordering is guaranteed by straight-line Python.

Both bots do this, and they boot together. That is already handled —
``migrations/env.py`` takes a transaction-scoped advisory lock
(``pg_advisory_xact_lock``), so the loser waits, then finds the schema at head and
does nothing.

Two things this module has to get right that the shell entrypoint got for free:

* **The event loop.** alembic's API is synchronous and does blocking I/O (and
  ``env.py`` calls ``asyncio.run`` of its own), so the whole thing goes through
  :func:`asyncio.to_thread` — a worker thread has no running loop, so ``asyncio.run``
  there is legal, and the gateway's loop is never blocked.
* **The config path.** ``alembic.ini`` resolves ``script_location`` relative to itself,
  so what has to be found is the ini, not a working directory (see
  :func:`_alembic_ini_path`).
"""

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from dd.common import cfg


def _config_candidates() -> tuple[Path, ...]:
    """Directories that may hold ``alembic.ini`` + ``migrations/``, best first.

    Two layouts have to work and neither can assume a working directory:

    * a source checkout (local runs, the test suite, the dev container), where this
      file sits at ``<repo>/dd/common/`` and the ini is two levels up — true however
      the process was launched, from the repo root or a subdirectory;
    * the runtime image, where ``dd`` is installed non-editable into the venv's
      site-packages and only ``alembic.ini`` + ``migrations/`` are copied to the
      ``/app`` WORKDIR — so there the package-relative guess misses and the cwd is
      what resolves it.
    """
    return (Path(__file__).resolve().parents[2], Path.cwd())


def _alembic_ini_path() -> Path:
    """Locate ``alembic.ini``, or raise with the paths that were tried.

    The ``migrations/`` sibling is required too: an ini without its script directory
    would fail deeper inside alembic with a much less obvious message.
    """
    tried: list[Path] = []
    for root in _config_candidates():
        ini = root / "alembic.ini"
        tried.append(ini)
        if ini.is_file() and (root / "migrations").is_dir():
            return ini

    raise FileNotFoundError(
        "Could not locate alembic.ini alongside a migrations/ directory. Tried: "
        + ", ".join(str(path) for path in tried)
    )


def _upgrade_head() -> None:
    """Synchronous ``alembic upgrade head`` — always called in a worker thread."""
    config = Config(str(_alembic_ini_path()))
    # alembic.ini carries logging config, and env.py would hand it to fileConfig(),
    # dropping the root logger to WARNING and adding a second stderr handler for the
    # rest of the process's life. This attribute is env.py's own opt-out (the migration
    # equivalence tests use it too) and is what makes running alembic in-process safe.
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")


async def run_migrations() -> None:
    """Bring the database to head, or abort the boot.

    Fatal by design: a bot that comes up against an un-migrated database will fail in
    scattered, confusing ways later, so the exception is logged loudly and re-raised —
    the same contract the shell entrypoint's ``&&`` had.
    """
    if not cfg.run_migrations_on_startup:
        logging.info(
            "Startup migrations disabled (RUN_MIGRATIONS_ON_STARTUP is off); "
            "assuming the database is already at head."
        )
        return

    logging.info("Running database migrations (alembic upgrade head)...")
    try:
        await asyncio.to_thread(_upgrade_head)
    except Exception:
        logging.critical(
            "Database migration failed — aborting startup rather than running "
            "against an un-migrated database.",
            exc_info=True,
        )
        raise
    logging.info("Database migrations complete.")
