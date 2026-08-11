// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// A design-token guard for the web_static stylesheets and inline page styles.
//
// Every colour in the panel comes from a token in shared.css's :root, so that moving the
// palette moves the whole app. Three tokens were being painted from that had never been
// defined — `--muted`, `--danger` and `--warning`, plausible-looking neighbours of the
// real `--text-muted`, `--danger-text` and `--warn` — across four surfaces, and neither
// palette change reached any of them.
//
// The unfallbacked ones fail in a way that is genuinely hard to see. `var(--muted)` on an
// undefined property is invalid at computed-value time: the declaration is DROPPED, and
// because `color` inherits, the element renders at its parent's colour. So the Bungie
// page's hints and both authoring forms' confirm dialogs were drawing secondary text at
// full body brightness, looking merely a bit bold rather than broken. One of them was
// `background: var(--muted)` on a status dot, which computed to transparent — an
// invisible dot, on a page nobody looks at twice.
//
// The fallbacked ones fail quietly instead: `var(--danger, #d9534f)` never consulted the
// palette at all, it just painted a red that appears nowhere else in the app.
//
// This is a text check over `var(--x)` references and bare colour literals. It proves the
// palette is reachable from one place; it cannot prove any colour is the RIGHT one, and it
// deliberately allows a short list of literals that exist for stated reasons.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const STATIC_DIR = path.join(__dirname, "..");
const SHARED_CSS = fs.readFileSync(path.join(STATIC_DIR, "shared.css"), "utf8");

/** Every custom property defined in shared.css (:root, and the reduced-motion block). */
function definedTokens() {
  return new Set([...SHARED_CSS.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gm)].map((m) => m[1]));
}

/** Stylesheets and pages we author. The vendored Tom Select sheet is not ours. */
function ourFiles() {
  return fs
    .readdirSync(STATIC_DIR)
    .filter((n) => n.endsWith(".css") || n.endsWith(".html"))
    .map((n) => ({ name: n, text: fs.readFileSync(path.join(STATIC_DIR, n), "utf8") }));
}

// Runtime values set from JS (measured heights, drag offsets) rather than design tokens.
// They legitimately carry a fallback because they are absent until a script runs.
const RUNTIME_PREFIXES = ["--cv2b-"];

test("every var(--token) names a token shared.css actually defines", () => {
  const defined = definedTokens();
  const unknown = [];
  for (const { name, text } of ourFiles()) {
    for (const m of text.matchAll(/var\(\s*(--[a-z0-9-]+)/g)) {
      const token = m[1];
      if (defined.has(token)) continue;
      if (RUNTIME_PREFIXES.some((p) => token.startsWith(p))) continue;
      unknown.push(`${name}: var(${token})`);
    }
  }
  assert.deepEqual(
    unknown,
    [],
    "these paint from a token that does not exist — an unfallbacked one inherits " +
      "instead, and a fallbacked one silently ignores the palette",
  );
});

// Literals that are deliberate, each with its reason stated where it is written. Keep
// this list short: an entry here is a promise that the colour must NOT follow the palette.
const ALLOWED_LITERALS = new Map([
  // A pure-white switch knob clears the 3:1 non-text floor on the pink fill (3.57:1)
  // where --text-1 sits at 3.01:1 — see settings_page.css.
  ["settings_page.css", ["#fff"]],
  // The slate section chrome of the post preview, which imitates Discord rather than
  // this panel — see cv2_preview.css.
  ["cv2_preview.css", []],
]);

test("colour literals outside shared.css are declared deliberate", () => {
  const strays = [];
  for (const { name, text } of ourFiles()) {
    if (name === "shared.css") continue; // the palette itself lives here
    const allowed = ALLOWED_LITERALS.get(name) || [];
    // Blank out comments rather than trying to recognise one line at a time: prose
    // explaining why a colour is NOT used still contains the colour, and a per-line
    // heuristic gets multi-line block comments wrong in both directions. Replacing each
    // comment with spaces of equal length keeps every later offset (and line number)
    // exactly where it was.
    const blank = (src) =>
      src
        .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "))
        .replace(/<!--[\s\S]*?-->/g, (c) => c.replace(/[^\n]/g, " "));
    const code = blank(text);
    for (const m of code.matchAll(/#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b/g)) {
      if (allowed.includes(m[0])) continue;
      const line = code.slice(0, m.index).split("\n").length;
      strays.push(`${name}:${line} ${m[0]}`);
    }
  }
  assert.deepEqual(
    strays,
    [],
    "use a token, or add the colour to ALLOWED_LITERALS with its reason at the site",
  );
});
