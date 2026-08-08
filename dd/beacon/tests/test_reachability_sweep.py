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

"""Unit tests for the ``reachability_sweep`` background task body.

The scheduled task is a ``lightbulb.tasks.Task`` wrapping a ``linkd`` ``AutoInjecting``
callable; the raw ``async def reachability_sweep(bot)`` coroutine is reached via
``mirror.reachability_sweep._func._func`` and invoked directly with a stub bot.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from dd.beacon import utils
from dd.beacon.extensions import mirror
from dd.common.schemas import MirroredChannel

pytestmark = pytest.mark.asyncio

# The raw coroutine behind the Task -> AutoInjecting wrappers (getattr: ty can't model
# the lightbulb task wrapper's private attrs).
_sweep = getattr(getattr(mirror.reachability_sweep, "_func"), "_func")  # noqa: B009


async def test_early_return_when_bad_channels_disabled(monkeypatch):
    """When the ``disable_bad_channels`` setting is off, the sweep must not even
    fetch."""
    monkeypatch.setattr(
        mirror.settings, "get_disable_bad_channels", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())

    fetch = AsyncMock()
    monkeypatch.setattr(MirroredChannel, "fetch_reachability_candidates", fetch)

    await _sweep(object())

    fetch.assert_not_awaited()


async def test_bucketing_reachable_unreachable(monkeypatch):
    """Verdicts sort into reachable / unreachable; UNKNOWN and raises are neither."""
    monkeypatch.setattr(
        mirror.settings, "get_disable_bad_channels", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())

    candidates = [(1, 2), (1, 3), (1, 4), (1, 5), (1, 6)]
    monkeypatch.setattr(
        MirroredChannel,
        "fetch_reachability_candidates",
        AsyncMock(return_value=candidates),
    )

    verdicts = {
        2: utils.DestVerdict.SENDABLE,
        3: utils.DestVerdict.CONFIRMED_UNSENDABLE,
        4: utils.DestVerdict.CONFIRMED_GONE,
        5: utils.DestVerdict.UNKNOWN,
        # 6 -> raises, treated as UNKNOWN (neither).
    }

    async def fake_confirm(bot, dest):
        if dest == 6:
            raise RuntimeError("probe boom")
        return verdicts[dest]

    monkeypatch.setattr(
        utils, "confirm_dest_unsendable", AsyncMock(side_effect=fake_confirm)
    )

    apply = AsyncMock(return_value=[])
    monkeypatch.setattr(MirroredChannel, "apply_reachability_sweep", apply)
    monkeypatch.setattr(mirror, "health_logger", MagicMock())

    await _sweep(object())

    apply.assert_awaited_once()
    assert apply.await_args is not None
    reachable, unreachable = apply.await_args.args
    assert reachable == [(1, 2)]
    assert unreachable == [(1, 3), (1, 4)]


@pytest.mark.parametrize(
    ("num_disabled", "expected_method"),
    [
        (11, "critical"),  # > _DISABLE_CRITICAL_MIN (10)
        (6, "error"),  # > _DISABLE_ERROR_MIN (5)
        (2, "warning"),  # <= 5
        (0, None),  # nothing disabled -> no health log at all
    ],
)
async def test_escalation_by_disabled_count(monkeypatch, num_disabled, expected_method):
    """The health-logger level escalates with the number of disabled mirrors."""
    monkeypatch.setattr(
        mirror.settings, "get_disable_bad_channels", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(mirror.aio, "sleep", AsyncMock())

    monkeypatch.setattr(
        MirroredChannel,
        "fetch_reachability_candidates",
        AsyncMock(return_value=[(1, 2)]),
    )
    monkeypatch.setattr(
        utils,
        "confirm_dest_unsendable",
        AsyncMock(return_value=utils.DestVerdict.SENDABLE),
    )

    disabled = [(1, 100 + i) for i in range(num_disabled)]
    monkeypatch.setattr(
        MirroredChannel,
        "apply_reachability_sweep",
        AsyncMock(return_value=disabled),
    )

    health = MagicMock()
    monkeypatch.setattr(mirror, "health_logger", health)

    await _sweep(object())

    for method in ("critical", "error", "warning"):
        if method == expected_method:
            getattr(health, method).assert_called_once()
        else:
            getattr(health, method).assert_not_called()
