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

"""Web control panel for the anchor process (anchor).

The landing page at ``/``: every web page/tool as a **grouped list of rows**, ordered by
errand rather than by name. Rows are contributed by each feature module at import time
via :func:`web.register_card` (mirroring how routes are contributed via
:func:`web.register_routes`), so a new page appears here without editing this module;
:func:`web.grouped_cards` decides the grouping and the order. Authentication is handled
centrally by the Discord-OAuth middleware in ``web_auth.py`` — it protects every
non-allowlisted route by default, so ``/`` is gated with no extra code here.
``/control_panel`` gives the owner a button to the panel.

Three rows are this page's own rather than another page's link — Configured channels and
Shut down open a dialog here, and Sign out posts a form — so they are appended to the
last group by :func:`_panel_rows` instead of being registered as cards. That keeps
:class:`web.Card` meaning "a page you can navigate to", which is what every other module
contributes.

The page also carries **bot administration** — the web replacement for ``/anchor info``
and ``/anchor stop``. It lives on the panel itself rather than a page of its own: it is
two actions and a read-only dump, and the panel is already the bot's front door.

- ``GET  /bot/info``  configuration state + the configured channels, by name
- ``POST /bot/stop``  shut the process down

**Stop is one-way from here.** The aiohttp app runs *inside* this process, so stopping
the bot stops the panel too — there is no web route back up, and recovery is a Railway
redeploy. That is the tradeoff the owner accepted when ``/anchor stop`` moved off
Discord (see ``plans/anchor_command_web_migration.md`` Phase 3). ``lifecycle.
request_shutdown`` only *schedules* ``bot.close()``, so the response is written before
the gateway unwinds and the browser gets a confirmation rather than a dropped socket.
"""

import asyncio
import html
import logging
from pathlib import Path

import aiohttp.web
import hikari as h
import lightbulb as lb

from ...common import cfg, feeds, iron_banner, lifecycle, settings
from ...common.components import cv2_error, cv2_notice, respond_cv2
from .. import web

logger = logging.getLogger(__name__)

loader = lb.Loader()

_PANEL_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "control_panel.html"
)
_GROUPS_PLACEHOLDER = "<!--__GROUPS__-->"

#: The Trials row, by the only handle this module has on it: a card is contributed by
#: ``trials.py`` and identified here by where it points. Matching on href rather than
#: title because the href is the route contract — the title is copy, and copy is exactly
#: the thing this branch is allowed to rewrite.
_TRIALS_HREF = "/trials"

#: What the Trials row says during an Iron Banner week. The row stays clickable: Trials
#: not running is a fact about the weekend, not a reason to lock the form.
_IRON_BANNER_NOTE = "Not running this week — Iron Banner is on."


async def _iron_banner_is_on() -> bool:
    """Whether an Iron Banner event is live right now — **failing open**.

    The schedule is the operator-editable one the Iron Banner post itself publishes from
    (``RotationData['iron_banner']``), so the panel and the post can never disagree.

    Any exception answers "no" and renders the row normally, and the breadth of that
    ``except`` is deliberate: ``load_rotation`` raises ``RuntimeError`` when the store
    is unreadable *and* nothing has loaded in this process yet — i.e. on a cold start
    during a database blip, which is precisely when someone is trying to open the panel
    to find out what is wrong. Rotation data must never take down the front door.

    One PK SELECT on a page that otherwise does no database work at all, so it ships
    uncached; if the panel ever grows a second query, they should share a cache rather
    than each get their own.
    """
    try:
        return (await iron_banner.load_rotation()).active_event() is not None
    except Exception:
        logger.exception("Iron Banner lookup failed; rendering the Trials row normally")
        return False


