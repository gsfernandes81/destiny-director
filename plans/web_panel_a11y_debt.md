# Web panel — accessibility and naming debt

**Status:** stub, for consideration. Nothing here is started.

All of it is **pre-existing** — none was introduced by the errand-list rebuild or the
follow-ups that closed it. It surfaced during an independent three-lens review of
`a6024db..8585244` and was deliberately left out of that work: each item is either a real
project of its own or a change to a surface the rebuild never touched, and folding them in
would have made a set of verified fixes unreviewable.

Every claim below was re-verified in a browser before being written down; where the review
overstated something, the entry says so.

## 1. The CV2 builder is mouse-only past its palette — the big one

Tab through `/cv2-builder/<id>` and it takes **eight stops** and then leaves the document:

1. `← Home`, 2. `Post`, 3-8. the six palette buttons (Text, Container, Section, Image
gallery, Separator, Link button).

The canvas is populated — 53 elements under `.cv2b-canvas`, one `.cv2-root` — and **none of
it is focusable**. Blocks are keyed on `data-path` / `data-kind` / `data-scope` /
`data-index` and are selected by clicking, re-ordered by dragging, and edited through a
`contenteditable` reached by double-click. The inspector, the per-block menus and the
`+ Add` insert points are all mouse-only. So the whole authoring surface of the builder is
unreachable without a pointer.

One correction to the review, which reported undo/redo as "not in the tab order at all":
they are focusable (`tabIndex: 0`) and simply `disabled` at load because there is nothing
to undo yet. That is correct behaviour, not a defect.

Note the irony worth keeping in view: the review that found this also confirmed the CV2
builder's *fields* now draw the shared focus ring — a fix on controls a keyboard cannot
reach.

**Shape of the work:** a roving-tabindex or `tabindex="0"` on each block plus arrow-key
navigation, Enter/F2 to edit, and keyboard equivalents for drag (move up/down). Real
design, not a patch — the drag layer has browser tests that assert behaviour, and they
would want extending rather than replacing.

## 2. `/rotation/edit` has twenty targets under the 24px floor

Measured at 390px on `?type=lost_sector`. WCAG 2.5.8 wants 24×24 CSS px:

| Size | What |
|---|---|
| 13×13 | the champion and shield checkboxes — Barrier, Overload, Unstoppable, Arc, Void, Solar, Stasis (×2 sector blocks) |
| 27×23 | `button.tiny.danger` — the `✕` remove-sector control |
| 56×16 / 82×16 | `← Home` and `All rotations` — the shared `.backlink`, so this one is site-wide |

The review reported two; there are twenty, and the smallest are the 13px native
checkboxes, which is the set an operator actually taps most on that page. The `.backlink`
row is the interesting one because it is `shared.css` and therefore every page — a
`padding` + negative-`margin` pair would fix it everywhere without changing the visual
density, which is the same trick the landing chips used.

## 3. One ungated smooth scroll

`dd/anchor/web_static/cv2_builder.js:1380` —
`found.scrollIntoView({ block: "center", behavior: "smooth" })`.

Chromium honours `prefers-reduced-motion` for CSS `scroll-behavior` but **not** for an
explicit JS `behavior: "smooth"`. Everything else in the app collapses correctly under
reduced motion (verified: the page entry, the landing stagger, all four dialogs and their
backdrops, every chip and row transition; the spinner slows to 2.4s rather than stopping).
This is the single residual, and it is a one-line `matchMedia` gate.

## 4. `.colorfield` carries `no-focus-ring` and does not need it

`shared.css` defines the marker for "controls whose WRAPPER already draws the ring" — the
autopost switch and the Trials set-card, both of which hide a real input and decorate an
ancestor. The hex field has no such wrapper: it suppresses the shared ring and then
re-adds its own in `settings_page.css`, at equal specificity (0,2,0), winning **only on
stylesheet link order**. Flip the two `<link>`s in `settings.html` and the hex field
silently loses its focus ring.

Nothing is broken today. It is a rule that holds for a reason nobody wrote down, which is
the state that produces a mysterious regression later. Drop the marker and the re-add.

## 5. Naming inconsistencies the retitle passes did not reach

Each verified in place:

- **`/bungie`** — `<title>` and `<h1>` say *Bungie Account*; the landing card that leads
  there says *Bungie connection*.
- **`editor.html:37`** — the backlink says *All rotations* and points at `/rotation`, which
  lists nine of the thirteen. From the Lost Sector, Xûr, Trials or Iron Banner editors it
  is a link to a page that explicitly does not list the thing you came from.
- **`dd/beacon/help_details.py:40`** — *"Crossposting is not waited on for manual sends."*
  in `/help`, the exact word `/mirror-logs` replaced. The surrounding right-click
  instructions are fine as they are — they describe a physical action.
- **`dd/beacon/extensions/mirror.py:940-943`** — `/mirror source_details` prints *Legacy
  sources* / *New style sources*, the database's names for the two mechanisms `/stats` now
  calls *following* and *sent a copy*.
- **`send_page.py:94`** — the non-breaking hyphen that stops *Ada-1* wrapping as "Ada- / 1"
  is applied to the landing card only. `/feeds` and `/send` render a plain hyphen and can
  still break it. Knowingly partial: it also makes the card text un-findable with Ctrl+F
  for "Ada-1", so applying it more widely is a trade, not an obvious win.

## 6. `/stats` compares two different measurements side by side

The per-feed number is a **live snapshot** from `MirroredChannel.count_dests()`; the
sparkline beside it is a 365-day series out of `autopost_daily_stat`. They are different
sources on different time bases, and the table's caption ("over time") now covers both and
invites the comparison. With seeded data the review saw `4` next to a sparkline peaking
near `40`.

Related, and the reason the column now says **Channels reached** rather than *Servers
reached*: `count_dests` counts destination-channel rows. `dest_server_id` is on the table
but nullable, so a `COUNT(DISTINCT dest_server_id)` would undercount rather than fix it.
Making the page able to say *servers* honestly means auditing and backfilling that column
first — worth doing, but it is a data change, not a copy change.

## 7. `plans/` has two rules pulling against each other

`CLAUDE.md` says a fully-executed plan is removed from `plans/`, and
`web_panel_followups.md`'s own header says "delete an entry when it lands, and the file
when it is empty". That file is now a decision record with everything landed — kept
deliberately, because the decisions in it (the Save model, the `/stats` collapse, the
WCAG 1.4.11 deferral) are exactly the kind that get re-proposed. Two reviewers flagged the
contradiction independently.

Either move those three paragraphs into the code comments that already restate most of
them and delete the file, or drop the "delete the file" line and retitle it as a decision
record. Owner's call; it should not be settled silently.
