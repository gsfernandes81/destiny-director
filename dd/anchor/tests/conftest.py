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
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from dd.common import schemas, settings


@pytest.fixture(scope="session", autouse=True)
def _test_db(tmp_path_factory: pytest.TempPathFactory):
    """Point the DB layer at a throwaway backend for the whole test session.

    Mirrors ``dd/beacon/tests/conftest.py``: a temp-file SQLite DB by default (no
    external service), or the real MySQL engine when ``TEST_USE_MYSQL`` is set.
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


@pytest.fixture
def configured_followables(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Give every followable a channel id, as a configured deploy has.

    Followable channels come from ``auto_post_settings`` rows and nowhere else — there
    is no env-var fallback behind them (see ``dd.common.settings``' docstring) — so a
    test whose subject is "what a producer does once its channel IS set" has to say so.
    Writes straight into the settings cache and marks it fresh, so both the sync and
    async getters resolve without a DB round trip.
    """
    ids = {
        feed: 900_000 + index
        for index, feed in enumerate(settings.FOLLOWABLE_SLUGS, start=1)
    }
    for feed, channel_id in ids.items():
        monkeypatch.setitem(
            settings._cache, settings.FOLLOWABLE_SLUGS[feed], (None, str(channel_id))
        )
    monkeypatch.setattr(settings, "_loaded_at", time.monotonic())
    return ids
