// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// A duration-token guard for the web_static stylesheets.
//
// Motion durations are tokens (--dur-fast / --dur / --dur-slow, defined in shared.css)
// for one reason: the prefers-reduced-motion block can only switch off motion it can
// reach. A hand-written `0.15s` keeps animating for a user who asked the OS to stop, and
// nothing about the page looks wrong to the author who added it — the failure is
// invisible unless you are the person it affects.
//
// The secondary reason is drift. Before the tokens existed there were six transitions
// across five files at 0.08s, 0.1s, 0.12s, 0.15s and 0.16s — five different answers to
// the same question, none of them deliberate.
//
// This is a text check. It proves durations are centralized and therefore switchable; it
// cannot prove any animation looks right, or that the reduced-motion block does what its
// name says. It also does not cover the View Transitions pseudo-elements, whose default
// animations come from UA styles rather than these tokens (see shared.css), or WAAPI
// durations passed as numbers in JS — both have to gate on matchMedia in JS instead.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const STATIC_DIR = path.join(__dirname, "..");
const SHARED_CSS = fs.readFileSync(path.join(STATIC_DIR, "shared.css"), "utf8");

// --dur-stagger is a DELAY rather than a duration, and it is in this list for the same
// reason the durations are: a hand-written `40ms` between the landing page's groups would
// sail straight through the reduced-motion collapse, which is the one failure the author
// of the delay can never see.
const TOKENS = ["--dur-fast", "--dur", "--dur-slow", "--dur-stagger"];

/** Every stylesheet we author: .css plus .html (pages inline their own <style>, which is
 *  where the switch on autopost_settings.html hid its durations from a CSS-only sweep).
 *  This read is deliberately NOT recursive: vendor/ is a subdirectory, and tom-select's
 *  minified CSS is full of literal durations we neither own nor edit. */
function authoredSheets() {
  return fs
    .readdirSync(STATIC_DIR)
    .filter((name) => /\.(css|html)$/.test(name))
    .map((name) => ({
      name,
      text: fs.readFileSync(path.join(STATIC_DIR, name), "utf8"),
    }));
}

// A transition/animation declaration and its value. Anchored on a preceding boundary so
// `--transition-foo:` (a custom property) does not match, and the optional suffix keeps
// `transition-behavior:` out — it carries no duration.
const DECL = /(?:^|[;{\s])(transition|animation)(?:-duration|-delay)?\s*:\s*([^;}]*)/gi;

// A raw CSS time literal: `.15s`, `0.08s`, `140ms`, `2s`. Must start at a value boundary
// so an identifier that merely ends in a digit and an s (`slide2s`) is not a match.
const TIME_LITERAL = /(?:^|[\s,(])(\d*\.?\d+m?s)(?=$|[\s,;)])/;

test("shared.css defines the motion duration tokens", () => {
  for (const token of TOKENS) {
    assert.match(
      SHARED_CSS,
      new RegExp(`${token}\\s*:\\s*\\d`),
      `shared.css must define ${token} — the whole guard rests on it existing`,
    );
  }
});

test("shared.css zeroes the duration tokens under prefers-reduced-motion", () => {
  const block = SHARED_CSS.match(
    /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[\s\S]*?\n\}/,
  );
  assert.ok(block, "shared.css must carry a prefers-reduced-motion block");
  for (const token of TOKENS) {
    assert.match(
      block[0],
      new RegExp(`${token}\\s*:`),
      `the reduced-motion block must reset ${token}; an unreset token keeps animating`,
    );
  }
  // `animation: none` would stop transitionend/animationend firing, which would hang any
  // JS awaiting an animation (the drag ghost's landing). Near-zero durations still fire.
  assert.doesNotMatch(
    block[0],
    /animation\s*:\s*none/,
    "reset the durations, not `animation: none` — JS awaits these events",
  );
});

test("no authored stylesheet hardcodes a motion duration", () => {
  const offenders = [];
  for (const { name, text } of authoredSheets()) {
    for (const [, prop, value] of text.matchAll(DECL)) {
      const hit = value.match(TIME_LITERAL);
      if (hit) offenders.push(`${name}: ${prop}: ${value.trim()}  (${hit[1]})`);
    }
  }
  // deepEqual prints the offending entries itself, so the message only has to say
  // what to do about them.
  assert.deepEqual(
    offenders,
    [],
    "use var(--dur-fast|--dur|--dur-slow) instead of a literal duration",
  );
});