def _row(
    title: str,
    description: str,
    *,
    href: str | None = None,
    element_id: str | None = None,
    danger: bool = False,
    quiet: bool = False,
    action: str = "Open",
    featured: bool = False,
) -> str:
    """One landing row: an ``<a>`` when it goes somewhere, a ``<button>`` when it acts.

    The whole row is the control — a row whose link is only its title gives the
    pointer a two-word target on a full-width surface. The trailing label is decorative
    (the row is already the target); it is there because a verb tells you what happens
    next in a way a chevron does not, and the verbs differ per row.
    """
    classes = " ".join(
        [
            "row",
            *(["featured"] if featured else []),
            *(["danger"] if danger else []),
            *(["quiet"] if quiet else []),
        ]
    )
    inner = (
        '<span class="row-text">'
        f'<span class="row-title">{html.escape(title)}</span>'
        f'<span class="row-desc">{html.escape(description)}</span>'
        "</span>"
        f'<span class="row-action">{html.escape(action)}</span>'
    )
    if href is not None:
        return f'<a class="{classes}" href="{html.escape(href)}">{inner}</a>'
    ident = f' id="{html.escape(element_id)}"' if element_id else ""
    return f'<button type="button" class="{classes}"{ident}>{inner}</button>'


def _link(
    title: str,
    *,
    href: str | None = None,
    element_id: str | None = None,
    danger: bool = False,
) -> str:
    """One entry in an errand list — a plain link, or a button that looks like one.

    Groups 2-4 are lists of destinations, not things to weigh up: rendering them as
    full rows gave every group on the page the same weight and made the ordering say
    nothing. A wrapped list of links reads as "and these are the other places you can
    go", which is what they are.
    """
    classes = " ".join(["qlink", *(["danger"] if danger else [])])
    label = html.escape(title)
    if href is not None:
        return f'<a class="{classes}" href="{html.escape(href)}">{label}</a>'
    ident = f' id="{html.escape(element_id)}"' if element_id else ""
    return f'<button type="button" class="{classes}"{ident}>{label}</button>'


def _panel_links() -> list[str]:
    """This page's own rows, appended to the last group.

    Sign out is a ``<form method="post">`` and not a link, which is load-bearing:
    ``web_auth`` made ``/auth/logout`` POST-only and origin-checked precisely because a
    ``GET`` logout is triggerable cross-site by an ``<img>`` or a prefetch. A styled
    anchor here would reopen the hole that closed.
    """
    return [
        _link("Configured channels", element_id="infoBtn"),
        '<form method="post" action="/auth/logout">'
        '<button type="submit" class="qlink">Sign out</button></form>',
        _link("Shut down", element_id="stopBtn", danger=True),
    ]


async def _render_panel_html() -> str:
    """Render the landing page: one section per group, in the enum's declaration order.

    Async because of the Iron Banner check — the only reason this page touches the
    database at all.
    """
    iron_banner_on = await _iron_banner_is_on()
    by_group = dict(web.grouped_cards())

    sections: list[str] = []
    for group in web.CardGroup:
        cards = by_group.get(group, [])
        # Group 1 is a stack of rows; the rest are lists of links. Weight descends with
        # how often the errand happens — see the note in the page's stylesheet.
        if group is web.CardGroup.SEND:
            entries = [
                _row(
                    card.title,
                    _IRON_BANNER_NOTE
                    if (iron_banner_on and card.href == _TRIALS_HREF)
                    else card.description,
                    href=card.href,
                    danger=card.danger,
                    quiet=iron_banner_on and card.href == _TRIALS_HREF,
                    action=card.action,
                    featured=card.featured,
                )
                for card in cards
            ]
            body = f'<div class="rows">{"".join(entries)}</div>'
        else:
            entries = [
                _link(card.title, href=card.href, danger=card.danger) for card in cards
            ]
            # The last group also holds the three actions that live on this page.
            if group is web.CardGroup.ADMIN:
                entries.extend(_panel_links())
            body = f'<div class="links">{"".join(entries)}</div>'
        if not entries:
            continue
        sections.append(
            f'<section class="group group-{group.name.lower()}">'
            f"<h2>{html.escape(group.value)}</h2>"
            f"{body}"
            "</section>"
        )

    return _PANEL_HTML_PATH.read_text(encoding="utf-8").replace(
        _GROUPS_PLACEHOLDER, "".join(sections)
    )


async def _handle_panel(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=await _render_panel_html(), content_type="text/html"
    )


