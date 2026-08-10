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

"""The anchor process's single persistent HTTP server.

Railway exposes exactly one port (``cfg.port``), so anchor runs one aiohttp app that
hosts every HTTP surface: the Bungie OAuth callback (previously a transient server spun
up per ``/bungie login``) and the rotation editor. Feature modules contribute routes by
registering a callback at import time via :func:`register_routes`; the app is built and
started once on ``StartedEvent`` (see ``dd/anchor/__main__.py``) and stopped on
``StoppingEvent``.
"""

import enum
import logging
import typing as t
from pathlib import Path

import aiohttp.typedefs
import aiohttp.web

from ..common import cfg
from ..common.bot import CachedFetchBot

logger = logging.getLogger(__name__)

# The directory of static web assets (editor html/css/js) served under /static/. Derived
# the same way the feature modules resolve their templates (this module lives in
# dd/anchor/, so its parent holds web_static/), not a hardcoded absolute path.
_WEB_STATIC_DIR = Path(__file__).resolve().parent / "web_static"

# Route registrars contributed by feature modules at import time. Applied in order when
# the app is built in start(). Kept as module state so modules stay decoupled from the
# app object and from each other.
_route_registrars: list[t.Callable[[aiohttp.web.Application], None]] = []
_runner: aiohttp.web.AppRunner | None = None


class CardGroup(enum.Enum):
    """Which errand a homepage entry belongs to.

    The homepage used to be an alphabetical list of *modules*, which is how the bot is
    built rather than how it is administered — the two most frequent errands, composing
    the Weekly Reset and Trials posts, sorted last. These four groups are the admin's
    errands instead, and the declaration order below **is** the display order: most
    frequent first, and the group holding the one irreversible action last.
    """

    SEND = "Send a post"
    DATA = "Fix the data behind a post"
    CHECK = "Check what happened"
    ADMIN = "Set up and admin"


class Card(t.NamedTuple):
    """A homepage entry for one web page/tool.

    ``href`` is a same-origin path (e.g. ``/rotation``); ``title``/``description`` are
    dev-authored copy rendered (escaped) into the homepage.

    ``group`` places the entry under one of the four errand headings; ``order`` sorts
    within the group, low first, ties broken by title. Order is explicit rather than
    alphabetical because within an errand the *frequency* of each task is the useful
    ranking and no property of the title encodes it.

    ``danger`` marks the entry as destructive. Exactly one card sets it — Shut down —
    and the homepage renders it as the only red thing on the page. That scarcity is the
    point: a page with one red link says which action is different from all the others,
    and a page with five says nothing.
    """

    title: str
    description: str
    href: str
    group: CardGroup = CardGroup.ADMIN
    order: int = 100
    danger: bool = False


# Homepage cards contributed by feature modules at import time (mirrors
# _route_registrars). Read at request time by the homepage handler, so contribution
# order is irrelevant — the homepage groups and sorts for display.
_cards: list[Card] = []


def register_routes(
    registrar: t.Callable[[aiohttp.web.Application], None],
) -> None:
    """Register a callback that adds routes to the shared app.

    Call at import time (e.g. module top-level). Registrars run when :func:`start`
    builds the app, so registration must happen before the gateway reaches
    ``StartedEvent``.
    """
    _route_registrars.append(registrar)


def register_card(card: Card) -> None:
    """Contribute a card to the web homepage.

    Call at import time alongside :func:`register_routes` so a feature page appears on
    the homepage without the homepage module needing to know about it. Cards are read
    (and sorted) at request time, so registration order does not matter.
    """
    _cards.append(card)


def registered_cards() -> list[Card]:
    """Return the contributed homepage cards (a copy; caller sorts for display)."""
    return list(_cards)


def grouped_cards() -> list[tuple[CardGroup, list[Card]]]:
    """The cards as the homepage renders them: by group, in group-declaration order.

    A group with no cards is omitted rather than rendered empty — a heading over
    nothing reads as something broken.
    """
    by_group: dict[CardGroup, list[Card]] = {group: [] for group in CardGroup}
    for card in _cards:
        by_group[card.group].append(card)
    return [
        (group, sorted(cards, key=lambda c: (c.order, c.title)))
        for group, cards in by_group.items()
        if cards
    ]


# --- the live bot -------------------------------------------------------------------

#: The live bot, for every route in the app. Stashed once, by the ``StartedEvent``
#: listener in ``dd/anchor/__main__.py`` immediately before :func:`start` — so a route
#: can never be served before it is set, which a per-module ``StartedEvent`` stash could
#: not promise (listeners run concurrently with the one that starts the server).
#:
#: One stash here rather than the module global each route-owning extension used to
#: keep: the copies drifted into three different "not ready yet" answers, and two were
#: written only when *that* module's unrelated startup work got that far — so a page
#: could keep refusing on a bot that had been up for hours.
_bot: CachedFetchBot | None = None

