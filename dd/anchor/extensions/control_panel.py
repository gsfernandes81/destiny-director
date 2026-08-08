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

A card-based landing page at ``/`` listing every web page/tool. Cards are contributed by
each feature module at import time via :func:`web.register_card` (mirroring how routes
are contributed via :func:`web.register_routes`), so a new page appears here without
editing this module. Authentication is handled centrally by the Discord-OAuth middleware
in ``web_auth.py`` — it protects every non-allowlisted route by default, so ``/`` is
gated with no extra code here. ``/control_panel`` gives the owner a button to the panel.

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

from ...common import cfg, lifecycle, settings
from ...common.components import cv2_error, cv2_notice, respond_cv2
from .. import web

logger = logging.getLogger(__name__)

loader = lb.Loader()

_PANEL_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "control_panel.html"
)
_CARDS_PLACEHOLDER = "<!--__CARDS__-->"


def _render_panel_html() -> str:
    """Render the control panel, substituting the card grid for the placeholder."""
    cards = sorted(web.registered_cards())
    if cards:
        items = "".join(
            f'<a class="card" href="{html.escape(card.href)}">'
            f'<div class="title">{html.escape(card.title)}</div>'
            f'<div class="desc">{html.escape(card.description)}</div>'
            "</a>"
            for card in cards
        )
    else:
        items = '<p class="empty">No web tools are available.</p>'
    return _PANEL_HTML_PATH.read_text(encoding="utf-8").replace(
        _CARDS_PLACEHOLDER, items
    )


async def _handle_panel(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(text=_render_panel_html(), content_type="text/html")


async def _channel_entry(feed: str, channel_id: int | None) -> dict[str, str | None]:
    """The finished ``/bot/info`` row for one configured followable.

    A raw snowflake tells the reader nothing, and a name you cannot click is only
    slightly better — the guild id is what turns it into a deep link, which is why this
    returns the assembled row rather than the pieces. Resolution is best-effort and
    per-channel: the bot may not be in the guild, the channel may be deleted, or it may
    simply not be up yet, none of which should cost the whole panel its config dump.
    """
    row: dict[str, str | None] = {
        "feed": feed,
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
