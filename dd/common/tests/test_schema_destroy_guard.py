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

from types import SimpleNamespace

import pytest
from sqlalchemy import make_url

from dd.common import schemas

_REMOTE = "postgresql+psycopg://u:p@viaduct.proxy.rlwy.net:12345/railway"


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
