# Anchor on Railway serverless + cron — splitting the process by lifetime

**Status: proposed, unbuilt.** Design only; no code in this branch. Measurements taken
2026-08-15 against the live `kyber` project (7-day windows, both environments).

Anchor is billed as one always-on process. It is really two workloads with nothing in
common: a **batch job** that fires seven times a day, and an **admin website** one person
uses in bursts. Neither needs a process at 03:00. This plan separates them so Railway
bills for the work rather than for the waiting.

---

## 0. The blocker, first

**Railway's serverless mode sleeps a service after 10 minutes with no _outbound_
traffic** ([docs](https://docs.railway.com/deployments/serverless)) — not inbound, not
idle CPU. Outbound packets of *any* kind reset the timer: "network requests, database
connections, or even NTP".

Anchor holds a hikari gateway websocket, which heartbeats roughly every 41 seconds, and a
SQLAlchemy connection pool to Postgres over the project's private network. **Enabling
serverless on anchor as it exists today changes nothing at all, forever** — the service
is structurally incapable of ever going 10 minutes without an outbound packet. There is
no toggle-only version of this plan, and a first attempt that consists of flipping the
switch will look like the feature is broken rather than like the bot is disqualified.

Everything below follows from that: the gateway has to go, and the DB pool has to stop
holding connections open.

---

## 1. What anchor actually is (measured, not assumed)

**Cost and load** — `get-service-metrics`, 7-day window, ~10k samples per series:

| | avg RAM | avg CPU | at Railway list rates |
| --- | --- | --- | --- |
| anchor / production | 0.237 GB | 0.0002 vCPU | ~$2.37/mo |
| anchor / dev | 0.241 GB | 0.0001 vCPU | ~$2.41/mo |

≈ **$4.78/mo, ~$57/yr**, of which CPU is under a cent. This is a RAM-minutes problem
exclusively — anchor uses about 0.02% of a core and is otherwise asleep at the wheel
while Railway bills it as awake.

**Four properties that make anchor unusually well suited to this, all verified in the
tree:**

1. **No domain gateway listeners.** Every `@bot.listen` in `dd/anchor/` is lifecycle:
   `StartingEvent`, `StartedEvent` ×3, `StoppingEvent` ×2, `GuildJoin`/`GuildLeave` for
   the status counter (`dd/anchor/__main__.py:84-149`). Nothing in anchor reacts to a
   message, a reaction, or a member. The gateway is carrying interactions and a guild
   count, and nothing else. (Contrast beacon, which mirrors — it stays on the gateway and
   this plan does not touch it.)
2. **The cache is an optimisation with a REST fallback everywhere.** Every read is
   `cache.get_x(...) or await rest.fetch_x(...)` — `dd/common/bot.py:51,64,89,102,113`,
   `dd/anchor/utils.py:144,206`. Losing the gateway cache costs REST calls, not
   correctness.
3. **The web session is stateless.** `web_auth.py` signs a `"<user_id>.<expiry>.<sig>"`
   HMAC cookie keyed off the bot token — "no server-side session store"
   (`dd/anchor/extensions/web_auth.py:34-39`). A sleep/wake cycle cannot log anyone out.
4. **Editor state is in the database.** CV2 drafts are `Cv2Draft` rows with autosave
   (`cv2_builder_page.py:19`, `238`, `312`); the rotation editor and the hybrid post
   forms are the same. There is no in-memory working state a sleep would drop.

The scheduled work is seven `aiocron.crontab` registrations, six of them at the same
minute:

| Feed | Schedule | Source |
| --- | --- | --- |
| `lost_sector` | `0 17 * * *` | `extensions/lost_sector.py:68` |
| `eververse` | `0 17 * * *` | `extensions/eververse.py:406` |
| `portal_ops` | `0 17 * * *` | `extensions/portal_ops.py:499` |
| `iron_banner` | `0 17 * * *` | `extensions/iron_banner.py:153` |
| `ada` | `0 17 * * TUE` | `extensions/ada.py:169` |
| `xur` | `0 17 * * FRI` | `extensions/xur.py:693` |
| CV2 draft prune | `0 4 * * *` | `extensions/cv2_builder_page.py:511` |

Two minutes a day of real duty cycle, held up by 1,440 minutes of paid-for process.

---

## 2. The shape: three services, one image, split by lifetime

The `Dockerfile` already builds one image for both bots and selects at runtime, so this
adds services, not build pipelines.

```
                    ┌───────────────────────────────────────────┐
   Discord ────────▶│ anchor-web        serverless: ON          │
   (interactions)   │  RESTBot (HTTP interactions) + aiohttp UI  │
   Operator ───────▶│  awake only while someone is using it     │
   (browser)        └───────────────────────────────────────────┘

   Railway cron ───▶┌───────────────────────────────────────────┐
   0 4,17 * * *     │ anchor-cron       start cmd bypasses      │
                    │  RESTClient + producers, no gateway,      │
                    │  no web, no lightbulb. Runs, posts, EXITS │
                    └───────────────────────────────────────────┘

                     beacon             unchanged, always on
```

### 2a. `anchor-cron` — the batch half

A new entry point, `dd/anchor/cron.py`, run as `python -OO -m dd.anchor.cron`. It builds
no `GatewayBot`, no lightbulb client and no aiohttp app — just a `hikari.impl.RESTApp`
client, the DB, and the existing producer coroutines, which already take `bot` only to
reach `.rest` and the cache fallback.

**One service, not seven.** Railway holds one cron expression per service, so seven
schedules would mean seven services. Instead: schedule the service at `0 4,17 * * *` and
keep the per-feed schedule as a dispatch table in Python, where it is already written and
where it can be unit-tested:

```python
# (hour, weekday-predicate) -> feed. The crontab strings above, moved from six
# decorators into one table the cron process walks on each fire.
```

The producers are already registered centrally (`autopost.register_feed`, keyed by
followable name — `dd/anchor/autopost.py:52-68`), so the dispatcher looks feeds up
rather than importing each module by hand. Feeds run under
`asyncio.gather(..., return_exceptions=True)` so one failing producer cannot take the
day's other five with it.

**Three hard requirements Railway imposes on a cron service, each of which breaks it
silently if missed:**

- **The start command must bypass supervisord.** The image's `CMD` is supervisord, which
  is designed never to exit — a cron service running it would stay `Active` forever, and
  *every subsequent run would be skipped* with no error anywhere. Set a Railway custom
  start command (`python -OO -m dd.anchor.cron`) so PID 1 is the job itself. This also
  sidesteps supervisord's `python -OO -m dd.%(ENV_RAILWAY_SERVICE_NAME)s` mapping, which
  could not name a `dd.anchor-cron` module anyway.
- **`discord_announcer`'s retry loop needs a deadline.** It is currently
  `while True:` with `await aio.sleep(min(2**retries, 300))` and no cap
  (`dd/anchor/autopost.py:96-108`). Inside a long-lived process that is correct. Inside a
  cron container it is the same trap as above: a Bungie outage at 17:00 pins the
  deployment `Active` and silently cancels tomorrow's post too. Give it a wall-clock
  deadline (~10 min) and let the run fail loudly instead.
- **Exit clean.** Dispose the SQLAlchemy engine and close the REST client; Railway
  requires "no open resources". `AppEmojiStore` is DB-backed (`AppEmojiCache`), so a
  fresh process re-reconciles with one REST call and loses nothing — but its pending
  `_touch` flush should be forced before exit.

Cost of this service: two container-minutes a day. Effectively free.

Worth noting this is a **reliability** win independent of the money: a post currently
depends on one process having stayed up since the last deploy. Under cron each post is an
independent run, and a crashed anchor at 16:00 stops being a missed Lost Sector.

### 2b. `anchor-web` — the interactive half, serverless ON

Same image, gateway removed. `hikari.impl.RESTBot` replaces `CachedFetchBot`: Discord
POSTs interactions to an HTTPS endpoint instead of pushing them down a socket, so an
interaction becomes *inbound* traffic that **wakes** the service rather than outbound
traffic that keeps it awake.

Verified available in the pinned stack (hikari 2.5.0, hikari-lightbulb 3.2.4):

- `hikari.impl.RESTBot(token, token_type, public_key=...)` — needs the app's Ed25519
  public key from the Discord developer portal, a new env var alongside the token.
- `RESTBot.on_interaction(body, signature, timestamp) -> Response` — so the interaction
  endpoint mounts as **a route on the existing aiohttp app** rather than as a second
  server. Railway exposes one port (`cfg.port`), which `web.py:17-19` already calls out.
- `lightbulb.client_from_app` accepts `RestClientAppT` and returns a `RestEnabledClient`
  — the command tree, `owner_only` hook and error handlers carry over unchanged.

**What is lost, honestly:** `bot.guild_count` / `update_status` (a RESTBot has no
presence to set) and the gateway cache. The status line is cosmetic on an owner-only bot;
the cache degrades to REST calls that already exist as fallbacks.

**Making it actually sleep** — each of these alone is enough to pin it awake forever:

- **DB pool → `NullPool`.** A pooled Postgres connection over the private network is
  outbound traffic. `cfg.py:250-260` already implements exactly this switch for pytest
  ("NullPool closes each connection on return"); it needs a second, env-driven trigger
  for the web service rather than a new mechanism.
- **No `aiocron` in this process.** Handled by §2a — but the CV2 prune at
  `cv2_builder_page.py:511` is registered from a page module, so it has to move
  deliberately rather than by deleting the six producer decorators.
- **Audit every periodic timer**: the emoji-store touch flusher, the Discord logging
  handler's batching, any `pool_recycle` keepalive. Anything on a timer under 10 minutes
  cancels the whole plan.

**Cold start is the real cost, and it lands on Discord.** Discord requires an interaction
ACK within 3 seconds. A woken Railway container must start, import
hikari + lightbulb + SQLAlchemy + aiohttp, run migrations, preload settings and load ~24
extensions — call it 10–20s, and the docs warn the first request "may return a 502". So:

> **The first slash command after ≥10 minutes idle will fail** with "application did not
> respond", every time. The second will work.

That is acceptable *here specifically* because anchor is owner-gated across the whole
client (`hooks=[owner_only]`, `__main__.py:59-63`) — the blast radius is one operator
retrying, not a user-facing outage. It would not be acceptable on beacon. Two things
sand it down:

- **Trim the wake path.** `run_migrations()` on every cold start is the obvious cut —
  move it to the cron service or a deploy-time one-shot; beacon runs it too, and
  `migrations/env.py`'s advisory lock already serialises them.
- **`sync_commands=False` on this service.** lightbulb defaults to `sync_commands=True`
  *and* `delete_unknown_commands=True`; re-syncing the command tree on every wake is
  wasted seconds and an easy way into a Discord rate limit. Sync from a deploy step
  instead.

**The dropout is avoidable, at a price.** Killing anchor's Discord surface entirely
removes the public key, the sync problem and the 3-second budget in one move — and it is
not as foreclosed as it first looks. **See §6**, which is where the interesting design
work is; §2b as written above is the version that keeps every command where it is and
eats the dropout.

---

## 3. What it saves

| | now | after | |
| --- | --- | --- | --- |
| anchor prod | $2.37/mo | ~$0.15–0.35/mo | web awake ~5% of the month |
| anchor dev | $2.41/mo | ~$0.05/mo | touched rarely |
| cron (both envs) | — | ~$0.03/mo | ~2 container-min/day |
| **total** | **~$4.78/mo** | **~$0.25–0.45/mo** | **~90% off anchor** |

**The honest framing: that is a ~90% cut and about $4.40 a month.** The percentage is
real and the absolute number is small. If the only goal is the money, this refactor does
not pay for itself soon — and the cheapest move by far is a policy one: **stop running
anchor in dev when nobody is developing against it**, which is half the total, costs no
code, and is reversible in a click.

The reasons to do it anyway are the ones that are not on the invoice: each scheduled post
becomes an independent run that a crashed bot cannot silently swallow, and anchor stops
pretending to be a gateway bot when it has not used a gateway event for anything but its
own lifecycle in a long time.

---

## 4. Order of work

Staged so each step is independently valuable and independently revertible. **Stages 1–2
save nothing on their own** — the money arrives only at stage 4 — but they are where the
reliability win is, and they de-risk the stage that touches Discord.

1. **Extract the cron half.** `dd/anchor/cron.py` + dispatch table + the
   `discord_announcer` deadline; deploy `anchor-cron` in **dev only**, with the six
   producer decorators still live in the main process but their feeds toggled off in dev.
   Verify a real 17:00 fire posts once and the container exits.
2. **Delete the `aiocron` registrations** from the producer modules and the CV2 page.
   `dd.common.feeds.FeedKind.ANCHOR_CRON`'s docstring points at `@aiocron.crontab` as the
   definition of a scheduled feed and will need rewording to point at the dispatcher.
3. **Sleep-proof the web process**: env-gated `NullPool`, audit the timers, move
   `run_migrations()` off the wake path. Confirm on dev that the service actually sleeps
   — Railway's metrics tab shows only *public* traffic, so a private-network keepalive
   will not appear there; watch for the service going inactive rather than for a quiet
   graph.
4. **RESTBot swap** on dev: public key env var, interaction route on the aiohttp app,
   `client_from_app` on the RESTBot, `sync_commands=False` + a deploy-time sync. Set the
   Interactions Endpoint URL in the developer portal (Discord validates it with a PING
   before saving — the service must be awake for that).
5. **Enable serverless on dev `anchor-web`**, live with it for a week, then production.

Prod deploys stay the owner's call, per `CLAUDE.md`.

## 5. Open questions for the owner

- **§6 or §2b** — eliminate the dropout by emptying anchor's Discord surface, or keep
  every command where it is and live with one failed invocation per idle period. §6 is
  strictly better UX and strictly more work, and it depends on un-deferring a decision
  from 2026-08-05.
- **Do dev and prod both need this**, or is "turn dev's anchor off" the answer for half
  of it — with serverless applied only to prod?
- **One cron service or two?** `0 4,17 * * *` with an hour-aware dispatcher keeps it to
  one service; a separate nightly service for the CV2 prune keeps the maintenance job's
  failures out of the posting job's logs.

---

## 6. Eliminating the dropout instead of tolerating it

### 6a. The two rules that decide everything

Deferring **does** work mechanically, and it is worth being precise about why, because it
sounds like it should solve this outright. An interaction token is valid for **15
minutes**, and the follow-up endpoints (`POST`/`PATCH /webhooks/{app_id}/{token}/...`)
take the token *as* the credential — no auth header, no relationship to the socket the
original POST arrived on. So any process holding the payload can finish the work long
after some other process ACKed it.

Two Discord rules constrain who can do what, and neither has a workaround:

1. **A modal must be the _first_ response.** `respond_with_modal` is response type 9;
   there is no defer-then-modal. `dd/anchor/embeds.py:178` already states this
   ("``respond_with_modal`` must be the first response on the button's context").
2. **Only a message's author can edit it.** No permission grants another bot
   `message.edit()` on anchor's post.

Rule 1 disqualifies the deferring proxy for the modal commands. Rule 2 disqualifies
beacon for the editing commands. They bite different rows:

| Entry | Modal-first? | Edits anchor's own message? | Thin handoff? |
| --- | --- | --- | --- |
| `/control_panel`, `/help`, `/source_code` | no | no | yes — a link |
| `/post components` | no | no | yes — `Cv2Draft` + link |
| `Edit post` (CV2 path) | no | no | yes — `_hand_off_to_web` |
| `Copy post` (CV2 path) | no | no | yes — `_hand_off_to_web` |
| `Edit post` (embed path) | **yes** | **yes** (`message.edit`, `posts.py:297`) | no |
| `Copy post` (embed path) | **yes** | no (sends new) | no |
| `/post embed` | **yes** | no (sends new) | no |
| `Convert to components` | no | **yes** (`message.edit` in place) | no |
| `ls_update` | no | **yes** (`msg_to_update.edit`, `lost_sector.py:59`) | no |
| `/testing` ×2 | — | — | test_env only (25d81b8) |

### 6b. Verdict on the three candidate mitigations

**Deferring front door (a minimal bot in front of a sleeping anchor).** Works, and holds
only the **public key** — never the bot token — since the initial ACK is just the HTTP
response to Discord's POST. Rejected on four counts, in descending order of weight:

- Rule 1 exempts the three modal commands, which are precisely the ones with no cheaper
  fix. The proxy would need a pass-through list, and pass-through means waiting for
  anchor, i.e. the dropout.
- **Deferring is irreversible.** Once ACKed you *must* follow up. If anchor fails to
  wake, the operator gets a permanent "thinking…" — worse than "did not respond", which
  at least reads as "retry".
- **The ephemeral flag is chosen at defer time.** The proxy has to know per command
  whether the eventual response is ephemeral (`ls_update` responds `ephemeral=True`;
  handoff errors do too). That is a second home for command semantics, in a different
  service, guaranteed to drift from the first.
- ~40–60 MB for a Python aiohttp process is 15–25% of anchor's 240 MB **permanently**,
  capping the saving near 75–80% rather than 90%, plus a second service and a
  private-network hop to operate. (A Cloudflare Worker would be free and does Ed25519 in
  WebCrypto — worth remembering, but it is a third platform for one endpoint.)

**Move commands to beacon.** Correct for every row above marked *thin handoff*: they mint
a `Cv2Draft` and return a URL, needing nothing from anchor's process. Beacon is already
in Kyber's guild (`emoji_servers=[cfg.kyber_discord_server_id]`, `beacon/__main__.py`) so
the context-menu guild scope carries over, and `owner_only` is already documented as
"beacon's per-command gate" (`dd/common/auth.py:22-23`) — this is an established pattern
here, not a new one. Blocked by rule 2 for the three editing rows. One wrinkle if adopted:
`_is_own` compares against `bot.get_me()` (`posts.py:194-197`), which on beacon resolves
to beacon — it would need to compare against anchor's application id.

**Cut commands.** The lever that unblocks the rest, and mostly already written down.

### 6c. The combination — anchor registers zero commands

Put together, the whole surface clears:

- **Six thin handoffs → beacon.** `/control_panel`, `/post components`, `Edit post`(CV2),
  `Copy post`(CV2), `/help`, `/source_code`. Each is a DB write plus a link, and **the
  link click is what wakes anchor** — moving the cold start onto a browser, which
  tolerates 15 seconds, and off Discord, which allows 3.
- **Three modal paths → the web embed builder.** `/post embed`, `Edit post`(embed),
  `Copy post`(embed) are exactly **Phase 2 of `plans/anchor_command_web_migration.md`**.
  Building it retires all three and rule 1 with them.
- **`Convert to components` → a draft, not an in-place edit.** Beacon mints a
  `Cv2Draft` with `ACTION_EDIT` prefilled from `embeds_to_container(message.embeds)`;
  the actual `message.edit` happens on publish from anchor's builder page, where anchor
  is awake because you are on its page. Rule 2 is satisfied by anchor's *token* doing the
  edit, which it still does.
- **`ls_update` → web**, per the already-written `plans/ls_update_web_migration.md`.
- **`/testing` ×2** are gated to `test_env` (25d81b8) and never register in production.

The result: **anchor has no interactions endpoint, no public key, no command sync, no
3-second budget and no front door.** It is an aiohttp web app plus a cron dispatcher.
`__main__.py` keeps a plain REST client and drops lightbulb entirely. The dropout is not
mitigated — there is nothing left for it to happen to.

Note what does *not* move: anchor still posts every feed and still edits its own messages,
from the cron process and from web publish actions, both using anchor's token over REST.
Only the **interaction entry point** relocates.

### 6d. The decision this actually asks for

§6 is not free, and its cost is a reversal: **Phase 2 was deferred indefinitely by the
owner on 2026-08-05**, keeping `/post embed` in Discord. §6 only reaches zero-dropout if
that is un-deferred. That is the owner's call and this plan should not assume it.

If Phase 2 stays deferred, the useful middle is available and is worth more than it
sounds:

> Move the six thin handoffs to beacon anyway, and keep a RESTBot on anchor for **only**
> the modal and editing commands. The commands you touch daily (`/control_panel` above
> all) never drop; the dropout survives only on the handful you reach for occasionally.

That keeps the public key and `sync_commands=False` complexity from §2b, so it is not a
simplification — but it moves the failure from the most-used command to the least-used
ones, which is most of the felt benefit for a fraction of the work.
