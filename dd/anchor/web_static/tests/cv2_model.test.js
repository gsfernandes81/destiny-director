// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Unit tests for cv2_model.js — the DOM-free client mirror of cv2_nodes.py.
// Run with `make test-js` (node --test); no browser, no bundler.
//
// The cases worth having here are the ones a rendering bug would hide: the nesting
// rules, path rebasing after a removal (two real bugs found while prototyping), the
// validation paths the UI anchors errors to, and markdown escaping.

// `<t:…>` tokens render in the VIEWER'S timezone now, so a test asserting one is only
// reproducible with the zone pinned. Set before requiring anything that reads a clock —
// Node picks TZ up on assignment, so this holds wherever the suite runs.
process.env.TZ = "UTC";

const test = require("node:test");
const assert = require("node:assert/strict");

const M = require("../cv2_model.js");

const text = (content) => ({ type: M.TEXT_DISPLAY, content: content });
const container = (children) => ({ type: M.CONTAINER, components: children || [] });
const section = (texts, accessory) => ({
  type: M.SECTION,
  components: texts,
  accessory: accessory,
});
const linkButton = (label, url) => ({
  type: M.ACTION_ROW,
  components: [{ type: M.BUTTON, style: 5, label: label, url: url }],
});

const contents = (list) => list.map((n) => n.content);

// --- classification ------------------------------------------------------------------

test("kind() classifies every authorable type", () => {
  assert.equal(M.kind(text("x")), "text");
  assert.equal(M.kind(container()), "container");
  assert.equal(M.kind({ type: M.SEPARATOR }), "separator");
  assert.equal(M.kind({ type: M.MEDIA_GALLERY }), "media");
  assert.equal(M.kind({ type: M.THUMBNAIL }), "thumbnail");
  // Both a bare button and a button wrapped in an action row are "link_button".
  assert.equal(M.kind({ type: M.BUTTON }), "link_button");
  assert.equal(M.kind({ type: M.ACTION_ROW }), "link_button");
  assert.equal(M.kind({ type: 999 }), "unknown");
  assert.equal(M.kind(null), "unknown");
});

test("makeContainer only seeds an accent when given one", () => {
  assert.equal(M.makeContainer(0xec42a5).accent_color, 0xec42a5);
  assert.ok(!("accent_color" in M.makeContainer()));
});

test("a fresh section starts with one text block, not zero", () => {
  // An empty section is invalid, so starting empty would greet the author with an error.
  assert.equal(M.makeSection().components.length, 1);
});

// --- nesting rules -------------------------------------------------------------------

test("containers are top level only", () => {
  const nodes = [container([])];
  assert.ok(M.allowedIn(nodes, []).includes("container"));
  assert.ok(!M.allowedIn(nodes, [0]).includes("container"));
});

test("a section accepts text blocks only", () => {
  const nodes = [section([text("a")], null)];
  assert.deepEqual(M.allowedIn(nodes, [0]), ["text"]);
});

test("a node cannot be dropped into itself or its own descendants", () => {
  const nodes = [container([section([text("a")], null)])];
  assert.ok(!M.canDrop(nodes, [0], "text", [0]));
  assert.ok(!M.canDrop(nodes, [0, 0], "text", [0]));
});

test("a full section refuses new text but still allows reordering its own", () => {
  const nodes = [section([text("a"), text("b"), text("c")], null)];
  assert.ok(!M.canDrop(nodes, [0], "text", null));
  assert.ok(M.canDrop(nodes, [0], "text", [0, 2]));
});

test("refusalReason explains the rule rather than just refusing", () => {
  const nodes = [container([])];
  assert.match(M.refusalReason(nodes, [0], "container"), /top level only/);
  assert.match(M.refusalReason(nodes, [], "thumbnail"), /accessory/);

  const full = [section([text("a"), text("b"), text("c")], null)];
  assert.match(M.refusalReason(full, [0], "text"), /at most 3/);
  assert.match(M.refusalReason(full, [0], "link_button"), /accessory slot/);
});

// --- path helpers --------------------------------------------------------------------

