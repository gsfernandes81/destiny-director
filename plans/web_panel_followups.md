# Web panel — deferred follow-ups

Left over from the errand-list rebuild (`web-panel-errand-list`). Each was found while doing
something else and deliberately **not** fixed there, to keep those changes reviewable. None is
urgent; all are small. Delete an entry when it lands, and the file when it is empty.

## 1. One dialog still suppresses its own backdrop fade

`dd/anchor/web_static/bungie_account.html:39` pins

```css
dialog.panelmodal::backdrop { background: rgba(0, 0, 0, .55); }
```

unconditionally. `shared.css` already sets the identical colour on `dialog[open]::backdrop`, and
the unconditional copy is what stops the fade animating — the transition has no start state to
move from. **Delete the line**; do not move it to `[open]`, because the shared rule already covers
it. The same duplicate was removed from `autopost_settings.html` during the feeds split, and the
one in `control_panel.html` is correct: it is `[open]`-scoped and sets the *danger* colour, which
shared.css does not.

Cheapest possible fix, and it makes the last dialog in the app behave like the others.

## 2. Reach & usage still speaks in slugs and mechanisms

Three changes on `/stats` that want doing **together**, because fixing one alone reads as an
oversight in the other two:

- The per-feed table renders raw slugs — `lost_sector`, `xur`, `weekly_nightfall`. They should
  resolve through `dd.common.feeds.FEEDS` to display names ("Lost Sector", "Xûr", "Weekly
  Nightfall"), exactly as the Configured channels modal now does.
- The column headers **"Followers"** and **"Mirrors"** are the bot's vocabulary for two ways a
  post reaches a server. An admin thinks in servers reached.
- `stats.html:75`'s caption — *"Each feed's total reach (followers + mirrors) over time."* —
  describes those two columns, so it has to change with them or contradict them.

Renaming the columns needs a decision about what the two numbers actually mean to an admin, which
is why this was not swept into a retitle pass.

## 3. Three pages still carry old-style titles

`editor.html`, `weekly_reset_form.html` and `trials_form.html` were outside every branch of the
rebuild. Their `<title>`/`h1` still read in the old register ("Rotation Editor — …"). The
backlink on them was already updated to "← Home", so they are half-converted, which is the worst
state to leave copy in.

## 4. The delivery log's filter is labelled "Source channel"

Not banned vocabulary, and not wrong — but "source" is the mirror system's word for the channel a
post was written in. Worth a better label whenever that page's chrome is next open. Low value on
its own; do it while already in the file.

## Done, recorded so it is not re-proposed

- **A third `FeedKind` member** was considered for the "written by you" section on `/feeds` and
  rejected: the distinction is which slugs have a registered `HybridPostSpec`, which only the
  anchor process knows, so it lives in `hybrid_post_core`'s spec registry and `dd.common` stays
  free of anchor-side knowledge.
- **Type-to-confirm on shutdown** was drafted and dropped. For a single-owner panel behind OAuth,
  row → armed dialog with Cancel focused → red isolation is proportionate; typed friction punishes
  the legitimate use without adding real protection.
- **View Transitions** were considered for page entry and rejected: their cross-fades live in UA
  styles on `::view-transition-*`, outside the `prefers-reduced-motion` token collapse, so
  honouring reduced motion would need a second motion system kept in sync with the first.

## From the browser review (desktop, tablet, phone)

A design review drove every page at 1440, 768 and 390, opened every dialog at each width, forced
`prefers-reduced-motion`, measured tap targets and overflow in the DOM, and computed contrast.
**768 holds everywhere** — the single-column body means tablet is just a comfortable desktop. The
debt is almost all at phone width, which nothing in the build had ever been checked at.

### 5. The label column on `/feeds` and `/settings` collapses at phone width — worst of these

At 390px each channel row squeezes its text block to **34px** beside the 224px picker, so
"Post to channel / The Kyber channel this feed posts to." renders as a tower of one word per line.
Portal Ops' four-sentence note becomes a ~20-line word ladder; the "Written by you" cards do it to
the feed *names* — "Trials / of / Osiris". Twelve feeds of it.

Cause: `.text { flex: 1; min-width: 0 }` shrinks to nothing before `flex-wrap` can trigger, because
the field's 14rem basis always fits once the text has been crushed. Fix is one rule — give the text
full width below ~500px so the field wraps under it (`flex: 1 1 100%`). The URL rows on the same
cards already stack that way and look right, so the shape is proven.

### 6. `/mirror-logs` scrolls the document sideways on a phone

Its runs table measures 395–414px inside a 390px viewport — the only horizontal page overflow
anywhere in the panel. `shared.css` already ships `.table-scroll` for exactly this and the table
simply is not wrapped in it. One line.

### 7. The Save model invites silent loss, and the two pages contradict each other

The feed switches are iOS-style toggles, whose universal meaning is "took effect when it flipped" —
but nothing persists until the single Save, which at 390px sits **~5,400px below** the first
switch, off-screen and not sticky. No dirty indicator, no `beforeunload` guard: flip a feed off,
tap "← Home", and it evaporates silently.

And the copy disagrees with itself — `/settings` says *"Changes apply immediately"*, which reads as
"no Save needed", while `/feeds` says *"Changes apply from the next post"*. Both mean "after you
save"; neither says so.

Three parts, and the third is a decision rather than an edit: make the actions bar sticky (the
hybrid forms already do this, the pattern exists), reword both headers to say nothing changes until
Save, and decide between a dirty guard and per-toggle instant save.

### 8. Every control on `/feeds` and `/settings` is nameless to assistive tech

The visible name lives in a sibling `div.text`; the controls carry nothing. A screen reader hears
"checkbox, checked" twelve times. The channel selects and the URL and colour inputs have no
`<label for>` or `aria-label` either, and the Save status `<span>` has no `role="status"`, so
"Saved." is never announced. `/`, `/send` and `/custom-post` all do carry `role="status"`, and
`/custom-post` labels its picker properly — so the pattern exists in the codebase and these two
pages are the exception.

### 9. Tap targets, and Shut down landing beside Sign out

The eleven landing-page links measure ~21px tall at 390px, and the admin group wraps so that
**Shut down sits immediately after Sign out on the same line** — on the page whose whole design is
that the destructive thing is set apart. The confirm dialog means a mis-tap costs a dismissal
rather than an outage, but the layout un-does the separation the design bought. Pad the links to a
≥40px effective target (padding plus negative margin keeps the visual density) and force Shut down
onto its own line when the group wraps.

### 10. Banned words that survived, outside the known `/stats` debt

- The **shutdown dialog** — *"nothing mirrors to any following server"*, inside the best-written
  dialog in the app.
- **`/mirror-logs`** — a "Crosspost" column header, a "Crossposts" tile, a per-run "Crosspost"
  stat; plus an "Operations" tile and a Create/Update legend sitting under a caption that had
  already translated them to "posted / edited / removed".
- **`feed_actions.py:319`** — *"… is dormant — no channel configured."*, which surfaces in the
  send dialog's status line.
- **`/settings`' Alerts card** — "Minimum log severity forwarded to the alerts channel", "ERROR+
  log records", "Inert while unset", and a raw DEBUG…CRITICAL dropdown. The design's own wording —
  *"How much to report — Errors only is the normal setting"* — was better than what shipped.

### 11. Small

- The **send dialog focuses the "Also push…" checkbox**, not Cancel. `control_panel.html`'s comment
  states the principle and the other irreversible dialog follows it.
- "Bot is still starting — try again in a moment" renders in `--err` though it describes a wait,
  not a breakage; `--warn` fits the sheet's own three-level rule.
- The delivery-log overview ends in a "100%" axis label one line under a "90% Success" tile, where
  the two numbers appear to argue.
- "Ada-1" wraps to "Ada- / 1" in the featured row at 390px.
- The rotation index says "(legacy)" in the title *and* on all nine links.

## Confirmed good — do not touch

The information architecture and the errand ordering (it holds at 390px — the filled-card versus
bare-link distinction survives). **Dimmed-always-explains held everywhere**, verified by
enumerating every disabled control on three pages. All four dialogs fit and stay usable at 390px,
with Esc and focus trapping. Reduced motion collapses correctly and nothing communicates state by
motion alone. And contrast is **better than feared** — every text tier passes AA on its surface
(muted 4.65–4.89:1, faint 5.96–6.39:1, links 8.4:1); the close greys are a style choice, not a
failure. Only disabled controls dip below, which WCAG exempts and which carry a sentence anyway.
