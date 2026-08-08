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

"""Web editor for the rotation JSON store (anchor).

``{public_base_url}/rotation`` is a homepage listing every rotation type
(``ROTATION_SCHEMAS``); each links to ``/rotation/edit?type=…`` so the owner edits the
document with a friendly form, previews the rendered post, and saves — the server
re-validates against the JSON schema on save. Reached from the control-panel card grid
(``/control_panel``), which replaced the former ``/rotation edit`` command.
Authentication for every page, preview and save is handled centrally by the
Discord-OAuth middleware in ``web_auth.py`` (this module carries no auth code of its
own).
"""

import asyncio
import datetime as dt
import html
import json
import logging
import typing as t
from pathlib import Path

import aiohttp.web
import hikari as h
import lightbulb as lb

from ...common import iron_banner, lost_sector, rotation_schema, schemas, settings
from ...common.bot import CachedFetchBot
from ...common.components import footer_button_specs
from ...common.legacy_activities import iter_wall_posts, load_seed_doc, weapon_values
from ...sector_accounting import (
    legacy_activities,
    sector_accounting,
    xur as xur_support_data,
)
from .. import hybrid_post_core, web
from .bungie_api import item_index

logger = logging.getLogger(__name__)

loader = lb.Loader()

_EDITOR_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "editor.html"
)
_HOME_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "rotation_home.html"
)
# Days of rendered output the preview spans (covers a daily reset).
_PREVIEW_DAYS = 4


# --- document helpers -------------------------------------------------------------


def _default_doc(post_type: str) -> dict[str, t.Any]:
    """An empty-but-renderable scaffold for a post type with no stored data yet."""
    if post_type == "lost_sector":
        return {
            "version": 1,
            "reference_date": "",
            "schedule": {zone: [] for zone in rotation_schema.LOST_SECTOR_ZONES},
            "sectors": [],
        }
    if post_type == "xur_location":
        return {"version": 1, "locations": []}
    if post_type == rotation_schema.TRIALS_LOOT_SLUG:
        # Start populated with the baked default sets + a one-loop schedule (so the
        # editor isn't blank), matching the producer's runtime fallback.
        return rotation_schema.trials_loot_default_doc()
    if post_type == rotation_schema.IRON_BANNER_SLUG:
        # Seeded schedule + the two default bonus focus pools (the producer's fallback).
        return rotation_schema.iron_banner_default_doc()
    if rotation_schema.is_world_activity(post_type):
        return rotation_schema.legacy_default_doc(post_type)
    return {}


def _vocab() -> dict[str, t.Any]:
    return {
        "champions": rotation_schema.CHAMPION_TYPES,
        "shields": rotation_schema.SHIELD_ELEMENTS,
        "zones": rotation_schema.LOST_SECTOR_ZONES,
    }


async def _lost_sector_posts(
    rotation: sector_accounting.Rotation,
    details_enabled: bool,
) -> list[tuple[str, hybrid_post_core.PostSpec]]:
    """The next few days of Lost Sector posts, rendered as Discord messages.

    Uses the SAME body builder the live post uses (``lost_sector.build_body``) so the
    editor previews exactly what will post; ``details_enabled`` mirrors the live
    champions/shields toggle.
    """
    image_url = await settings.get_lost_sector_image_url()
    now = dt.datetime.now(dt.UTC)
    # Align to the daily 17:00 UTC reset so entry 0 is the currently-live post.
    base = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if now < base:
        base -= dt.timedelta(days=1)
    posts: list[tuple[str, hybrid_post_core.PostSpec]] = []
    for offset in range(_PREVIEW_DAYS):
        date = base + dt.timedelta(days=offset)
        label = date.strftime("%a, %b %d") + (" · now" if offset == 0 else "")
        try:
            sectors = rotation(date)
        except (KeyError, IndexError):
            # A TBC day: the schedule names a sector with no data, or has no entry for
            # this day. Narrow on purpose — a different error is a real bug, so let it
            # surface (the handler 400s it with the message) rather than hide as "TBC".
            posts.append((label, hybrid_post_core.PostSpec.cv2("*No data (TBC).*")))
            continue
        body = lost_sector.build_body(sectors, details_enabled)
        posts.append(
            (
                label,
                hybrid_post_core.PostSpec.cv2(
                    body,
                    image_url,
                    buttons=footer_button_specs(guides=lost_sector.GUIDES),
                ),
            )
        )
    return posts


