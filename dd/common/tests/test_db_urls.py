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

"""``cfg._db_urls`` / ``cfg._db_config``: the env-var-to-SQLAlchemy-URL mapping.

Pure functions over the environment — no engine is built and no database is contacted,
so this is a unit test despite living next to the DB layer.
"""

import pytest
from sqlalchemy import make_url
from sqlalchemy.pool import NullPool

from dd.common import cfg

_PRIMARY = "DATABASE_PRIVATE_URL"
_FALLBACK = "DATABASE_URL"


def _urls(monkeypatch: pytest.MonkeyPatch, url: str | None, **env: str):
    monkeypatch.delenv(_PRIMARY, raising=False)
    monkeypatch.delenv(_FALLBACK, raising=False)
    monkeypatch.delenv("DATABASE_SSL", raising=False)
    if url is not None:
        monkeypatch.setenv(_PRIMARY, url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return cfg._db_urls(_PRIMARY, _FALLBACK)


def test_private_url_wins_over_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    sync, _ = _urls(
        monkeypatch,
        "postgresql://u:p@private/db",
        **{_FALLBACK: "postgresql://u:p@public/db"},
    )
    assert make_url(sync).host == "private"


def test_falls_back_to_plain_url(monkeypatch: pytest.MonkeyPatch) -> None:
    sync, async_ = _urls(monkeypatch, None, **{_FALLBACK: "postgres://u:p@h/db"})
    assert sync == async_ == "postgresql+psycopg://u:p@h/db?sslmode=require"


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgres_schemes_normalise_to_psycopg(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    sync, async_ = _urls(monkeypatch, f"{scheme}://u:p@h:5432/db")
    # psycopg's SQLAlchemy dialect is dual sync/async, so both URLs are the same one.
    assert sync == async_ == "postgresql+psycopg://u:p@h:5432/db?sslmode=require"


def test_sslmode_appended_to_existing_query(monkeypatch: pytest.MonkeyPatch) -> None:
    sync, _ = _urls(monkeypatch, "postgresql://u:p@h/db?connect_timeout=5")
    assert sync == "postgresql+psycopg://u:p@h/db?connect_timeout=5&sslmode=require"


def test_explicit_sslmode_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    sync, _ = _urls(monkeypatch, "postgresql://u:p@h/db?sslmode=verify-full")
    assert sync == "postgresql+psycopg://u:p@h/db?sslmode=verify-full"


def test_ssl_disabled_leaves_libpq_default(monkeypatch: pytest.MonkeyPatch) -> None:
    sync, _ = _urls(monkeypatch, "postgresql://u:p@h/db", DATABASE_SSL="false")
    assert sync == "postgresql+psycopg://u:p@h/db"


def test_sqlite_gets_aiosqlite_on_the_async_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync, async_ = _urls(monkeypatch, "sqlite:////var/lib/dd/dd.db")
    assert sync == "sqlite:////var/lib/dd/dd.db"
    # No sslmode is grafted on: SQLite has no such concept.
    assert async_ == "sqlite+aiosqlite:////var/lib/dd/dd.db"


def test_mysql_is_rejected_with_a_pointed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="no longer supported"):
        _urls(monkeypatch, "mysql://u:p@h/db")


def test_unknown_scheme_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="Unsupported database scheme"):
        _urls(monkeypatch, "clickhouse://u:p@h/db")


def test_library_mode_blank_url_stays_connectable_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank URL (Library Mode) still yields something create_engine can parse."""
    sync, async_ = _urls(monkeypatch, "")
    assert sync == async_ == "postgresql+psycopg://"
    assert make_url(async_).get_backend_name() == "postgresql"


def test_engine_args_are_dialect_appropriate(monkeypatch: pytest.MonkeyPatch) -> None:
    # We are *in* pytest, and cfg swaps in NullPool (dropping the QueuePool-only args)
    # when it sees PYTEST_VERSION. Hide it so this exercises the deployed shape.
    monkeypatch.delenv("PYTEST_VERSION", raising=False)

    pg = cfg._db_config("postgresql+psycopg://u:p@h/db")[3]
    # Finite overflow: the Pi's Postgres runs with max_connections=25.
    assert pg["max_overflow"] == 10
    assert pg["isolation_level"] == "READ COMMITTED"
    assert pg["pool_use_lifo"] is True

    sqlite = cfg._db_config("sqlite+aiosqlite:///x.db")[3]
    # QueuePool / server-side knobs SQLite would reject.
    assert "max_overflow" not in sqlite
    assert "isolation_level" not in sqlite
    assert "pool_use_lifo" not in sqlite
    assert sqlite["pool_pre_ping"] is True


def test_postgres_sessions_are_pinned_to_utc() -> None:
    """Every psycopg connection opens with ``timezone=UTC``.

    The schema's datetime columns are naive TIMESTAMP but some writes bind tz-aware
    values, which Postgres converts using the *session* TimeZone — so a server on a
    local zone would store those rows offset. SQLite has no such setting.
    """
    assert cfg._db_config("postgresql+psycopg://u:p@h/db")[2] == {
        "options": "-c timezone=UTC"
    }
    assert cfg._db_config("sqlite+aiosqlite:///x.db")[2] == {}


def test_pytest_swaps_in_nullpool() -> None:
    """Under pytest the pool is NullPool and the QueuePool-only args are dropped.

    Pooled connections outlive the short-lived event loops the suite spins up, and a
    psycopg connection is bound to its creating loop — NullPool closes each one inside
    the live loop instead.
    """
    args = cfg._db_config("postgresql+psycopg://u:p@h/db")[3]
    assert args["poolclass"] is NullPool
    assert "max_overflow" not in args
    assert "pool_use_lifo" not in args
