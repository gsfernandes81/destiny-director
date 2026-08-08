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

"""Discord OAuth — the sole auth gate for the anchor web UI.

Every HTTP surface the anchor serves (the rotation editor, the weekly-reset form) is
protected by one aiohttp middleware registered here. Instead of the old per-surface
magic-link tokens — which only proved *someone received a link* — a visitor now proves a
specific Discord identity via OAuth (``identify`` scope) and is admitted only if that id
is a bot owner / team member (exactly ``CachedFetchBot.fetch_owner_ids()``). So
authorization is re-derived from the owner list on every request — refreshed on a short
interval (:func:`_is_owner`) so a removed owner loses access promptly — not from
possession of a secret.

Flow:

1. The middleware sees an unauthenticated GET and 302s to ``/auth/login?next=<path>``.
2. ``/auth/login`` mints a single-use ``state`` (holding the sanitized ``next``) and
   redirects to Discord's consent screen.
3. Discord redirects back to ``/auth/callback``; we consume ``state`` (CSRF defence),
   exchange the code for a short-lived token, read the user's id, verify ownership, mint
   a stateless signed session cookie (30d) and 302 to the stored ``next``. The Discord
   token is **not** persisted — ``identify`` is a one-shot to learn the id.

The session cookie is a stateless HMAC (``"<user_id>.<expiry>.<sig>"``) keyed by a
secret derived from the anchor bot token, mirroring the proven scheme the feature
modules used — so no server-side session store and no new signing secret. A dev-only
bypass (``DEV_AUTH_USER_ID``) is honored only when ``TEST_ENV`` is also set, so it is
inert in prod even if the env var leaks into a prod config.
"""

import datetime as dt
import functools
import hashlib
import hmac
import logging
from uuid import uuid4

import aiohttp
import aiohttp.typedefs
import aiohttp.web
import lightbulb as lb
from yarl import URL

from ...common import cfg
from ...common.bot import CachedFetchBot
from .. import web

logger = logging.getLogger(__name__)

# Nothing is registered on it — the routes and middleware are contributed to the web app
# at import time, and the bot now comes from web.get_bot() — but load_extensions_strict
# requires every extension module to expose a Loader, so it stays.
loader = lb.Loader()

# --- constants --------------------------------------------------------------------

_SESSION_COOKIE = "dd_auth"
# 30-day session. A long TTL is safe because the cookie only proves *identity*, never
# authorization: every request re-checks the id against the owner list (_is_owner),
# which force-refreshes fetch_owner_ids() on a short interval — so removing someone from
# the owner/team list revokes their access within _OWNER_REFRESH_INTERVAL regardless of
# how long their cookie stays valid. The TTL only governs how often an owner re-does the
# (near-instant) Discord redirect.
_SESSION_TTL = dt.timedelta(days=30)
_STATE_TTL = dt.timedelta(minutes=5)
# How stale the cached owner list may get before the next auth check force-refreshes it.
# CachedFetchBot.fetch_owner_ids() otherwise memoises for the whole process lifetime
# (warmed once on StartedEvent), so without this a demoted owner would keep access until
# the process restarts. Bounded so a removed owner loses access within this window while
# the common case still serves from cache (one REST fetch_application per interval).
_OWNER_REFRESH_INTERVAL = dt.timedelta(minutes=10)

# The single callback path. The redirect_uri handed to Discord (at /auth/login and in
# the token exchange) and registered in the Developer Portal must match EXACTLY —
# deriving all three from one place (``_redirect_uri``) guards the top OAuth failure.
_CALLBACK_PATH = "/auth/callback"

_DISCORD_API = "https://discord.com/api"
AUTHORIZE_URL = f"{_DISCORD_API}/oauth2/authorize"
TOKEN_URL = f"{_DISCORD_API}/oauth2/token"
USER_URL = f"{_DISCORD_API}/users/@me"

# Paths the middleware lets through without auth: the login flow and static assets
# (both legitimately whole subtrees, hence prefixes), plus the *Bungie* OAuth callback
# (self-guarded by its own state code, must stay reachable). The Bungie callback is an
# EXACT match, not a prefix, so it can't also whitelist siblings like
# ``/oauth/callback-evil``. Everything else is protected by default.
_ALLOWLIST_PREFIXES = ("/auth/", "/static/")
_ALLOWLIST_EXACT = frozenset({"/oauth/callback"})

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: When the owner list was last force-refreshed (see :func:`_is_owner`). ``None`` until
#: the first auth check.
_owner_ids_refreshed_at: dt.datetime | None = None


