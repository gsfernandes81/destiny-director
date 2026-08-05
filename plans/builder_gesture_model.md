# CV2 builder — removing the grab handle

## Question

Can `.cv2b-grip` (the `⠿` handle) go away, with dragging moved onto the block body:

- **touch** — hold to lift, then move to drag; hold and release without moving for the
  options menu; haptic feedback on the lift if the platform has any
- **desktop** — press-and-move to drag, click-and-release to select/edit, right-click for
  the menu, and no marquee text selection on the canvas

## Verdict: viable, with one premise that has to bend

Nothing here is a blocker. But the honest version is narrower than "delete the handle":
**leaf blocks lose their handle cleanly; sections cannot.** A section's children fill it
edge to edge, and no amount of gesture cleverness conjures a target that isn't there. The
fix is either padding, or keeping a small deliberate grab surface for nesting blocks only
— see §3.

This document was revised after an adversarial review; §6 is what that review found, and
several claims in the first draft were simply wrong (§7 names them).

**A standing caveat on all evidence below.** Everything was measured in emulated Chromium
against `web_static/tests/builder_harness.html`. That rig cannot reproduce the *native*
gesture layer of either mobile platform — verified: a 900ms CDP touch-hold fires no
`contextmenu` in emulation, though Chrome on Android does fire one on a real device. So
emulation is sound for hit-testing and event plumbing, and blind to platform gestures. The
first draft framed iOS as "the one untested thing"; Android is equally untested by this
method, and §6.1 is the bug that hid there.

---

## 1. Touch: hold-to-lift works, but only via a non-passive `touchmove`

This was the load-bearing unknown — today's touch drag works *only* because
`.cv2b-grip { touch-action: none }` (cv2_builder.css:302) hands the gesture to us before it
starts. A block body cannot have that: it is how the canvas scrolls on a phone. And
`touch-action` is latched at pointerdown, so it cannot be flipped when the hold fires.

The technique that does work — the one SortableJS and react-beautiful-dnd ship — is a
**non-passive `touchmove` that `preventDefault()`s only once the hold has armed**. Measured
with `touch-action` left at its default:

| gesture | scrollTop | `pointercancel` | `pointermove` after arming | `touchmove.cancelable` |
|---|---|---|---|---|
| move immediately (no hold) | **105px** — scrolled | 1 | — (never armed) | — |
| hold 600ms, then move | **0** — did not scroll | 0 | 12 of 12 | `true` |

Both halves hold: a flick still scrolls, a hold-then-move survives with the full pointer
stream intact. Note the existing `ev.preventDefault()` on `pointermove` (cv2_builder.js:1823)
does **not** suppress panning — that is the grip's `touch-action` doing the work today.

**Attach it per gesture, not globally.** In `onPointerDown` for touch pointers, removed in
`cleanup()`. A permanent non-passive `touchmove` on `window` forces *every* touch scroll in
the page — canvas, palette, the bottom sheet's `overflow-y` body (css:1036), the emoji
picker list — to block on main-thread JS for the whole session, which is precisely the jank
passive listeners exist to prevent. The per-gesture listener also answers "what if it never
arms": it idles for one gesture and dies.

iOS Safari is untested here. The technique is what the mainstream libraries use on iOS, so
the risk is low, but it wants a check on a real phone. iOS also needs
`-webkit-touch-callout: none` on blocks or the hold raises the system callout.

## 2. Haptics: Android yes, iPhone no — from the docs, not from a probe

`navigator.vibrate(12)` returns `true` under emulated Chromium, but that return value only
means the call was not rejected; there is no vibration hardware in a container. The
conclusion is right — Android Chrome/Firefox have the Vibration API, **iOS Safari does not
implement it at all** and there is no web workaround — but it is documented, not measured.
Do not let it borrow credibility from the tables that are.

Call it `navigator.vibrate?.(12)`. Two consequences: iPhones lift silently, so the *visual*
lift has to carry the feedback alone (argues for an obvious scale + shadow, not a subtle
one); and Chrome may suppress the call for want of sticky user activation if a hold-to-drag
is the session's very first interaction.

## 3. Nesting: sections are the real cost

`pointerdown` resolves via `closest('.cv2b-blk')`, i.e. the *innermost* block. Without a
grip, a nesting block is grabbable only where its own chrome is not covered by a child.
Measured own-ring thickness in px, per edge:

| block | desktop L/R/T/B | phone L/R/T/B | own area |
|---|---|---|---|
| text / separator / link_button / thumbnail | whole body | whole body | **100%** |
| container | 14 / 12 / 18 / 20 | 32 / 12 / 30 / 32 | 39–44% |
| **section** | **0 / 0 / 8 / 10** | **0 / 0 / 20 / 22** | 20–67% |

Leaves are fine. **A section is not**: its text child plus accessory fill it edge to edge,
leaving an 8px top/bottom band on desktop — below a usable mouse target, far below the
~44px touch guidance.

This is not a *new* defect. `click` already uses the same `closest()` (cv2_builder.js:998),
so selecting a section already depends on that 8px band; the grip was the escape hatch that
made it not matter.

### The phone container ring is the grip gutter — measured

