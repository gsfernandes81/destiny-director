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

**Why the simpler "drop Discord entirely" variant is closed.** Killing interactions
outright would remove the public key, the sync problem and the 3-second budget in one
move. It is foreclosed by work already done: `plans/anchor_command_web_migration.md` §Set
C deliberately keeps 10 entries in Discord, including three context menus whose input
*is* the right-clicked message ("no web equivalent short of pasting message links") and
`/control_panel`, the entry point into the web UI itself. That set was chosen on purpose;
this plan should not quietly reverse it.

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

- **Is the 3-second cold-start miss acceptable?** Everything else here is mechanical;
  this is the one genuine behaviour regression, and it is a judgement call about your own
  daily use of `/control_panel`.
- **Do dev and prod both need this**, or is "turn dev's anchor off" the answer for half
  of it — with serverless applied only to prod?
- **One cron service or two?** `0 4,17 * * *` with an hour-aware dispatcher keeps it to
  one service; a separate nightly service for the CV2 prune keeps the maintenance job's
  failures out of the posting job's logs.