# --- session cookie (stateless HMAC) ----------------------------------------------


@functools.cache
def _session_key() -> bytes:
    """Stable secret for signing session cookies, derived from the anchor bot token.

    A DISTINCT salt from the (now-removed) editor/weekly-reset session keys, and from
    other bot-token-derived secrets, so this cookie can only authenticate the web-auth
    surface. Deriving (not using the token raw) keeps the bot token out of the signing
    material; no new env var is needed. ``functools.cache`` computes the digest once
    (the bot token is an import-time constant) instead of re-hashing on every ``_sign``
    — and ``_sign`` runs on ``resolve_session``, i.e. once per request on the middleware
    hot path.
    """
    return hashlib.sha256(
        b"anchor-web-auth-session|" + cfg.discord_token_anchor.encode()
    ).digest()


def _sign(user_id: int, expiry_epoch: int) -> str:
    """The signed cookie value ``"<user_id>.<expiry>.<hex_hmac>"`` for a payload."""
    payload = f"{user_id}.{expiry_epoch}"
    sig = hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def mint_session(user_id: int) -> str:
    """A fresh 30-day session cookie value binding ``user_id``."""
    expiry = dt.datetime.now(dt.UTC) + _SESSION_TTL
    return _sign(user_id, int(expiry.timestamp()))


# Request key holding the authenticated owner's Discord id, set by the middleware once
# a request is admitted. Routes that need to know *which* owner is acting (rather than
# just that one is) read it via :func:`authed_user_id` instead of re-parsing the cookie
# — one place decides what a valid session is.
AUTH_USER_KEY = "dd_auth_user_id"


def authed_user_id(request: aiohttp.web.Request) -> int:
    """The Discord id of the owner making this request.

    Only ever called from a handler behind the middleware, which sets the key before
    dispatching — so a missing key means the route was allowlisted by mistake, and
    failing loudly is right.
    """
    user_id = request.get(AUTH_USER_KEY)
    if user_id is None:
        raise aiohttp.web.HTTPUnauthorized(text="Not signed in.")
    return int(user_id)


def resolve_session(cookie: str) -> int | None:
    """The user id from a well-signed, unexpired cookie, else ``None`` (fails closed).

    Recomputes the signature over the parsed ``user_id``/``expiry`` and constant-time
    compares the *whole* value, so a tampered id, tampered expiry or forged signature
    all reject. ``compare_digest`` raises ``TypeError`` on a non-ASCII cookie, so that
    (and a non-integer field) is caught and treated as invalid — a hostile cookie must
    never 500 the middleware.
    """
    parts = cookie.split(".")
    if len(parts) != 3:
        return None
    user_id_str, expiry_str, _sig = parts
    try:
        user_id = int(user_id_str)
        expiry_epoch = int(expiry_str)
        valid = hmac.compare_digest(cookie, _sign(user_id, expiry_epoch))
    except (ValueError, TypeError):
        return None
    if not valid:
        return None
    if expiry_epoch <= int(dt.datetime.now(dt.UTC).timestamp()):
        return None
    return user_id


def set_session_cookie(response: aiohttp.web.StreamResponse, token: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=int(_SESSION_TTL.total_seconds()),
        httponly=True,
        # Secure only when actually served over https (local http tunnels can't set a
        # Secure cookie); Railway's public_base_url is https.
        secure=cfg.public_base_url.startswith("https"),
        # Lax (not Strict) so the cookie survives the cross-site top-level redirect back
        # from discord.com, while still withheld on cross-site POSTs.
        samesite="Lax",
        # One cookie for the whole app (replaces the old per-surface cookie paths).
        path="/",
    )


def clear_session_cookie(response: aiohttp.web.StreamResponse) -> None:
    response.del_cookie(_SESSION_COOKIE, path="/")


# --- OAuth state (single-use, server-held next path) ------------------------------


