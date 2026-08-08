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

# The session-scoped test database lives in the repo-root conftest.py, so its
# non-local-DB wipe guard has exactly one home. Only anchor-specific fixtures belong
# here.

import time

import pytest

from dd.common import settings


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