test("resolve() walks child indices and the accessory segment", () => {
  const acc = { type: M.THUMBNAIL, media: { url: "https://e.invalid/a.png" } };
  const nodes = [container([section([text("deep")], acc)])];
  assert.equal(M.resolve(nodes, [0, 0, 0]).content, "deep");
  assert.equal(M.resolve(nodes, [0, 0, "acc"]), acc);
});

test("childList() returns the real array so callers can splice it", () => {
  const nodes = [container([])];
  M.childList(nodes, [0]).push(text("added"));
  assert.equal(nodes[0].components.length, 1);
});

test("adjustAfterRemoval rebases paths that descend past the removed node", () => {
  // Removing index 0 shifts [1] -> [0], and anything under it.
  assert.deepEqual(M.adjustAfterRemoval([1], [0]), [0]);
  assert.deepEqual(M.adjustAfterRemoval([1, 3], [0]), [0, 3]);
  // Earlier siblings and unrelated branches are untouched.
  assert.deepEqual(M.adjustAfterRemoval([0], [1]), [0]);
  assert.deepEqual(M.adjustAfterRemoval([0, 5], [1]), [0, 5]);
  // Removing an accessory is a field delete — no index shifts at all.
  assert.deepEqual(M.adjustAfterRemoval([1, 2], [0, "acc"]), [1, 2]);
});

// --- mutations -----------------------------------------------------------------------

test("moveNode drags a top-level block into a container that sits below it", () => {
  // The regression that motivated adjustAfterRemoval: removing "A" shifts the
  // container from index 1 to index 0 while we are holding the path [1].
  const nodes = [text("A"), container([text("B")])];
  const at = M.moveNode(nodes, [0], [1], 1);
  assert.equal(nodes.length, 1);
  assert.deepEqual(contents(nodes[0].components), ["B", "A"]);
  assert.deepEqual(at, [0, 1]);
});

test("moveNode reorders within one scope without an off-by-one", () => {
  const nodes = [text("A"), text("B"), text("C")];
  M.moveNode(nodes, [0], [], 3); // A to the end
  assert.deepEqual(contents(nodes), ["B", "C", "A"]);
  M.moveNode(nodes, [2], [], 0); // A back to the front
  assert.deepEqual(contents(nodes), ["A", "B", "C"]);
});

test("moveNode promotes a child out of its container", () => {
  const nodes = [container([text("X"), text("Y")])];
  M.moveNode(nodes, [0, 0], [], 1);
  assert.deepEqual(contents(nodes[0].components), ["Y"]);
  assert.equal(nodes[1].content, "X");
});

test("removeAt returns the next sensible selection", () => {
  const nodes = [text("A"), text("B"), text("C")];
  assert.deepEqual(M.removeAt(nodes, [1]), [1]); // C shifted into slot 1
  assert.deepEqual(contents(nodes), ["A", "C"]);
  assert.deepEqual(M.removeAt(nodes, [1]), [0]); // clamps to the last remaining
  assert.equal(M.removeAt(nodes, [0]), null); // nothing left to select
});

test("removeAt on an accessory deletes the field and selects the section", () => {
  const nodes = [section([text("a")], { type: M.THUMBNAIL, media: { url: "" } })];
  assert.deepEqual(M.removeAt(nodes, [0, "acc"]), [0]);
  assert.ok(!nodes[0].accessory);
});

test("setAccessory unwraps an action row to a bare button", () => {
  // Discord accepts a bare button as a section accessory, never an action row.
  const nodes = [section([text("a")], null)];
  M.setAccessory(nodes, [0], linkButton("Go", "https://e.invalid"));
  assert.equal(nodes[0].accessory.type, M.BUTTON);
  assert.equal(nodes[0].accessory.label, "Go");
});

// --- validation ----------------------------------------------------------------------

test("an empty message is a problem with no path", () => {
  const problems = M.validate([]);
  assert.equal(problems.length, 1);
  assert.equal(problems[0].path, null);
  assert.match(problems[0].msg, /empty/);
});

