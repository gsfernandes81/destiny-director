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

# Stats page: the /stats/data endpoint returns a well-formed JSON payload read from the
# DB, /stats serves the shell, and the homepage card is registered. Exercised with fake
# request (no live server); auth is the web_auth middleware, tested in test_web_auth.py.

import asyncio
import datetime as dt
import json
import typing as t

import aiohttp.web
import pytest
from sqlalchemy import delete

from dd.anchor import web
from dd.anchor.extensions import stats_page
from dd.common import schemas

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_tables() -> t.Iterator[None]:
    """Start each test from empty stats tables (session-scoped DB).

    Sync fixture driving the async delete via ``asyncio.run`` — mirrors the anchor test
    suite convention (see test_autopost_settings.py).
    """

    async def _clear() -> None:
        async with schemas.db_session() as session, session.begin():
            await session.execute(delete(schemas.CommandUsage))
            await session.execute(delete(schemas.AutopostDailyStat))
            await session.execute(delete(schemas.ServerStatistics))

    asyncio.run(_clear())
    yield


def _as_request() -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, object())


def _text(resp: aiohttp.web.Response) -> str:
    # Response.text is typed str | None; every handler here sets it, so narrow to str.
    assert resp.text is not None
    return resp.text


@pytest.mark.integration
async def test_data_endpoint_returns_seeded_rows() -> None:
    today = dt.datetime.now(tz=dt.UTC).date()
    async with schemas.db_session() as session, session.begin():
        session.add(schemas.CommandUsage(command_name="xur", date=today, count=5))
        session.add(
            schemas.CommandUsage(
                command_name="xur", date=today - dt.timedelta(days=1), count=3
            )
        )
        session.add(
            schemas.AutopostDailyStat(date=today, feed="xur", kind="follow", count=9)
        )
    await schemas.ServerStatistics.add_server(123456789012345678, 5000)

    resp = await stats_page._handle_data(_as_request())

    assert resp.status == 200
    assert resp.content_type == "application/json"
    payload = json.loads(_text(resp))

    # Command daily rows (name, iso-date, count); xur totals 8 across the two days.
    assert ["xur", today.isoformat(), 5] in payload["commands"]
    assert sum(c for n, _, c in payload["commands"] if n == "xur") == 8
    # Autopost snapshot row.
    assert [today.isoformat(), "xur", "follow", 9] in payload["autoposts"]
    # Snowflake id survives as a string (JS-safe), paired with its population.
    assert ["123456789012345678", 5000] in payload["populations"]
    # current is always a list (empty here — no followable has a DB row in tests).
    assert isinstance(payload["current"], list)


@pytest.mark.integration
async def test_data_endpoint_windows_out_old_rows() -> None:
    today = dt.datetime.now(tz=dt.UTC).date()
    old = today - dt.timedelta(days=stats_page._WINDOW_DAYS + 5)
    async with schemas.db_session() as session, session.begin():
        session.add(schemas.CommandUsage(command_name="ancient", date=old, count=1))
        session.add(
            schemas.AutopostDailyStat(date=old, feed="xur", kind="mirror", count=1)
        )

    payload = json.loads(_text(await stats_page._handle_data(_as_request())))

    assert payload["commands"] == []
    assert payload["autoposts"] == []


async def test_page_shell_served() -> None:
    resp = await stats_page._handle_page(_as_request())

    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = _text(resp)
    assert "Statistics" in body
    assert "/static/stats.js" in body


async def test_card_is_registered() -> None:
    card = next((c for c in web.registered_cards() if c.title == "Statistics"), None)
    assert card is not None
    assert card.href == "/stats"
