# Web panel — the errand list

**Status: in progress** on `web-panel-errand-list`. IA settled (Scheme A); the other three
schemes were drawn and rejected. This file is the build spec — delete it when the work lands.

## The shape

Landing page `/` is a **grouped list of rows**, not a card grid. Four groups, in this fixed
order — frequency descending, blast radius ascending:

1. **Send a post** — Weekly Reset, Trials of Osiris, Send a scheduled post now, Custom one-off post
2. **Fix the data behind a post** — Lost sector rotation, Xûr location map, Trials loot pool,
   Iron Banner, World activity pages (legacy)
3. **Check what happened** — Delivery log, Reach & usage
4. **Set up and admin** — Feeds, Appearance & alerts, Bungie connection, Configured channels,
   Sign out, **Shut down**

`/autopost_settings` splits into `/feeds` (the 12 feed groups, in three sections) and
`/settings` (branding + alerts). Old route 301s to `/feeds`.

## Rules that are not negotiable

- **Copy names outcomes, never mechanisms.** Banned from user-visible strings: *autopost,
  mirror, crosspost, CV2, followable, dormant, ledger, fanned out, logging*.
- **Shut down is the only red thing on the panel.** One `danger` card. Scarcity is the
  mechanism: one red link says which action is unlike the others; five say nothing.
- **Sign out is a `<form method="post">`, not a link.** `web_auth.py` made logout POST-only and
  origin-checked on purpose; a GET link reopens the hole it closed.
- **The backlink becomes "← Home"** in all eight pages that carry it, in one commit — half-done
  reads as two products.
- **No state change may be communicated by motion alone.** Motion annotates a transition between
  states that are each independently legible via colour, position, text or presence.

## Landing page details

- **Row anatomy:** the whole row is the link. Title `--link` 1.05rem/600, one-line description
  `--text-faint` .85rem, `→` chevron hard right. Hover tints the row `--surface-2` and underlines
  the title (rows share a box, so `border-hover` is wrong here).
- **No `.primary` button anywhere on this page.** It is navigation; the answer to "which is the
  primary action" is "none".
- **Group 1 gets slightly larger row padding** (.85rem vs .7rem). Groups 2–4 uniform.
- **Iron Banner weeks:** the Trials row de-emphasises — title drops to `--text-faint`, description
  becomes *"Not running this week — Iron Banner is on."* It **stays clickable**; de-emphasis is a
  hint, not a gate. Source: `dd.common.iron_banner.load_rotation().active_event()`, the same
  operator-editable schedule the Iron Banner post publishes from.
  **It must fail open** — any exception renders the row normally. Rotation data must never take
  down the front door. It is one PK SELECT on a page that currently does zero DB work; ship it
  uncached and note the seam.
- **Configured channels** resolves feed slugs to `display_name` via `dd.common.feeds.FEEDS` —
  the modal should say "Lost Sector", not `lost_sector`.
- **Shut down dialog:** the existing two-paragraph lede stays word-for-word; it is the best-written
  surface on the panel. Add `autofocus` to Cancel so first focus is deliberate rather than
  accidental. No type-to-confirm — the row → armed dialog → red isolation is proportionate for a
  single-owner panel behind OAuth, and typed friction punishes the legitimate use.

## Motion system

Existing tokens in `shared.css` keep their meanings; these are the rules for applying them.

| Token | Means | Test |
|---|---|---|
| `--dur-fast` 80ms | feedback for something happening *now* | is it following the pointer or keyboard? |
| `--dur` 140ms | something appearing, changing, being replaced | caused one action ago, seen dozens of times |
| `--dur-slow` 240ms | a moment with weight, seen rarely | seen ten times a session? then it is not this |
| `--dur-spin` 700ms | one spinner rotation; not a transition token | — |

**One new token: `--dur-stagger: 40ms`.** Required for the landing group reveal. A hand-written
delay would dodge the reduced-motion collapse — which is exactly what `motion_tokens.test.js`
exists to catch. Add it to `:root`, to the `prefers-reduced-motion` block, and to that test's
`TOKENS` list **in the same commit**; shipping two of the three is the documented failure mode.

### The inventory

- **M1 page entry** — `main` only (not children): opacity + `translateY(4px)`, `--dur`. The header
  stays put; it is the frame that makes `main`'s rise legible.
- **M2 landing group reveal** — the four `section.group`s, `animation-delay: calc(n * var(--dur-stagger))`
  via `nth-of-type`. **Groups, never rows** — staggering ~17 rows takes half a second and punishes
  every repeat visit.
- **M3 hover** — background/border/colour, `--dur-fast`. No transform on hover, ever.
- **M4 press** — `translateY(1px)`, `--dur-fast`. Extend the existing button rule to landing rows.
- **M5 dropdown** — Tom Select `.ts-dropdown`, opacity + `translateY(-4px)`, `--dur-fast`, via
  `@starting-style`, in `tom_select_dark.css`. Fast not `--dur`: it opens against typing. Native
  `<select>` gets nothing — the OS owns that popup.