class _AuthStateManager:
    """In-memory single-use OAuth ``state`` codes, each holding a post-login ``next``.

    Mirrors the Bungie ``OAuthStateManager`` (uuid4 codes, 5-min TTL, proactive sweep)
    but maps ``state -> (expiry, next_path)`` so the redirect target is server-held and
    never trusted from the callback query. Its own instance, isolated from Bungie's, so
    the two OAuth surfaces can't cross-authenticate; single-use ``consume`` is the CSRF
    defence.
    """

    _states: dict[str, tuple[dt.datetime, str]] = {}

    @classmethod
    def _sweep(cls) -> None:
        """Drop expired codes proactively so an abandoned login can't leak an entry."""
        now = dt.datetime.now()
        for code in [c for c, (exp, _n) in cls._states.items() if exp <= now]:
            cls._states.pop(code, None)

    @classmethod
    def issue(cls, next_path: str) -> str:
        cls._sweep()
        while True:
            state = str(uuid4())
            if state not in cls._states:
                break
        cls._states[state] = (dt.datetime.now() + _STATE_TTL, next_path)
        return state

    @classmethod
    def consume(cls, state: str) -> str | None:
        """Pop ``state`` and return its ``next_path``; ``None`` if unknown/expired.

        Single-use: a replayed or forged state finds nothing and returns ``None``.
        """
        if not state:
            return None
        entry = cls._states.pop(state, None)
        if entry is None:
            return None
        expiry, next_path = entry
        if expiry <= dt.datetime.now():
            return None
        return next_path


# --- helpers ----------------------------------------------------------------------


def _redirect_uri() -> str:
    """The OAuth redirect URI — the ONE place it is derived (exact-match critical)."""
    return cfg.public_base_url + _CALLBACK_PATH


def _sanitize_next(raw: str) -> str:
    """Reduce a caller-supplied ``next`` to a safe internal path (open-redirect guard).

    Only a single-leading-slash path is honored. Protocol-relative (``//host``),
    backslash-smuggled (``/\\host`` or any embedded backslash) and absolute
    (``https://host``) forms — all of which a browser can resolve to an off-site URL in
    a ``Location`` header — collapse to ``/``, so the post-login redirect can't go
    off-origin. This is the one security-critical validation here.
    """
    # Reject any ASCII control char first (incl. TAB/CR/LF). aiohttp strips whitespace
    # control chars from the Location header, so a value like "/\t/evil.com" — which
    # otherwise passes the checks below — would collapse to the protocol-relative
    # "//evil.com" and redirect off-site. Drop them before the structural checks.
    if not raw or any(ch < " " or ch == "\x7f" for ch in raw):
        return "/"
    if not raw.startswith("/"):
        return "/"
    if raw.startswith(("//", "/\\")):
        return "/"
    if "\\" in raw:
        return "/"
    return raw


def _config_error() -> aiohttp.web.Response | None:
    """A plain-text 500 if OAuth can't run (mirrors the public_base_url guard style)."""
    if not cfg.public_base_url:
        return aiohttp.web.Response(
            status=500,
            text=(
                "No public base URL is configured (set PUBLIC_BASE_URL or run on "
                "Railway), so Discord login can't be started."
            ),
        )
    if not cfg.discord_oauth_client_id or not cfg.discord_oauth_client_secret:
        return aiohttp.web.Response(
            status=500,
            text=(
                "Discord OAuth is not configured (set DISCORD_OAUTH_CLIENT_ID and "
                "DISCORD_OAUTH_CLIENT_SECRET)."
            ),
        )
    return None


def _dev_bypass_active() -> bool:
    """Whether the dev auth bypass is in effect.

    Triple-gated, so it can only ever fire on a local dev box: honored ONLY when
    ``TEST_ENV`` is set (``cfg.test_env`` truthy) AND a ``DEV_AUTH_USER_ID`` is set AND
    there is no public base URL. The last gate matters because ``TEST_ENV`` is a
    guild-scoping flag that is *also* set on the internet-facing dev deployment — where
    ``public_base_url`` is non-empty (Railway injects ``RAILWAY_PUBLIC_DOMAIN``), so the
    bypass stays inert there. Local dev without a tunnel has an empty
    ``public_base_url`` — the only place the bypass is meant to run.
    """
    return bool(cfg.test_env) and bool(cfg.dev_auth_user_id) and not cfg.public_base_url


def _is_allowlisted(path: str) -> bool:
    """Whether ``path`` is exempt from auth (login flow, static, Bungie callback).

    The single named home for the allowlist so the exact-vs-prefix intent (see
    :data:`_ALLOWLIST_EXACT`) lives in one place, not an inline compound boolean.
    """
    return path in _ALLOWLIST_EXACT or path.startswith(_ALLOWLIST_PREFIXES)