def _render_xur_location_preview_html(
    locations: xur_support_data.XurLocations,
) -> str:
    """A compact HTML rendering of the resolved Xûr location map."""
    items: list[str] = []
    for loc in locations.values():
        friendly = html.escape(loc.friendly_location_name or loc.api_location_name)
        api_name = html.escape(loc.api_location_name)
        if loc.link:
            link = html.escape(loc.link, quote=True)
            label = f"<a href='{link}' target='_blank' rel='noopener'>{friendly}</a>"
        else:
            label = friendly
        items.append(f"<li>{label} <small>(API: {api_name})</small></li>")
    if not items:
        return "<p><em>No locations defined yet.</em></p>"
    return "<ul>" + "".join(items) + "</ul>"


def _legacy_posts(
    destination_key: str,
    rotation: legacy_activities.LegacyRotation,
) -> list[tuple[str, hybrid_post_core.PostSpec]]:
    """The next few legacy posts for a destination, rendered as Discord messages.

    Reuses ``dd.common.legacy_activities.iter_wall_posts`` — the same per-mode paging
    the beacon read commands use — so the editor previews exactly what the
    ``/<destination>`` command posts, capped to a few periods for a compact editor pane.
    """
    now = dt.datetime.now(dt.UTC)
    return [
        (label, hybrid_post_core.PostSpec.cv2(body, None))
        for label, body in iter_wall_posts(
            destination_key, rotation, now, count=_PREVIEW_DAYS
        )
    ]


# --- per-type dispatch ------------------------------------------------------------


def _build_trials_loot(data: dict[str, t.Any]) -> list[tuple[str, list[str]]]:
    """Expand the loot doc into its looping ``(set_name, weapons)`` schedule.

    The hard gate beyond the schema: every schedule entry must name a defined set
    (otherwise the producer would silently drop that week). Returns the expanded
    rotation, which the preview renders."""
    sets = {
        str(s["name"]): list(s.get("weapons") or []) for s in data.get("sets") or []
    }
    schedule = [str(n) for n in data.get("schedule") or []]
    missing = sorted({n for n in schedule if n not in sets})
    if missing:
        raise ValueError("schedule references undefined set(s): " + ", ".join(missing))
    return [(n, sets[n]) for n in schedule]


def _render_trials_loot_preview_html(rotation: list[tuple[str, list[str]]]) -> str:
    """The looping loot schedule, one week per row (the order the producer walks)."""
    if not rotation:
        return "<p><em>No schedule defined yet.</em></p>"
    items: list[str] = []
    for name, weapons in rotation:
        listed = ", ".join(html.escape(w) for w in weapons if w) or "—"
        items.append(f"<li><strong>{html.escape(name)}</strong> — {listed}</li>")
    return "<ol>" + "".join(items) + "</ol>"


