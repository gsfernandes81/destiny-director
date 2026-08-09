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

"""Beacon-package fixtures. The session-wide database lives in the root conftest."""

import typing as t

import pytest

from dd.beacon import utils


@pytest.fixture(autouse=True)
def _isolated_feed_channel_watchers(
    monkeypatch: pytest.MonkeyPatch,
) -> t.Iterator[list[t.Any]]:
    """Give each test its own feed-channel watcher registry.

    ``utils._feed_channel_watchers`` is a process-global appended to at import (by
    ``free_games``) and by every ``setup_nav_pages`` call — including the ones tests
    make. Without this a test that calls ``reconcile_feed_channels`` would drive
    whatever holders earlier tests happened to leave behind, and the list would grow
    for the whole session.
    """
    watchers: list[t.Any] = []
    monkeypatch.setattr(utils, "_feed_channel_watchers", watchers)
    yield watchers
