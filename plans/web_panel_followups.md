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