The first draft listed "containers are fine, 12–32px ring" and "reclaim the grip gutters"
as independent wins. They contradict each other. The phone container's 32px left ring **is**
`.cv2b-canvas .cv2-container { padding-left: 1.9rem }` (css:~1000), which exists only to
hold nested grips; the container's intrinsic padding is `0.6rem 0.75rem`
(cv2_preview.css:31). Re-measured with both grip gutters reclaimed:

| | phone container L | change |
|---|---|---|
| grips present | 32px | — |
| gutters reclaimed | **14px** | **−18px** |

Desktop is unaffected (that rule is inside the phone media query). So reclaiming the gutter
costs the phone container its best grab edge on the surface that can least afford it.
Either keep some of that padding as deliberate grab chrome, or extend the section fix to
containers — but do not bank both.

### Fixes, in preference order

1. **Pad the nesting blocks.** ~6–8px on a section's body takes its ring to ~14–18px
   desktop / ~26–30px phone. Cheap, improves selection too, no new gesture concepts.
2. **Keep a grab surface for nesting blocks only.** The honest concession: leaves get their
   whole body, sections and containers keep a small explicit handle. Reusing the existing
   `.cv2b-tag` chip is tempting — it is already rendered and already appears on
   hover/selection — but it carries a landmine: the tag is deliberately `pointer-events: none`
   (css:345–351) because it was swallowing 36% of the insert rail above its block, and
   `targetAt` hit-tests with `elementFromPoint`. Making it grabbable re-opens that exact
   regression unless it also stops overlapping the rail.
3. **~~Escalate to the selected block~~** — proposed in the first draft, **withdrawn**. See
   §6.2; it is worse than the problem it solves.

## 4. Desktop: press-and-move already paints a text selection

Confirmed — a plain press-and-move across two blocks selects
`"Weekly Reset\n⠿\nNightfall: The Corrupted\nimage URL missing"`. Needs `user-select: none`
on the canvas with `user-select: text` restored under `[data-editing]`. That combination was
tested against the live harness and behaves: Ctrl+A selects exactly the editor's content,
typing lands, and a marquee across the canvas selects nothing.

Incidental: that selection string contains `⠿`. The handle currently pollutes copy/paste,
and removing it fixes that for free.

## 5. Desktop: an `<img>` hijacks the drag

Starting a press on an image inside the canvas — a gallery tile, a section thumbnail — lets
the browser's native image drag take over and kill the gesture:

| | `dragstart` | `pointercancel` |
|---|---|---|
| as-is | 1 | 1 |
| with `img { -webkit-user-drag: none }` | 0 | 0 |

One CSS line for Chrome and Safari. Firefox does not implement `-webkit-user-drag`, so the
renderer's images also want `draggable="false"` (cv2_render.js:183, :236, and :158 for the
emoji that sit inside text blocks). Natively-draggable *selected text* is moot once §4's
`user-select: none` lands — the only selectable text left is inside `[data-editing]`, where
:1869 bails before a drag can start.

---

## 6. Hazards the first draft missed

**6.1 — Android's native `contextmenu` collides with the armed hold.** Chrome on Android
fires a real `contextmenu` at ~500ms of stationary touch. The canvas handler (:1019)
unconditionally `preventDefault`s, sets `state.sel`, calls `render()`, and opens the block
menu. Today this is masked: the 450ms timer opens the same menu ~50ms earlier, idempotently.
Under the proposal — especially with `LONG_PRESS_MS` lowered — every Android touch drag arms
the lift and *then* takes a native `contextmenu` that calls `render()` **mid-drag**,
rebuilding the canvas while `drag` is live and wiping `.cv2b-dragging` without re-running
`markValidTargets`. Fix is cheap (bail in that handler when a gesture is pending or the
event is touch-sourced) but it must be on the list. This is the bug the evidence rig was
structurally blind to.

**6.2 — Why "escalate to the selected block" is withdrawn.** The rule was: if a block is
already selected, a press inside it drags *that* block. Three problems:
- **You can no longer drag a child out of a selected parent** — every press inside goes to
  the parent. And for the commonest child kind there is no select-without-edit: clicking a
  text block goes straight to `startEdit` (:1010–1013), so on a phone the escape route is
  tap → contenteditable focus → **soft keyboard** → Esc → then hold-drag.
- **The mobile flow it mandates is physically obstructed.** Selecting opens the bottom-sheet
  inspector (`sheetDismissed = false` at :1007; `position: fixed; max-height: 55vh; z-index: 50`
  at css:1005–1022), and nothing hides it during a drag. `elementFromPoint` over the sheet
  returns sheet elements, so `targetAt` falls through to `nearestRailTarget` — whose rail
  rects *behind* the sheet are still live. Drops can arm rails the author cannot see, and
  the bottom autoscroll band sits under the sheet. Today that state is avoidable by
  grip-dragging without selecting; the fix would have made it the canonical section-drag path.
- The drag target becomes a function of selection state whose only indicator is an outline.