#: What every "the bot isn't up yet" answer says, in one place.
BOT_STARTING_MSG = "Bot is still starting — try again in a moment."


class BotNotReady(RuntimeError):
    """Raised by :func:`require_bot` before the bot has been stashed.

    A plain exception, not ``aiohttp.web.HTTPServiceUnavailable``: a handler that
    reports ``str(e)`` to the page (rather than letting the exception propagate as a
    response) renders the HTTP one as "Service Unavailable" — aiohttp's stringification
    of the class, with the sentence explaining what to do dropped on the floor, and that
    sentence is the whole value of the error. The ones that DO propagate become the
    standard 503 JSON in :func:`_bot_not_ready_middleware`.
    """


def stash_bot(bot: CachedFetchBot) -> None:
    """Record the live bot for the whole web app. Call once, at ``StartedEvent``."""
    global _bot
    _bot = bot


def get_bot() -> CachedFetchBot | None:
    """The live bot, or ``None`` if the process has not finished starting.

    For the call sites that *degrade* rather than refuse — a preview that renders
    without guild emoji, a panel row that stays a raw snowflake, the fail-closed channel
    check that owes the operator a readable reason. Anything that simply needs the bot
    wants :func:`require_bot` instead.
    """
    return _bot


def require_bot() -> CachedFetchBot:
    """The live bot, or raise :class:`BotNotReady` (a 503 via the middleware)."""
    if _bot is None:
        raise BotNotReady(BOT_STARTING_MSG)
    return _bot


def bot_not_ready_response() -> aiohttp.web.Response:
    """The standard body for a request that arrived before the bot was up.

    JSON, because every page here reads ``data.error`` off a failed fetch; a text/plain
    503 surfaces as an unhelpful parse error instead.
    """
    return aiohttp.web.json_response({"error": BOT_STARTING_MSG}, status=503)


@aiohttp.web.middleware
async def _bot_not_ready_middleware(
    request: aiohttp.web.Request,
    handler: aiohttp.typedefs.Handler,
) -> aiohttp.web.StreamResponse:
    """Turn a handler's :class:`BotNotReady` into :func:`bot_not_ready_response`.

    So a route needing the bot is one ``require_bot()`` call and no error plumbing —
    which is what keeps the answer identical across every page, rather than each route
    inventing its own status and wording again.
    """
    try:
        return await handler(request)
    except BotNotReady:
        logger.info("%s %s arrived before the bot was up", request.method, request.path)
        return bot_not_ready_response()


#: Everything under web_static/tests/. Matched ahead of the static mount in `start`.
_TEST_FIXTURE_ROUTE = "/static/tests/{tail:.*}"


#: The response headers every route gets, including ``/static/``.
#:
#: **Why CSP is here.** The mirror log renders *other servers'* captured Discord posts,
#: in the browser (``web_static/cv2_render.js``). That input is controlled by anyone who
#: can post in a mirrored channel. The renderer's own defences are structural — text
#: reaches the DOM through ``textContent``, URLs are ``http(s)``-checked at the one
#: place they become attributes, and a single field reaches ``innerHTML`` carrying only
#: escape-by-construction markdown — but the markdown tokenizer is hand-rolled, and the
#: comment at ``cv2_model.js``'s ``INLINE`` records that its predecessor really did
#: mangle escaping on real posts. ``script-src 'self'`` is what caps the cost of that
#: happening again at defacement, instead of script execution in an owner's session on a
#: cookie-authed app whose routes publish to Discord.
#:
#: Directive notes, since the shape is deliberate:
#:
#: - ``default-src 'none'`` — the app loads nothing in the unlisted categories (fonts,
#:   media, workers, frames), so an accidental future dependency fails loudly rather
#:   than riding a permissive default. Subsumes ``object-src 'none'``.
#: - ``style-src`` keeps ``'unsafe-inline'`` deliberately: ``charts.js`` and
#:   ``mirror_log.js`` build ``style=`` attributes into markup, for tooltip swatches
#:   and progress-bar widths. Removing it means refactoring those, and what it
#:   concedes to an attacker who already has HTML injection is CSS-based exfiltration
#:   of a DOM holding no secrets (the session cookie is HttpOnly). Not worth it here.
#: - ``img-src ... http: https:`` is the mirrored-post reality: a captured post embeds
#:   images on any host, so a host list would be fiction. It still excludes ``data:``
#:   and ``blob:``. ``http:`` is listed because every URL check in this codebase accepts
#:   it — ``cv2_render.js``'s ``isHttpUrl``, ``hybrid_post_core.post_spec_nodes`` — so
#:   omitting it made the POLICY the one component that disagreed: an author pasting an
#:   ``http://`` image into the weekly-reset form saw a blank where the published post
#:   carried the image fine. Over HTTPS the browser's own mixed-content rules block it
#:   anyway; that is the browser being honest about an http image, not us hiding one.
#: - ``base-uri 'none'`` stops injected markup retargeting every ``/static/*.js``
#:   path, and ``form-action 'self'`` blunts the phishing-form variant that survives
#:   CSP — both are one token against attacks the threat model actually has.
_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' http: https:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    # A rendered post loads images from arbitrary third-party hosts, and each of those
    # requests would otherwise hand the admin app's URL to whoever runs the image host.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


