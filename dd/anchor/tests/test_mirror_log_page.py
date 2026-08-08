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

# Mirror-log page: /mirror-logs/data returns the ledger as JSON (recent runs, or one
# run's rows for ?src=), /mirror-logs serves the shell, and the homepage card is
# registered. Exercised with a fake request (no live server); auth is the web_auth
# middleware, tested in test_web_auth.py. Confirms the web layer's JSON shaping — ids as
# strings, ledger datetimes stamped UTC — on top of the query tests in
# dd/beacon/tests/test_mirror_log_queries.py.

import asyncio
import json
import types
import typing as t

import aiohttp.web
import pytest
import pytest_asyncio
from sqlalchemy import delete

from dd.anchor import web
from dd.anchor.extensions import mirror_log
from dd.common import schemas, settings
from dd.common.schemas import DeliveryState, MirrorDelivery

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_ledger() -> t.Iterator[None]:
    """Start each test from an empty mirror_delivery table (session-scoped DB)."""

    async def _clear() -> None:
        async with schemas.db_session() as session, session.begin():
            await session.execute(delete(MirrorDelivery))

    asyncio.run(_clear())
    yield


@pytest_asyncio.fixture
async def _stale_settings_cache() -> t.AsyncIterator[None]:
    """Empty ``auto_post_settings`` and an *unloaded* settings cache, both ways round.

    Opt-in rather than autouse: only the followable-naming test cares, and both the
    table and the cache are process-global (session-scoped DB, module-level cache), so
    the cleanup afterwards matters as much as the setup — a row left behind would keep
    being served to later modules for a whole TTL window.
    """

    async def _wipe() -> None:
        async with schemas.db_session() as session, session.begin():
            await session.execute(delete(schemas.AutoPostSettings))
        settings.reset_cache_for_tests()

    await _wipe()
    yield
    await _wipe()


def _as_request(query: dict | None = None) -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, types.SimpleNamespace(query=query or {}))


def _text(resp: aiohttp.web.Response) -> str:
    assert resp.text is not None
    return resp.text


async def _seed(rows: list[dict]) -> None:
    async with schemas.db_session() as session, session.begin():
        for r in rows:
            session.add(MirrorDelivery(**r))


def _base(src_msg_id: int, dest_ch_id: int, **over: object) -> dict:
    now = schemas._utcnow()
    row: dict[str, object] = dict(
        src_msg_id=src_msg_id,
        dest_ch_id=dest_ch_id,
        src_ch_id=555,
        state=DeliveryState.DELIVERED.value,
        created_at=now,
        finished_at=now,
        due_at=now,
        dest_msg_id=None,
        desired_version=1,
        applied_version=1,
        attempts=0,
        deleted=False,
    )
    row.update(over)
    return row


@pytest.mark.integration
async def test_data_endpoint_returns_runs_json_shaped() -> None:
    await _seed(
        [
            _base(1111111111111111111, 10, dest_msg_id=2222222222222222222),
            _base(1111111111111111111, 20, dest_msg_id=2222222222222222223),
        ]
    )

    resp = await mirror_log._handle_data(_as_request())

    assert resp.status == 200
    assert resp.content_type == "application/json"
    payload = json.loads(_text(resp))
    assert payload["window_days"] == mirror_log._WINDOW_DAYS
    (run,) = payload["runs"]
    # Snowflakes survive as strings (JS-safe); timestamps carry a UTC offset.
    assert run["src_msg_id"] == "1111111111111111111"
    assert run["src_ch_id"] == "555"
    assert run["src_name"] is None  # 555 is not a configured followable in tests
    assert run["total"] == 2 and run["delivered"] == 2
    assert run["started"].endswith("+00:00")
    assert run["last_at"].endswith("+00:00")


@pytest.mark.integration
async def test_data_endpoint_names_a_followable_configured_since_the_last_preload(
    _stale_settings_cache: None,
) -> None:
    # _collect_runs awaits get_followables() rather than reading the cache through
    # get_followables_sync(). The distinction only shows with a cache that is stale (or,
    # as here, never loaded) while the row is already in the DB — the state a running
    # anchor is in for up to one TTL window after a channel is set on the settings page.
    # The sync reader rendered a bare snowflake until some unrelated async getter
    # happened to refresh; the awaited getter refreshes here, on this request.
    await _seed([_base(3333333333333333333, 10, src_ch_id=4242, dest_msg_id=44)])
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "4242")

    resp = await mirror_log._handle_data(_as_request())

    (run,) = json.loads(_text(resp))["runs"]
    assert run["src_name"] == "lost_sector"


@pytest.mark.integration
async def test_data_endpoint_detail_returns_versions_only() -> None:
    # The detail view is the mirrored message (its version snapshots), not the
    # per-destination delivery list — so no rows, just versions (empty here: none
    # captured for this seeded run). Version-list content is covered in
    # test_mirror_log_render.py.
    await _seed([_base(777, 10, dest_msg_id=999)])

    resp = await mirror_log._handle_data(_as_request({"src": "777"}))

    payload = json.loads(_text(resp))
    assert payload["src_msg_id"] == "777"
    assert payload["versions"] == []
    assert "rows" not in payload  # destinations list removed


async def test_data_endpoint_rejects_non_integer_src() -> None:
    with pytest.raises(aiohttp.web.HTTPBadRequest):
        await mirror_log._handle_data(_as_request({"src": "not-a-number"}))


async def test_page_shell_served() -> None:
    resp = await mirror_log._handle_page(_as_request())

    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = _text(resp)
    assert "Mirror logs" in body
    assert "/static/mirror_log.js" in body


async def test_card_is_registered() -> None:
    card = next((c for c in web.registered_cards() if c.title == "Mirror logs"), None)
    assert card is not None
    assert card.href == "/mirror-logs"
