# Architecture & code patterns

How the Destiny Director codebase is laid out and the patterns to reuse when adding to
it. `CLAUDE.md` carries the short version; this is the deep-dive. Read the relevant
section **before** adding a command, touching the DB layer, or building a message.

## Package layout

Everything lives under `dd/`:

- `dd.beacon` — the main bot. `__main__.py` boots it; the **mirror subsystem**
  (`mirror_worker.py` + `mirror_core.py`, see below), `nav.py` (paged-message system),
  `utils.py`, `help_details.py`, and **`extensions/`** (the command modules).
- `dd.anchor` — the "secondary" bot, but substantial: `__main__.py`, an aiohttp **web UI**
  (`web.py` + `web_static/`) for rotation editing and post authoring, the
  **Components V2** model (`cv2_nodes.py`, `cv2_raw.py`) and its structural diff
  (`cv2_render.py`), `embeds.py`, `search_json.py`, and `extensions/` — including the
  **`bungie_api/`** subpackage (Bungie OAuth + API client).
  > The `cv2_*` modules are Discord **Components V2**, not OpenCV — an easy misread, and
  > one this doc used to make.
- `dd.common` — shared infrastructure (far more than schemas): `cfg.py`, `bot.py`,
  `auth.py`, `components.py`, `discord_logging.py`, `extension_loader.py`, `lifecycle.py`,
  `schemas.py`, `utils.py`, plus domain helpers (`rotation_schema.py`, `lost_sector.py`).
- `dd.hmessage` — the `HMessage` message representation (see below).
- `dd.sector_accounting` — Destiny sector/rotation domain data.

**Implicit namespace packages.** There is intentionally no `dd/__init__.py`,
`dd/common/__init__.py`, or `dd/anchor/__init__.py`. Only `beacon`, `hmessage`,
`sector_accounting`, and the `extensions/` subpackages carry `__init__.py`. Don't add the
missing ones to "fix" imports.

`migrations/` (Alembic `env.py` + revision scripts) sits at the **repo root**, not under
`dd/`.
Design context lives in `docs/` (e.g. `v2_v3_behavior_audit.md`, `decisions/`) and
`plans/`.

## Adding a command (lightbulb v3)

Commands are **classes**, not v2 decorators. A command module under
`dd/<bot>/extensions/`:

1. Declares a module-level `loader = lb.Loader()`.
2. Defines `class Foo(lb.SlashCommand, name="…", description="…")` with an
   `@lb.invoke async def invoke(self, ctx: lb.Context)` method.
3. Registers it with `loader.command(Foo)`.
4. Adds listeners with `@loader.listener(h.StartedEvent)`.

The canonical, copy-me example is **`dd/beacon/extensions/template.py`**. Its shape:

```python
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage
from ...common import cfg
from ..nav import NavigatorView, NavPages

loader = lb.Loader()

class SlashCommand(lb.SlashCommand, name="xur", description="…"):
    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        navigator = NavigatorView(pages=pages)
        await navigator.send(ctx)

loader.command(SlashCommand)
```