async def _security_headers(
    request: aiohttp.web.Request, response: aiohttp.web.StreamResponse
) -> None:
    """Attach :data:`SECURITY_HEADERS` to every response.

    A response hook rather than a middleware, and that is load-bearing: :func:`start`
    fails closed on ``app.middlewares`` being empty, because the auth middleware is this
    app's only security boundary. A second middleware would satisfy that check even when
    ``web_auth`` failed to load — silently reopening the hole the guard exists to close.

    Every response, including ``/static/``: the static mount is allowlisted
    unauthenticated so a page's assets load before sign-in, and it serves the raw page
    templates too (``/static/editor.html`` renders).
    """
    response.headers.update(SECURITY_HEADERS)


async def _hide_test_fixtures(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """404 the browser-test fixtures, which are not app assets.

    aiohttp's static handler serves a directory wholesale, and the auth middleware
    (``web_auth``) allowlists ``/static/`` so a page's css/js can load before sign-in.
    Together those would publish ``tests/builder_harness.html`` — a fully working CV2
    builder with no auth and no database behind it. The tests load it over ``file://``,
    so nothing needs it served.
    """
    raise aiohttp.web.HTTPNotFound()


async def start(port: int | None = None) -> None:
    """Build the app from all registered route contributors and start listening."""
    global _runner
    if _runner is not None:
        logger.warning("Anchor web app already started; ignoring duplicate start()")
        return

    app = aiohttp.web.Application()
    for registrar in _route_registrars:
        registrar(app)

    # Fail closed: the auth middleware (dd.anchor.extensions.web_auth) is this app's
    # only security boundary — every feature module deleted its per-handler auth and
    # relies on it being installed here. If no middleware registered (e.g. web_auth
    # failed to import and load_extensions_strict skipped it), refuse to serve rather
    # than expose the editor / weekly-reset form unauthenticated.
    if not app.middlewares:
        raise RuntimeError(
            "Anchor web app has no middleware registered — refusing to start an "
            "unauthenticated web surface (is the web_auth extension loading?)."
        )

    # Installed here, AFTER the check above, and that order is the point: the check asks
    # whether a REGISTRAR contributed a middleware, i.e. whether web_auth loaded. A
    # middleware contributed from inside this module would answer that question on
    # web_auth's behalf and silently reopen the hole the check exists to close (the trap
    # _security_headers documents). Appending also nests it INSIDE the auth middleware
    # — aiohttp treats middlewares[0] as outermost — so it only ever converts a
    # BotNotReady raised by a handler the auth gate already admitted.
    app.middlewares.append(_bot_not_ready_middleware)

    # Registered BEFORE the static route below, because the router matches in
    # registration order — see _hide_test_fixtures for why.
    app.router.add_route("*", _TEST_FIXTURE_ROUTE, _hide_test_fixtures)

    # Serve the split editor assets (css/js) so pages can <link>/<script> them instead
    # of inlining. The /static/ prefix is distinct from every feature route (/rotation…,
    # OAuth callback), so it can't collide.
    app.router.add_static("/static/", _WEB_STATIC_DIR)

    # Force browsers to revalidate the static assets on every load. aiohttp's static
    # handler sends only ETag/Last-Modified (no Cache-Control), so browsers apply
    # heuristic caching and can hold a stale /static/shared.css across a deploy. The
    # page HTML now depends on the CSS custom properties defined in shared.css, so a
    # stale copy silently breaks every var() reference (missing borders/toggles).
    # "no-cache" keeps the file cached but requires a conditional GET each load — the
    # ETag makes the common case a cheap 304, and a deploy is picked up immediately.
    async def _revalidate_static(
        request: aiohttp.web.Request, response: aiohttp.web.StreamResponse
    ) -> None:
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(_revalidate_static)
    app.on_response_prepare.append(_security_headers)

    # access_log=None disables aiohttp's default request-line access log. The editor
    # entry links and the OAuth callback carry secrets in the query string
    # (?token=…, ?code=/?state=…); the default log records the full request line, which
    # would leak those to anyone with log-read access (CWE-532). This app logs its own
    # meaningful events via the module logger, so the request log has little value here.
    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    bind_port = cfg.port if port is None else port
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", bind_port)
    await site.start()
    _runner = runner
    logger.info("Anchor web app listening on 0.0.0.0:%s", bind_port)


async def stop() -> None:
    """Stop the server and release the port (idempotent)."""
    global _runner
    if _runner is None:
        return
    await _runner.shutdown()
    await _runner.cleanup()
    _runner = None
    logger.info("Anchor web app stopped")
