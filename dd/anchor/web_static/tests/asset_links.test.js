// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Cross-file CSS guards: which sheet owns what, and which sheet wins.
//
// There is no bundler, so nothing links a page's <script> to the stylesheet that script's
// output needs. That pairing is a convention, and conventions rot silently: a page that
// draws charts without charts.css renders real, correctly-shaped SVG in the wrong colours
// — nothing throws, nothing logs, and it looks plausible enough to miss.
//
// This is what replaced the copy: chart chrome used to be pasted into both stats.css and
// mirror_log.css (whose own comment admitted it "mirrors stats.css"), so the two had
// already drifted into different formattings of the same rules.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const STATIC_DIR = path.join(__dirname, "..");

const pages = () =>
  fs
    .readdirSync(STATIC_DIR)
    .filter((name) => name.endsWith(".html"))
    .map((name) => ({
      name,
      text: fs.readFileSync(path.join(STATIC_DIR, name), "utf8"),
    }));

test("a page that draws charts also loads the chart styles", () => {
  const missing = pages()
    .filter((p) => p.text.includes("/static/charts.js"))
    .filter((p) => !p.text.includes("/static/charts.css"))
    .map((p) => p.name);
  assert.deepEqual(
    missing,
    [],
    "these pages load charts.js without charts.css, so charts render unstyled",
  );
});

test("a page that styles a rendered post also loads the renderer", () => {
  // The mirror image of the charts rule. cv2_preview.css styles what cv2_render.js
  // emits; a page with the sheet and no renderer shows nothing, and a page with the
  // renderer and no sheet shows an unstyled pile of divs that still reads as a message
  // — plausible enough to ship by accident.
  const mismatched = pages()
    .filter(
      (p) =>
        p.text.includes("/static/cv2_preview.css") !==
        p.text.includes("/static/cv2_render.js"),
    )
    .map((p) => p.name);
  assert.deepEqual(
    mismatched,
    [],
    "cv2_preview.css and cv2_render.js must be loaded together",
  );
});

test("cv2_render.js is loaded after the model it consumes", () => {
  // Load order IS the dependency graph here — cv2_render.js reads window.CV2Model at
  // definition time, so a page that lists it first gets `undefined` and dies on the
  // first render with a message that points nowhere near the real mistake. Two shared
  // files were manageable by convention; four are not.
  const wrong = pages()
    .filter((p) => p.text.includes("/static/cv2_render.js"))
    .filter((p) => {
      const model = p.text.indexOf("/static/cv2_model.js");
      return model === -1 || model > p.text.indexOf("/static/cv2_render.js");
    })
    .map((p) => p.name);
  assert.deepEqual(
    wrong,
    [],
    "these pages load cv2_render.js without cv2_model.js before it",
  );
});

test("the Tom Select dark theme is loaded after the sheet it overrides", () => {
  // Same class of failure as the cv2_model/cv2_render order above, but in CSS, where it
  // is quieter still: nothing is undefined and nothing throws — the vendored LIGHT theme
  // simply cascades in last and wins, so the picker renders dark-on-white inside an
  // otherwise dark page. tom_select_dark.css's own header says "link this file AFTER the
  // vendored sheet"; a comment is not a guard, and there are three pages relying on it.
  const VENDOR = "/static/vendor/tom-select.min.css";
  const DARK = "/static/tom_select_dark.css";
  const wrong = pages()
    .filter((p) => p.text.includes(VENDOR) || p.text.includes(DARK))
    .filter((p) => {
      const vendor = p.text.indexOf(VENDOR);
      const dark = p.text.indexOf(DARK);
      return vendor === -1 || dark === -1 || vendor > dark;
    })
    .map((p) => p.name);
  assert.deepEqual(
    wrong,
    [],
    "load the vendored Tom Select sheet first, then tom_select_dark.css",
  );
});

test("a page with a Tom Select picker loads the widget and its styles", () => {
  // The pairing in the other direction: the sheets style what the vendored script builds,
  // so either half alone is wrong — markup with no widget, or a widget with no theme.
  const SCRIPT = "/static/vendor/tom-select.complete.min.js";
  const DARK = "/static/tom_select_dark.css";
  const mismatched = pages()
    .filter((p) => p.text.includes(SCRIPT) !== p.text.includes(DARK))
    .map((p) => p.name);
  assert.deepEqual(
    mismatched,
    [],
    "tom-select.complete.min.js and tom_select_dark.css must be loaded together",
  );
});