async def _is_owner(bot: CachedFetchBot, user_id: int) -> bool:
    """Whether ``user_id`` is a current bot owner, on a bounded-freshness owner list.

    Force-refreshes ``fetch_owner_ids()`` when the cached list is older than
    :data:`_OWNER_REFRESH_INTERVAL`, so a removed owner loses access within that window
    rather than only on process restart; otherwise serves from the cache.
    """
    global _owner_ids_refreshed_at
    now = dt.datetime.now(dt.UTC)
    stale = (
        _owner_ids_refreshed_at is None
        or now - _owner_ids_refreshed_at >= _OWNER_REFRESH_INTERVAL
    )
    owner_ids = await bot.fetch_owner_ids(force_refresh=stale)
    if stale:
        _owner_ids_refreshed_at = now
    return user_id in owner_ids


def _reject(
    request: aiohttp.web.Request, status: int, message: str
) -> aiohttp.web.Response:
    """A rejection response: JSON for state-changing (XHR) callers, else plain text.

    The feature forms POST via fetch() and read ``res.json()`` (weekly_reset_form.js
    reads ``data.error``); a text/plain body would throw in their JSON parse and
    dead-end the form with no re-auth path on session expiry. GET page loads get plain
    text (an unauthenticated GET is redirected to login by the middleware instead).
    """
    if request.method in _UNSAFE_METHODS:
        return aiohttp.web.json_response({"error": message}, status=status)
    return aiohttp.web.Response(status=status, text=message)


def _origin_ok(request: aiohttp.web.Request) -> bool:
    """Whether a state-changing request's ``Origin`` matches ours (CSRF defence).

    Belt-and-suspenders atop ``SameSite=Lax``: a browser sends ``Origin`` on cross-site
    POSTs, so a mismatch is a forged request. Absent ``Origin`` (or no configured base
    URL) we can't compare, so we allow and defer to SameSite. Shared by the middleware
    and the logout handler (which is allowlisted, so the middleware's check doesn't run
    for it yet it still mutates session state).
    """
    origin = request.headers.get("Origin")
    if not origin or not cfg.public_base_url:
        return True
    return origin.rstrip("/") == cfg.public_base_url.rstrip("/")


# --- route handlers ---------------------------------------------------------------


