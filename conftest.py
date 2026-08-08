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

"""Root conftest: the one and only place the test session's database is set up.

This lives at the repo root rather than in each package's ``tests/`` dir because the
fixture below carries the guard that refuses to run against a non-local database — the
direct descendant of a real dev-DB-wipe incident (``plans/
dev_db_auto_wipe_investigation.md``). Three verbatim copies meant three places for that
guard to drift out of sync, so it gets exactly one home. Package-specific fixtures stay
in their package's ``tests/conftest.py``.
"""

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from dd.common import schemas


@pytest.fixture(scope="session", autouse=True)
def _test_db(tmp_path_factory: pytest.TempPathFactory):
    """Point the DB layer at a throwaway backend for the whole test session.

    By default this is a temp-file SQLite database, so the suite needs no external
    service. Set ``TEST_USE_MYSQL`` to instead exercise the real MySQL engine
    configured from the environment (full-dialect fidelity before deploys).

    A *temp file* is used rather than ``:memory:`` because each aiosqlite
    connection to ``:memory:`` gets its own private database, so schema created on
    one connection/event loop would be invisible to the next. ``NullPool`` opens a
    fresh connection per checkout inside the live loop, matching the production
    NullPool-under-pytest reasoning for asyncmy in ``cfg.py``.

    Autouse across the whole repo, not just the DB-touching packages: dd.common.settings
    — which backs what used to be plain cfg.py env vars like embed colors and
    default_url — reads through the DB layer, so even nominally "pure-logic" tests can
    reach it."""
    if os.getenv("TEST_USE_MYSQL"):
        if not schemas._db_is_local() and not os.getenv("ALLOW_REMOTE_SCHEMA_DESTROY"):
            pytest.fail(
                "TEST_USE_MYSQL is set but the configured DB is not local "
                f"(host={schemas.db_engine.url.host!r}); refusing to run against it — "
                "it would be wiped. Point it at a local/throwaway MySQL (or set "
                "ALLOW_REMOTE_SCHEMA_DESTROY=1 to override).",
                pytrace=False,
            )
        asyncio.run(schemas.wait_for_db())
        engine = None
    else:
        db_path: Path = tmp_path_factory.mktemp("dd_db") / "test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool
        )
        schemas.configure_test_db(engine)

    asyncio.run(schemas.create_all())
    yield
    # No teardown drop: the temp SQLite file is discarded, and we must NEVER auto-drop a
    # real MySQL — that ordering (reset_db then destroy_all) is what wiped the dev DB.
    if engine is not None:
        asyncio.run(engine.dispose())
        schemas.reset_db()