test("charts.css is not loaded by pages that draw no charts", () => {
  // The reverse direction, so the sheet does not quietly become a second shared.css.
  const pointless = pages()
    .filter((p) => p.text.includes("/static/charts.css"))
    .filter((p) => !p.text.includes("/static/charts.js"))
    .map((p) => p.name);
  assert.deepEqual(pointless, [], "these pages load charts.css but draw no charts");
});

test("chart chrome lives only in charts.css", () => {
  // The duplication this file exists to prevent: a page sheet re-styling what charts.js
  // emits. Page-level *placement* of a chart is fine (stats sets its own margin), so only
  // the classes charts.js actually draws are off limits.
  const OWNED = [
    "chart-svg",
    "chart-grid",
    "chart-tick",
    "chart-line",
    "chart-dot",
    "chart-bar",
    "chart-crosshair",
    "chart-overlay",
    "chart-empty",
    "chart-legend",
    "chart-tooltip",
    "legend-item",
    "legend-key",
    "spark-line",
    "sparkline",
  ];
  const sheets = fs
    .readdirSync(STATIC_DIR)
    .filter((n) => n.endsWith(".css") && n !== "charts.css");

  const offenders = [];
  for (const name of sheets) {
    const text = fs.readFileSync(path.join(STATIC_DIR, name), "utf8");
    for (const cls of OWNED) {
      if (new RegExp(`\\.${cls}\\b[^;{]*\\{`).test(text)) {
        offenders.push(`${name}: .${cls}`);
      }
    }
  }
  assert.deepEqual(offenders, [], "move these into charts.css rather than restating them");
});


// --- shared.css must not out-specify the pages it serves -----------------------------