**6.3 — Multi-touch is unhandled, and body-dragging makes it routine.** No `isPrimary`
check, no `pointerId` filtering, no `setPointerCapture` anywhere in the file (0 matches). The
`move`/`up` closures (:1809–1830) react to *any* pointer: a second finger runs a second
`onPointerDown` whose `beginDrag` overwrites the single global `drag` (:1392), and whose
moves feed the first closure's threshold math against the wrong start point. Today this
needs two fingers on two grips — essentially never. With the whole canvas draggable, a thumb
resting on a block while the other hand scrolls, or an attempted pinch-zoom, hits it
constantly. Needs: ignore `!e.isPrimary` at :1868, filter `move`/`up`/`cancel` by `pointerId`.

**6.4 — Accessories silently become draggable for the first time.** `renderAccessory` (:555)
emits a `.cv2b-blk` with a `.cv2b-tag` and **no grip**, so today `onGrip` is always false and
an accessory can never start a drag. Making `gripOnly` always-true flips that. The model
mostly copes (`moveNode` handles `fromIdx === "acc"`, cv2_model.js:301), but a hold on a
thumbnail now lifts it and paints *every* rail red — a never-designed state — and the
acc-as-source path through `endDrag` (:1697–1717) has zero coverage. Decide it is a feature
or exclude it; do not ship it by accident.

**6.5 — The menu now opens under the finger that is about to release.** `showMenu` places the
menu's top-left at the pointer (:1897–1898), and `shouldSwallow` guards only the canvas click
handler (:985) and the document mousedown closer (:1886) — the menus' own click listeners
(:1935, :1960) are unguarded. Today the menu opens at 450ms and release comes later; in the
new model menu-open and the synthetic click are simultaneous *every time*. Needs a test that
the release cannot activate the item under the finger.

**6.6 — The editing block's own chrome stays draggable.** The `[data-editing]` bail at :1869
covers only the contenteditable div. Its sibling `.cv2b-edit-hint` (:485) and the block's
padding are outside it, so a press there would drag the block whose editor is open, tearing
it out mid-edit. One-line fix: also bail when the block is `state.editing`.

**6.7 — Lowering `LONG_PRESS_MS` to 250–300ms creates a new false-positive class.** Today a
300ms press-and-release is a tap. Under the proposal it becomes: vibrate at ~275ms, block
lifts, release opens a menu. Slow deliberate taps are common, and this moves the misfire
boundary into their range *and* makes the misfire louder. There is also a feedback
contradiction: the arm signal (lift + vibrate) says "drag me", and releasing then produces a
menu. Both mobile OSes open long-press menus *during* the hold; menu-on-release is
nonstandard and will read as lag. The threshold that "fixes" the slower menu makes this
worse — they trade against each other and the plan should not pretend otherwise.

## 7. Corrections to the first draft

- **Test claims were wrong twice.** `test_builder_drag.py` has **eight** tests, not six, and
  only **four** call `_grab` (:169, :205, :222, :296). Worse, both blocks they grab are
  *leaves*, so a rewritten press-the-body helper works for them only by luck — press a
  section or container body centre and you get the innermost child. Any new nesting-block
  drag test cannot reuse the helper without §3's targeting baked in. And `_grab`'s assertion
  at :154 ("the grip did not become visible on hover") is deleted behaviour, not carried-over
  behaviour. The "smaller than it looks" cost estimate leaned on this and should be re-read.
- **The copy list overclaimed.** Of four cited sites, only :419 (`title="Drag to move"`) and
  :634 name the handle. :334's empty-canvas hint describes *palette* drag and "Tap + Add";
  :2328 describes palette drag and right-click. Neither changes.
- **Container ring vs. gutter reclamation** — see §3, now measured.
- **Haptics were not measured** — see §2.

## 8. Accessibility — unchanged, but say it out loud

The grip's `title="Drag to move"` is the gesture's only textual affordance; the grip is a
non-focusable span. There is **no keyboard reorder at all** today: `onKey` (:2046–2107)
covers undo/redo/duplicate/delete/insert/escape, and "Move to top/bottom" lives solely
behind the right-click and long-press menus. So removing the grip does not regress keyboard
users — they were already at zero. But a change whose entire content is deleting the last
visible drag affordance should state that, and it is the natural moment to add `Alt`+arrow
reordering rather than deepen a pointer-only dependency.

## 9. What the change costs in code

`onPointerDown` (:1803) already models "a press is a pending gesture; what happens next
decides what it becomes", with a 6px `DRAG_THRESHOLD` and a long-press timer. The change is
to its branches, not its shape:

- `gripOnly` becomes always-true for mouse; the :1815 guard treating a body-move as a scroll
  stays, but only for touch-before-arming.
- the touch long-press at :1846 stops opening the menu and instead **arms** (vibrate, lift).
  Menu moves to release-without-move; drag to move-after-arm.
- add the per-gesture non-passive `touchmove` (§1).
- `swallowSyntheticClick()` still needed on the release-into-menu path, and now needs to
  cover the menus' own click listeners (§6.5).
- plus §6.1, §6.3, §6.4, §6.6 — each small, none optional.

Discoverability is the soft cost. `cursor: grab` on block hover covers desktop; on touch the
help text at :634 becomes the only cue and has to actually describe the hold.
