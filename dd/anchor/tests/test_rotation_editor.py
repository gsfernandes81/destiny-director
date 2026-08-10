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

# Rotation editor: the aiohttp route handlers (homepage / GET page / preview / save),
# exercised against the SQLite test DB from conftest with lightweight fake requests (no
# live server). Authentication is handled centrally by the web_auth middleware (covered
# in test_web_auth.py), so these handlers assume an already-authenticated request.

import json
import typing as t

import aiohttp.web
import pytest

from dd.anchor.extensions import rotation_editor as editor
from dd.common import (
    rotation_schema as rs,
    schemas,
    settings,
)
from dd.sector_accounting import sector_accounting

pytestmark = pytest.mark.asyncio

ZONES = rs.LOST_SECTOR_ZONES


def _doc(first: str = "Alpha") -> dict[str, t.Any]:
    return {
        "version": 1,
        "reference_date": "2023-07-20",
        "schedule": {z: [first] for z in ZONES},
        "sectors": [
            {
                "name": first,
                "shortlink_gfx": "https://x/a",
                "expert": {"champions": ["Barrier"], "shields": ["Arc"]},
                "master": {"champions": [], "shields": []},
            }
        ],
    }


def _xur_doc(name: str = "Nessus, Watcher's Grave") -> dict[str, t.Any]:
    return {
        "version": 1,
        "locations": [
            {
                "api_location_name": name,
                "friendly_location_name": "Watcher's Grave, Nessus",
                "link": "https://kyber3000.com/x",
            }
        ],
    }


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in for the handlers."""

    def __init__(
        self,
        query: dict[str, str] | None = None,
        body: t.Any = None,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        raise_json: bool = False,
    ) -> None:
        self.query = query or {}
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._body = body
        self._raise_json = raise_json

    async def json(self) -> t.Any:
        if self._raise_json:
            raise ValueError("bad json")
        return self._body


def _req(
    query: dict[str, str] | None = None,
    body: t.Any = None,
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    raise_json: bool = False,
) -> aiohttp.web.Request:
    return t.cast(
        aiohttp.web.Request,
        _FakeRequest(
            query=query,
            body=body,
            cookies=cookies,
            headers=headers,
            raise_json=raise_json,
        ),
    )


# --- GET /rotation (homepage) ----------------------------------------------------


async def test_home_lists_all_rotation_types():
    resp = await editor._handle_home_get(_req())
    assert resp.status == 200
    body = resp.text or ""
    # Every registered rotation type is linked, and a friendly title renders.
    for slug in rs.ROTATION_SCHEMAS:
        assert f"type={slug}" in body
    assert "Lost sector rotation" in body


# --- GET /rotation/edit ----------------------------------------------------------


def _bootstrap(page: str) -> t.Any:
    """The parsed contents of the page's ``<script type="application/json">`` block.

    The block is a JSON one so CSP can forbid inline *executable* scripts outright (see
    ``web.SECURITY_HEADERS``), which means the substituted text now has to be valid JSON
    rather than merely valid JavaScript — worth asserting rather than assuming.
    """
    marker = '<script type="application/json" id="bootstrap">'
    start = page.index(marker) + len(marker)
    return json.loads(page[start : page.index("</script>", start)])


async def test_edit_get_renders_page():
    resp = await editor._handle_edit_get(_req(query={"type": "lost_sector"}))
    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = resp.text
    assert body is not None
    assert "/*__BOOTSTRAP__*/ null" not in body

    boot = _bootstrap(body)
    assert boot["type"] == "lost_sector"
    assert "data" in boot and "vocab" in boot


async def test_bootstrap_survives_a_closing_script_tag_in_the_data():
    """A ``</script>`` in stored data must not break out of the block.

    The handler escapes ``<`` to ``\\u003c`` before substituting, which is both valid
    JSON and inert inside the block. This is the case that escaping exists for, and
    until now it was asserted only by the *absence* of the placeholder.
    """
    doc = _doc()
    doc["sectors"][0]["name"] = "Alpha</script><script>alert(1)</script>"
    await schemas.RotationData.set_data("lost_sector", doc)

    resp = await editor._handle_edit_get(_req(query={"type": "lost_sector"}))
    body = resp.text or ""

    # Exactly one script block carries data, and the payload round-trips intact.
    assert body.count('<script type="application/json"') == 1
    assert "</script><script>alert(1)" not in body
    assert _bootstrap(body)["data"]["sectors"][0]["name"] == doc["sectors"][0]["name"]


async def test_edit_get_unknown_type_is_404():
    resp = await editor._handle_edit_get(_req(query={"type": "nope"}))
    assert resp.status == 404


# --- POST /rotation/preview ------------------------------------------------------


async def test_preview_renders_valid_document():
    resp = await editor._handle_preview(
        _req(body={"type": "lost_sector", "data": _doc()})
    )
    assert resp.status == 200
    # The preview is a wall of real posts now: each entry carries the node tree
    # build_cv2 would send, which the page draws with the shared renderer. It used to
    # be server-rendered `.post-*` markup approximating the same thing.
    payload = json.loads(resp.text or "{}")
    assert payload["kind"] == "wall"
    assert payload["posts"], "expected at least one upcoming post"
    body = json.dumps(payload["posts"])
    assert "Alpha" in body
    assert "World Lost Sectors" in body  # the post header, from lost_sector.build_body


def _patch_ls_image_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    async def _get_image_url() -> str:
        return url

    monkeypatch.setattr(settings, "get_lost_sector_image_url", _get_image_url)


async def test_lost_sector_preview_omits_a_blank_image_url(
    monkeypatch: pytest.MonkeyPatch,
):
    # The image url is a DB setting that may legitimately be blank now. A PostSpec image
    # is absent-or-a-url: an empty string would reach the previewer as an image node
    # with a blank src, which the browser resolves against the page and re-fetches.
    _patch_ls_image_url(monkeypatch, "")
    posts = await editor._lost_sector_posts(
        sector_accounting.Rotation.from_json(_doc()), details_enabled=False
    )
    assert posts
    assert all(spec.image_url is None for _label, spec in posts)


async def test_lost_sector_preview_carries_a_configured_image_url(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_ls_image_url(monkeypatch, "https://kyberscorner.com/ls.gif")
    posts = await editor._lost_sector_posts(
        sector_accounting.Rotation.from_json(_doc()), details_enabled=False
    )
    # Days with data carry the url; a TBC day is a bare "no data" post with no image.
    assert any(
        spec.image_url == "https://kyberscorner.com/ls.gif" for _label, spec in posts
    )
    assert all(spec.image_url != "" for _label, spec in posts)


async def test_preview_rejects_invalid_document():
    bad = _doc()
    bad["reference_date"] = "not-a-date"
    resp = await editor._handle_preview(_req(body={"type": "lost_sector", "data": bad}))
    assert resp.status == 400


# --- POST /rotation/edit ---------------------------------------------------------


async def test_save_persists_and_allows_repeated_edits():
    resp = await editor._handle_edit_post(
        _req(body={"type": "lost_sector", "data": _doc("Saved")})
    )
    assert resp.status == 200
    stored = await schemas.RotationData.get_data("lost_sector")
    assert stored is not None
    assert stored["sectors"][0]["name"] == "Saved"
    # A second save still works (handlers hold no per-request auth state).
    resp2 = await editor._handle_edit_post(
        _req(body={"type": "lost_sector", "data": _doc("Again")})
    )
    assert resp2.status == 200
    stored2 = await schemas.RotationData.get_data("lost_sector")
    assert stored2 is not None
    assert stored2["sectors"][0]["name"] == "Again"


async def test_save_rejects_invalid_document_without_writing():
    bad = _doc()
    del bad["sectors"]
    resp = await editor._handle_edit_post(
        _req(body={"type": "lost_sector", "data": bad})
    )
    assert resp.status == 400


async def test_malformed_body_is_a_400():
    resp = await editor._handle_edit_post(_req(body=None, raise_json=True))
    assert resp.status == 400


# --- xur_location (a second post type through the same handlers) ------------------


async def test_default_doc_for_xur_location():
    assert editor._default_doc("xur_location") == {"version": 1, "locations": []}


async def test_xur_preview_renders_resolved_locations():
    resp = await editor._handle_preview(
        _req(body={"type": "xur_location", "data": _xur_doc()})
    )
    assert resp.status == 200
    body = resp.text or ""
    # Friendly name (apostrophe HTML-escaped) + the link both render.
    assert "Grave, Nessus" in body
    assert "https://kyber3000.com/x" in body


async def test_xur_save_persists():
    resp = await editor._handle_edit_post(
        _req(body={"type": "xur_location", "data": _xur_doc("Tower")})
    )
    assert resp.status == 200
    stored = await schemas.RotationData.get_data("xur_location")
    assert stored is not None
    assert stored["locations"][0]["api_location_name"] == "Tower"


async def test_xur_save_rejects_invalid_document_without_writing():
    bad = _xur_doc()
    del bad["locations"]
    resp = await editor._handle_edit_post(
        _req(body={"type": "xur_location", "data": bad})
    )
    assert resp.status == 400


# --- trials_loot (standalone weapons-only set pool) ------------------------------


def _trials_loot_doc() -> dict[str, t.Any]:
    return {
        "version": 1,
        "schedule": ["Pool B", "Pool A"],
        "sets": [
            {"name": "Pool A", "weapons": ["Astral Horizon", "The Scholar"]},
            {"name": "Pool B", "weapons": ["The Immortal (Submachine Gun)"]},
        ],
    }


async def test_default_doc_for_trials_loot_is_the_baked_default():
    doc = editor._default_doc("trials_loot")
    assert doc == rs.trials_loot_default_doc()
    # It's populated (not blank), so the editor opens with the current loop.
    assert doc["sets"] and doc["schedule"]


async def test_trials_loot_is_not_a_world_activity():
    # Must stay out of the world-activity machinery (no bake/reset/date-anchor).
    assert not rs.is_world_activity("trials_loot")
    assert "trials_loot" in rs.ROTATION_SCHEMAS


async def test_trials_loot_preview_expands_the_schedule():
    resp = await editor._handle_preview(
        _req(body={"type": "trials_loot", "data": _trials_loot_doc()})
    )
    assert resp.status == 200
    body = resp.text or ""
    # The schedule renders in order (Pool B first), listing each set's weapons.
    assert body.index("Pool B") < body.index("Pool A")
    assert "The Immortal (Submachine Gun)" in body
    assert "Astral Horizon" in body


async def test_trials_loot_save_persists():
    resp = await editor._handle_edit_post(
        _req(body={"type": "trials_loot", "data": _trials_loot_doc()})
    )
    assert resp.status == 200
    stored = await schemas.RotationData.get_data("trials_loot")
    assert stored is not None
    assert stored["schedule"] == ["Pool B", "Pool A"]
    # Not a world activity → no item_links baking on save.
    assert "item_links" not in stored


async def test_trials_loot_save_rejects_schedule_naming_unknown_set():
    bad = _trials_loot_doc()
    bad["schedule"] = ["Pool A", "Ghost Pool"]
    resp = await editor._handle_edit_post(
        _req(body={"type": "trials_loot", "data": bad})
    )
    assert resp.status == 400
    assert "Ghost Pool" in (resp.text or "")
    # The hard gate blocked the write.
    stored = await schemas.RotationData.get_data("trials_loot")
    assert stored is None or stored.get("schedule") != ["Pool A", "Ghost Pool"]


async def test_trials_loot_save_rejects_set_with_armor_key():
    # The schema is weapons-only (additionalProperties: false) — armor must not slip in.
    bad = _trials_loot_doc()
    bad["sets"][0]["armor"] = ["Some Helmet"]
    resp = await editor._handle_edit_post(
        _req(body={"type": "trials_loot", "data": bad})
    )
    assert resp.status == 400


# --- the landing page's group 2 ------------------------------------------------------
#
# The editor contributes one row per *subject* rather than one row for itself, so an
# admin who came to fix the lost sector schedule reaches it without passing through an
# index first. That makes five hrefs whose `type=` has to stay real.


async def test_data_cards_name_real_rotation_types() -> None:
    # A renamed or mistyped slug would render a row that 404s on click, and nothing else
    # in the suite would notice — the card registry is dev-authored copy, not data.
    featured = [
        href.split("type=", 1)[1]
        for _slug, _title, _desc in editor._FEATURED_ROTATIONS
        for href in [f"/rotation/edit?type={_slug}"]
    ]

    assert featured == ["lost_sector", "xur_location", "trials_loot", "iron_banner"]
    for slug in featured:
        assert slug in rs.ROTATION_SCHEMAS, slug


async def test_the_featured_rotations_are_not_the_legacy_pile() -> None:
    # The four are promoted precisely because they are not world-activity pages; if one
    # ever were, it would be listed twice — once by name and once behind the last row.
    for slug, _title, _desc in editor._FEATURED_ROTATIONS:
        assert slug not in rs.WORLD_ACTIVITY_SLUGS, slug