test("more than 10 top-level blocks is refused", () => {
  const nodes = Array.from({ length: 11 }, (_, i) => text("t" + i));
  assert.ok(M.validate(nodes).some((p) => /Too many top-level/.test(p.msg)));
});

test("problems point at the offending node, not its parent", () => {
  const nodes = [container([text("  ")])];
  const problems = M.validate(nodes);
  const empty = problems.find((p) => /text block is empty/.test(p.msg));
  assert.deepEqual(empty.path, [0, 0]);
});

test("a section reports both arity and a missing accessory", () => {
  const problems = M.validate([section([], null)]);
  assert.ok(problems.some((p) => /1–3 text blocks/.test(p.msg)));
  assert.ok(problems.some((p) => /missing its accessory/.test(p.msg)));
});

test("an incomplete accessory is reported against the accessory itself", () => {
  const nodes = [section([text("a")], { type: M.THUMBNAIL, media: { url: "" } })];
  const problem = M.validate(nodes).find((p) => /thumbnail has no image URL/.test(p.msg));
  assert.deepEqual(problem.path, [0, "acc"]);

  const withButton = [section([text("a")], { type: M.BUTTON, style: 5, label: "x" })];
  const btnProblem = M.validate(withButton).find((p) => /label and a URL/.test(p.msg));
  assert.deepEqual(btnProblem.path, [0, "acc"]);
});

// --- emoji substitution --------------------------------------------------------------
// The mirror of cv2_nodes.substitute_emoji. The client needs it only to COUNT: the
// mention is what Discord's 4000-character cap measures, so counting the authored
// shortcodes would let the canvas say "Ready to post" about a tree the server refuses.

const GUILD_EMOJI = {
  armor: { url: "https://cdn.invalid/111.png", id: "111", animated: false },
  wave: { url: "https://cdn.invalid/222.gif", id: "222", animated: true },
};

test("shortcodes become mentions, and an animated one keeps its <a: prefix", () => {
  const out = M.substituteEmoji([text(":armor: and :wave:")], GUILD_EMOJI);
  assert.equal(out[0].content, "<:armor:111> and <a:wave:222>");
});

test("substitution leaves resolved mentions, unknown names and labels alone", () => {
  const out = M.substituteEmoji([text("<:armor:111> :nope:")], GUILD_EMOJI);
  assert.equal(out[0].content, "<:armor:111> :nope:");
  // Discord renders no markdown in a button label; its emoji is a field of its own.
  const row = linkButton(":armor: Loot", "https://e.invalid");
  assert.equal(M.substituteEmoji([row], GUILD_EMOJI)[0].components[0].label, ":armor: Loot");
});

test("substitution reaches nested text and does not mutate the input", () => {
  const nodes = [container([section([text(":armor:")], { type: M.THUMBNAIL })])];
  const out = M.substituteEmoji(nodes, GUILD_EMOJI);
  assert.equal(out[0].components[0].components[0].content, "<:armor:111>");
  assert.equal(nodes[0].components[0].components[0].content, ":armor:");
});

test("the length check counts the substituted text, not the shortcodes", () => {
  const nodes = [text(":armor: ".repeat(400))]; // 3200 authored, 4800 resolved
  assert.deepEqual(M.validate(nodes), []);
  assert.ok(M.validate(nodes, GUILD_EMOJI).some((p) => /Too much text/.test(p.msg)));
});

// --- multi-button rows ---------------------------------------------------------------
// A row loaded from a live post can hold up to five buttons. Assuming one meant the
// second was rendered but not editable, and its missing URL slipped past validation.

const twoButtonRow = (b2) => ({
  type: M.ACTION_ROW,
  components: [
    { type: M.BUTTON, style: 5, label: "More Details", url: "https://e.invalid/a" },
    Object.assign({ type: M.BUTTON, style: 5 }, b2),
  ],
});

test("buttonsOf returns every button in a row", () => {
  const row = twoButtonRow({ label: "Support Us", url: "https://e.invalid/b" });
  assert.equal(M.buttonsOf(row).length, 2);
  assert.equal(M.buttonsOf(row)[1].label, "Support Us");
  // buttonOf still means "the first", for the accessory/label cases that want it.
  assert.equal(M.buttonOf(row).label, "More Details");
});