A new extension just needs to be a module under `extensions/` that exposes a `loader`.
Extensions are loaded by **`dd/common/extension_loader.py::load_extensions_strict()`**,
which pre-imports each module so a broken extension logs CRITICAL instead of silently
vanishing (lightbulb's default swallows `ImportError`).

Owner-only commands: gate with the `owner_only` hook from `dd/common/auth.py`
(`hooks=[owner_only]` on the command, or via `client_from_app(..., hooks=[owner_only])`),
paired with `owner_check_error_handler`.

## Database access

- Import the session factory: `from dd.common.schemas import db_session`.
- Use it as an async context manager with a transaction:

  ```python
  async with db_session() as session, session.begin():
      ...
  ```

- For helper functions that should accept an optional caller-supplied session, decorate
  with **`@ensure_session(db_session)`** from `dd/common/utils.py` (see the many call
  sites in `schemas.py`).
- **Do not build engines/sessionmakers by hand.** `db_session` is a rebindable
  `_SessionmakerProxy` (`schemas.py`); the test suite swaps its target via
  `configure_test_db()` to point at a throwaway SQLite DB. Hand-rolled engines bypass that
  and can hit the real database.

Schemas are defined in `dd/common/schemas.py`, which also serves as Alembic's target
metadata (autogenerate diffs the live database against it — see `migrations/env.py`) and
a management CLI (`--create-all` / `--destroy-all`). `--destroy-all` refuses a non-local
DB unless `ALLOW_REMOTE_SCHEMA_DESTROY=1` — never bypass this guard.

## Building messages — `HMessage`

`HMessage` (`from dd.hmessage import HMessage, MultiImageEmbedList`) is a mutable,
mergeable representation of a Discord message (content + embeds + attachments). It's used
throughout for building, mirroring, and announcing. Prefer it over hand-assembling raw
`hikari` embeds. Typical use (from `template.py`):

```python
from dd.common.utils import accumulate

msg = (
    accumulate([HMessage.from_message(m) for m in messages])
    .merge_content_into_embed()
    .merge_attachements_into_embed(default_url=cfg.default_url)
)
```

For **Components V2** posts, use `build_container()` from `dd/common/components.py`.

## Rendering a message on the web — one renderer

Every web surface that shows what a Discord message will look like — the CV2 builder's
canvas and its publish confirmation, the mirror log's snapshot and diff panes, the
weekly-reset / trials / rotation form previews, and the per-feed page's post preview —
draws through **one** renderer:
`dd/anchor/web_static/cv2_render.js`. It walks a CV2 node tree (or a classic
content+embeds payload) into a plain-data *spec*, which two back ends turn into
something: `serialize()` for an HTML string (pure, so `node --test` can assert it) and
`materialize()` for real DOM (what pages use).

The server sends **data, not markup**. Routes hand over the node tree — sanitized for
the builder's confirmation, aligned-and-annotated for the mirror log's diff — and the
page renders it. Rendering had to end up client-side because the builder canvas repaints
per keystroke and cannot round-trip; having it live in exactly one place is what stops
the two drifting, which they had.

Two consequences worth knowing before touching any of it:

- **The mirror log renders untrusted content** — other servers' captured posts. Safety
  is structural rather than remembered: text reaches the DOM via `textContent`, a URL is
  `http(s)`-validated at the single place it becomes an attribute, a colour is assigned
  as a style *property*, and only `cv2_model.renderMd` output ever reaches `innerHTML`.
- **`dd/anchor/preview_fixtures/`** is the golden corpus. Python asserts the data it
  produces, JavaScript asserts the drawing, and every expectation is audited against the
  tag/attribute/URL whitelist. Change a render and you regenerate it — deliberately, and
  reading the diff. See that directory's README.

The structural diff stays in Python (`cv2_render.py`) because it needs `difflib`; what
ships is its verdict, as annotations the shared renderer draws.

## Paged messages — `dd/beacon/nav.py`

For multi-page / navigable responses use the nav system rather than rolling your own:
`NavPages` (a date-range-keyed page store), `NavigatorView` (the interactive view), and
`make_navigator_command()` (builds a navigator-backed command). `Pages.from_channel(...)`
pulls messages from a followable channel; see `template.py`.

## Mirror subsystem — `dd/beacon/mirror_worker.py` + `mirror_core.py`

The mirror subsystem fans one source-channel message out to N legacy destination
channels (`legacy=True` rows in `mirrored_channel`; non-legacy rows are native Discord
channel-follows, untouched by this). It is a **durable delivery ledger** driven by a
single worker (there is only ever one process — the ceiling is Discord's global ~50
req/s, so concurrency is small):

- **`mirror_delivery`** (`dd/common/schemas.py`) — one row per `(src_msg_id, dest_ch_id)`
  carrying `desired_version` / `applied_version` / `state` / `deleted` / `crosspost_state`.
  It stores *intent*, never content — content is fetched fresh from Discord at delivery
  time. States are `PENDING / DELIVERED / FAILED / CANCELLED`; there is **no CLAIMED
  state** — a row being worked right now is tracked only in the one worker's memory, so a
  crash just leaves it PENDING for the next pick.
- **Gateway handlers** (`extensions/mirror.py`) do one transactional enqueue each —
  `enqueue_send` (create), `bump_for_edit` (edit → bump `desired_version`), `mark_deleted`
  (delete) — then start a progress card and nudge the worker.
- **The worker** (`mirror_worker.py`, one `MirrorWorker` per process, started from
  `StartedEvent`) runs a single **pick → process → flush** loop: it `pick_batch`es due
  rows (biggest-server-first, no lease/lock), converges each destination under a
  concurrency semaphore + a global token-bucket `RateLimiter`, and `flush_outcomes`
  writes every result back **before the next pick** — that ordering is what makes a row
  safe to re-pick after a crash without duplicating a send. Retries are `due_at` backoffs
  in the ledger, not in-process sleeps. A fresh send to a Discord announcement (news)
  channel is recorded `crosspost_state = PENDING` and crossposted durably by a later pick
  (idempotent — "already crossposted" counts as success), so a crash between send and
  crosspost never drops the publish.
- **Progress cards** re-render from a cheap ledger `GROUP BY state` count
  (`state_counts`) until the run drains (no `PENDING` rows left) — the ledger is the
  single source of truth for progress, so there is no in-memory accounting to drift.
- **Auto-disable is off the hot path.** A separate low-load task (`reachability_sweep`)
  probes each enabled legacy destination's reachability + send perms
  (`utils.confirm_dest_unsendable`; `SEND_MESSAGES` for channels, `SEND_MESSAGES_IN_THREADS`
  for threads) and disables a pair only once it has stayed continuously unreachable past
  `cfg.mirror_unreachable_grace_hours` (tracked by `mirrored_channel.unreachable_since`).
  No failure counting. `prune` keeps ledger rows for `cfg.mirror_retention_days` (bar the
  latest DELIVERED message per destination channel, kept indefinitely as an anchor).
- **`mirror_core.py`** holds the pure survivors: `MirrorOperationType`, the global
  token-bucket `RateLimiter`, and `RunView` / `RunCounts` (progress card display state).

The pre-rewrite in-memory implementation is snapshotted on `mirror-v1`; the durable-ledger
rewrite on `mirror-v2`.

## Configuration — `dd/common/cfg.py`

`cfg.py` is the single config source. It reads env vars and **validates required ones at
import time** (raises `ValueError` if a required var is missing). It exposes
`cfg.followables`, tokens, colors, alert thresholds, DB URLs, Bungie creds, etc. Because
validation happens at import, running anything that imports `cfg` without a populated
environment (e.g. bare `pytest` without `--env-file .env`) fails fast — this is why tests
go through `make test`.

## Logging

Standard `logging.getLogger(__name__)`. On top of that, `DiscordLogHandler`
(`dd/common/discord_logging.py`, installed via `install_discord_logging()` in
`__main__`) forwards records to a Discord alerts channel, dedupes by signature, and
escalates storms. Deterministic error reference codes live in `dd/common/utils.py`.

## Tests

Tests live inside each package as `tests/` subdirs (including nested ones, e.g.
`dd/anchor/extensions/tests/`, `dd/anchor/extensions/bungie_api/tests/`). Every package
has tests. `conftest.py` exists only in `dd/beacon/tests/` and `dd/anchor/tests/`; its
key fixture is a session-scoped, autouse `_test_db` that repoints the DB layer at a
temp-file SQLite database via `schemas.configure_test_db()`. Set `TEST_USE_POSTGRES=1` to
run DB tests against Postgres instead (guarded by `_db_is_local()` /
`ALLOW_REMOTE_SCHEMA_DESTROY` so it can't wipe a remote DB). Run tests with `make test`
(see `CLAUDE.md`).