async def _handle_login(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    """Start OAuth: mint state (holding a sanitized ``next``), then 302 to Discord."""
    error = _config_error()
    if error is not None:
        return error
    next_path = _sanitize_next(request.query.get("next", "/"))
    state = _AuthStateManager.issue(next_path)
    authorize = URL(AUTHORIZE_URL).with_query(
        client_id=cfg.discord_oauth_client_id,
        redirect_uri=_redirect_uri(),
        response_type="code",
        scope="identify",
        state=state,
    )
    return aiohttp.web.HTTPFound(str(authorize))


async def _handle_callback(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    """Complete OAuth: verify ownership, set the session cookie, then 302 to next."""
    # A user who declined consent (or any Discord-side error) comes back with ?error=.
    if request.query.get("error"):
        return aiohttp.web.Response(status=403, text="Discord login was denied.")

    # Check the things that don't depend on state BEFORE consuming it, so a misconfig
    # (500) or a request in the startup window before the bot is stashed (503) doesn't
    # burn the single-use state and force the owner to restart the whole login.
    error = _config_error()
    if error is not None:
        return error
    # text/plain, not the shared JSON 503: this is a browser navigation landing back
    # from Discord, not a page's fetch(), so the body is read by a human.
    bot = web.get_bot()
    if bot is None:
        return aiohttp.web.Response(status=503, text=web.BOT_STARTING_MSG)

    # Single-use state consume is the CSRF defence and yields the server-held next path;
    # an unknown / expired / replayed state finds nothing.
    next_path = _AuthStateManager.consume(request.query.get("state", ""))
    if next_path is None:
        return aiohttp.web.Response(
            status=400, text="Login session expired or invalid — please try again."
        )
    code = request.query.get("code", "")
    if not code:
        return aiohttp.web.Response(status=400, text="Missing authorization code.")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TOKEN_URL,
                data={
                    "client_id": cfg.discord_oauth_client_id,
                    "client_secret": cfg.discord_oauth_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(),
                },
            ) as token_resp:
                token_json = await token_resp.json()
            try:
                access_token = token_json["access_token"]
            except (KeyError, TypeError):
                logger.error("Discord OAuth token exchange failed: %s", token_json)
                return aiohttp.web.Response(
                    status=502, text="Could not complete Discord login."
                )
            async with session.get(
                USER_URL, headers={"Authorization": f"Bearer {access_token}"}
            ) as user_resp:
                user_json = await user_resp.json()
    # ValueError covers json.JSONDecodeError (a malformed 200 body with a JSON
    # content-type), which is NOT an aiohttp.ClientError; without it that would escape
    # as an unhandled 500 instead of the deliberate 502.
    except (aiohttp.ClientError, ValueError):
        logger.exception("Discord OAuth network error")
        return aiohttp.web.Response(
            status=502, text="Could not reach Discord — please try again."
        )

    try:
        user_id = int(user_json["id"])
    except (KeyError, TypeError, ValueError):
        logger.error("Discord OAuth user fetch failed: %s", user_json)
        return aiohttp.web.Response(
            status=502, text="Could not read your Discord identity."
        )

    if not await _is_owner(bot, user_id):
        logger.info("Web login denied for non-owner Discord id %s", user_id)
        return aiohttp.web.Response(
            status=403, text="This account is not authorized to use this tool."
        )

    response = aiohttp.web.HTTPFound(next_path)
    # Do NOT persist the Discord token: `identify` was a one-shot to learn the id.
    set_session_cookie(response, mint_session(user_id))
    logger.info("Web login succeeded for owner id %s", user_id)
    return response


async def _handle_logout(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    """Clear the session cookie and bounce to the login route.

    POST-only and origin-checked so a cross-site page can't force-logout the owner: a
    ``GET`` variant would be triggerable by ``<img>``/link/prefetch, and because logout
    is allowlisted the middleware's own CSRF check doesn't run for it — so it re-checks
    the Origin here. The gap was only an annoyance (a forced re-login), but closing it
    keeps every mutating route uniformly origin-gated.
    """
    if not _origin_ok(request):
        return _reject(request, 403, "Cross-origin request refused.")
    response = aiohttp.web.HTTPFound("/auth/login")
    clear_session_cookie(response)
    return response


def register_auth_routes(app: aiohttp.web.Application) -> None:
    """Add the auth routes to the shared persistent app."""
    app.router.add_get("/auth/login", _handle_login)
    app.router.add_get(_CALLBACK_PATH, _handle_callback)
    # Logout is POST-only (a GET could be triggered cross-site by <img>/link/prefetch →
    # forced logout) and additionally origin-checked in the handler.
    app.router.add_post("/auth/logout", _handle_logout)


# --- enforcement middleware -------------------------------------------------------


@aiohttp.web.middleware
async def _auth_middleware(
    request: aiohttp.web.Request,
    handler: aiohttp.typedefs.Handler,
) -> aiohttp.web.StreamResponse:
    """Auth-by-default gate: every non-allowlisted route requires an owner identity.

    Centralizing here makes "authenticated owner" an invariant — any future route is
    protected automatically unless its prefix is explicitly allowlisted.
    """
    if _is_allowlisted(request.path):
        return await handler(request)

    # Dev bypass (triple-gated; only ever active on a local dev box): treat as owner.
    if _dev_bypass_active():
        request[AUTH_USER_KEY] = int(cfg.dev_auth_user_id)
        return await handler(request)

    # Origin defence for state-changing requests (belt-and-suspenders atop SameSite).
    # The Discord callback is a GET, so this never touches the login flow.
    if request.method in _UNSAFE_METHODS and not _origin_ok(request):
        return _reject(request, 403, "Cross-origin request refused.")

    user_id = resolve_session(request.cookies.get(_SESSION_COOKIE, ""))
    if user_id is None:
        # Never redirect a non-GET (a 302 on a POST loses the body); ask it to re-auth.
        if request.method in ("GET", "HEAD"):
            login = URL("/auth/login").with_query(next=request.path_qs)
            return aiohttp.web.HTTPFound(str(login))
        return _reject(
            request, 401, "Your session has expired — reload the page to sign in again."
        )

    # This is the middleware itself, so it answers rather than raising BotNotReady:
    # web's converter middleware is installed INSIDE this one and would never see it.
    bot = web.get_bot()
    if bot is None:
        return _reject(request, 503, web.BOT_STARTING_MSG)
    if not await _is_owner(bot, user_id):
        return _reject(request, 403, "This account is not authorized to use this tool.")
    request[AUTH_USER_KEY] = user_id
    return await handler(request)


def register_auth_middleware(app: aiohttp.web.Application) -> None:
    """Install the auth middleware on the shared app.

    Registrars run in :func:`dd.anchor.web.start` before ``runner.setup()``, so adding
    a middleware here is safe — no change to ``web.py`` is needed.
    """
    app.middlewares.append(_auth_middleware)


# Auth routes AND the enforcement middleware are contributed at import time; the app is
# assembled from all registrars when the web server starts.
web.register_routes(register_auth_routes)
web.register_routes(register_auth_middleware)