async def _iron_banner_posts(
    rotation: iron_banner.IronBannerRotation,
    emoji_dict: dict[str, h.Emoji],
) -> list[tuple[str, hybrid_post_core.PostSpec]]:
    """The next few Iron Banner events, rendered as the real Discord posts.

    Uses the SAME layout the live post uses (``iron_banner.build_body`` +
    ``hybrid_post_core.resolve_weapon_lines``) so the editor previews exactly what will
    post — dates, game modes, and the bonus focus pool as light.gg links + weapon-type
    emoji. Shows the upcoming events (falling back to the last few when the whole
    schedule is in the past) so an operator can eyeball what's next.
    """
    now = dt.datetime.now(dt.UTC)
    ts = int(now.timestamp())
    upcoming = [e for e in rotation.events if e.end_ts > ts]
    events = (upcoming or rotation.events[-_PREVIEW_DAYS:])[:_PREVIEW_DAYS]
    available = set(emoji_dict) | {"weapon"}
    posts: list[tuple[str, hybrid_post_core.PostSpec]] = []
    for event in events:
        pool_lines = await hybrid_post_core.resolve_weapon_lines(
            event.pool_weapon_names, available
        )
        start = dt.datetime.fromtimestamp(event.start_ts, dt.UTC).strftime("%b %d, %Y")
        live = event.start_ts <= ts < event.end_ts
        label = f"{start} · {event.pool_name}" + (" · now" if live else "")
        posts.append(
            (
                label,
                hybrid_post_core.PostSpec.cv2(
                    iron_banner.build_body(event, pool_lines),
                    buttons=footer_button_specs(guides=iron_banner.GUIDES),
                ),
            )
        )
    return posts


def _build_domain_object(post_type: str, data: t.Any) -> t.Any:
    """Construct the domain object for ``post_type`` (a hard gate beyond the schema).

    Raises if the document is structurally unusable — caught by the preview / save
    handlers and surfaced to the editor. New rotation types register their builder
    here alongside :data:`rotation_schema.ROTATION_SCHEMAS`.
    """
    if post_type == "xur_location":
        return xur_support_data.XurLocations.from_json(data)
    if post_type == rotation_schema.TRIALS_LOOT_SLUG:
        return _build_trials_loot(data)
    if post_type == rotation_schema.IRON_BANNER_SLUG:
        return iron_banner.IronBannerRotation.from_json(data)
    if rotation_schema.is_world_activity(post_type):
        return legacy_activities.LegacyRotation.from_json(data)
    return sector_accounting.Rotation.from_json(data)


async def _render_preview(
    post_type: str, obj: t.Any, bot: CachedFetchBot | None
) -> dict[str, t.Any]:
    """The preview payload for a rotation document, tagged by what it is.

    Two shapes, because two of these are not Discord posts:

    - ``{"kind": "html", "html": …}`` — xur_location (a location map) and trials_loot
      (a set schedule) are *data* views of a document that never posts on its own, so
      they stay small server-rendered tables with no emoji or DB lookup.
    - ``{"kind": "wall", "posts": [{"label", "nodes"}], "emoji": {…}}`` — the rest are
      real posts, and the page draws them with the shared renderer from the same node
      trees ``build_cv2`` would send.
    """
    if post_type == "xur_location":
        return {"kind": "html", "html": _render_xur_location_preview_html(obj)}
    if post_type == rotation_schema.TRIALS_LOOT_SLUG:
        return {"kind": "html", "html": _render_trials_loot_preview_html(obj)}

    emoji_dict = await hybrid_post_core.preview_emoji_dict(bot)
    if post_type == rotation_schema.IRON_BANNER_SLUG:
        # Iron Banner needs the dict for more than rendering: which weapon-type emoji
        # the guild actually has decides what goes in the body text.
        posts = await _iron_banner_posts(obj, emoji_dict)
    elif rotation_schema.is_world_activity(post_type):
        key = post_type.removeprefix(rotation_schema.ROTATION_SLUG_PREFIX)
        posts = _legacy_posts(key, obj)
    else:
        details = bool(await schemas.AutoPostSettings.get_lost_sector_details_enabled())
        posts = await _lost_sector_posts(obj, details)

    return {
        "kind": "wall",
        "posts": [
            {"label": label, "nodes": hybrid_post_core.post_spec_nodes(spec)}
            for label, spec in posts
        ],
        "emoji": hybrid_post_core.emoji_payload(emoji_dict),
    }


# --- route handlers ---------------------------------------------------------------


def _read_json_body(payload: t.Any) -> tuple[str, t.Any]:
    """Pull ``(type, data)`` from a parsed JSON POST body (auth is via the cookie)."""
    post_type = str(payload.get("type", ""))
    data = payload.get("data")
    return post_type, data


