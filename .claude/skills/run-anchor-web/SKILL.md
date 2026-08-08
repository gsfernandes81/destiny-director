---
name: run-anchor-web
description: Run, launch, start, drive or screenshot the anchor bot's web UI (the aiohttp app in dd/anchor/web.py) headless in a real browser — the rotation editor, weekly-reset and trials post forms, the CV2 builder canvas and its publish confirmation, and the mirror log. Use to see a change working in the real app rather than in tests, to smoke-test the Discord message preview renderer, or to get screenshots of any web page this repo serves.
---

# Run the anchor web UI

`dd.anchor` is a Discord bot that also serves an aiohttp web app (`dd/anchor/web.py`) —
rotation editing, two hybrid-post authoring forms, a Components V2 message builder, and
the mirror log. Every one of those pages renders a Discord message preview through the
shared client renderer (`dd/anchor/web_static/cv2_render.js`), so this is the surface to
check whenever preview, markdown, or CV2 rendering changes.

**The driver is `.claude/skills/run-anchor-web/driver.py`.** It boots the real app and
drives it in Chromium. All paths below are relative to the repo root.

Do **not** reach for `make run-anchor-local` — that starts the whole bot, Discord
gateway included, and needs a real token and a MySQL server. Neither exists in a
container. The driver skips the gateway entirely (see Gotchas).

## Prerequisites

Nothing to `apt-get`. Chromium ships at `/opt/pw-browsers/` and the driver finds it;
Playwright and aiohttp are already dev dependencies (`uv sync` if the venv is cold).

You need a `.env`, because `dd/common/cfg.py` validates required env **at import time**
and raises `ValueError` without one. Dummy values are fine — the same set CI uses
(`.github/workflows/ci.yml`). If `.env` is missing:

```bash
cat > .env <<'EOF'
MYSQL_SSL=false
MYSQL_URL=mysql://user:pass@localhost/dd
SHEETS_PROJECT_ID=ci
SHEETS_PRIVATE_KEY_ID=ci
SHEETS_PRIVATE_KEY=ci
SHEETS_CLIENT_EMAIL=ci@example.com
SHEETS_CLIENT_ID=ci
SHEETS_CLIENT_X509_CERT_URL=https://example.com/cert
SHEETS_LS_URL=https://example.com/ls
EOF
```

`.env` is gitignored. No build step — the web assets are served as-is, no bundler.

## Run (agent path)

```bash
uv run --env-file .env python .claude/skills/run-anchor-web/driver.py
```

Boots the app on `127.0.0.1:8813`, seeds the DB, then walks every surface. Roughly 25s.
Output is one line per page plus what each preview host actually drew:

```
anchor web app listening on http://127.0.0.1:8813
  /                                          HTTP 200
  /rotation/edit?type=lost_sector            HTTP 200
  rotation post wall: {'roots': 4, 'containers': 4, 'texts': 4, 'buttons': 8, 'code': 0, 'imgs': 4, …}
  /weekly_reset                              HTTP 200
  /weekly_reset preview: {'roots': 1, 'containers': 1, 'texts': 1, 'buttons': 1, …}
  /trials                                    HTTP 200
  /trials preview: {'roots': 1, 'containers': 1, 'texts': 1, 'buttons': 2, …}
  /cv2-builder/runskill000000000000000000000000 HTTP 200
  builder canvas: {'roots': 1, 'containers': 1, 'texts': 2, 'buttons': 2, 'code': 1, …}
  publish confirmation: {'roots': 1, 'containers': 1, 'texts': 2, 'buttons': 2, 'code': 1, …}
  /mirror-logs                               HTTP 200

screenshots: /tmp/anchor-web-shots
CSP violations: none
problems: none
```

**Exit code is 1** if any page returned non-200, any script threw, any preview host drew
zero `.cv2-root`s, or the page logged a CSP violation. The element counts are the useful
signal — `roots: 0` means the page loaded but the renderer drew nothing, which no HTTP
status would have told you.

**Look at the screenshots.** They land in `/tmp/anchor-web-shots/` (`--out` to change).
`05_confirm.png` is the highest-value one: it shows the builder canvas and the publish
dialog at once, and those two must render *identically* — same accent bar, headings,
bullets, section-with-accessory, separator, button row. That agreement is the whole point
of the shared renderer, and the browser tests deliberately assert behaviour rather than
appearance, so nothing else checks it.

One surface at a time:

```bash
uv run --env-file .env python .claude/skills/run-anchor-web/driver.py --surface builder
```

`--surface` takes `all` (default), `home`, `rotation`, `weekly_reset`, `trials`,
`builder`, `mirror_log`, or a comma list. `--port` moves it off 8813.

### Poking it yourself

