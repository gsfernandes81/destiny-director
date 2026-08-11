# Web panel — deferred follow-ups

Left over from the errand-list rebuild (`web-panel-errand-list`). Each was found while doing
something else and deliberately **not** fixed there, to keep those changes reviewable. Delete an
entry when it lands, and the file when it is empty.

**Everything actionable in this file has now landed** (branch `web-panel-followups`). What remains
below is the record: decisions taken and rejected, so they are not re-proposed, and the findings
that were deliberately left alone with a reason.

## Decisions taken, recorded so they are not re-litigated

- **The Save model** — sticky actions bar, an unsaved-change count, and a `beforeunload` guard.
  Rejected: saving each toggle the moment it flips. It matches what an iOS-style switch means, but
  the channel, URL and colour fields still need an explicit save, so the page would have ended up
  running two save models at once and an operator would have to know which control obeyed which.
- **`/stats` columns** — one "Servers reached" number per feed, with "N following · N sent a copy"
  as a quieter second line. "Followers" and "Mirrors" name the two *mechanisms* by which a post
  reaches a server; the question the page answers is "did this land, and is it growing". The split
  is kept rather than dropped because a mirror count that suddenly falls is worth being able to see.
- **The alert level** — plain words in the dropdown ("Errors only (the normal setting)"), with the
  stored value untouched. Nothing persists the labels, so they can be reworded freely; the
  `DEBUG`…`CRITICAL` list was the logging module's vocabulary, saying which constant is compared
  rather than how much noise to expect.

## Confirmed, and deliberately not fixed

### Control boundaries sit below the 3:1 non-text floor

Found by sweeping every token pair after the palette change, not by looking — a border this quiet
is invisible as a *defect* precisely because it is doing its job as decoration.

WCAG 1.4.11 wants 3:1 between a control's boundary and what is either side of it. Ours is
**1.73:1** for a card edge against the page and **1.57:1** for an input edge against a card, and the
fills barely differ either (`--surface-3` on `--surface-2` is 1.11:1), so an input on a card is
genuinely hard to locate without hovering it.

Pre-existing — the numbers were 1.59:1 and 1.49:1 before the palette moved, and the lift to
`--border: #374154` made every edge better than it was. Closing the gap properly needs about
`#5c6a85`, a visibly light edge on every card and input in the app. **Owner's call, taken: leave
it.** The quiet chrome is a deliberate style choice; reopen this only with a before/after in hand.

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

## Confirmed good — do not touch

The information architecture and the errand ordering (it holds at 390px — the filled-card versus
bare-link distinction survives). **Dimmed-always-explains held everywhere**, verified by
enumerating every disabled control on three pages. All four dialogs fit and stay usable at 390px,
with Esc and focus trapping. Reduced motion collapses correctly and nothing communicates state by
motion alone. And contrast is **better than feared** — every text tier passes AA on its surface;
the close greys are a style choice, not a failure. Only disabled controls dip below, which WCAG
exempts and which carry a sentence anyway.
