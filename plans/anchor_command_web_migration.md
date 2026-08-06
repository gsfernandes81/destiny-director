# Anchor Discord commands → web control panel

## Status: Phases 0, 1, 3 and 4 built (2026-08-04/05). **Phase 2 deferred indefinitely.**

Phases 4 and 3 were taken out of order, ahead of 2 — the phases are independent. Phase 2
(the web embed builder, dropping `/post embed`) was **deferred indefinitely by the owner
on 2026-08-05**; `/post embed` stays in Discord, and `plans/web_embed_builder.md` stays
unbuilt. Phase 1 is built and **verified on dev** — the owner drove Send end to end
2026-08-05, which is what produced the fix rounds since. Owner decisions locked
2026-08-04.

Anchor's Discord surface is **39 invocable entries**, all owner-gated
(`hooks=[owner_only]` on the whole client in `dd/anchor/__main__.py:53`). The web control
panel already covers settings and observability; this plan takes it the rest of the way
and reduces Discord to a minimal set — **11 entries** as built, since Phase 2's
`/post embed` is staying.

Related: `plans/anchor_web_ia.md` (the unresolved page-shape question that Phase 1 must
answer), `plans/web_embed_builder.md` (dependency of Phase 2),
`plans/website_user_commands.md` (same "web UI as sole authoring surface" direction, for
beacon's user commands).

---

## 1. The three sets

### Set A — delete now, nothing to build (11 entries)

Each of these is **already** served by an existing web page. They are live duplicates.

| Command | Already on |
| --- | --- |
| `/ls auto`, `/xur auto`, `/eververse auto`, `/ada auto`, `/portal_ops auto`, `/iron_banner auto` | `/autopost_settings` — the same `AutoPostSettings` rows |
| `/ls details` | `/autopost_settings` (`lost_sector_details`) |
| `/xur default_image` | `/autopost_settings` (`xur_default_image`) |
| `/rotation edit` | card on `/` → `/rotation` |
| `/weekly_reset create` | card on `/` → `/weekly_reset` |
| `/trials create` | card on `/` → `/trials` |

`autopost_settings.py`'s docstring currently says it "does not replace those; it is an
additional, consolidated surface". That sentence becomes false with this phase — update it.

The `auto` subcommand is generated inside `make_autopost_control_commands`
(`dd/anchor/autopost.py:93`), so removing it is one edit for all six feeds. `details` and
`default_image` are per-feed classes registered onto the group
(`lost_sector.py:134`, `xur.py:750`).

### Set B — migrate, needs building (18 entries)

| Commands | Target | Notes |
| --- | --- | --- |
| `send` × 6, `show` × 6 | per-feed preview + send-now on web | Phase 1. The one genuinely new feature. |
| `/post embed` | web embed builder | Phase 2. **Deferred indefinitely 2026-08-05** — stays in Discord. |
| `/anchor restart \| stop \| info` | web admin controls | Phase 3. `restart` was deleted outright rather than migrated. |
| `/bungie login`, `/bungie account_numbers` | web Bungie account card | Phase 4. |

### Set C — stays in Discord (10 entries)

| Command | Why it stays |
| --- | --- |
| `/control_panel` | The entry point into everything above. |
| `/post components` | Already a thin handoff — writes a `Cv2Draft` and links to `/cv2-builder/{draft}`. The **invoking channel is the input**; replacing it on web means building a channel picker for no gain. |
| `Edit post`, `Copy post`, `Convert to components` (context menus) | "Right-click *this* message" has no web equivalent short of pasting message links. They already hand off to the web builder for the actual editing — the ideal hybrid. |
| `ls_update` (context menu) | Same reason, for now. **Deferred migration**, reusing weekly reset's `DraftMeta` / `post_or_edit_unpublished` lifecycle — see `plans/ls_update_web_migration.md`. |
| `/help`, `/source_code` | Cheap, conventional, no web work. `/help` and `help_details.py` need pruning as entries disappear. |
| `/testing convert_sample`, `/testing overflow_alert` | Both exist to post a Discord message and eyeball a rendering path. Discord-native by construction. |

---

## 2. Phases

Ordered by dependency. Phases 1–4 are independent of each other and can ship in any
order after Phase 0.

### Phase 0 — delete the duplicates ✅ DONE 2026-08-04

Set A, all 11 removed. `make lint`, `make typecheck` and the full non-Discord suite
(1195 passed) are green.

One thing the plan did not anticipate: dropping the `auto` subcommand orphaned the
factory's `enabled_getter` / `enabled_setter` parameters, and with them the six
`_get_<feed>_enabled` wrappers that existed solely to be passed in — every cron already
calls `schemas.AutoPostSettings.get_<feed>_enabled` directly. Both parameters and all six
wrappers are gone; `make_autopost_control_commands` now takes only what `send` and `show`
actually use.

`help_details.py` needed no change — none of its entries covered a Set A command.

### Phase 1 — per-feed send + preview on web ✅ BUILT + VERIFIED 2026-08-05

Built in three commits (registry groundwork → page → removal). `make lint`,
`make typecheck` and the full non-Discord suite (1209 passed) are green.

**Verified on dev 2026-08-05** (owner): the pages, the preview and a real Send were all
driven in a browser against live Discord. That round is what produced the two fix
batches after it — the Trials button regression, the weekly-reset load time, the raw
channel ids. Note the inversion it leaves behind, since it recurs every time: **the
fixes a test round produces are themselves untested**, so "we tested it" and "what we
would ship is tested" drift apart by exactly one batch each time.

Still open: whether the **mirror leg** ran, which depends on beacon having been up in
dev during that session. Everything else on the send path fails visibly; a broken
crosspost fan-out fails quietly, in other people's servers.

What differs from the plan as written:

- **There is no page.** The plan had a feed page carrying toggle + Preview + Send. The
  toggle stayed on `/autopost_settings` (two surfaces writing one row is a needless
  second write path), which left a page holding two buttons — so the buttons moved onto
  the row instead and the page went away. `dd/anchor/extensions/feed_actions.py` is now
  two endpoints with no shell.
- **Send confirms against the post.** The send modal renders what is about to go out,
  and its confirm button stays disabled until that render succeeds — there is no reason
  to allow a blind send when a preview is one call away. The publish checkbox lives in
  the modal, so the crosspost choice is made at the moment of confirming.
- **Send returns before the post lands.** Awaiting the announcer inside an HTTP handler
  would mean a request that can hang for hours: both announcers retry construction
  forever, `send_message` retries too, and `api_to_discord_announcer` posts its
  placeholder to the live channel *before* constructing, so its retries must not be
  bounded from outside. The handler builds once up front (catching the common failure
  without touching the channel), then hands off to a background task. A per-feed
  in-flight slot stops a double-click double-posting.
- **The snapshot serializer moved.** The renderer walks raw component dicts, but
  constructors return `HMessage`; the bridge lived in `dd/beacon/mirror_worker.py`, and
  anchor imports nothing from beacon. Hoisted the pure transform to
  `dd.hmessage.snapshot`, beacon delegates. (The plan's "`show` renders through
  `cv2_render`" skipped this step.)

**Rebased onto dev 2026-08-05**, after the preview-renderer unification landed. That
branch deleted the Python renderer and added a CSP, and the rebase produced **no
textual conflicts** while invalidating two things the page was built on — the failure
mode worth remembering here:

- `cv2_render.render_snapshot` no longer exists; drawing is `web_static/cv2_render.js`.
  `/preview` now returns `{kind, payload, message_kind}` — byte-identical to what
  `/mirror-logs/render` serves — and the page draws it with the same
  `CV2Render.snapshotSpec` call. Both preview surfaces go through the one renderer.
- `script-src 'self'` forbids inline script, so the page's server-injected bootstrap
  and its inline handlers would both have been dead on arrival. The shell is now static
  and fetches `GET /feed/{name}/data`; behaviour lives in `web_static/feed_page.js`.

#### Original design notes

Kills the 12 remaining autopost subcommands, which empties
`make_autopost_control_commands` entirely — the factory and its `lb.Group` disappear, and
each feature module keeps only its cron listener and constructor.

**`plans/anchor_web_ia.md` §4 is *not* a blocker for this phase** (an earlier draft of
this plan said it was — it was wrong). The §3 scaling objection was specifically about
*status chips and sparklines* — per-instance health data, where 20–30 healthy rows crowd
out the one that matters. A per-feed **settings** list has no such problem, and the proof
is that `/autopost_settings` is already exactly that flat list, already carries every
feed, and drew no objection.

**There is no feed detail page** (an earlier revision built one; it was removed
2026-08-05). The actions live inline on each `/autopost_settings` row — two buttons,
Preview and Send now — and the rendered post appears in that page's modals. A feed has
no state a page could show that the row does not already, so a page was only ever a
click in the way. §2's feed page "surviving as the detail view" turned out not to hold
once the detail was just two buttons.

The exceptions-first / by-time hub stays deferred indefinitely — it is an *observability*
question that belongs with mirror log and stats, and it blocks none of the 12 subcommands
this phase kills. §4's durable insight is that per-feed and per-instance state are
tangled; the clean cut is to put **actions** on the feed page and leave **health** to a
hub that does not exist yet.

The finding in `anchor_web_ia.md` §1 still holds and makes the data side easy: `feed`
(the followable name) is already the shared key across autopost settings, mirror log and
stats, so no schema change and no new query shape.

Server-side, `send` and `show` map onto existing coroutines — each feed already passes
its `message_constructor_coro` and `message_announcer_coro` into the factory, so the web
routes need the same two callables in a registry keyed by feed name rather than closed
over in a command class. `show` renders through `cv2_render` (the same walker the mirror
log uses); `send` is a POST behind a confirm, since it publishes to a real channel.

Two things the Discord versions do that the web versions must keep: `send`'s `publish`
flag (crosspost on/off, defaults true) and the retry-with-backoff in `discord_announcer`.

### Phase 2 — web embed builder → drop `/post embed`

Blocked on `plans/web_embed_builder.md` (status: ready to build). Once the builder exists,
`/post embed` becomes a web action alongside the CV2 builder. `/post components` stays as
the Discord handoff; the `post` group then has one subcommand.

`Edit post` and `Copy post` keep their embed paths — they call `build_embed_with_user`
today (`posts.py:292`, `posts.py:335`) and should hand off to the web embed builder the
same way their CV2 paths hand off to `cv2_builder_page`, so the in-Discord embed builder
(`dd/anchor/embeds.py`) can be deleted rather than kept alive for two callers.

### Phase 3 — bot admin on web ✅ BUILT 2026-08-05

`/anchor stop | info` moved to the panel — as `GET /bot/info` and `POST /bot/stop`, with
the buttons and their modals on `/` itself rather than a page of their own (two actions
and a read-only dump do not need one). `dd/anchor/extensions/controller.py` is gone;
beacon keeps `/beacon stop | info` via the shared factory, which shed its now-dead
`show_followables` parameter (anchor was its only user).

**Owner's call, against my recommendation — recording the tradeoff so it is not
rediscovered:** the aiohttp app runs
*inside* the anchor process, so a web-only control cannot restart a wedged web server. The
fallback is redeploying from Railway. Two things soften it: `restart` is already disabled
in prod (`dd/common/controller.py` — a non-zero exit is a crash to Railway, which applies
crash-loop backoff), and the gateway and aiohttp fail independently often enough that the
common case is recoverable either way.

Port with it: the DANGER-override confirm flow, and the restart-disabled-in-prod refusal.

**Do not delete `make_controller_group`** — beacon uses it too (`/beacon stop|restart|
info`) and beacon has no web server. Only anchor's wrapper (`dd/anchor/extensions/
controller.py`) goes away. Beacon's `mirror_check` argument stays untouched.

#### Should `restart` or `stop` be removed from the codebase entirely?

Asked 2026-08-04 on the premise that one of the two is broken or dangerous in prod.
**Verified against live Railway state — the premise does not hold. Neither is.**

| Service | Env | `restartPolicyType` | max retries |
| --- | --- | --- | --- |
| beacon | production | unset → **ON_FAILURE** | default (10) |
| beacon | dev | unset → **ON_FAILURE** | default (10) |
| anchor | production | unset → **ON_FAILURE** | **7** (explicit) |
| anchor | dev | unset → **ON_FAILURE** | **7** (explicit) |
| MySQL | production | unset → **ON_FAILURE** | default (10) |

**No service is set to `ALWAYS`**, and no restart policy is committed in the repo
(`railway.toml` carries only a `[build]` block). So:

- **`stop` works everywhere**, prod included — exit 0 under `ON_FAILURE` stays down.
  The docstring's claim about the 2026-06-25 flip off `ALWAYS` still holds.
- **`restart` is unreachable in prod by design**, not broken: `restarts_enabled()`
  (`dd/common/controller.py:64`) is `bool(cfg.test_env)`, and `TEST_ENV` is set on the
  dev services only. The gate keys off exactly the right thing.

If one is still to go, it is **`restart`** — but for a different reason than the premise
gave: it is prod-dead by design, so it exists only for dev, where each invocation burns
one of anchor's 7 retries and repeated use inside a deployment can exhaust the budget and
leave dev anchor down until a redeploy. That is a "narrow utility, real cost" argument,
not a "broken" one. **Not actioned — needs a decision on the corrected facts**, and note
it would take `restart` off `/beacon` too, which has no web panel to replace it with.

Separate small fix this turned up: the docstring's "`restart` … works under any
restart-on-failure policy" glosses over the retry ceiling, and anchor's explicit
`restartPolicyMaxRetries: 7` is undocumented anywhere. Worth a sentence.

`info` with `show_followables=True` is a read-only config dump — the easy half of this
phase, and a reasonable first slice.

### Phase 4 — Bungie account card on web ✅ BUILT 2026-08-05

`/bungie` carries link status (linked / expired / never linked, with the stored expiry),
a **Log in with Bungie** action, and an on-demand account-numbers lookup. `/bungie login`
and `/bungie account_numbers` are gone, and with them the whole `bungie` command group.

Notes worth keeping:

- **Login stopped needing to wait.** The command printed a URL then blocked up to 15
  minutes polling `OAuthStateManager.get_access_token()`. On the web the redirect back
  *is* the completion signal, so `GET /bungie/login` mints a state code, redirects, and
  the callback returns the browser to `/bungie`, which re-reads status on load.
  `_wait_for_token_from_login` now has no caller — left in place, since it is the
  documented shape of the flow and costs nothing.
- **The state code is minted on click, not on render.** `oauth_url()` stores a one-shot
  code per call, so minting it in the page handler would litter unused codes for the
  sweeper. There is a test pinning that.
- **The access token still never leaves the server** — the constraint the Discord
  command carried in a comment, now also a test.
- The callback's "you can close this tab" response became a redirect to `/bungie`. That
  wording was right when the flow started in Discord and the browser was a detour; it
  isn't now.

#### Original design notes

`/bungie login` and `/bungie account_numbers` become one card. The OAuth callback is
already web-side (`/oauth/callback`, `bungie_api/oauth.py:179`), so login becomes a link
+ status rather than a command that blocks for 15 minutes waiting on a transient server.
The card shows link status and the character / membership ids.

Keep the existing rule: the OAuth **access token is never surfaced** in the UI
(`bungie_api/__init__.py` says so explicitly for the Discord response — it holds for the
web page too, which is if anything easier to screenshot).

---

## 3. End state

**39 → 10.** Discord keeps: `/control_panel`, `/post components`, four context menus
(`Edit post`, `Copy post`, `Convert to components`, `ls_update`), `/help`,
`/source_code`, and `/testing convert_sample|overflow_alert`.

Everything remaining is either the entry point into the web UI, anchored to a specific
Discord message, or a dev helper whose whole job is to post in Discord.