test("buttonsOf treats a bare accessory button as a row of one", () => {
  const bare = { type: M.BUTTON, style: 5, label: "Go", url: "https://e.invalid" };
  assert.deepEqual(M.buttonsOf(bare), [bare]);
});

test("an incomplete SECOND button is caught, naming which one", () => {
  const problems = M.validate([twoButtonRow({ label: "Support Us" })]); // no url
  const hit = problems.find((p) => /Button 2/.test(p.msg));
  assert.ok(hit, "second button not validated: " + JSON.stringify(problems));
  assert.match(hit.msg, /label and a URL/);
});

test("a complete two-button row validates clean", () => {
  const row = twoButtonRow({ label: "Support Us", url: "https://e.invalid/b" });
  assert.deepEqual(M.validate([row]), []);
});

test("a single-button row keeps the original message, not 'Button 1'", () => {
  const problems = M.validate([linkButton("Go", "")]);
  assert.match(problems[0].msg, /^A link button needs/);
});

test("an empty row is reported rather than silently passing", () => {
  const problems = M.validate([{ type: M.ACTION_ROW, components: [] }]);
  assert.ok(problems.some((p) => /no buttons/.test(p.msg)));
});

test("a link button needs both a label and a URL", () => {
  assert.ok(M.validate([linkButton("Go", "")]).some((p) => /label and a URL/.test(p.msg)));
  assert.ok(M.validate([linkButton("", "https://e.invalid")]).length > 0);
  assert.equal(M.validate([linkButton("Go", "https://e.invalid")]).length, 0);
});

test("a fully-formed message validates clean", () => {
  const nodes = [
    container([
      text("# Weekly Reset"),
      section([text("body")], { type: M.THUMBNAIL, media: { url: "https://e.invalid/a.png" } }),
      linkButton("More", "https://e.invalid/more"),
    ]),
  ];
  assert.deepEqual(M.validate(nodes), []);
});

// --- markdown ------------------------------------------------------------------------

test("headings, small text and bullets render to the shared md-* classes", () => {
  assert.match(M.renderMd("# Hi"), /md-h1/);
  assert.match(M.renderMd("## Hi"), /md-h2/);
  assert.match(M.renderMd("### Hi"), /md-h3/);
  assert.match(M.renderMd("-# fine print"), /md-small/);
  assert.match(M.renderMd("- item"), /md-bullet/);
});

test("text leaves are escaped", () => {
  const out = M.renderMd("<script>alert(1)</script>");
  assert.ok(!out.includes("<script>"));
  assert.match(out, /&lt;script&gt;/);
});

