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

"""``lifecycle.apply_oom_score_adj`` and the ``OOM_SCORE_ADJ`` env parsing.

Every write goes to a ``tmp_path`` stand-in — the real ``/proc/self/oom_score_adj`` is
never touched, since raising it is irreversible without CAP_SYS_RESOURCE and would
follow the pytest process around for the rest of the run.
"""

import sys
from pathlib import Path

import pytest

from dd.common import cfg, lifecycle

_KEY = "OOM_SCORE_ADJ"


@pytest.fixture
def fake_procfs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A writable stand-in for ``/proc/self/oom_score_adj``, pre-seeded with the
    kernel default so ``.exists()`` passes."""
    # The Linux guard is real, so pin the platform rather than skipping the whole
    # module off Linux.
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "oom_score_adj"
    path.write_text("0\n")
    return path


def test_noop_when_unset(fake_procfs: Path) -> None:
    lifecycle.apply_oom_score_adj(None, fake_procfs)
    assert fake_procfs.read_text() == "0\n"


@pytest.mark.parametrize("score_adj", [800, 1000, -1000, 0])
def test_writes_configured_value(fake_procfs: Path, score_adj: int) -> None:
    lifecycle.apply_oom_score_adj(score_adj, fake_procfs)
    assert int(fake_procfs.read_text()) == score_adj


@pytest.mark.parametrize("score_adj", [1001, -1001, 100000])
def test_out_of_range_is_refused(fake_procfs: Path, score_adj: int) -> None:
    lifecycle.apply_oom_score_adj(score_adj, fake_procfs)
    assert fake_procfs.read_text() == "0\n"


def test_missing_procfs_path_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    absent = tmp_path / "nope" / "oom_score_adj"
    lifecycle.apply_oom_score_adj(800, absent)
    assert not absent.exists()


def test_non_linux_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, fake_procfs: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    lifecycle.apply_oom_score_adj(800, fake_procfs)
    assert fake_procfs.read_text() == "0\n"


def test_write_failure_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch, fake_procfs: Path
) -> None:
    """A refused write must warn and continue — this is an optimisation, not a
    correctness requirement."""

    def boom(*_args: object, **_kwargs: object) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "write_text", boom)
    lifecycle.apply_oom_score_adj(800, fake_procfs)


# --- cfg parsing -------------------------------------------------------------


def _parse(monkeypatch: pytest.MonkeyPatch, value: str | None) -> int | None:
    monkeypatch.delenv(_KEY, raising=False)
    if value is not None:
        monkeypatch.setenv(_KEY, value)
    return cfg._getenv_oom_score_adj(_KEY)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_cfg_unset_or_blank_is_none(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    assert _parse(monkeypatch, value) is None


@pytest.mark.parametrize(("value", "expected"), [("800", 800), (" -50 ", -50)])
def test_cfg_parses_integer(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    assert _parse(monkeypatch, value) == expected


@pytest.mark.parametrize("value", ["high", "8.5", "1001", "-1001"])
def test_cfg_rejects_bad_value(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A typo must fail loudly at import time rather than reach procfs."""
    with pytest.raises(ValueError, match=_KEY):
        _parse(monkeypatch, value)
