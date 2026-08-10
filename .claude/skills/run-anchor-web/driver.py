#!/usr/bin/env python
# Copyright © 2019-present gsfernandes81
#
# This file is part of "dd" henceforth referred to as "destiny-director".
# Licensed under the GNU AGPL v3 or later; see the project LICENSE.

"""Boot the real anchor web app headless and drive it in Chromium.

This is NOT the test suite. It starts ``dd.anchor.web.start()`` with every feature
module's routes registered exactly as production registers them — the real auth
middleware, the real security headers, the real page templates and handlers — then opens
each page in a real browser and reports what actually rendered.

Two substitutions, both forced by the container and both the same ones the test suite
already makes:

- **SQLite instead of Postgres.** The dummy ``DATABASE_URL``/``DATABASE_SSL`` in the
  ``.env`` recipe below only satisfies ``cfg``'s import-time validation — there is no
  Postgres server in a container. ``schemas.configure_test_db`` is what actually repoints
  the live engine at a throwaway SQLite file; ``dd/anchor/tests/conftest.py`` does the
  same thing.
- **The repo's own dev auth bypass instead of Discord OAuth.** Triple-gated in
  ``web_auth._dev_bypass_active`` — ``TEST_ENV`` set, ``DEV_AUTH_USER_ID`` set, and
  ``public_base_url`` empty. All three are arranged below, before ``cfg`` is imported.

Everything else is the real thing. In particular the Discord gateway is never started,
which is what makes this runnable without a bot token: routes are registered at *import*
time (``web.register_routes`` at module level), so importing the extension modules is
enough to build the whole app.

Usage (from the repo root):

    D=.claude/skills/run-anchor-web/driver.py
    uv run --env-file .env python $D
    uv run --env-file .env python $D --surface builder
    uv run --env-file .env python $D --serve

Exits non-zero if any surface failed to load, threw, or drew nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import pathlib
import sys
import tempfile
import typing as t

#: Any owner id will do — the dev bypass authenticates as whoever this names, and no
#: Discord call is ever made to check it.
USER_ID = 803358534885408809

_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument(
    "--surface",
    default="all",
    help="which flow to drive: all (default), home, rotation, weekly_reset, trials, "
    "builder, mirror_log, feeds, settings. Repeatable as a comma list.",
)
_parser.add_argument("--port", type=int, default=8813)
_parser.add_argument(
    "--out",
    default=os.path.join(tempfile.gettempdir(), "anchor-web-shots"),
    help="where full-page screenshots land",
)
_parser.add_argument(
    "--serve",
    action="store_true",
    help="start the app and block, instead of driving it — for poking by hand or with "
    "your own Playwright script",
)
ARGS = _parser.parse_args()

# MUST happen before dd.common.cfg is imported: it validates env at import time, and the
# dev-auth bypass reads all three of these. PUBLIC_BASE_URL/RAILWAY_PUBLIC_DOMAIN being
# EMPTY is a gate, not an oversight — a non-empty one means "internet-facing", where the
# bypass deliberately stays inert.
os.environ["TEST_ENV"] = "1"
os.environ["DEV_AUTH_USER_ID"] = str(USER_ID)
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from dd.common import schemas  # noqa: E402

SHOTS = pathlib.Path(ARGS.out)
BASE = f"http://127.0.0.1:{ARGS.port}"
DRAFT_ID = "runskill" + "0" * 24


def _chromium() -> str | None:
    """The bundled Chromium, or None to let Playwright find its own."""
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if explicit and os.path.exists(explicit):
        return explicit
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return found[-1] if found else None


#: The demo draft the builder surface opens. Deliberately covers the shapes that have
#: broken before: a code span and a `* ` bullet (the two drift items that motivated the
#: renderer unification), a section with a button accessory, a large separator, and an
#: action row.
DRAFT_NODES: list[dict[str, t.Any]] = [
    {
        "type": 17,
        "accent_color": 0xEC42A5,
        "components": [
            {"type": 10, "content": "# Smoke test\n- a `code` span\n* a bullet"},
            {
                "type": 9,
                "components": [{"type": 10, "content": "text beside a button"}],
                "accessory": {
                    "type": 2,
                    "style": 5,
                    "label": "Open",
                    "url": "https://example.com/go",
                },
            },
            {"type": 14, "spacing": 2},
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 5,
                        "label": "Docs",
                        "url": "https://example.com/d",
                    }
                ],
            },
        ],
    }
]


async def _seed() -> None:
    """The minimum stored state each surface needs to have something to show."""
    from dd.common import rotation_schema as rs

    await schemas.RotationData.set_data(
        "lost_sector",
        {
            "version": 1,
            "reference_date": "2023-07-20",
            "schedule": {z: ["Perdition"] for z in rs.LOST_SECTOR_ZONES},
            "sectors": [
                {
                    "name": "Perdition",
                    "shortlink_gfx": "https://kyber3000.com/perdition.png",
                    "expert": {"champions": ["Barrier"], "shields": ["Arc"]},
                    "master": {"champions": ["Overload"], "shields": ["Void"]},
                }
            ],
        },
    )
    # `action` must be one of Cv2Draft.ACTIONS ("post"/"edit"/"copy") and `id` is
    # caller-supplied, not generated.
    await schemas.Cv2Draft.create(
        id=DRAFT_ID, created_by=USER_ID, action="post", nodes=DRAFT_NODES
    )


async def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    # Start from a clean DB every run: the seed inserts a draft at a fixed id, so a
    # leftover file from the previous run collides on the primary key.
    db = SHOTS / "run.db"
    db.unlink(missing_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}", poolclass=NullPool)
    schemas.configure_test_db(engine)
    await schemas.create_all()

    # Importing a feature module registers its routes (module-level register_routes).
    # web_auth is NOT optional: web.start() refuses to serve when app.middlewares is
    # empty, because the auth middleware is the app's only security boundary.
    from dd.anchor import web
    from dd.anchor.extensions import (  # noqa: F401
        autopost_settings,
        control_panel,
        cv2_builder_page,
        mirror_log,
        rotation_editor,
        trials,
        web_auth,
        weekly_reset,
    )

    await _seed()
    await web.start(ARGS.port)
    print(f"anchor web app listening on {BASE}")

    if ARGS.serve:
        print("--serve: blocking. Ctrl-C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            await web.stop()
        return 0

    from playwright.async_api import async_playwright

    wanted = {s.strip() for s in ARGS.surface.split(",")}
    want = lambda name: "all" in wanted or name in wanted  # noqa: E731
    problems: list[str] = []
    csp: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=_chromium())
        # timezone_id is load-bearing: `<t:…>` renders in the VIEWER'S zone, so a page
        # left on the host's zone shows different wall-clock times run to run.
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1000}, timezone_id="UTC"
        )
        page = await ctx.new_page()
        page.on(
            "console",
            lambda m: (
                csp.append(m.text)
                if m.type == "error" and "Content Security Policy" in m.text
                else None
            ),
        )
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

        async def go(path: str, shot: str) -> None:
            resp = await page.goto(BASE + path)
            status = resp.status if resp else 0
            if status != 200:
                problems.append(f"{path} -> HTTP {status}")
            await page.wait_for_timeout(700)
            await page.screenshot(path=str(SHOTS / f"{shot}.png"), full_page=True)
            print(f"  {path:<42} HTTP {status}")

        async def drew(selector: str, label: str) -> None:
            """What a preview host actually rendered — counts, not a screenshot diff."""
            info = await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    return {
                      roots: el.querySelectorAll(".cv2-root").length,
                      containers: el.querySelectorAll(".cv2-container").length,
                      texts: el.querySelectorAll(".cv2-text").length,
                      buttons: el.querySelectorAll(".cv2-button").length,
                      code: el.querySelectorAll("code").length,
                      imgs: el.querySelectorAll("img").length,
                      text: (el.textContent || "").trim().slice(0, 80),
                    };
                }""",
                selector,
            )
            if not info or not info["roots"]:
                problems.append(f"{label}: nothing rendered into {selector} ({info})")
            print(f"  {label}: {info}")

        if want("home"):
            await go("/", "00_home")

        if want("rotation"):
            await go("/rotation/edit?type=lost_sector", "01_rotation_editor")
            await page.click('button[data-tab="preview"]')
            await page.wait_for_timeout(1500)
            await page.screenshot(
                path=str(SHOTS / "02_rotation_preview.png"), full_page=True
            )
            await drew("#previewBox", "rotation post wall")

        for name, path in (("weekly_reset", "/weekly_reset"), ("trials", "/trials")):
            if not want(name):
                continue
            await go(path, f"03_{name}")
            # The live previewer is debounced; give it a beat to POST and draw.
            await page.wait_for_timeout(1800)
            await page.screenshot(
                path=str(SHOTS / f"03_{name}_preview.png"), full_page=True
            )
            await drew("#previewBox", f"{path} preview")

        if want("builder"):
            await go(f"/cv2-builder/{DRAFT_ID}", "04_builder")
            await page.wait_for_timeout(900)
            await drew(".cv2b-canvas", "builder canvas")
            # The publish button is [data-a="publish"]; there is no .cv2b-publish.
            await page.click('[data-a="publish"]')
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(SHOTS / "05_confirm.png"), full_page=True)
            await drew(".cv2b-confirm-body", "publish confirmation")

        if want("mirror_log"):
            await go("/mirror-logs", "06_mirror_log")

        # The two settings pages. Both hydrate their channel pickers from a fetch that
        # needs the live bot, which there is none of here — the pickers fall back to
        # their rendered <select>, which is exactly the state worth seeing, and the
        # 503 that fetch gets is expected rather than a problem.
        if want("feeds"):
            await go("/feeds", "07_feeds")
        if want("settings"):
            await go("/settings", "08_settings")

        await browser.close()

    await web.stop()
    await engine.dispose()

    print(f"\nscreenshots: {SHOTS}")
    print(
        "CSP violations: "
        + ("\n  " + "\n  ".join(dict.fromkeys(csp)) if csp else "none")
    )
    print("problems: " + ("\n  " + "\n  ".join(problems) if problems else "none"))
    return 1 if problems or csp else 0


sys.exit(asyncio.run(main()))
