// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Pure Components-V2 node model for the web builder — the client-side mirror of
// dd/anchor/cv2_nodes.py. A "node" is a raw Discord component-payload dict (the exact
// JSON the REST API accepts); the builder holds an ordered array of top-level nodes and
// mutates it through the helpers here.
//
// This file has NO DOM access and NO module-level mutable state — every function takes
// the node list it operates on, exactly like cv2_nodes.py does. That is what makes it
// unit-testable under `node --test` (see tests/cv2_model.test.js, run by `make test-js`).
// The UI layer lives in cv2_builder.js.
//
// Keep this in lockstep with cv2_nodes.py. The server re-sanitizes (sanitize_for_preview)
// and re-validates (validate) every node list on publish, so a client/server drift shows
// up as a preview that differs from the sent post — never as an invalid post reaching
// Discord.
//
// Paths. A path is an array addressing one node from the root list: [0, 2] is the third
// child of the first top-level node. The single non-integer segment is the string "acc",
// addressing a section's accessory (which is a named field, not a child index).
//
// Runs as a classic browser script (attaches window.CV2Model) and as a CommonJS module
// (module.exports) so the same file is importable from node:test.

(function () {
  "use strict";

  // --- Discord component type ids (mirror cv2_nodes) --------------------------------
  const ACTION_ROW = 1;
  const BUTTON = 2;
  const SECTION = 9;
  const TEXT_DISPLAY = 10;
  const THUMBNAIL = 11;
  const MEDIA_GALLERY = 12;
  const FILE = 13;
  const SEPARATOR = 14;
  const CONTAINER = 17;

  const LINK_BUTTON_STYLE = 5; // hikari.ButtonStyle.LINK
  const MAX_GALLERY_ITEMS = 10;
  const MAX_SECTION_TEXTS = 3;
  const MAX_TOP_LEVEL = 10;
  const MAX_ROW_BUTTONS = 5; // Discord's per-action-row cap
  const MAX_BUTTON_LABEL = 80;
  // Discord caps a CV2 message's total text at 4000 UTF-16 units (an astral glyph
  // counts as 2). Mirrors CV2_TEXT_LIMIT in dd/common/components.py.
  const MAX_TEXT = 4000;

  const KIND_BY_TYPE = {
    [CONTAINER]: "container",
    [TEXT_DISPLAY]: "text",
    [SECTION]: "section",
    [MEDIA_GALLERY]: "media",
    [SEPARATOR]: "separator",
    [FILE]: "file",
    [THUMBNAIL]: "thumbnail",
    [ACTION_ROW]: "link_button",
    [BUTTON]: "link_button",
  };

  // Human labels, mirroring cv2_nodes.ADD_LABELS.
  const KIND_LABEL = {
    container: "Container",
    text: "Text",
    section: "Section",
    media: "Image gallery",
    separator: "Separator",
    link_button: "Link button",
    thumbnail: "Thumbnail",
    file: "File",
  };

  /** Classify a node into a builder "kind" (cv2_nodes.kind). */
  function kind(node) {
    return (node && KIND_BY_TYPE[node.type]) || "unknown";
  }

  /**
   * The FIRST button inside a link-button node, unwrapping the action row.
   *
   * Only correct where exactly one button is possible — a section accessory, or a label
   * preview. For editing or validating a ROW use `buttonsOf`: a row loaded from a live
   * post can carry up to five, and assuming one meant the others could not be edited.
   */
  function buttonOf(node) {
    return node.type === ACTION_ROW ? node.components[0] : node;
  }

  /** Every button in a link-button node (a row may hold several; a bare button, one). */
  function buttonsOf(node) {
    return node.type === ACTION_ROW ? node.components || [] : [node];
  }

  // --- constructors -----------------------------------------------------------------
  // `defaultAccent` mirrors cv2_nodes.make_container seeding cfg.embed_default_color, so
  // a container made here matches one made by /post components. The host passes the
  // server's value; omitting it yields Discord's neutral bar.
  function makeContainer(defaultAccent) {
    const node = { type: CONTAINER, components: [] };
    if (Number.isInteger(defaultAccent)) node.accent_color = defaultAccent;
    return node;
  }
  function makeText(content) {
    return { type: TEXT_DISPLAY, content: content || "" };
  }
  // A fresh section starts with one empty text block: an accessory-only section is
  // invalid, and starting empty makes the very first thing you see a validation error.
  function makeSection() {
    return { type: SECTION, components: [makeText("")] };
  }
  function makeMediaGallery() {
    return { type: MEDIA_GALLERY, items: [] };
  }
  function makeSeparator() {
    return { type: SEPARATOR, divider: true, spacing: 1 };
  }
  function makeThumbnail() {
    return { type: THUMBNAIL, media: { url: "" } };
  }
  /** A bare link button, as used for a section accessory. */
  function makeButton() {
    return { type: BUTTON, style: LINK_BUTTON_STYLE, label: "", url: "" };
  }
  /** A link button wrapped in its own action row (buttons can't be loose children). */
  function makeLinkButton() {
    return { type: ACTION_ROW, components: [makeButton()] };
  }

  function makeNode(k, defaultAccent) {
    switch (k) {
      case "container":
        return makeContainer(defaultAccent);
      case "text":
        return makeText("");
      case "section":
        return makeSection();
      case "media":
        return makeMediaGallery();
      case "separator":
        return makeSeparator();
      case "link_button":
        return makeLinkButton();
      case "thumbnail":
        return makeThumbnail();
      default:
        throw new Error("Unknown node kind: " + k);
    }
  }

  // --- path helpers -----------------------------------------------------------------

  function samePath(a, b) {
    return !!a && !!b && a.length === b.length && a.every((v, i) => v === b[i]);
  }

  /** Whether `p` is `q` itself or one of its ancestors. */
  function isPrefix(p, q) {
    return p.length <= q.length && p.every((v, i) => v === q[i]);
  }

  /** The node at `path` (cv2_nodes.resolve_path). */
  function resolve(nodes, path) {
    let node = { type: CONTAINER, components: nodes };
    for (const seg of path) {
      node = seg === "acc" ? node.accessory : node.components[seg];
    }
    return node;
  }

  /**
   * The *mutable* child list of the container/section at `scope` (the root list when
   * `scope` is empty). Returns the real array reference even when empty, so callers can
   * splice into it — mirroring cv2_nodes.scope_children's explicit `is None` check.
   */
  function childList(nodes, scope) {
    if (!scope.length) return nodes;
    const node = resolve(nodes, scope);
    if (!node.components) node.components = [];
    return node.components;
  }

  function scopeKind(nodes, scope) {
    return scope.length ? kind(resolve(nodes, scope)) : "root";
  }

  /**
   * Rebase a path captured *before* a removal.
   *
   * Splicing a node out shifts every later sibling down one, and therefore every path
   * that descends through one. Without this, "drag a top-level block into a container
   * that sits below it" resolves to the wrong node (or throws): the container's own
   * index moved while we were holding it.
   */
  function adjustAfterRemoval(path, removed) {
    if (!path) return path;
    const scope = removed.slice(0, -1);
    const idx = removed[removed.length - 1];
    if (idx === "acc") return path; // an accessory is a field, not an index
    if (path.length > scope.length && isPrefix(scope, path) && path[scope.length] > idx) {
      const out = path.slice();
      out[scope.length] -= 1;
      return out;
    }
    return path;
  }

  // --- nesting rules (mirror cv2_nodes.addable_kinds) --------------------------------

  /** The kinds that may be inserted directly into `scope`. */
  function allowedIn(nodes, scope) {
    const sk = scopeKind(nodes, scope);
    // A section holds text displays plus one accessory; the accessory is set through
    // its own slot, not by inserting into the child list, so it isn't listed here.
    if (sk === "section") return ["text"];
    const base = ["text", "section", "media", "separator", "link_button"];
    // Containers are top-level only — they cannot nest.
    if (sk === "root") base.unshift("container");
    return base;
  }

  /**
   * Why `k` may not go into `scope`, in the author's words.
   *
   * The in-Discord builder expressed the nesting rules by *omitting* options from a
   * dropdown, so a rule you tripped over was invisible. On the web there is room to say
   * it, which turns a dead end into an explanation.
   */
  function refusalReason(nodes, scope, k) {
    const sk = scopeKind(nodes, scope);
    if (sk === "section") {
      if (k === "thumbnail" || k === "link_button") {
        return "Drop it on the section's accessory slot instead.";
      }
      if (k === "text" && childList(nodes, scope).length >= MAX_SECTION_TEXTS) {
        return "A section holds at most " + MAX_SECTION_TEXTS + " text blocks.";
      }
      return "A section holds text blocks and one accessory — nothing else.";
    }
    if (k === "container") return "Containers are top level only — they can't nest.";
    if (k === "thumbnail") return "A thumbnail is only ever a section's accessory.";
    return "That block can't go there.";
  }

  /**
   * Whether a node of kind `k` may be dropped into `scope`.
   *
   * `movingFrom` is the path of an existing node being dragged (null when adding a new
   * one), and gates two cases the kind alone can't: dropping a node into itself or its
   * own descendants, and reordering within an already-full section (which adds nothing).
   */
  function canDrop(nodes, scope, k, movingFrom) {
    if (movingFrom && isPrefix(movingFrom, scope)) return false;
    if (
      scopeKind(nodes, scope) === "section" &&
      childList(nodes, scope).length >= MAX_SECTION_TEXTS &&
      !(movingFrom && samePath(movingFrom.slice(0, -1), scope))
    ) {
      return false;
    }
    return allowedIn(nodes, scope).indexOf(k) !== -1;
  }

  /** Whether this kind can be a section accessory. */
  function isAccessoryKind(k) {
    return k === "thumbnail" || k === "link_button";
  }

  // --- mutations --------------------------------------------------------------------
  // Each returns the path that should now be selected, so the caller never has to
  // recompute one from a tree it just reshaped.

  function insertAt(nodes, scope, index, node) {
    childList(nodes, scope).splice(index, 0, node);
    return scope.concat([index]);
  }

  /** Remove the node at `path`; returns the path to select next (or null). */
  function removeAt(nodes, path) {
    const scope = path.slice(0, -1);
    const last = path[path.length - 1];
    if (last === "acc") {
      delete resolve(nodes, scope).accessory;
      return scope;
    }
    childList(nodes, scope).splice(last, 1);
    const list = childList(nodes, scope);
    if (list.length) return scope.concat([Math.min(last, list.length - 1)]);
    return scope.length ? scope : null;
  }

  /** Move the node at `from` into `toScope` at `toIndex`; returns its new path. */
  function moveNode(nodes, from, toScope, toIndex) {
    const node = JSON.parse(JSON.stringify(resolve(nodes, from)));
    const fromScope = from.slice(0, -1);
    const fromIdx = from[from.length - 1];
    let scope = toScope;
    let index = toIndex;
    if (fromIdx === "acc") {
      delete resolve(nodes, fromScope).accessory;
    } else {
      childList(nodes, fromScope).splice(fromIdx, 1);
      // Removing an earlier sibling in the same scope shifts the target left...
      if (samePath(fromScope, toScope) && fromIdx < index) index -= 1;
      // ...and removing anything shifts paths that descend past it (no-op when equal).
      scope = adjustAfterRemoval(scope, from);
    }
    return insertAt(nodes, scope, index, node);
  }

  /** Set a section's accessory. A section accessory is a BARE button, never a row. */
  function setAccessory(nodes, sectionPath, accessory) {
    const node = accessory.type === ACTION_ROW ? accessory.components[0] : accessory;
    resolve(nodes, sectionPath).accessory = node;
    return sectionPath.concat(["acc"]);
  }

  // --- validation (mirror cv2_nodes.validate) ---------------------------------------
  // Same messages as the Python, but each problem carries the PATH of the node that
  // caused it, so the UI can select and scroll to the offender instead of printing a
  // wall of prose the way the in-Discord builder had to.

  /**
   * Total displayable text across the tree, in the UTF-16 units Discord counts.
   *
   * A JS string's `.length` already IS its UTF-16 code-unit count, so an astral glyph
   * counts as 2 here exactly as it does for Discord — and exactly as
   * `cv2_utf16_len` computes it server-side.
   */
  function totalTextLength(nodes) {
    let total = 0;
    for (const node of nodes) {
      if (kind(node) === "text") total += String(node.content || "").length;
      if (Array.isArray(node.components)) total += totalTextLength(node.components);
    }
    return total;
  }

  // One `:name:` shortcode. Mirrors `re_user_side_emoji` in dd/common/utils.py — the
  // trailing id group is what marks an ALREADY-resolved mention, which is left alone.
  const SHORTCODE = /(<a?)?:(\w+)(?:~\d)*:(\d+>)?/g;

  /**
   * Resolve `:name:` shortcodes to `<:name:id>` mentions across a node tree.
   *
   * Mirrors `cv2_nodes.substitute_emoji`, which is what actually runs at publish. The
   * client needs it only to COUNT: a mention is ~20 characters longer than the
   * shortcode it replaces and Discord's 4000-character cap counts the mention, so
   * measuring the authored text would let the canvas say "Ready to post" about a tree
   * the server then refuses. Rendering still resolves shortcodes to <img>, not to text.
   *
   * A name the guild map doesn't know (or knows without an id — the bare-URL map shape)
   * is left as its literal text, exactly as Discord would show it.
   */
  function substituteEmoji(nodes, emoji) {
    if (!emoji) return nodes;
    const sub = (text) =>
      String(text).replace(SHORTCODE, (whole, prefix, name, idGroup) => {
        if (idGroup) return whole;
        const entry = emojiEntry(emoji, name);
        if (!entry || !entry.id) return whole;
        return "<" + (entry.animated ? "a" : "") + ":" + name + ":" + entry.id + ">";
      });
    const walk = (node) => {
      const out = Object.assign({}, node);
      if (out.type === TEXT_DISPLAY && typeof out.content === "string") {
        out.content = sub(out.content);
      }
      if (Array.isArray(out.components)) out.components = out.components.map(walk);
      if (out.accessory && typeof out.accessory === "object") {
        out.accessory = walk(out.accessory);
      }
      return out;
    };
    return nodes.map(walk);
  }

  function validate(nodes, emoji) {
    const problems = [];
    const push = (path, msg) => problems.push({ path: path, msg: msg });

    if (!nodes.length) push(null, "The message is empty — add at least one block.");
    if (nodes.length > MAX_TOP_LEVEL) {
      push(
        null,
        "Too many top-level blocks (" +
          nodes.length +
          "); Discord allows " +
          MAX_TOP_LEVEL +
          ". Group some inside a container.",
      );
    }

    // The cap Discord enforces at send time; without checking it here the only symptom
    // is a rejected send long after the text was written. Measured on the SUBSTITUTED
    // tree because that is the payload Discord counts (see `substituteEmoji`).
    const textLen = totalTextLength(substituteEmoji(nodes, emoji));
    if (textLen > MAX_TEXT) {
      push(
        null,
        "Too much text (" +
          textLen +
          " of " +
          MAX_TEXT +
          " characters). Shorten it by about " +
          (textLen - MAX_TEXT) +
          " characters.",
      );
    }

    (function walk(list, base) {
      list.forEach((node, i) => {
        const path = base.concat([i]);
        const k = kind(node);
        if (k === "container") {
          const children = node.components || [];
          if (!children.length) {
            push(path, "A container is empty — add a block inside or delete it.");
          }
          walk(children, path);
        } else if (k === "section") {
          const texts = node.components || [];
          if (texts.length < 1 || texts.length > MAX_SECTION_TEXTS) {
            push(
              path,
              "A section must have 1–" +
                MAX_SECTION_TEXTS +
                " text blocks (it has " +
                texts.length +
                ").",
            );
          }
          if (!node.accessory) {
            push(path, "A section is missing its accessory (thumbnail or button).");
          } else if (
            kind(node.accessory) === "thumbnail" &&
            !(node.accessory.media || {}).url
          ) {
            push(path.concat(["acc"]), "The section's thumbnail has no image URL.");
          } else if (kind(node.accessory) === "link_button") {
            const b = buttonOf(node.accessory);
            if (!(b.label && b.url)) {
              push(
                path.concat(["acc"]),
                "The section's button needs both a label and a URL.",
              );
            }
          }
          walk(texts, path);
        } else if (k === "text") {
          if (!String(node.content || "").trim()) push(path, "A text block is empty.");
        } else if (k === "media") {
          const items = node.items || [];
          if (!items.length) push(path, "A media gallery has no images.");
          else if (items.length > MAX_GALLERY_ITEMS) {
            push(
              path,
              "A media gallery has " +
                items.length +
                " images; Discord allows " +
                MAX_GALLERY_ITEMS +
                ".",
            );
          }
        } else if (k === "link_button") {
          const btns = buttonsOf(node);
          if (!btns.length) push(path, "A button row has no buttons.");
          if (btns.length > MAX_ROW_BUTTONS) {
            push(
              path,
              "A button row has " +
                btns.length +
                " buttons; Discord allows " +
                MAX_ROW_BUTTONS +
                ". Split them across two rows.",
            );
          }
          btns.forEach((b, bi) => {
            const many = btns.length > 1;
            const where = many ? "Button " + (bi + 1) : "A link button";
            const label = String(b.label || "");
            const url = String(b.url || "");
            if (!(label && url)) {
              push(
                path,
                many
                  ? where + " needs both a label and a URL."
                  : "A link button needs both a label and a URL.",
              );
              return;
            }
            // A scheme-less URL is the common typo; the preview silently drops such a
            // button (only http(s) becomes an href) so nothing else would flag it.
            if (!/^https?:\/\//.test(url)) {
              push(path, where + "'s URL must start with http:// or https://.");
            }
            if (label.length > MAX_BUTTON_LABEL) {
              push(
                path,
                where +
                  "'s label is " +
                  label.length +
                  " characters; Discord allows " +
                  MAX_BUTTON_LABEL +
                  ".",
              );
            }
          });
        }
      });
    })(nodes, []);

    return problems;
  }

  // --- markdown --------------------------------------------------------------------
  // The leaf layer of THE renderer — every preview surface's text goes through here
  // (see cv2_render.js). It used to mirror a Python twin, hybrid_post_core._render_line;
  // that twin is gone, and the drift between them is what motivated the unification.
  //
  // Everything is escaped first and only http(s) links become anchors. That is not a
  // formality: the mirror log draws captured posts from other servers, so this runs on
  // untrusted input.

  // Matches Python's html.escape(s, quote=True) character for character, including the
  // apostrophe as &#x27;. That is not cosmetic: the shared golden corpus
  // (dd/anchor/preview_fixtures) is asserted byte-for-byte by BOTH the Python tests and
  // the JS ones, so an escape the two spell differently would make the corpus unable to
  // hold them to the same output.
  const _ESC = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#x27;",
  };

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => _ESC[c]);
  }

  const EMOJI_CDN = "https://cdn.discordapp.com/emojis/";

  /**
   * Look a name up in the emoji map, tolerating both entry shapes.
   *
   * The page supplies `{name: {url, id, animated}}` — the id is what a *button* needs,
   * since Discord renders a custom emoji on one only from `{id, name}`. A bare URL
   * string is still accepted so callers (and tests) that only care about rendering can
   * pass the simpler map.
   */
  function emojiEntry(emoji, name) {
    if (!emoji || !name) return null;
    const hit = emoji[name] || emoji[String(name).toLowerCase()];
    if (!hit) return null;
    return typeof hit === "string" ? { url: hit } : hit;
  }
  const MONTHS_SHORT = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");
  const MONTHS_LONG = (
    "January February March April May June July " +
    "August September October November December"
  ).split(" ");

  // One inline-markdown token. The ORDER is the contract: *** beats ** beats *, and a
  // <t:…> timestamp beats the emoji arm (both can start with "<").
  //
  // A single ordered pass is not a stylistic choice: a chain of .replace() calls
  // substitutes into HTML the earlier passes already emitted. That is what mangled real
  // posts — a custom emoji `<:name:123>` had its inner `:name:` swapped for an <img>,
  // leaving a stray "<" and "123>" around it on screen.
  const INLINE = new RegExp(
    "(?<bi>\\*\\*\\*(?<biInner>.+?)\\*\\*\\*)" +
      "|(?<b>\\*\\*(?<bInner>.+?)\\*\\*)" +
      "|(?<i>\\*(?<iInner>.+?)\\*)" +
      "|(?<code>`(?<codeInner>[^`\\n]+)`)" +
      "|(?<link>\\[(?<label>[^\\]]+)\\]\\((?<url>[^)\\s]+)\\))" +
      "|(?<ts><t:(?<tsval>\\d+):(?<tsfmt>[A-Za-z])>)" +
      "|(?<emoji>(?<eprefix><a?)?:(?<ename>\\w+)(?:~\\d)*:(?<eid>\\d+>)?)",
    "g",
  );

  function _img(url, name) {
    return '<img class="emoji" src="' + esc(url) + '" alt=":' + esc(name) + ':">';
  }

  /**
   * One emoji token → an <img>, or its escaped literal text.
   *
   * Merges the two server-side substituters, because the builder sees both shapes:
   * content seeded from a live post carries full `<:name:id>` / `<a:name:id>` (resolved
   * straight off the CDN, like cv2_render's substituter — so it renders even when the
   * guild emoji fetch failed), while content the author types carries a bare `:name:`
   * (resolved through the name map, like hybrid_post_core's).
   */
  function _emojiHtml(whole, prefix, name, idGroup, emoji) {
    if (idGroup) {
      const id = idGroup.replace(">", "");
      if (/^\d+$/.test(id)) {
        return _img(EMOJI_CDN + id + (prefix === "<a" ? ".gif" : ".png"), name);
      }
    }
    const entry = emojiEntry(emoji, name);
    return entry && entry.url ? _img(entry.url, name) : esc(whole);
  }

  /**
   * A `<t:UNIX:X>` token, in the VIEWER'S timezone — which is what Discord does.
   *
   * This used to render UTC with an explicit "(UTC)" note, because the renderer was a
   * server and a server cannot know the reader's zone. A client renderer can, so the
   * apology is gone and a preview now shows the same wall-clock time the post will.
   *
   * `now` is an injectable epoch-ms clock for the relative format; tests that need a
   * fixed zone set `process.env.TZ` (see tests/preview_fixtures.test.js).
   *
   * Returns null for a unix value `Date` cannot represent — the caller then leaves the
   * token as literal text. The token regex accepts unbounded `\d+`, so a mirrored post
   * can carry one, and every getter on an Invalid Date returns NaN: without this the
   * reader saw the words "undefined" and "NaN" laid out as if they were a date.
   */
  function _timestampText(unix, fmt, now) {
    const d = new Date(unix * 1000);
    if (Number.isNaN(d.getTime())) return null;
    const hour24 = d.getHours();
    const h12 = hour24 % 12 || 12;
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ampm = hour24 < 12 ? "AM" : "PM";
    if (fmt === "R") {
      const delta = unix - Math.floor((now === undefined ? Date.now() : now) / 1000);
      const future = delta >= 0;
      let secs = Math.abs(delta);
      let unit = "second";
      let n = secs;
      for (const [label, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
        if (secs >= size) {
          unit = label;
          n = Math.floor(secs / size);
          break;
        }
      }
      const text = n + " " + unit + (n !== 1 ? "s" : "");
      return future ? "in " + text : text + " ago";
    }
    if (fmt === "t" || fmt === "T") {
      const ss = fmt === "T" ? ":" + String(d.getSeconds()).padStart(2, "0") : "";
      return h12 + ":" + mm + ss + " " + ampm;
    }
    if (fmt === "d") {
      return (
        String(d.getMonth() + 1).padStart(2, "0") +
        "/" +
        String(d.getDate()).padStart(2, "0") +
        "/" +
        d.getFullYear()
      );
    }
    if (fmt === "D") {
      return MONTHS_LONG[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
    }
    // f / F / anything else: Discord's long-date short-time.
    return (
      MONTHS_SHORT[d.getMonth()] +
      " " +
      d.getDate() +
      ", " +
      d.getFullYear() +
      " " +
      h12 +
      ":" +
      mm +
      " " +
      ampm
    );
  }

  /**
   * Inline markdown → safe HTML.
   *
   * `emoji` is an optional {name: url} map for bare `:shortcode:`; `now` is an injectable
   * epoch-ms clock so relative timestamps are testable. Text outside a token is escaped,
   * and only http(s) links become anchors.
   */
  function inlineMd(s, emoji, now) {
    const text = String(s);
    let out = "";
    let last = 0;
    // matchAll, not exec-in-a-loop: this function RECURSES (for the inner text of bold
    // and italic), and a shared /g regex driven by .exec would have the inner call
    // clobber the outer call's lastIndex — which spins forever. matchAll clones the
    // regex internally and never writes back to it.
    for (const m of text.matchAll(INLINE)) {
      out += esc(text.slice(last, m.index));
      last = m.index + m[0].length;
      const g = m.groups;
      if (g.bi !== undefined) {
        out += "<strong><em>" + inlineMd(g.biInner, emoji, now) + "</em></strong>";
      } else if (g.b !== undefined) {
        out += "<strong>" + inlineMd(g.bInner, emoji, now) + "</strong>";
      } else if (g.i !== undefined) {
        out += "<em>" + inlineMd(g.iInner, emoji, now) + "</em>";
      } else if (g.code !== undefined) {
        out += "<code>" + esc(g.codeInner) + "</code>";
      } else if (g.link !== undefined) {
        // Discord's `[label](<url>)` form wraps the URL in angle brackets to suppress
        // its preview embed; the real URL is inside them.
        let url = g.url;
        if (url.length > 1 && url.startsWith("<") && url.endsWith(">")) {
          url = url.slice(1, -1);
        }
        out += /^https?:\/\//.test(url)
          ? '<a href="' +
            esc(url) +
            '" target="_blank" rel="noopener noreferrer">' +
            // The label may itself carry markdown, e.g. "[**View…**](url)" — the shape
            // every Lost Sector title uses. Recurse rather than escape it flat.
            inlineMd(g.label, emoji, now) +
            "</a>"
          : esc(m[0]);
      } else if (g.ts !== undefined) {
        // An unrepresentable unix value stays the literal token — the honest degrade,
        // and what a Discord client shows for one it cannot render either.
        const when = _timestampText(Number(g.tsval), g.tsfmt, now);
        out += esc(when === null ? m[0] : when);
      } else {
        out += _emojiHtml(m[0], g.eprefix, g.ename, g.eid, emoji);
      }
    }
    return out + esc(text.slice(last));
  }

  function lineMd(line, emoji, now) {
    if (line.startsWith("-# ")) {
      return '<span class="md-small">' + inlineMd(line.slice(3), emoji, now) + "</span>";
    }
    if (line.startsWith("### ")) {
      return '<span class="md-h3">' + inlineMd(line.slice(4), emoji, now) + "</span>";
    }
    if (line.startsWith("## ")) {
      return '<span class="md-h2">' + inlineMd(line.slice(3), emoji, now) + "</span>";
    }
    if (line.startsWith("# ")) {
      return '<span class="md-h1">' + inlineMd(line.slice(2), emoji, now) + "</span>";
    }
    // Discord accepts both `- ` and `* `. The Python renderer this replaced took only
    // `- `, which is how a star bullet came to render differently on the builder canvas
    // than in the confirmation claiming to show the same post.
    if (/^[-*] /.test(line)) {
      return '<span class="md-bullet">' + inlineMd(line.slice(2), emoji, now) + "</span>";
    }
    return inlineMd(line, emoji, now);
  }

  /**
   * Rework blank lines around `##`/`###` headings to match Discord's spacing.
   *
   * Discord gives a sub-heading a margin *above* and renders the content right below it
   * tight, but a body conventionally puts the blank line *after* the heading. Under
   * `white-space: pre-wrap` that literal blank lands below the heading, so the preview
   * reads differently from the posted message. Collapse to exactly one blank line
   * before each `##`/`###` (none if it is the first line) and drop any blank directly
   * after it. `#` (the H1 title) is left alone — its trailing blank already matches.
   *
   * Ported from the Python renderer this replaced, which applied it and whose
   * absence here was the first measured drift between the two.
   */
  function normalizeHeadingSpacing(lines) {
    const isSub = (line) => line.startsWith("## ") || line.startsWith("### ");
    const out = [];
    for (const line of lines) {
      if (isSub(line)) {
        while (out.length && out[out.length - 1] === "") out.pop();
        if (out.length) out.push("");
        out.push(line);
      } else if (line === "" && out.length && isSub(out[out.length - 1])) {
        continue;
      } else {
        out.push(line);
      }
    }
    return out;
  }

  /** Render a text leaf's content. Newlines survive via the .cv2-text pre-wrap. */
  function renderMd(content, emoji, now) {
    return normalizeHeadingSpacing(String(content).split("\n"))
      .map((line) => lineMd(line, emoji, now))
      .join("\n");
  }

  // --- editor segmentation ----------------------------------------------------------
  // The inline editor shows emoji as images while you type, which a <textarea> cannot
  // do — so the editor is a contenteditable built from these segments. Kept here (pure,
  // no DOM) so the tokenising rules stay tested and identical to the render path.

  // Same two arms as INLINE, same order, for the same reason: `<t:1753894800:t>` contains
  // `:1753894800:`, which the emoji arm would otherwise happily claim as a shortcode.
  const EDIT_TOKEN = new RegExp(
    "(?<ts><t:\\d+:[A-Za-z]>)" +
      "|(?<emoji>(?<eprefix><a?)?:(?<ename>\\w+)(?:~\\d)*:(?<eid>\\d+>)?)",
    "g",
  );

  /** The image URL a token resolves to, or null if it cannot be resolved. */
  function emojiUrlFor(prefix, name, idGroup, emoji) {
    if (idGroup) {
      const id = idGroup.replace(">", "");
      if (/^\d+$/.test(id)) {
        return EMOJI_CDN + id + (prefix === "<a" ? ".gif" : ".png");
      }
    }
    const entry = emojiEntry(emoji, name);
    return (entry && entry.url) || null;
  }

  /**
   * Split raw content into segments for the inline editor.
   *
   * Returns `{type:"text", value}` and `{type:"emoji", token, name, url}` in order.
   * An UNRESOLVABLE token stays text on purpose: a typo like `:kybber:` must remain
   * editable characters, not become an opaque atom you cannot fix. Timestamps likewise
   * stay text — they are something you edit, not a picture.
   */
  function emojiSegments(text, emoji) {
    const src = String(text);
    const out = [];
    let last = 0;
    const pushText = (value) => {
      if (!value) return;
      const prev = out[out.length - 1];
      if (prev && prev.type === "text") prev.value += value;
      else out.push({ type: "text", value });
    };
    for (const m of src.matchAll(EDIT_TOKEN)) {
      const g = m.groups;
      if (g.ts !== undefined) continue; // leave timestamps in the surrounding text
      const url = emojiUrlFor(g.eprefix, g.ename, g.eid, emoji);
      if (!url) continue;
      pushText(src.slice(last, m.index));
      out.push({ type: "emoji", token: m[0], name: g.ename, url });
      last = m.index + m[0].length;
    }
    pushText(src.slice(last));
    return out;
  }

  /**
   * Emoji names matching a `:partial` the author is typing, best-first.
   *
   * Prefix matches rank above substring matches, so `:arc` offers `arc` before
   * `sparc`. `limit` keeps the popup to a glanceable size.
   */
  function emojiSuggestions(query, emoji, limit) {
    if (!emoji) return [];
    const q = String(query || "").toLowerCase();
    const names = Object.keys(emoji);
    const starts = [];
    const contains = [];
    for (const name of names) {
      const lower = name.toLowerCase();
      if (!q) starts.push(name);
      else if (lower.startsWith(q)) starts.push(name);
      else if (lower.indexOf(q) !== -1) contains.push(name);
    }
    starts.sort();
    contains.sort();
    return starts.concat(contains).slice(0, limit || 8).map((name) => ({
      name,
      url: (emojiEntry(emoji, name) || {}).url,
      token: ":" + name + ":",
    }));
  }

  /**
   * The Discord emoji object for what an author typed in a button's Emoji field.
   *
   * A custom guild emoji MUST carry its id — `{"name": "kyber"}` is a valid shape only
   * for a *unicode* emoji, and Discord renders nothing at all for a custom one. So a
   * name that matches the guild map resolves to `{id, name, animated}`; anything else
   * is treated as a literal unicode character. Returns null for empty input, meaning
   * "no emoji".
   */
  function buttonEmojiFor(raw, emoji) {
    const text = String(raw || "").trim();
    if (!text) return null;
    // Accept ":name:" and the full "<:name:id>" as well as a bare name, since all three
    // are things an author might paste in.
    const custom = /^<(a?):(\w+):(\d+)>$/.exec(text);
    if (custom) {
      return {
        id: custom[3],
        name: custom[2],
        animated: custom[1] === "a",
      };
    }
    const name = text.replace(/^:|:$/g, "");
    const entry = emojiEntry(emoji, name);
    if (entry && entry.id) {
      return { id: String(entry.id), name: name, animated: !!entry.animated };
    }
    return { name: text };
  }

  /**
   * The `:partial` shortcode immediately before `caret` in `text`, or null.
   *
   * Requires a boundary before the colon so a `:` in `https://x` or mid-word never opens
   * the picker, and refuses one containing whitespace.
   */
  function shortcodeBefore(text, caret) {
    const src = String(text).slice(0, caret);
    const colon = src.lastIndexOf(":");
    if (colon === -1) return null;
    const query = src.slice(colon + 1);
    if (/[^\w]/.test(query)) return null;
    const before = colon === 0 ? "" : src[colon - 1];
    if (before && !/[\s(>]/.test(before)) return null;
    return { start: colon, query };
  }

  // --- drop targeting geometry ------------------------------------------------------
  // Geometry rather than model, but it lives here because it is PURE — it takes plain
  // rectangles, touches no DOM, and is therefore the one part of the drag layer a node
  // test can cover. cv2_builder.js measures the rails and hands them over.
  //
  // Why a nearest search exists at all: a rail is 0.62rem tall (~10px). Hitting one
  // exactly with a fingertip is a coin flip, so dropping on touch used to fail silently
  // and the block sprang back with no explanation. An exact hit still wins when there is
  // one — this only decides where an otherwise-missed release should land.

  /** Beyond this, a release is a CANCEL rather than a drop. Load-bearing: releasing over
   *  the palette or the inspector has to stay an escape hatch, not a far-away commit. */
  const NEAREST_RAIL_MAX_PX = 120;
  /** A rival rail must beat the armed one by this much to steal it. Without hysteresis
   *  the armed rail flickers whenever content shifts under a stationary pointer. */
  const NEAREST_RAIL_HYSTERESIS_PX = 12;

  /** Vertical distance from a point to a rail's midline. Rails are wide and thin, so
   *  only the y axis meaningfully separates them; x is handled by the column test. */
  function railDistance(rect, y) {
    return Math.abs(y - (rect.top + rect.bottom) / 2);
  }

  /**
   * Which rail a release at (x, y) should land on: an index into `rects`, or -1 for none.
   *
   * `rects` must already be filtered to LEGAL targets — an illegal rail still explains
   * itself through the exact-hit path, but must never be snapped to.
   *
   * Rails whose horizontal span contains x win outright over rails that do not. Inner
   * rails are indented inside their container, so a pointer in the container's column
   * must not snap out to the container's own outer rail merely because it is a few
   * pixels closer vertically.
   *
   * `current` is the index of the currently armed rail, or -1. It gets the hysteresis
   * margin so the armed target stays put through small shifts.
   */
  function nearestRail(rects, x, y, current) {
    if (!rects || !rects.length) return -1;

    // In-column rails win outright over rails that do not span x; if none does, every
    // rail is a candidate. Expressed as a predicate rather than an index list so the
    // hysteresis check below can ask the same question of the armed rail directly.
    const spans = (r) => x >= r.left && x <= r.right;
    const eligible = rects.some(spans) ? spans : () => true;

    let best = -1;
    let bestDistance = Infinity;
    rects.forEach((rect, i) => {
      if (!eligible(rect)) return;
      const d = railDistance(rect, y);
      if (d < bestDistance) {
        bestDistance = d;
        best = i;
      }
    });
    if (best === -1 || bestDistance > NEAREST_RAIL_MAX_PX) return -1;

    // Keep the armed rail unless a rival clearly beats it — but only while it is still
    // a plausible target itself (in the pointer's column, and inside the cap).
    if (
      Number.isInteger(current) &&
      current >= 0 &&
      current < rects.length &&
      eligible(rects[current])
    ) {
      const currentDistance = railDistance(rects[current], y);
      if (
        currentDistance <= NEAREST_RAIL_MAX_PX &&
        currentDistance - bestDistance < NEAREST_RAIL_HYSTERESIS_PX
      ) {
        return current;
      }
    }
    return best;
  }

  // --- exports ----------------------------------------------------------------------

  const CV2Model = {
    // type ids
    ACTION_ROW,
    BUTTON,
    SECTION,
    TEXT_DISPLAY,
    THUMBNAIL,
    MEDIA_GALLERY,
    FILE,
    SEPARATOR,
    CONTAINER,
    LINK_BUTTON_STYLE,
    // limits
    MAX_GALLERY_ITEMS,
    MAX_SECTION_TEXTS,
    MAX_TOP_LEVEL,
    MAX_ROW_BUTTONS,
    MAX_BUTTON_LABEL,
    MAX_TEXT,
    // labels
    KIND_LABEL,
    // classification
    kind,
    buttonOf,
    buttonsOf,
    // constructors
    makeContainer,
    makeText,
    makeSection,
    makeMediaGallery,
    makeSeparator,
    makeThumbnail,
    makeButton,
    makeLinkButton,
    makeNode,
    // paths
    samePath,
    isPrefix,
    resolve,
    childList,
    scopeKind,
    adjustAfterRemoval,
    // rules
    allowedIn,
    refusalReason,
    canDrop,
    isAccessoryKind,
    // mutations
    insertAt,
    removeAt,
    moveNode,
    setAccessory,
    // validation
    validate,
    totalTextLength,
    substituteEmoji,
    // markdown
    esc,
    inlineMd,
    lineMd,
    renderMd,
    normalizeHeadingSpacing,
    // drop targeting geometry
    nearestRail,
    NEAREST_RAIL_MAX_PX,
    NEAREST_RAIL_HYSTERESIS_PX,
    // editor segmentation + emoji autocomplete
    emojiEntry,
    emojiSegments,
    buttonEmojiFor,
    emojiSuggestions,
    shortcodeBefore,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = CV2Model;
  if (typeof window !== "undefined") window.CV2Model = CV2Model;
})();
