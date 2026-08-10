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

"""Bungie account page — the web replacement for ``/bungie login`` and
``/bungie account_numbers``.

The bot posts as one Bungie account, and the vendor-backed feeds (Xûr, Eververse, Ada,
Portal Ops) stop working when its refresh token lapses. This page is where that link
lives: whether it is healthy, when it expires, how to re-link it, and the account
numbers the producers are configured against.

The OAuth callback was already web-side (``/oauth/callback``, registered by
``bungie_api.oauth``), so login was the odd half — a slash command that printed a URL
and then *blocked for up to 15 minutes* polling for the token
(``oauth.LOGIN_WAIT_TIMEOUT_SECONDS``). On the web the redirect back **is** the
completion signal, so nothing has to wait: ``GET /bungie/login`` mints a one-shot state
code and redirects to Bungie, and the callback returns the browser here.

Routes (all behind the shared Discord-OAuth middleware in ``web_auth``, except
``/oauth/callback``, which is allowlisted because Bungie redirects to it):

- ``GET /bungie``          the static page shell
- ``GET /bungie/data``     link status: linked, expiry, expired
- ``GET /bungie/login``    mint a state code and redirect to Bungie's consent screen
- ``GET  /bungie/account``  the character / membership ids, fetched on demand
- ``POST /bungie/logout``   forget the stored refresh token

**The access token is never sent to the browser.** The Discord command carried an
explicit note about that (an ephemeral message is still a message); it holds here too,
and more so — a page is trivially screenshotted. ``/bungie/account`` returns only the
ids, exactly what the command showed.
"""

import datetime as dt
import logging
from pathlib import Path

import aiohttp
import aiohttp.web
import lightbulb as lb

from ...common import schemas
from .. import web
from .bungie_api import (
    DestinyMembership,
    get_webserver_runner,
    oauth_url,
    refresh_api_tokens as _refresh_api_tokens,
)
from .bungie_api.oauth import OAuthStateManager

logger = logging.getLogger(__name__)

# No commands or listeners live here, but load_extensions_strict requires every
# extension module to expose a Loader, so define an (empty) one.
loader = lb.Loader()

_PAGE_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "bungie_account.html"
)


async def _handle_page(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=_PAGE_HTML_PATH.read_text(encoding="utf-8"), content_type="text/html"
    )


async def _handle_data(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Whether the bot's Bungie link is healthy, and until when.

    ``expires`` is **not rendered on the page** — a date nobody can act on is noise, and
    the status sentence already says what to do. It is served here (and hidden in the
    status line's hover title) for troubleshooting only. See
    ``plans/bungie_token_auto_refresh.md`` for removing the expiry as a concern
    altogether.

    ``refresh_token_expires`` is stored with a 20% safety factor already applied (see
    ``BungieCredentials.set_refresh_token``), so it is the moment the bot treats the
    link as dead — not Bungie's own expiry. Reporting the stored value is the honest
    one: it is what ``refresh_api_tokens`` actually checks.
    """
    credentials = await schemas.BungieCredentials.get_credentials()
    expires = (
        getattr(credentials, "refresh_token_expires", None) if credentials else None
    )
    linked = bool(credentials and getattr(credentials, "refresh_token", None))
    return aiohttp.web.json_response(
        {
            "linked": linked,
            # Naive local-clock datetimes, as the model stores them; the page renders
            # the string as-is rather than pretending to a timezone it does not have.
            "expires": (
                expires.isoformat(sep=" ", timespec="minutes") if expires else None
            ),
            "expired": bool(expires and dt.datetime.now() > expires),
        }
    )


async def _handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Mint a one-shot OAuth state code and send the browser to Bungie.

    Minted on click rather than on page load: ``oauth_url`` stores a state code each
    call, and a page that mints one per render would leave a trail of unused codes for
    the sweeper.
    """
    raise aiohttp.web.HTTPFound(location=str(oauth_url()))


async def _handle_account(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """The character / membership ids for the linked account.

    Fetched on demand — it costs a token refresh plus two Bungie round-trips, which is
    why the Discord command had to ack first and edit the result in.
    """
    try:
        access_token = await _refresh_api_tokens(runner=get_webserver_runner())
        async with aiohttp.ClientSession() as session:
            membership = await DestinyMembership.from_api(session, access_token)
            character_id = await membership.get_character_id(session, access_token)
    except Exception as e:
        logger.exception("Bungie account lookup failed")
        return aiohttp.web.json_response({"error": str(e) or e.__class__.__name__})

    # Deliberately no access token in this payload — see the module docstring.
    return aiohttp.web.json_response(
        {
            "characterId": str(character_id),
            "membershipId": str(membership.membership_id),
            "membershipType": str(membership.membership_type),
        }
    )


async def _handle_logout(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Forget the stored Bungie link.

    Clears the in-memory access token and blanks the persisted refresh token, so the
    next producer fetch raises "please log in" rather than silently using a link the
    operator believed they had revoked. Bungie is not told — this is the bot forgetting
    its credential, not an OAuth revocation, and the docstring says so because the
    difference matters if the token leaked.
    """
    OAuthStateManager.clear_access_token()
    await schemas.BungieCredentials.set_refresh_token(
        refresh_token=None, refresh_token_expires=0
    )
    logger.warning("Bungie link cleared from the web control panel")
    return aiohttp.web.json_response({"ok": True})


def register_bungie_account_routes(app: aiohttp.web.Application) -> None:
    """Add the Bungie account routes to the shared persistent app."""
    app.router.add_get("/bungie", _handle_page)
    app.router.add_get("/bungie/data", _handle_data)
    app.router.add_get("/bungie/login", _handle_login)
    app.router.add_get("/bungie/account", _handle_account)
    app.router.add_post("/bungie/logout", _handle_logout)


web.register_routes(register_bungie_account_routes)
web.register_card(
    web.Card(
        "Bungie connection",
        "The account the bot reads Destiny data with — status and re-login.",
        "/bungie",
        web.CardGroup.ADMIN,
        30,
    )
)