test("the shared focus ring stays at element specificity", () => {
  // A class in this selector lifts it to (0,2,1), above the page overrides at (0,2,0)
  // that exist precisely to change it — stats' 1px search ring and its INSET -2px
  // segmented control. Adding `:not(.no-focus-ring)` here did exactly that, and the only
  // symptom was two rings quietly changing offset. The opt-out is its own rule instead.
  const css = fs
    .readFileSync(path.join(STATIC_DIR, "shared.css"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, ""); // comments discuss selectors; only rules count
  const rule = css.match(/([^\n{}]*:focus-visible[^{]*)\{[^}]*outline:\s*2px/);
  assert.ok(rule, "shared.css should define one element-level focus ring");
  assert.doesNotMatch(
    rule[1],
    /\.[a-zA-Z]/,
    `the shared focus ring must not carry a class selector — found: ${rule[1].trim()}`,
  );
});

test("no page carries an executable inline script", () => {
  // This is the invariant `script-src 'self'` rests on (SECURITY_HEADERS in
  // dd/anchor/web.py). A `<script>` with no `src` and no non-executable `type` is
  // exactly what CSP blocks — and it would fail in production only, on a page a
  // developer had already tested locally without the header. Catch it here instead.
  //
  // `type="application/json"` is allowed: the three templated pages ship their
  // server-injected data that way, and CSP treats a non-executable type as data.
  const offenders = [];
  for (const page of pages()) {
    for (const [, attrs] of page.text.matchAll(/<script([^>]*)>/g)) {
      const hasSrc = /\ssrc=/.test(attrs);
      const isData = /\stype="application\/json"/.test(attrs);
      if (!hasSrc && !isData) offenders.push(`${page.name}: <script${attrs}>`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "inline scripts are blocked by script-src 'self' — move them to /static/",
  );
});

test("a page driven by initPostForm has every element it dereferences", () => {
  // shared.js reaches for these by id and assigns straight through
  // (`_byId("createBtn").hidden = …`), so a missing one is a TypeError on page load,
  // not a degraded form. That is exactly what happened while editing these templates:
  // an over-greedy edit removed weekly_reset_form.html's whole toolbar, and nothing
  // failed — the JS tests don't load pages and the Python tests don't read them.
  //
  // The ids are shared.js's contract with both hybrid-post forms, which its own header
  // comment describes as "the SAME element ids". Deriving them from the source keeps
  // this honest if that contract grows.
  const sharedJs = fs.readFileSync(path.join(STATIC_DIR, "shared.js"), "utf8");
  const required = [
    ...new Set(
      [...sharedJs.matchAll(/_byId\("([a-zA-Z]+)"\)/g)].map((m) => m[1]),
    ),
  ];
  assert.ok(required.length > 5, "expected shared.js to dereference several ids");

  // Which pages drive the form lifecycle. Derived from the SCRIPTS, because a shell
  // never names `initPostForm` itself — it loads a page script that calls it. Filtering
  // the pages on that string directly matched nothing at all, so the first version of
  // this guard passed on a page with its whole toolbar deleted.
  const drivers = fs
    .readdirSync(STATIC_DIR)
    .filter((n) => n.endsWith(".js") && n !== "shared.js")
    .filter((n) =>
      fs.readFileSync(path.join(STATIC_DIR, n), "utf8").includes("initPostForm("),
    );
  assert.ok(drivers.length > 1, "expected both hybrid-post forms to call initPostForm");

  const inspected = [];
  const missing = [];
  for (const page of pages()) {
    if (!drivers.some((d) => page.text.includes(`/static/${d}`))) continue;
    inspected.push(page.name);
    for (const id of required) {
      if (!page.text.includes(`id="${id}"`)) missing.push(`${page.name}: #${id}`);
    }
  }
  // The guard on the guard. A filter that selects nothing reports no problems, which is
  // exactly how this test passed while being inert.
  assert.equal(
    inspected.length,
    drivers.length,
    `expected one page per driver script; inspected ${JSON.stringify(inspected)}`,
  );
  assert.deepEqual(missing, [], "shared.js will throw on these pages");
});

// A page script calling a helper that exists nowhere throws on first use — and if that
// use is the first line of a click handler, the button disables and never recovers.
// That shipped once: bungie_account.js called busy() when the only definition lived in
// autopost_settings.js. shared.js's helpers are top-level functions (so plain globals),
// so a bare call is fine — what must hold is that the helper is DEFINED somewhere and
// that the page actually loads shared.js.
//
// The helper list is DERIVED from shared.js's `window.x =` exports, not hardcoded: a
// hardcoded list leaves the next shared helper uncovered until someone remembers, which
// is the very failure this exists to catch. The export is also the real contract —
// deleting the `window.` lines would keep a `function say` assertion green while
// breaking every page.
test("page scripts only call shared helpers that shared.js defines", () => {
  const SHARED_JS = fs.readFileSync(path.join(STATIC_DIR, "shared.js"), "utf8");
  const HELPERS = [...SHARED_JS.matchAll(/^window\.(\w+)\s*=/gm)]
    .map((m) => m[1])
    // Not a helper — the page's server-injected data, assigned to the same namespace.
    .filter((n) => n !== "__BOOTSTRAP__");
  const offenders = [];

  // The guard on the guard, matching the one above: an empty list finds no offenders.
  assert.ok(
    HELPERS.length >= 3,
    `expected shared.js to export several helpers, found ${JSON.stringify(HELPERS)}`,
  );

  const HOSTS = pages();

  for (const name of fs.readdirSync(STATIC_DIR).filter((n) => n.endsWith(".js"))) {
    if (name === "shared.js") continue;
    const text = fs
      .readFileSync(path.join(STATIC_DIR, name), "utf8")
      // Comments mention these helpers by name; only real calls matter.
      .replace(/\/\/[^\n]*/g, "")
      .replace(/\/\*[\s\S]*?\*\//g, "");
    const used = HELPERS.filter(
      (h) =>
        new RegExp(`(?:^|[^.\\w])${h}\\s*\\(`, "m").test(text) &&
        // A file may define its own — cv2_builder_page.js has an api() of its own
        // shape, scoped to the draft routes, and wants nothing from shared.js.
        !new RegExp(`function\\s+${h}\\b`).test(text),
    );
    if (!used.length) continue;

    // Every page that loads this script must also load shared.js before it.
    for (const host of HOSTS.filter((p) => p.text.includes(`/static/${name}`))) {
      const shared = host.text.indexOf("/static/shared.js");
      if (shared === -1 || shared > host.text.indexOf(`/static/${name}`)) {
        offenders.push(
          `${host.name}: loads ${name} (uses ${used.join(", ")}) without shared.js before it`,
        );
      }
    }
  }

  assert.deepEqual(offenders, [], "load shared.js before the script that calls into it");
});
