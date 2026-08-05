# CV2 builder — removing the grab handle

## Question

Can `.cv2b-grip` (the `⠿` handle) go away, with dragging moved onto the block body:

- **touch** — hold to lift, then move to drag; hold and release without moving for the
  options menu; haptic feedback on the lift if the platform has any
- **desktop** — press-and-move to drag, click-and-release to select/edit, right-click for
  the menu, and no marquee text selection on the canvas

## Verdict: viable

Every hazard below was measured in Chromium against the real
`web_static/tests/builder_harness.html` (probe scripts were throwaway; the numbers are
reproducible from the descriptions). Four of the five have cheap fixes. The two real
costs are **sections become hard to grab without a CSS change**, and **iPhones get no
haptic, ever**.

---

## 1. Touch: hold-to-lift works, but only via a non-passive `touchmove`

This was the load-bearing unknown — today's touch drag works *only* because
`.cv2b-grip { touch-action: none }` (cv2_builder.css:302) hands the gesture to us before
it starts. A block body cannot have `touch-action: none`: that is how the canvas scrolls
on a phone. And `touch-action` is latched when the pointer goes down, so it cannot be
flipped mid-gesture when the hold fires.

The technique that does work — the one SortableJS and react-beautiful-dnd ship — is a
**non-passive `touchmove` listener that calls `preventDefault()` only once the hold has
armed**. Measured, with `touch-action` left at its default:

| gesture | scrollTop | `pointercancel` | `pointermove` after arming | `touchmove.cancelable` |
|---|---|---|---|---|
| move immediately (no hold) | **105px** — scrolled | 1 | — (never armed) | — |
| hold 600ms, then move | **0** — did not scroll | 0 | 12 of 12 | `true` |

So both halves hold: a flick still scrolls the canvas, and a hold-then-move survives with
the full pointer stream intact. Note the existing `ev.preventDefault()` on `pointermove`
(cv2_builder.js:1823) does **not** suppress panning — it is the grip's `touch-action` doing
that work today, and a `touchmove` listener has to replace it.

Untested here: iOS Safari. The technique is the one the mainstream drag libraries use on
iOS, so the risk is low, but it is the one thing worth checking on a real phone before
shipping. iOS also needs `-webkit-touch-callout: none` on blocks or the long-press raises
the system callout.

## 2. Haptics: Android yes, iPhone no

`navigator.vibrate(12)` returns `true` under emulated mobile Chromium. In the field that
means Android Chrome and Firefox. **iOS Safari does not implement the Vibration API at
all** — there is no workaround from a plain web page. Call it as `navigator.vibrate?.(12)`
and let iPhones lift silently; the visual lift has to carry the feedback on its own, which
argues for making it obvious (scale + shadow on the lifted block), not subtle.

## 3. Sections are the real cost — nothing else is

`pointerdown` resolves via `closest('.cv2b-blk')`, i.e. the *innermost* block. Without a
grip, a nesting block is grabbable only where its own chrome is not covered by a child.
Measured own-hit ring thickness, in px, per edge:

| block | desktop L/R/T/B | phone L/R/T/B | own area |
|---|---|---|---|
| text / separator / link_button / thumbnail | whole body | whole body | **100%** |
| container | 14 / 12 / 18 / 20 | 32 / 12 / 30 / 32 | 39–44% |
| **section** | **0 / 0 / 8 / 10** | **0 / 0 / 20 / 22** | 20–67% |

Leaves are fine. Containers are fine — a 12–32px ring on all four sides. **A section is
the exception**: its text child plus its accessory fill it edge to edge, leaving an 8px
top/bottom band on desktop. That is below a usable mouse target and far below the ~44px
touch guidance.

Worth being clear that this is not a *new* defect — `click` already uses the same
`closest()` (cv2_builder.js:998), so selecting a section already depends on that 8px band.
The grip was simply the escape hatch that made it not matter.

Two fixes, complementary:

- **Pad the section.** ~6–8px on `.cv2b-blk[data-kind="section"] > .cv2b-body` takes the
  ring to ~14–18px desktop / ~26–30px phone. Cheap, and it improves selection too.
- **Escalate to the selection.** If a block is already selected, a press *inside* it drags
  **that** block rather than the innermost descendant. One tap to select the section (via
  the padded ring), and then its whole area is a grab target. This also gives containers a
  deliberate way to grab the parent from anywhere.

## 4. Desktop: press-and-move already paints a text selection

Confirmed on the harness — a plain press-and-move across two blocks selects
`"Weekly Reset\n⠿\nNightfall: The Corrupted\nimage URL missing"`. Needs `user-select: none`
on the canvas, re-enabled under `[data-editing]` so caret placement and selection inside
the block you are actually editing survive (the `pointerdown` handler already bails on
`[data-editing]`, cv2_builder.js:1869). The existing rule only kicks in *after* the drag
starts (`body.cv2b-dragging-now *`, css:331), which is 6px too late.

Incidental: that selection string contains `⠿`. The handle is currently polluting
copy/paste, and removing it fixes that for free.

## 5. Desktop: an `<img>` hijacks the drag

Starting a press on an image inside the canvas — a gallery tile, a section thumbnail —
lets the browser's native image drag take over and kill the gesture. Measured:

| | `dragstart` | `pointercancel` |
|---|---|---|
| as-is | 1 | 1 |
| with `img { -webkit-user-drag: none }` | 0 | 0 |

One CSS line for Chrome and Safari. Firefox does not implement `-webkit-user-drag`, so the
images the renderer emits also want `draggable="false"` (cv2_render.js:183, :236 — and
:158 for emoji, which sit inside text blocks).

---

## What the change actually costs in code

Smaller than the analysis suggests. `onPointerDown` (cv2_builder.js:1803) already models
"a press is a pending gesture; what happens next decides what it becomes", with a 6px
`DRAG_THRESHOLD` and a long-press timer. The change is to its *branches*, not its shape:

- `gripOnly` becomes "always true for mouse" — the guard at :1815 that treats a body-move
  as a scroll stays, but only for touch-before-arming.
- the touch long-press at :1846 stops opening the menu when it fires; it **arms** instead
  (vibrate, lift the block). Menu moves to release-without-move; drag to move-after-arm.
- add the non-passive `touchmove` that `preventDefault`s while armed.
- `swallowSyntheticClick()` still needed on the release-into-menu path.

**One UX regression to accept**: the options menu now costs a hold *and* a lift, where it
used to appear at 450ms unprompted. Since the hold is now a lift rather than a commit,
`LONG_PRESS_MS` (:1403) probably wants to come down to ~250–300ms.

**Reclaimable once the grip goes**: `.cv2b-canvas { padding-left: 2rem }` (css:~218) and the
phone-only `.cv2b-canvas .cv2-container { padding-left: 1.9rem }` (css:~997) exist purely to
hold grips. Dropping them widens the canvas on a phone, which is the surface that needs it.

**Copy that names the handle**: cv2_builder.js:419 (the span), :634 (`"Drag the ⠿ handle to
move or re-nest"`), :334 (empty-canvas hint), :2328 (palette hint). Discoverability is the
soft cost of the whole change — `cursor: grab` on block hover covers desktop; on touch the
only cue left is the help text, so it has to actually describe the hold.

**Tests**: all six drag tests in `dd/anchor/tests/test_builder_drag.py` funnel through
`_grab()` (:149), which hovers and presses the grip. Rewriting that one helper to press the
block body carries every existing assertion over unchanged — they assert *where a block
lands*, which this change does not touch. Then add: a flick scrolls and does not drag; a
hold arms and a subsequent move drags; a hold-and-release opens the menu; a press on an
image still drags the block.