- **M6 routine dialog** — promote the builder's `.cv2b-confirm` mechanics to a shared rule:
  opacity + `translateY(.5rem) scale(.98)`, backdrop fade, `transition-behavior: allow-discrete`,
  `overlay`/`display` in the transition list, `@starting-style` for entry so the **exit animates
  too**. `--dur` both ways.
- **M7 consequence dialog** (Shut down, publish-to-followers) — M6 mechanics at `--dur-slow` on
  open, `--dur` on close. Once they have decided, get out of the way. Danger variant also
  transitions its backdrop to the existing red-black.
- **M8 toggle** — track background + knob `translateX`, `--dur`, `--ease-out`. Must **not** fire on
  page load with server state; CSS gives that for free, so do not flip classes in JS at hydration.
- **M9 async arc** — pending: disable + spinner immediately (never delay or fade a pending
  indicator). Success: swap spinner for the sentence, nothing else moves. Failure: same swap in
  error colour, button re-arms. **No shake** — it vanishes entirely under reduced motion and adds
  nothing over colour and copy.
- **M10 status/validation appearing** — opacity + `translateY(-.25rem)`, `--dur`, discrete display
  transition. Promote the existing `#problems` pattern out of `.form-page` scope so every status
  line shares one implementation.
- **M11 fold** — marker rotates, body fades. **Height is not animated.**

### What must NOT animate

1. **The focus ring.** Already excluded from the global transition; a fading ring reads as lag.
2. **Anything during hydration.** A page that shuffles as it finishes loading reads as broken.
3. **Layout position of settled content** — no `height`/`width`/`top`/`margin`.
4. **The live post preview re-render** — it redraws on every editing burst; fading it strobes.
5. **Charts and tables as data changes.** Motion between two datasets fabricates a continuity that
   does not exist, and it is the one place this app could genuinely jank.
6. **The Shut down row and dialog** — no pulse, no attention animation. Motion that recruits
   attention toward a destructive control inverts the panel's safety posture.
7. **Spinners** never stop and never speed up.
8. **Text content swaps.** Words are read, not watched. M10 animates arrival into an empty slot,
   never a swap between two messages.
9. **Scroll.** No `scroll-behavior: smooth`, no scripted scrolls.

### Performance

Animatable: `opacity`, `transform`, the colour family, plus the discrete `display`/`overlay` flips
that bracket dialogs. Nothing else. `will-change` **nowhere** on these pages — everything animates
one-shot on an event and half of it is already in the top layer. Code that awaits an animation
awaits `transitionend`/`animationend`, never a `setTimeout` mirroring a duration — a timeout
duplicates the token and desynchronises the moment someone tunes it.

**View Transitions: no.** Its cross-fades live in UA styles on `::view-transition-*`, outside the
token collapse, so honouring reduced motion would need a second motion system kept in sync with the
first — which is how the pre-token drift happened. Payoff over M1 is small.

### Where it lives

`shared.css`: the new token, the shared dialog motion, the appear utility, the page-entry rule.
Per-page CSS: the landing stagger, the Tom Select dropdown, the stats fold.
**No new JS** — dialog motion rides `showModal()`/`close()` plus `@starting-style`, which the
builder already proves works including animated exits.

## Page copy

**`/`** — h1 "Destiny Director"; sub *"Everything the bot posts, and the switches behind it.
You're signed in with Discord — no need to sign in again between visits."*

**`/feeds`** — h1 "Feeds"; sub *"What posts where. Switches stop the bot writing a feed — they
don't change which servers follow it. Changes apply from the next post; no restart needed."*
Three sections: **Posted on a schedule** (6 cron feeds, keep Preview/Send now), **Written by you**
(Trials, Weekly Reset — channel row plus a trailing "Open the form →"), **Posted by someone else**
(the other 4 — channel row only).

**`/settings`** — h1 "Appearance & alerts"; sub *"Default colours and links for every post, and
where problems get reported. Changes apply immediately."* Rename the category "Logging & Alerts"
→ "Alerts"; relabel `disable_bad_channels` → *"Stop sending to unreachable servers"*.

**`/send`** — h1 "Send a post now"; sub *"Builds the post from live data — exactly as the schedule
would — and posts it to the feed's channel. Nothing here is a draft; Preview first if you're
unsure."* Six rows. **No on-page primary** — the primary lives in the confirm dialog. A feed that
is toggled off still sends by hand (say so); a feed with no channel gets Send disabled with the
reason and a link to `/feeds`, but **Preview stays enabled**.

**`/custom-post`** — h1 "Custom one-off post"; sub *"Pick the channel this message will go to, then
write it in the builder. The channel can't be changed after this step — come back here to start
over if you picked wrong."* One picker + one primary "Open the builder". **Announcement channels
sort first but text channels remain selectable** — a one-off is sent, not followed. The feed
pickers stay strict (announce-only) for the opposite reason: a text channel there means following
servers cannot mirror the feed at all.

**`/rotation`** — h1 "World activity pages (legacy)"; lists only the nine `world_activity_*` types;
drop the `<code>` slug chips beside each title.

**`/mirror-logs`** → h1 "Delivery log", empty state *"No deliveries in the last 30 days."*
**`/stats`** → h1 "Reach & usage", section header "Autopost reach" → "Feed reach".
