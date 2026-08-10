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

"""Statistics dashboard page for the anchor web control panel.

An owner-only page (linked from the control-panel homepage via
:func:`web.register_card`) that visualises bot usage: command invocations and autopost
reach over time, current follower/mirror counts per feed, and server populations. It
replaces the Discord ``/stats`` command group.

Two routes:

- ``GET /stats`` serves the static page shell (``web_static/stats.html``); the page
  fetches its data and renders everything client-side.
- ``GET /stats/data`` returns the whole dashboard payload as JSON, read entirely from
  the shared DB — **no Discord API calls on page load** (the old ``/stats
  populations`` / ``server_list`` commands fetched guild names one-by-one, which is far
  too slow for a web request; server names are therefore out of scope here — see the
  plan's deferred list).

Time series are served at **daily** granularity; the browser re-buckets them to
weekly/monthly. Authentication is the shared Discord-OAuth middleware (``web_auth``),
which protects every non-allowlisted route, so this module needs no auth code. An
authenticated ``GET`` passes the middleware and, being a safe method, skips the CSRF
Origin check — so the page's plain ``fetch('/stats/data')`` (session cookie) just works.
"""

import datetime as dt
import logging
from pathlib import Path

import aiohttp.web
import lightbulb as lb

from ...common import schemas, settings
from .. import web

logger = logging.getLogger(__name__)

# No commands or listeners live here, but load_extensions_strict requires every
# extension module to expose a Loader, so define an (empty) one.
loader = lb.Loader()

_PAGE_HTML_PATH = Path(__file__).resolve().parent.parent / "web_static" / "stats.html"

# How far back the time series reach. Bounds the /stats/data payload (command usage is
# name×day rows) while still covering a year so the monthly-resolution view has depth.
_WINDOW_DAYS = 365


async def _collect_data() -> dict:
    """Gather the whole dashboard payload from the DB in one read session.

    Discord snowflake ids exceed JavaScript's safe-integer range (2**53), so every id
    is emitted as a string to survive JSON round-tripping. Dates are ISO strings; the
    client parses and buckets them.
    """
    since = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=_WINDOW_DAYS)
    async with schemas.db_session() as session:
        commands = await schemas.CommandUsage.fetch_daily(since=since, session=session)
        autoposts = await schemas.AutopostDailyStat.fetch_series(
            since=since, session=session
        )
        populations = await schemas.ServerStatistics.fetch_server_populations(
            session=session
        )
        current: list[dict] = []
        for feed, src_id in (await settings.get_followables()).items():
            follows = await schemas.MirroredChannel.count_dests(
                src_id, legacy_only=False, session=session
            )
            mirrors = await schemas.MirroredChannel.count_dests(
                src_id, legacy_only=True, session=session
            )
            current.append({"feed": feed, "follows": follows, "mirrors": mirrors})

    return {
        # [name, "YYYY-MM-DD", count]
        "commands": [[n, d.isoformat(), c] for n, d, c in commands],
        # ["YYYY-MM-DD", feed, kind, count]
        "autoposts": [[d.isoformat(), f, k, c] for d, f, k, c in autoposts],
        # per-feed live follower/mirror counts
        "current": current,
        # [id, population] — id as a string (see docstring)
        "populations": [[str(sid), pop] for sid, pop in populations],
    }


async def _handle_page(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # Auth is enforced by the web_auth middleware; this just serves the shell.
    return aiohttp.web.Response(
        text=_PAGE_HTML_PATH.read_text(encoding="utf-8"), content_type="text/html"
    )


async def _handle_data(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response(await _collect_data())


def register_stats_routes(app: aiohttp.web.Application) -> None:
    """Add the stats routes to the shared persistent app."""
    app.router.add_get("/stats", _handle_page)
    app.router.add_get("/stats/data", _handle_data)


web.register_routes(register_stats_routes)
web.register_card(
    web.Card(
        "Reach & usage",
        "How many servers each feed reaches, and which commands people run.",
        "/stats",
        web.CardGroup.CHECK,
        20,
    )
)
