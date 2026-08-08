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

"""The destructive-schema guard: refuse to drop a non-local DB (no engine/DB needed).

``_assert_schema_destroy_allowed`` reads ``schemas.db_engine.url``; we monkeypatch a
fake engine carrying a parsed URL so the guard's decision is exercised without any
connection."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import make_url

from dd.common import schemas

_REMOTE = "postgresql+psycopg://u:p@viaduct.proxy.rlwy.net:12345/railway"

# dd/common/tests/this_file.py -> dd/common/tests -> dd/common -> dd -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _point_at(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setattr(schemas, "db_engine", SimpleNamespace(url=make_url(url)))


def test_sqlite_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _point_at(monkeypatch, "sqlite+aiosqlite:///test.db")
    schemas._assert_schema_destroy_allowed()  # no raise


def test_local_postgres_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _point_at(monkeypatch, "postgresql+psycopg://u:p@127.0.0.1:5432/db")
    schemas._assert_schema_destroy_allowed()  # no raise


def test_remote_postgres_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_REMOTE_SCHEMA_DESTROY", raising=False)
    _point_at(monkeypatch, _REMOTE)
    with pytest.raises(RuntimeError, match="non-local database"):
        schemas._assert_schema_destroy_allowed()


def test_remote_postgres_allowed_with_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_REMOTE_SCHEMA_DESTROY", "1")
    _point_at(monkeypatch, _REMOTE)
    schemas._assert_schema_destroy_allowed()  # override → no raise


def test_test_db_fixture_has_exactly_one_home() -> None:
    """The session-DB fixture — and so its non-local-DB refusal — is not copied.

    It used to be duplicated verbatim into three package conftests, which is three
    places for the refusal to drift or be dropped. Any conftest that decides what
    ``TEST_USE_POSTGRES`` points the DB layer at is that fixture, so finding a second
    one means a copy has come back."""
    conftests = [
        path
        for path in _REPO_ROOT.rglob("conftest.py")
        # Skip installed packages and any other dot-dir the venv/tooling drags in.
        if not any(part.startswith(".") for part in path.relative_to(_REPO_ROOT).parts)
    ]
    homes = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in conftests
        if "TEST_USE_POSTGRES" in path.read_text(encoding="utf-8")
    ]
    assert homes == ["conftest.py"]


def test_session_db_is_throwaway() -> None:
    """The root fixture is autouse repo-wide, so *this* package's run is repointed too.

    Asserted through the same predicate the fixture guards on: whatever the session is
    talking to must be a local/throwaway backend — unless the run explicitly opted out
    with the override."""
    assert schemas._db_is_local() or os.getenv("ALLOW_REMOTE_SCHEMA_DESTROY")