test("only http(s) links become anchors", () => {
  assert.match(M.renderMd("[x](https://e.invalid)"), /<a href="https:\/\/e\.invalid"/);
  // A javascript: URL is left as literal text — it never reaches an href.
  const bad = M.renderMd("[x](javascript:alert(1))");
  assert.ok(!bad.includes("<a "));
  assert.ok(!bad.includes("href"));
  assert.match(bad, /\[x\]\(javascript:/);
});

test("a quote in link text cannot break out of the href attribute", () => {
  const out = M.renderMd('[a"onerror="x](https://e.invalid)');
  assert.ok(!out.includes('"onerror="'));
  assert.match(out, /&quot;/);
});

test("known emoji shortcodes resolve, unknown ones stay as text", () => {
  const emoji = { kyber: "https://cdn.invalid/1.png" };
  assert.match(M.renderMd("hi :kyber:", emoji), /<img class="emoji"/);
  assert.match(M.renderMd("hi :nope:", emoji), /:nope:/);
  assert.ok(!M.renderMd("hi :nope:", emoji).includes("<img"));
});

test("a full custom emoji from a live post resolves off the CDN, intact", () => {
  // The regression: a chain of .replace() passes swapped the inner `:name:` of
  // `<:name:id>` for an <img> and left a stray "<" and "id>" on screen. Content seeded
  // from a real post is full of these, and no emoji map is needed to render them.
  const out = M.renderMd("<:champion_barrier:849727805994565662> Excavation Site");
  assert.match(out, /cdn\.discordapp\.com\/emojis\/849727805994565662\.png/);
  assert.ok(!out.includes("&lt;"), "stray < left over: " + out);
  assert.ok(!out.includes("849727805994565662<"), "stray id left over: " + out);
  assert.ok(!out.includes("&gt;"), "stray > left over: " + out);
  assert.match(out, /Excavation Site/);
});

test("an animated custom emoji resolves to .gif", () => {
  assert.match(M.renderMd("<a:spin:123456789>"), /emojis\/123456789\.gif/);
});

test("a custom emoji renders without any emoji map at all", () => {
  // The map comes from a REST fetch that is allowed to fail; seeded posts must still
  // render, so the id path must not depend on it.
  assert.match(M.renderMd("<:x:99>", null), /emojis\/99\.png/);
});

test("emoji inside bold still resolves", () => {
  const out = M.renderMd("**hit :kyber: hard**", { kyber: "https://cdn.invalid/1.png" });
  assert.match(out, /<strong>/);
  assert.match(out, /<img class="emoji"/);
});

test("a <t:...> timestamp renders as a time, not as raw text", () => {
  // 1753894800 = 2025-07-30 17:00:00 UTC, and TZ is pinned to UTC at the top of this
  // file — these render in the VIEWER'S zone now, which is what Discord does, so
  // without that pin the expectations would depend on where the suite ran.
  const at = 1753894800;
  assert.match(M.renderMd(`Changes daily at <t:${at}:t> local time.`), /5:00 PM/);
  assert.match(M.renderMd(`<t:${at}:T>`), /5:00:00 PM/);
  assert.match(M.renderMd(`<t:${at}:d>`), /07\/30\/2025/);
  assert.match(M.renderMd(`<t:${at}:D>`), /July 30, 2025/);
  assert.match(M.renderMd(`<t:${at}:f>`), /Jul 30, 2025 5:00 PM/);
  // None of them should leak the raw token.
  assert.ok(!M.renderMd(`<t:${at}:t>`).includes("&lt;t:"));
});

test("a relative timestamp reads off the injected clock", () => {
  const at = 1753894800;
  const now = (at - 3 * 86400) * 1000;
  assert.match(M.renderMd(`<t:${at}:R>`, null, now), /in 3 days/);
  const later = (at + 60 * 60) * 1000;
  assert.match(M.renderMd(`<t:${at}:R>`, null, later), /1 hour ago/);
});

test("a timestamp beats the emoji arm — both can start with '<'", () => {
  const out = M.renderMd("<t:1753894800:t>", { t: "https://cdn.invalid/t.png" });
  assert.ok(!out.includes("<img"), "the emoji arm swallowed a timestamp: " + out);
});

test("an emoji URL is escaped into the src attribute", () => {
  const out = M.renderMd(":x:", { x: 'https://e.invalid/a.png"onload="y' });
  assert.ok(!out.includes('"onload="'));
});

test("newlines survive so the pre-wrap canvas keeps line breaks", () => {
  assert.equal(M.renderMd("a\nb").split("\n").length, 2);
});

// --- editor segmentation (emoji visible while editing) -------------------------------

const EMOJI = { kyber: "https://cdn.invalid/kyber.png", arc: "https://cdn.invalid/arc.png" };

test("segments split text around resolvable emoji", () => {
  const segs = M.emojiSegments("hit :kyber: hard", EMOJI);
  assert.deepEqual(
    segs.map((s) => s.type),
    ["text", "emoji", "text"],
  );
  assert.equal(segs[0].value, "hit ");
  assert.equal(segs[1].token, ":kyber:");
  assert.equal(segs[1].url, EMOJI.kyber);
  assert.equal(segs[2].value, " hard");
});

test("a full custom emoji segments off its id, with no map", () => {
  const segs = M.emojiSegments("<:boss:123456789> down", null);
  assert.equal(segs[0].type, "emoji");
  assert.match(segs[0].url, /emojis\/123456789\.png/);
  assert.equal(segs[0].token, "<:boss:123456789>");
});

test("an UNRESOLVABLE shortcode stays editable text", () => {
  // The point: a typo must remain characters you can fix, not an opaque image atom.
  const segs = M.emojiSegments("oops :kybber: here", EMOJI);
  assert.deepEqual(segs.map((s) => s.type), ["text"]);
  assert.equal(segs[0].value, "oops :kybber: here");
});

test("a timestamp is never mistaken for an emoji in the editor", () => {
  // `<t:1753894800:t>` contains `:1753894800:`, which the emoji arm would claim.
  const segs = M.emojiSegments("at <t:1753894800:t> ok", { 1753894800: "x" });
  assert.deepEqual(segs.map((s) => s.type), ["text"]);
});

test("segments round-trip back to the original content", () => {
  const original = "a :kyber: b <:boss:99> c :unknown: d";
  const segs = M.emojiSegments(original, EMOJI);
  const back = segs.map((s) => (s.type === "text" ? s.value : s.token)).join("");
  assert.equal(back, original);
});

test("adjacent text segments are merged, so the DOM stays flat", () => {
  const segs = M.emojiSegments(":nope: :alsonope:", EMOJI);
  assert.equal(segs.length, 1);
});

// --- emoji autocomplete ---------------------------------------------------------------

test("suggestions prefer prefix matches over substring matches", () => {
  const emoji = { arc: "a", sparc: "b", archer: "c" };
  const names = M.emojiSuggestions("arc", emoji).map((s) => s.name);
  assert.deepEqual(names.slice(0, 2), ["arc", "archer"]); // prefix first, alphabetical
  assert.equal(names[2], "sparc"); // substring match last
});

test("suggestions are capped and carry an insertable token", () => {
  const many = {};
  for (let i = 0; i < 50; i++) many["emoji" + i] = "u" + i;
  const out = M.emojiSuggestions("emoji", many, 5);
  assert.equal(out.length, 5);
  assert.equal(out[0].token, ":" + out[0].name + ":");
});

test("an empty query lists everything (the picker opens on a bare colon)", () => {
  assert.equal(M.emojiSuggestions("", EMOJI).length, 2);
});

test("shortcodeBefore finds the partial being typed", () => {
  const at = "hi :kyb".length;
  assert.deepEqual(M.shortcodeBefore("hi :kyb", at), { start: 3, query: "kyb" });
  assert.deepEqual(M.shortcodeBefore("hi :", 4), { start: 3, query: "" });
});

test("shortcodeBefore ignores a colon that is not opening a shortcode", () => {
  // A URL, and a colon glued to the end of a word — neither should open the picker.
  assert.equal(M.shortcodeBefore("see https://x", "see https://x".length), null);
  assert.equal(M.shortcodeBefore("time12:30", "time12:30".length), null);
  // Whitespace after the colon means the author moved on.
  assert.equal(M.shortcodeBefore("hi : there", "hi : there".length), null);
});

test("shortcodeBefore only looks behind the caret", () => {
  const text = "hi :kyb more";
  assert.equal(M.shortcodeBefore(text, 7).query, "kyb");
  assert.equal(M.shortcodeBefore(text, 3), null); // caret before the colon
});

// --- Discord limits the builder previously let through --------------------------------
// Each of these was silently valid client- and server-side, and only failed when Discord
// refused the send — the exact failure the validator exists to prevent.

test("total text over 4000 UTF-16 units is refused", () => {
  const long = { type: M.TEXT_DISPLAY, content: "x".repeat(4001) };
  const problem = M.validate([long]).find((p) => /Too much text/.test(p.msg));
  assert.ok(problem, "4001 chars passed validation");
  assert.match(problem.msg, /Shorten it by about 1 characters/);
  assert.equal(problem.path, null); // a whole-message problem, not one block's
});

test("text is counted across the whole tree, not just top level", () => {
  const nested = {
    type: M.CONTAINER,
    components: [
      { type: M.TEXT_DISPLAY, content: "x".repeat(2500) },
      { type: M.TEXT_DISPLAY, content: "y".repeat(2500) },
    ],
  };
  assert.equal(M.totalTextLength([nested]), 5000);
  assert.ok(M.validate([nested]).some((p) => /Too much text/.test(p.msg)));
});

test("an astral glyph counts as 2, like Discord counts it", () => {
  // Matches cv2_utf16_len server-side; counting characters would under-report and let
  // an over-long message through.
  assert.equal(M.totalTextLength([{ type: M.TEXT_DISPLAY, content: "🎃" }]), 2);
});

test("exactly at the cap is allowed", () => {
  const atCap = { type: M.TEXT_DISPLAY, content: "x".repeat(4000) };
  assert.ok(!M.validate([atCap]).some((p) => /Too much text/.test(p.msg)));
});

test("more than 5 buttons in one row is refused", () => {
  const six = {
    type: M.ACTION_ROW,
    components: Array.from({ length: 6 }, (_, i) => ({
      type: M.BUTTON,
      style: 5,
      label: "b" + i,
      url: "https://e.invalid/" + i,
    })),
  };
  assert.ok(M.validate([six]).some((p) => /Discord allows 5/.test(p.msg)));
});

test("a scheme-less button URL is refused", () => {
  // The common typo. The preview drops such a button silently (only http(s) becomes an
  // href), so nothing else in the UI would flag it.
  const bad = linkButton("Go", "kyber3000.com");
  assert.ok(M.validate([bad]).some((p) => /must start with http/.test(p.msg)));
  const ok = linkButton("Go", "https://kyber3000.com");
  assert.deepEqual(M.validate([ok]), []);
});

test("a javascript: button URL is refused", () => {
  const bad = linkButton("Go", "javascript:alert(1)");
  assert.ok(M.validate([bad]).some((p) => /must start with http/.test(p.msg)));
});

test("a button label over 80 characters is refused", () => {
  const bad = linkButton("L".repeat(81), "https://e.invalid");
  assert.ok(M.validate([bad]).some((p) => /Discord allows 80/.test(p.msg)));
});

test("more than 10 gallery images is refused", () => {
  const many = {
    type: M.MEDIA_GALLERY,
    items: Array.from({ length: 11 }, (_, i) => ({
      media: { url: "https://e.invalid/" + i + ".png" },
    })),
  };
  assert.ok(M.validate([many]).some((p) => /Discord allows 10/.test(p.msg)));
});

// --- button emoji ---------------------------------------------------------------------
// Discord renders a custom emoji on a button only from {id, name}. Storing {name} alone
// — what the field did — is valid for a unicode emoji and silently nothing for a custom
// one, so the field invited input that did nothing.

const EMOJI_MAP = {
  kyber: { url: "https://cdn.discordapp.com/emojis/123.png", id: "123", animated: false },
  spin: { url: "https://cdn.discordapp.com/emojis/456.gif", id: "456", animated: true },
};

test("a server emoji name resolves to an id, not just a name", () => {
  assert.deepEqual(M.buttonEmojiFor("kyber", EMOJI_MAP), {
    id: "123",
    name: "kyber",
    animated: false,
  });
});

test("the :name: and <:name:id> forms resolve too", () => {
  assert.equal(M.buttonEmojiFor(":kyber:", EMOJI_MAP).id, "123");
  assert.deepEqual(M.buttonEmojiFor("<a:spin:456>", null), {
    id: "456",
    name: "spin",
    animated: true,
  });
});

test("an animated emoji keeps its animated flag", () => {
  assert.equal(M.buttonEmojiFor("spin", EMOJI_MAP).animated, true);
});

test("an unmatched value is treated as a literal unicode emoji", () => {
  assert.deepEqual(M.buttonEmojiFor("🙂", EMOJI_MAP), { name: "🙂" });
  assert.deepEqual(M.buttonEmojiFor("nosuch", EMOJI_MAP), { name: "nosuch" });
});

test("empty input means no emoji", () => {
  assert.equal(M.buttonEmojiFor("", EMOJI_MAP), null);
  assert.equal(M.buttonEmojiFor("   ", EMOJI_MAP), null);
});

test("the emoji map still accepts the plain {name: url} shape", () => {
  // Rendering-only callers pass the simpler map; both shapes must keep working.
  assert.equal(M.emojiEntry({ x: "https://e.invalid/x.png" }, "x").url,
    "https://e.invalid/x.png");
  assert.match(M.renderMd(":x:", { x: "https://e.invalid/x.png" }), /<img class="emoji"/);
  assert.match(M.renderMd(":kyber:", EMOJI_MAP), /emojis\/123\.png/);
});

// --- drop targeting geometry ---------------------------------------------------------
// nearestRail decides where an otherwise-missed release lands. These cases are the ones
// that would silently ruin a drop: snapping out of a container's column, stealing the
// armed rail on a sub-pixel shift, and eating the release-to-cancel gesture.

/** A rail rect. Top-level rails span the full canvas; nested ones are indented. */
const rail = (top, left, right) => ({ top: top, bottom: top + 10, left: left, right: right });
const OUTER = [0, 400];
const INNER = [32, 380];

test("nearestRail picks the vertically closest rail", () => {
  const rects = [rail(0, ...OUTER), rail(100, ...OUTER), rail(200, ...OUTER)];
  assert.equal(M.nearestRail(rects, 200, 4, -1), 0);
  assert.equal(M.nearestRail(rects, 200, 98, -1), 1);
  assert.equal(M.nearestRail(rects, 200, 260, -1), 2);
});

test("a pointer in the gutter does not snap into a nested container", () => {
  // The inner rail is vertically NEARER, but the pointer sits left of the container's
  // indent. Snapping in would re-parent the block into a scope the author is not
  // pointing at — the outer rail is the one whose column they are actually in.
  // (A top-level rail spans the full width, so the column test only ever excludes
  // *nested* rails; it cannot pull you into one.)
  const rects = [rail(100, ...OUTER), rail(112, ...INNER)];
  assert.equal(M.nearestRail(rects, 10, 114, -1), 0);
});

test("with no rail spanning the pointer's column, every rail is still a candidate", () => {
  const rects = [rail(100, ...INNER), rail(300, ...INNER)];
  assert.equal(M.nearestRail(rects, 10, 108, -1), 0);
});

test("beyond the distance cap there is no target, so a release still cancels", () => {
  // Releasing over the palette or the inspector must stay an escape hatch.
  const rects = [rail(0, ...OUTER)];
  assert.equal(M.nearestRail(rects, 200, M.NEAREST_RAIL_MAX_PX + 40, -1), -1);
  assert.equal(M.nearestRail([], 200, 0, -1), -1);
});

test("the armed rail survives a shift smaller than the hysteresis margin", () => {
  // Two rails equidistant-ish: without hysteresis the armed one would flicker as content
  // moves under a stationary pointer.
  const rects = [rail(100, ...OUTER), rail(120, ...OUTER)];
  const nudged = 116; // 11px from rail 0's midline, 9px from rail 1's
  assert.equal(M.nearestRail(rects, 200, nudged, -1), 1, "unarmed, the nearer rail wins");
  assert.equal(M.nearestRail(rects, 200, nudged, 0), 0, "armed rail 0 holds through 2px");
});

test("a rival that clearly beats the armed rail does steal it", () => {
  const rects = [rail(100, ...OUTER), rail(300, ...OUTER)];
  assert.equal(M.nearestRail(rects, 200, 298, 0), 1);
});

test("hysteresis does not apply once the pointer leaves the armed rail's column", () => {
  // Armed on an inner rail, pointer moves out of the container: the inner rail is no
  // longer plausible, so it must not be held on to.
  const rects = [rail(100, ...OUTER), rail(104, ...INNER)];
  assert.equal(M.nearestRail(rects, 10, 103, 1), 0);
});

test("an out-of-range armed index is ignored rather than trusted", () => {
  const rects = [rail(100, ...OUTER)];
  assert.equal(M.nearestRail(rects, 200, 104, 7), 0);
  assert.equal(M.nearestRail(rects, 200, 104, -1), 0);
});
