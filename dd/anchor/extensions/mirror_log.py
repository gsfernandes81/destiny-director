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

"""Mirror delivery log page for the anchor web control panel.

An owner-only page (linked from the control-panel homepage via
:func:`web.register_card`) that renders the durable ``mirror_delivery`` ledger — the
source of truth for how a mirrored announcement fanned out to its follower channels. It
is the web-native replacement for the beacon Discord "progress card": because it is
rendered on demand from the DB, there is no long-lived Discord message to supersede,
cancel or freeze.

Three routes, mirroring the ``/stats`` shell + JSON pattern:

- ``GET /mirror-logs`` serves the static shell (``web_static/mirror_log.html``); the
  page fetches its data and renders everything client-side (``mirror_log.js``).
- ``GET /mirror-logs/data`` returns recent runs as JSON, read entirely from the shared
  DB (no Discord API calls). ``?src=<src_msg_id>`` returns that run's captured version
  list for the expandable detail view (the mirrored message itself).
- ``GET /mirror-logs/render?src=<id>&v=<n>`` returns one captured version for the page
  to draw with the shared renderer (``web_static/cv2_render.js``); adding ``&diff=<m>``
  returns a word-level diff against version ``m`` as an annotated tree (see
  :func:`cv2_render.diff_payload`). Pull/stateless.

Discord snowflake ids exceed JavaScript's safe-integer range, so ids are emitted as
strings; ledger timestamps are naive-UTC wall clocks, stamped UTC here so the browser
parses them unambiguously. Authentication is the shared Discord-OAuth middleware
(``web_auth``), so this module carries no auth code.
"""

import datetime as dt
import logging
from pathlib import Path

import aiohttp.web
import lightbulb as lb

from ...common import schemas, settings
from .. import web
from ..cv2_render import diff_payload

logger = logging.getLogger(__name__)

# No commands or listeners live here, but load_extensions_strict requires every
# extension module to expose a Loader, so define an (empty) one.
loader = lb.Loader()

_PAGE_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "mirror_log.html"
)

# How far back the run list reaches, and its cap. The window keeps the query on the
# ledger's created_at prune index; the cap bounds the JSON payload.
_WINDOW_DAYS = 30
_RUN_LIMIT = 50


def _iso_utc(value: dt.datetime | None) -> str | None:
    """Stamp a naive-UTC ledger datetime as UTC ISO-8601 (or pass through ``None``)."""
    return value.replace(tzinfo=dt.UTC).isoformat() if value is not None else None


async def _collect_runs() -> dict:
    runs = await schemas.MirrorDelivery.recent_runs(
        limit=_RUN_LIMIT, within_days=_WINDOW_DAYS
    )
    # One batch lookup of each source's latest captured snapshot supplies the run-list
    # summary label + the source guild id for a jump-to-source link (retiring the last
    # bare snowflake). Sources predating the capture deploy simply have no entry.
    latest = await schemas.MirrorMessageVersion.latest_for(
        [run["src_msg_id"] for run in runs]
    )
    # Built once rather than per row — followable_name would otherwise re-resolve
    # every followable's channel id on every one of up to _RUN_LIMIT lookups below.
    # Awaited rather than get_followables_sync(): this handler is already async, so the
    # sync reader bought nothing here except skipping the TTL freshness check — a feed
    # whose channel was set (or changed) on the settings page would keep rendering as a
    # bare snowflake until some *unrelated* async getter happened to refresh the cache.
    followables = await settings.get_followables()
    for run in runs:
        # Resolve the source channel to its configured feed name (else None → the page
        # falls back to the id). followable_name returns the id itself when unknown.
        name = settings.followable_name(id=run["src_ch_id"], followables=followables)
        run["src_name"] = name if isinstance(name, str) else None
        snap = latest.get(run["src_msg_id"])
        run["summary"] = snap["summary"] if snap else None
        run["src_guild_id"] = (
            str(snap["src_guild_id"])
            if snap and snap["src_guild_id"] is not None
            else None
        )
        run["src_msg_id"] = str(run["src_msg_id"])
        run["src_ch_id"] = str(run["src_ch_id"])
        run["started"] = _iso_utc(run["started"])
        run["last_at"] = _iso_utc(run["last_at"])
    # Settled operations across the window feed the per-row op chips + the overview's
    # per-op-type daily chart (the live/in-flight op's numbers are the run row itself).
    operations = await schemas.MirrorOperationLog.recent(within_days=_WINDOW_DAYS)
    return {
        "window_days": _WINDOW_DAYS,
        "run_limit": _RUN_LIMIT,
        "runs": runs,
        "operations": [
            {
                "src_msg_id": str(op["src_msg_id"]),
                "op_type": op["op_type"],
                "version": op["version"],
                "total": op["total"],
                "delivered": op["delivered"],
                "failed": op["failed"],
                "finished_at": _iso_utc(op["finished_at"]),
            }
            for op in operations
        ],
    }