```bash
uv run --env-file .env python .claude/skills/run-anchor-web/driver.py --serve
```

Starts the app, seeds it, and blocks. Then `curl` it or point your own Playwright at it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8813/          # 200
curl -sI http://127.0.0.1:8813/static/shared.css | grep -i content-sec   # the real CSP
```

The seeded CV2 draft is at `/cv2-builder/runskill000000000000000000000000`.

## Test

```bash
make check        # lint + typecheck + pytest + node --test
make test-js      # the JS unit tests alone
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  uv run --env-file .env python -m pytest -m browser -q
```

Browser tests are excluded from `make test`'s default selection and need that env var, or
they skip. The rest of the suite does not need a browser.

## Gotchas

- **Env must be set before `dd.common.cfg` is imported.** `cfg` reads and validates at
  module scope, so `os.environ[...] = ...` after any `dd.*` import is too late. The driver
  sets `TEST_ENV` / `DEV_AUTH_USER_ID` at the top of the file, above its own imports, and
  the `# noqa: E402` on those imports is deliberate.
- **The auth bypass is triple-gated** (`web_auth._dev_bypass_active`): `TEST_ENV` set,
  `DEV_AUTH_USER_ID` set, **and** `public_base_url` empty. The third gate is why the
  driver *pops* `PUBLIC_BASE_URL` and `RAILWAY_PUBLIC_DOMAIN` — a non-empty one means
  "internet-facing", where the bypass stays inert on purpose. Without all three you get
  a 302 to Discord OAuth on every page.
- **Routes register at import time.** `web.register_routes(...)` runs at module scope in
  each extension, so you *import* `dd.anchor.extensions.rotation_editor` and friends —
  there is nothing to call. Forget one and its pages 404 with no other symptom.
- **`web_auth` is not optional.** `web.start()` raises if `app.middlewares` is empty,
  because the auth middleware is the app's only security boundary. Import it or the app
  refuses to serve.
- **The DB is MySQL-only in config.** `cfg._db_urls` hardcodes `mysql+asyncmy`, and there
  is no MySQL in a container. `schemas.configure_test_db(engine)` swaps in SQLite — the
  same thing `dd/anchor/tests/conftest.py` does. Don't try to point `MYSQL_URL` at SQLite.
- **`Cv2Draft.create` takes `id=`, `created_by=`, `action=`** — the id is caller-supplied
  (a UUID4 hex string), and `action` must be one of `post` / `edit` / `copy`. There is no
  `owner_id` or `purpose`.
- **The builder's publish button is `[data-a="publish"]`.** There is no `.cv2b-publish`
  class; the builder keys its chrome on `data-` attributes throughout.
- **Pin the browser timezone.** `<t:UNIX:X>` renders in the *viewer's* zone, so a context
  without `timezone_id="UTC"` shows different wall-clock times run to run — and makes any
  screenshot comparison useless. `dd/anchor/tests/test_renderer_dom.py` pins it the same
  way, for the same reason.
- **Emoji render as raw `:shortcode:` text** (`:LS:`, `:trials:`). Expected: resolving
  them needs a live bot to fetch the guild emoji dict, and there is no gateway here. Not
  a rendering bug.
- **Bungie API calls log a 403 traceback** on the weekly-reset form (manifest weapon
  pool). Expected without an API key; the page still renders. Filter the log noise with
  `grep -vE '^20[0-9]{2}-'` if it's in the way.
- **`file://` does not work for these pages.** They reference `/static/...` absolutely, so
  a page opened off disk loads no CSS or JS and hangs waiting for `window.initCv2Builder`.
  Serve over HTTP. (`dd/anchor/web_static/tests/builder_harness.html` is the exception —
  it uses relative paths precisely so browser tests can load it over `file://`.)

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ValueError: Environment variable '…' not found.` | No `.env`, or you ran without `--env-file .env`. See Prerequisites. |
| `RuntimeError: Anchor web app has no middleware registered` | You didn't import `web_auth`. |
| Every page 302s to `discord.com/oauth2` | A dev-bypass gate isn't met — most often `PUBLIC_BASE_URL` or `RAILWAY_PUBLIC_DOMAIN` is set in your environment. |
| `TimeoutError: waiting for locator(...)` on the builder | You're on `file://`, or the selector is a class — use `[data-a="publish"]`. |
| `IntegrityError` / `UNIQUE constraint failed: cv2_draft.id` | A stale `run.db` in the out dir. The driver deletes it each run; if you copied the driver elsewhere, `rm -rf /tmp/anchor-web-shots`. |
| `Address already in use` | A previous `--serve` is still up. `--port 8814`, or kill it. |
| Screenshots are blank/dark with no content | The app served but a script threw — check `problems:` in the output, which captures `pageerror`. |