def _render_home_html() -> str:
    """The rotations homepage: one link per ``ROTATION_SCHEMAS`` type."""
    items: list[str] = []
    for slug in sorted(rotation_schema.ROTATION_SCHEMAS):
        title = html.escape(
            str(rotation_schema.ROTATION_SCHEMAS[slug].get("title", slug))
        )
        slug_attr = html.escape(slug, quote=True)
        items.append(
            f'<li><a href="/rotation/edit?type={slug_attr}">{title}</a>'
            f" <code>{html.escape(slug)}</code></li>"
        )
    list_html = '<ul class="rotations">' + "".join(items) + "</ul>"
    return _HOME_HTML_PATH.read_text(encoding="utf-8").replace(
        "<!--__ROTATIONS__-->", list_html
    )


async def _handle_home_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # Auth is enforced by the web_auth middleware; this just renders the homepage.
    return aiohttp.web.Response(text=_render_home_html(), content_type="text/html")


async def _handle_edit_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    post_type = request.query.get("type", "")
    if post_type not in rotation_schema.ROTATION_SCHEMAS:
        return aiohttp.web.Response(
            status=404, text=f"Unknown rotation type {post_type!r}."
        )
    # Re-bind to the canonical allowlist key so the value reflected into the page below
    # is sourced from our constant schema table, not the raw query string (clears
    # CodeQL's reflected-XSS taint; the bootstrap JSON is additionally `<`-escaped for
    # its inline <script> context).
    post_type = next(k for k in rotation_schema.ROTATION_SCHEMAS if k == post_type)

    doc = await schemas.RotationData.get_data(post_type)
    if doc is None:
        doc = _default_doc(post_type)

    bootstrap = {"type": post_type, "data": doc, "vocab": _vocab()}
    # Escape "<" so a "</script>" in the data can't break out of the inline <script>.
    bootstrap_js = json.dumps(bootstrap).replace("<", "\\u003c")
    page = _EDITOR_HTML_PATH.read_text(encoding="utf-8").replace(
        "/*__BOOTSTRAP__*/ null", bootstrap_js
    )
    return aiohttp.web.Response(text=page, content_type="text/html")