async def _channel_entry(feed: str, channel_id: int | None) -> dict[str, str | None]:
    """The finished ``/bot/info`` row for one configured followable.

    A raw snowflake tells the reader nothing, and a name you cannot click is only
    slightly better — the guild id is what turns it into a deep link, which is why this
    returns the assembled row rather than the pieces. Resolution is best-effort and
    per-channel: the bot may not be in the guild, the channel may be deleted, or it may
    simply not be up yet, none of which should cost the whole panel its config dump.

    The feed travels as its **display name** (``dd.common.feeds`` is the one place those
    are decided): the dialog should say "Lost Sector", not ``lost_sector``. A slug with
    no catalog entry falls back to itself rather than vanishing — that would be a feed
    configured in the database and nowhere else, which is exactly the state an operator
    opens this dialog to discover.
    """
    followable = feeds.FEEDS.get(feed)
    row: dict[str, str | None] = {
        "feed": followable.display_name if followable else feed,
        "channelId": str(channel_id) if channel_id else None,
        "channelName": None,
        "url": None,
    }
    bot = web.get_bot()
    if not channel_id or bot is None:
        return row
    try:
        channel = await bot.fetch_channel(channel_id)
    except Exception:
        logger.info("Could not resolve channel %s for /bot/info", channel_id)
        return row
    name = getattr(channel, "name", None)
    guild_id = getattr(channel, "guild_id", None)
    if name:
        row["channelName"] = f"#{name}"
    if guild_id:
        row["url"] = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return row


async def _handle_bot_info(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Configuration state — what ``/anchor info`` printed, plus channel names.

    Anchor never had the mirror-status block (that is beacon's, gated on a
    ``mirror_check``), so this is the config dump and the configured channels.
    """
    return aiohttp.web.json_response(
        {
            "bot": "anchor",
            "controlServerId": str(cfg.control_discord_server_id),
            # A tuple of guild ids, empty in prod — its emptiness is what marks an
            # environment as production, so it is worth showing verbatim.
            "testEnv": [str(guild_id) for guild_id in cfg.test_env],
            "channels": await asyncio.gather(
                *(
                    _channel_entry(feed, channel_id)
                    for feed, channel_id in (await settings.get_followables()).items()
                )
            ),
        }
    )


async def _handle_bot_stop(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Shut the bot down cleanly (exit 0, so Railway leaves it down).

    Auth + Origin (CSRF) are already enforced by the middleware. The shutdown is
    scheduled rather than awaited, so this response reaches the browser first — see the
    module docstring. A request landing in the window before the bot is up raises
    ``BotNotReady``, which ``web``'s middleware answers with the shared 503.
    """
    bot = web.require_bot()
    logger.warning("Shutdown requested from the web control panel")
    await lifecycle.request_shutdown(bot, lifecycle.STOP_EXIT_CODE)
    return aiohttp.web.json_response({"ok": True, "stopping": True})


def register_panel_routes(app: aiohttp.web.Application) -> None:
    """Add the control-panel routes to the shared persistent app."""
    app.router.add_get("/", _handle_panel)
    app.router.add_get("/bot/info", _handle_bot_info)
    app.router.add_post("/bot/stop", _handle_bot_stop)


web.register_routes(register_panel_routes)


# --- slash commands ---------------------------------------------------------------


class ControlPanel(
    lb.SlashCommand,
    name="control_panel",
    description="Open the anchor web control panel",
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context) -> None:
        if not cfg.public_base_url:
            await respond_cv2(
                ctx,
                cv2_error(
                    "No control panel link available",
                    "No public base URL is configured (set PUBLIC_BASE_URL or run "
                    "on Railway), so I can't mint a reachable link.",
                ),
                ephemeral=True,
            )
            return

        url = f"{cfg.public_base_url}/"
        # Ephemeral (owner-private) response with a link button, mirroring the
        # weekly-reset form command. The panel is gated by Discord OAuth (web_auth.py) —
        # you sign in with Discord on first open.
        container = cv2_notice(
            "Open the control panel with the button below — it lists every web tool. "
            "You'll sign in with Discord the first time."
        )
        row = h.impl.MessageActionRowBuilder()
        row.add_component(h.impl.LinkButtonBuilder(url=url, label="Open control panel"))
        container.add_component(row)
        await respond_cv2(ctx, container, ephemeral=True)


loader.command(ControlPanel)
