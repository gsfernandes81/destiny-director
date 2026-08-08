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

    Mirrors ``dd/beacon/tests/conftest.py`` / ``dd/anchor/tests/conftest.py``: a
    temp-file SQLite DB by default (no external service), or the real MySQL engine
    when ``TEST_USE_MYSQL`` is set. Needed here (unlike before) because
    dd.common.settings — now backing what used to be plain cfg.py env vars like embed
    colors and default_url — reads through the DB layer, so even the "pure-logic"
    tests in this package touch it.
    """
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