def _op_json(op: dict) -> dict:
    return {
        "op_type": op["op_type"],
        "version": op["version"],
        "started_at": _iso_utc(op["started_at"]),
        "finished_at": _iso_utc(op["finished_at"]),
        "total": op["total"],
        "delivered": op["delivered"],
        "failed": op["failed"],
        "cancelled": op["cancelled"],
        "attempts": op["attempts"],
        "failures": op["failure_refs"],
    }


async def _collect_detail(src_msg_id: int) -> dict:
    # The detail carries the mirrored *message* (the version render columns), each
    # operation's own recorded stats (create/update/delete — the durable op log), and
    # the run's current failure breakdown (for the in-flight op, which has no log row).
    versions = await schemas.MirrorMessageVersion.versions_for(src_msg_id)
    failures = await schemas.MirrorDelivery.failure_breakdown(src_msg_id)
    operations = await schemas.MirrorOperationLog.for_message(src_msg_id)
    return {
        "src_msg_id": str(src_msg_id),
        # Version snapshots power the render columns; empty pre-capture-deploy.
        "versions": [
            {
                "version": v["version"],
                "captured_at": _iso_utc(v["captured_at"]),
                "summary": v["summary"],
                "kind": v["kind"],
            }
            for v in versions
        ],
        # Per-operation stats (create/update/delete), oldest first; empty pre-deploy.
        "operations": [_op_json(op) for op in operations],
        "failures": [
            {"ref": ref, "error_class": err_class, "count": count, "sample": sample}
            for (ref, err_class, count, sample) in failures
        ],
    }


async def _handle_page(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # Auth is enforced by the web_auth middleware; this just serves the shell.
    return aiohttp.web.Response(
        text=_PAGE_HTML_PATH.read_text(encoding="utf-8"), content_type="text/html"
    )


async def _handle_data(request: aiohttp.web.Request) -> aiohttp.web.Response:
    src = request.query.get("src")
    if src is not None:
        try:
            src_msg_id = int(src)
        except ValueError:
            raise aiohttp.web.HTTPBadRequest(text="src must be an integer") from None
        return aiohttp.web.json_response(await _collect_detail(src_msg_id))
    return aiohttp.web.json_response(await _collect_runs())


def _int_param(request: aiohttp.web.Request, name: str) -> int:
    raw = request.query.get(name)
    if raw is None:
        raise aiohttp.web.HTTPBadRequest(text=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise aiohttp.web.HTTPBadRequest(text=f"{name} must be an integer") from None


async def _handle_render(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """One captured version of a source message, for the page to draw.

    ``?src=<id>&v=<n>`` returns version ``n``; adding ``&diff=<m>`` returns a word-level
    diff against version ``m`` instead. Pull/stateless — no live message, no lifecycle.

    Two shapes, because the two halves are at different stages of
    ``docs/architecture.md``, "Rendering a message on the web":

    - ``{"kind": "snapshot", "payload": …, "message_kind": "cv2"|"classic"}`` — the
      captured payload itself, drawn client-side by the shared renderer.
    - ``{"kind": "diff", "diff": …}`` — the *annotated* tree: the alignment stays here
      (it needs ``difflib``), but what ships is its verdict, which the same renderer
      draws. See :func:`cv2_render.diff_payload`.

    **This is the untrusted sink.** A payload here is whatever some other server's
    message contained, so the renderer's guarantees are what stand between it and the
    reader: text reaches the DOM through ``textContent``, a URL is ``http(s)``-validated
    at the one place it becomes an attribute, and only markdown from ``renderMd`` —
    which escapes by construction — reaches ``innerHTML``. The corpus in
    ``dd/anchor/preview_fixtures`` holds that line from both languages, injection probes
    included.
    """
    src_msg_id = _int_param(request, "src")
    version = _int_param(request, "v")
    new = await schemas.MirrorMessageVersion.get_version(src_msg_id, version)
    if new is None:
        raise aiohttp.web.HTTPNotFound(text="No snapshot for that version.")

    diff = request.query.get("diff")
    if diff is not None:
        old = await schemas.MirrorMessageVersion.get_version(
            src_msg_id, _int_param(request, "diff")
        )
        if old is None:
            raise aiohttp.web.HTTPNotFound(
                text="No snapshot for the diff-against version."
            )
        return aiohttp.web.json_response(
            {
                "kind": "diff",
                "diff": diff_payload(
                    new["payload"], new["kind"], old["payload"], old["kind"]
                ),
            }
        )
    return aiohttp.web.json_response(
        {
            "kind": "snapshot",
            "payload": new["payload"],
            "message_kind": new["kind"],
        }
    )


def register_mirror_log_routes(app: aiohttp.web.Application) -> None:
    """Add the mirror-log routes to the shared persistent app."""
    app.router.add_get("/mirror-logs", _handle_page)
    app.router.add_get("/mirror-logs/data", _handle_data)
    app.router.add_get("/mirror-logs/render", _handle_render)


web.register_routes(register_mirror_log_routes)
web.register_card(
    web.Card(
        "Mirror logs",
        "How each mirrored post fanned out to its follower channels",
        "/mirror-logs",
    )
)