async def _handle_preview(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Malformed request body.")
    post_type, data = _read_json_body(payload)
    if post_type not in rotation_schema.ROTATION_SCHEMAS:
        return aiohttp.web.Response(
            status=400, text=f"Unknown rotation type {post_type!r}."
        )

    try:
        rotation_schema.validate(post_type, data)
    except Exception as e:
        return aiohttp.web.Response(status=400, text=f"Document is invalid:\n{e}")

    try:
        obj = _build_domain_object(post_type, data)
        payload_out = await _render_preview(post_type, obj, _bot)
    except Exception as e:
        return aiohttp.web.Response(status=400, text=f"Could not render preview:\n{e}")
    return aiohttp.web.json_response(payload_out)


async def _handle_edit_post(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Malformed request body.")
    post_type, data = _read_json_body(payload)
    if post_type not in rotation_schema.ROTATION_SCHEMAS:
        return aiohttp.web.Response(
            status=400, text=f"Unknown rotation type {post_type!r}."
        )

    try:
        rotation_schema.validate(post_type, data)
    except Exception as e:
        return aiohttp.web.Response(status=400, text=f"Document is invalid:\n{e}")

    # Hard gate: the document must build into its domain object (catches structural
    # issues the schema alone doesn't, e.g. a bad reference_date that parses but not as
    # a date).
    try:
        _build_domain_object(post_type, data)
    except Exception as e:
        return aiohttp.web.Response(status=400, text=f"Document is unusable:\n{e}")

    if rotation_schema.is_world_activity(post_type):
        _bake_item_links(data)

    await schemas.RotationData.set_data(post_type, data)
    logger.info("Rotation data for %s saved via web editor", post_type)
    return aiohttp.web.Response(text="Saved")


def _bake_item_links(data: dict[str, t.Any]) -> None:
    """Resolve a legacy doc's weapon values to light.gg URLs, in place (``item_links``).

    Server-owned: recomputed on every save. If the manifest index isn't warm yet the doc
    just saves without links — they appear on a later save (or the backfill script)."""
    data.pop("item_links", None)
    if not item_index.ready():
        return
    links: dict[str, str] = {}
    for value in weapon_values(data):
        url = item_index.resolve_light_gg_url(value)
        if url:
            links[value] = url
    if links:
        data["item_links"] = links


async def _handle_reset(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Reset a world-activity rotation to its committed seed document ("defaults").

    The recovery path for a stored doc that has gone bad (e.g. an unparseable row a
    command surfaces as an error): the operator resets it here to the known-good seed.
    Only world-activity types ship a seed; the seed already carries its own baked
    ``item_links`` so it's saved verbatim (no manifest access needed)."""
    try:
        payload = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Malformed request body.")
    post_type = str(payload.get("type", ""))
    if not rotation_schema.is_world_activity(post_type):
        return aiohttp.web.Response(
            status=400,
            text=f"{post_type!r} has no committed defaults to reset to.",
        )

    key = post_type.removeprefix(rotation_schema.ROTATION_SLUG_PREFIX)
    doc = load_seed_doc(key)
    if doc is None:
        return aiohttp.web.Response(
            status=404, text=f"No committed seed document for {post_type!r}."
        )

    # Sanity-gate the seed (it's committed, but a broken seed must not overwrite live
    # data): it has to validate and build before we persist it.
    try:
        rotation_schema.validate(post_type, doc)
        _build_domain_object(post_type, doc)
    except Exception as e:
        return aiohttp.web.Response(
            status=500, text=f"Committed seed for {post_type!r} is itself invalid:\n{e}"
        )

    await schemas.RotationData.set_data(post_type, doc)
    logger.info(
        "Rotation data for %s reset to committed defaults via web editor", post_type
    )
    return aiohttp.web.Response(text="Reset to defaults")


async def _handle_search(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Autocomplete for the editor: manifest weapon/armor items matching ``?q=``."""
    query = request.query.get("q", "")
    kind = request.query.get("kind") or None
    return aiohttp.web.json_response(item_index.search(query, kind=kind))


def register_rotation_routes(app: aiohttp.web.Application) -> None:
    """Add the rotation editor routes to the shared persistent app."""
    app.router.add_get("/rotation", _handle_home_get)
    app.router.add_get("/rotation/edit", _handle_edit_get)
    app.router.add_post("/rotation/edit", _handle_edit_post)
    app.router.add_post("/rotation/preview", _handle_preview)
    app.router.add_post("/rotation/reset", _handle_reset)
    app.router.add_get("/rotation/search", _handle_search)


web.register_routes(register_rotation_routes)


# Hold a strong reference to the background warm task: the event loop keeps only a weak
# ref to a bare create_task(), so without this the task can be garbage-collected —
# and cancelled — mid-download, leaving the index cold for the whole process.
_warm_tasks: set[asyncio.Task[None]] = set()

#: The live bot, stashed by the StartedEvent listener so the preview can fetch guild
#: emoji for the render (``preview_emoji_dict`` degrades to escaped ``:name:`` if None).
_bot: CachedFetchBot | None = None


@loader.listener(h.StartedEvent)
async def _warm_item_index(
    _event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
) -> None:
    """Build the manifest weapon/armor index in the background (for autocomplete + link
    baking), so requests never block on the (large) manifest download; also stash the
    live bot so the preview can fetch guild emoji for the rendered post."""
    global _bot
    _bot = bot
    task = asyncio.create_task(item_index.warm(schemas.BungieCredentials.api_key))
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)


web.register_card(
    web.Card(
        "Rotation Editor",
        "Edit rotation post data (Xûr, weekly rotations, …)",
        "/rotation",
    )
)


# There is deliberately no slash command here. The editor is reached from the web
# control panel's card grid (registered above); ``/control_panel`` is the single Discord
# entry point into every web tool.
