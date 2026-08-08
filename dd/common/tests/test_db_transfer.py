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

"""Cover ``db_transfer`` with two SQLite databases standing in for MySQL and Postgres.

WHAT THIS CAN AND CANNOT PROVE, stated up front because the gap is the whole risk. It
exercises the copy engine — the streaming, the refusals, the truncate path, the
reconciliation — against real engines and the real models. It does NOT exercise the
MySQL dialect, because that would need a MySQL server, and it does NOT exercise the
Postgres sequence reset, which is skipped on any non-Postgres destination.

That is deliberate rather than lazy: ``transfer()`` takes engines precisely so this can
run in the default suite, and the two dialect-specific pieces are each one narrow thing
to check by hand on the day (the `--source` guard below covers the only MySQL-shaped
logic that lives in Python). A rehearsal against the real dev databases is what covers
the rest, and no unit test substitutes for it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from dd.common.db_transfer import TransferError, _source_url, transfer
from dd.common.schemas import AutoPostSettings, Base, MirroredChannel


async def _engine(tmp_path, name: str, *, create: bool = True):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    if create:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    return engine


async def _seed(engine) -> None:
    """A row in two tables, one with a natural composite key and one with a boolean.

    Deliberately spans a `BOOLEAN` and a `DATETIME`: those are the two column kinds
    whose MySQL storage (tinyint, and a DATETIME with no timezone) differs most from
    Postgres, and the claim this module rests on is that SQLAlchemy converts both
    because the same model describes each side.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s, s.begin():
        # Every dict carries the same keys on purpose: executemany compiles one
        # statement for the batch, so a row that omits a column raises rather than
        # falling back to its default. The transfer itself cannot hit this — it builds
        # batches from `select(table)`, which always yields the full column set — but a
        # hand-written seed can, and did.
        await s.execute(
            insert(MirroredChannel.__table__),
            [
                {
                    "src_id": 1,
                    "dest_id": 10,
                    "legacy": True,
                    "enabled": True,
                    "unreachable_since": dt.datetime(2026, 1, 2, 3, 4, 5),
                },
                {
                    "src_id": 1,
                    "dest_id": 11,
                    "legacy": False,
                    "enabled": True,
                    "unreachable_since": None,
                },
            ],
        )
        await s.execute(
            insert(AutoPostSettings.__table__), [{"name": "xur", "enabled": False}]
        )


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_reports_what_it_would(tmp_path) -> None:
    src = await _engine(tmp_path, "src.db")
    dst = await _engine(tmp_path, "dst.db")
    await _seed(src)

    reports = await transfer(src, dst)

    by_table = {r.table: r for r in reports}
    assert by_table["mirrored_channel"].source_rows == 2
    assert by_table["mirrored_channel"].copied == 0
    assert by_table["auto_post_settings"].source_rows == 1
    assert all(r.ok for r in reports)

    sessions = async_sessionmaker(dst)
    async with sessions() as s:
        assert (await s.execute(select(MirroredChannel.__table__))).all() == []

    await src.dispose()
    await dst.dispose()


@pytest.mark.asyncio
async def test_execute_copies_rows_and_preserves_values(tmp_path) -> None:
    src = await _engine(tmp_path, "src.db")
    dst = await _engine(tmp_path, "dst.db")
    await _seed(src)

    reports = await transfer(src, dst, execute=True)

    by_table = {r.table: r for r in reports}
    assert by_table["mirrored_channel"].copied == 2
    assert by_table["mirrored_channel"].dest_rows_after == 2
    assert all(r.ok for r in reports)

    sessions = async_sessionmaker(dst)
    async with sessions() as s:
        rows = (
            (
                await s.execute(
                    select(MirroredChannel.__table__).order_by(
                        MirroredChannel.__table__.c.dest_id
                    )
                )
            )
            .mappings()
            .all()
        )
        assert [r["dest_id"] for r in rows] == [10, 11]
        # The boolean survived as a boolean, and the naive datetime came across intact.
        assert rows[0]["legacy"] is True
        assert rows[1]["legacy"] is False
        assert rows[0]["unreachable_since"] == dt.datetime(2026, 1, 2, 3, 4, 5)

        settings = (
            (await s.execute(select(AutoPostSettings.__table__))).mappings().all()
        )
        assert settings[0]["enabled"] is False

    await src.dispose()
    await dst.dispose()


@pytest.mark.asyncio
async def test_refuses_a_populated_destination(tmp_path) -> None:
    src = await _engine(tmp_path, "src.db")
    dst = await _engine(tmp_path, "dst.db")
    await _seed(src)
    await _seed(dst)

    with pytest.raises(TransferError, match="not empty"):
        await transfer(src, dst, execute=True)

    await src.dispose()
    await dst.dispose()


@pytest.mark.asyncio
async def test_truncate_empties_then_copies_exactly_once(tmp_path) -> None:
    """The re-run path. Without --truncate this is the doubling the refusal prevents."""
    src = await _engine(tmp_path, "src.db")
    dst = await _engine(tmp_path, "dst.db")
    await _seed(src)
    await _seed(dst)

    reports = await transfer(src, dst, execute=True, truncate=True)

    by_table = {r.table: r for r in reports}
    assert by_table["mirrored_channel"].dest_rows_after == 2
    assert all(r.ok for r in reports)

    await src.dispose()
    await dst.dispose()


@pytest.mark.asyncio
async def test_refuses_a_destination_with_no_schema(tmp_path) -> None:
    src = await _engine(tmp_path, "src.db")
    dst = await _engine(tmp_path, "empty.db", create=False)
    await _seed(src)

    with pytest.raises(TransferError, match="alembic upgrade head"):
        await transfer(src, dst, execute=True)

    await src.dispose()
    await dst.dispose()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("mysql://u:p@h:3306/db", "mysql+asyncmy://u:p@h:3306/db"),
        ("mysql+asyncmy://u:p@h/db", "mysql+asyncmy://u:p@h/db"),
        ("mariadb://u:p@h/db", "mysql+asyncmy://u:p@h/db"),
    ],
)
def test_source_url_forces_the_async_driver(raw: str, expected: str) -> None:
    assert _source_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["postgresql://u:p@h/db", "sqlite:///x.db", "not-a-url"]
)
def test_source_url_rejects_anything_but_mysql(raw: str) -> None:
    """There is no reverse direction and no same-engine mode; both are typos."""
    with pytest.raises(TransferError):
        _source_url(raw)
