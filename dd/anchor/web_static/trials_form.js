// Trials of Osiris form script. A self-contained form for editing the trials draft,
// served statically from /static/trials_form.js (no build step). The page
// (trials_form.html) is served by dd.anchor.extensions.trials, which substitutes
// {draft, loot_sets, current_loot_set, emoji_urls, default_image_url,
// accent_color, post_this_period, crossposted} into a small inline <script> as __BOOTSTRAP__
// before this runs. This reads that global, builds the Trials-specific widgets and a
// readForm() that shapes the payload, then hands the shared lifecycle (preview + the
// create/edit/delete buttons) to shared.js's initPostPreview/initPostForm — auth
// is the central Discord-OAuth session cookie (sent automatically on the same-origin
// fetch). The bonus focus pool is a set-card picker (one card per named rotation set); the
// server re-resolves the picked set's weapons and re-validates, so this form is a
// convenience, not a trust boundary.

const BOOT = window.__BOOTSTRAP__;
const {
  draft,
  loot_sets: lootSets = [],
  current_loot_set: currentLootSet = null,
  emoji_urls: emojiUrls = {},
} = BOOT;
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, kids = []) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) n.append(k);
  return n;
};

$("authNote").textContent =
  "Signed in via Discord (about 30 days). Save writes straight to the draft.";

// --- bonus focus pool: pick one named set (card picker) ----------------
// The pool is always one curated set from the editor-managed rotation (shipped resolved in
// `lootSets`); the operator picks which card. Selecting a card makes its weapons the focus
// pool, submitted (readForm) as the same hash/name value array the server already resolves.
// A "No bonus pool" card posts an empty pool. The pool + schedule are edited in the
// rotation editor (linked from the fieldset), not here.

// Normalised identity of a weapon-ref list, so the saved draft pool can be matched to a set
// by hash (when linked) or lower-cased name, regardless of order.
const poolKey = (weapons) =>
  (weapons || [])
    .map((w) => (w.hash != null ? "#" + w.hash : (w.name || "").toLowerCase()))
    .sort()
    .join("|");

const draftPool = draft.focus_pool || [];
const draftKey = poolKey(draftPool);
const matchingSet = lootSets.find((s) => poolKey(s.weapons) === draftKey);

// The cards: "No bonus pool", then each set. If the saved draft pool is non-empty but
// matches no set (a legacy hand-edited draft), prepend a transient card that re-posts it
// verbatim so we never silently drop an existing pool — not a way to author new pools.
const cards = [{ value: "", label: "No bonus pool", weapons: [], empty: true }];
if (draftPool.length && !matchingSet) {
  cards.push({
    value: "__custom__",
    label: "Current custom pool (not in rotation)",
    weapons: draftPool,
  });
}
for (const s of lootSets) {
  cards.push({
    value: s.name,
    label: s.name,
    weapons: s.weapons,
    thisWeek: s.name === currentLootSet,
  });
}

// Which card starts checked: the set matching the saved pool, else the custom card, else
// "No bonus pool" (an empty pool).
const checkedValue = draftPool.length ? (matchingSet ? matchingSet.name : "__custom__") : "";
// value -> weapons, for readForm.
const weaponsByValue = new Map(cards.map((c) => [c.value, c.weapons]));

const setGrid = $("setGrid");
for (const c of cards) {
  const radio = el("input", {
    type: "radio",
    name: "focusSet",
    className: "set-radio no-focus-ring",
    value: c.value,
    checked: c.value === checkedValue,
  });
  const head = el("div", { className: "set-card-head" }, c.label);
  if (c.thisWeek) head.append(el("span", { className: "set-tag" }, "This week"));
  const kids = [radio, head];
  if (c.weapons.length) {
    kids.push(
      el(
        "ul",
        { className: "set-weapons" },
        c.weapons.map((w) => {
          const li = el("li", {}, w.name);
          // Prefix with the weapon-type emoji (icon), falling back to the generic weapon
          // icon; matches the emoji shown in the post/preview.
          const url = emojiUrls[w.emoji_name] || emojiUrls.weapon;
          if (url) li.prepend(el("img", { className: "emoji", src: url, alt: "" }));
          return li;
        }),
      ),
    );
  } else if (c.empty) {
    kids.push(el("p", { className: "set-empty" }, "Post without a bonus pool."));
  }
  setGrid.append(el("label", { className: "set-card" }, kids));
}

// The selected card's set weapons (drives readForm + preview).
const selectedWeapons = () => {
  const checked = setGrid.querySelector(".set-radio:checked");
  return (checked && weaponsByValue.get(checked.value)) || [];
};

// --- populate the native fields ----------------------------------------
$("resetAt").value = draft.reset_ts
  ? new Date(draft.reset_ts * 1000).toISOString().slice(0, 16)
  : "";
$("mapsText").value = (draft.featured_maps || []).join("\n");
$("notesText").value = (draft.notes || []).join("\n");
$("imageUrl").value = draft.image_url || "";
// Pre-check "use as default" when this week's image already is the saved default.
$("imageDefault").checked =
  !!BOOT.default_image_url && (draft.image_url || "") === BOOT.default_image_url;

// --- read the form into the payload the server expects -----------------
function readForm() {
  const at = $("resetAt").value;
  return {
    reset_ts: at ? Math.floor(Date.parse(at + "Z") / 1000) : draft.reset_ts,
    maps_text: $("mapsText").value,
    // The selected set's weapons as hash strings (linked) and/or names (server resolves).
    focus_pool: selectedWeapons().map((w) => (w.hash != null ? String(w.hash) : w.name)),
    image_url: $("imageUrl").value.trim(),
    set_default_image: $("imageDefault").checked,
    notes_text: $("notesText").value,
  };
}

// --- shared lifecycle: preview + create/edit/delete -----------
// The previewer and the form buttons live in shared.js (initPostPreview / initPostForm);
// this form supplies only the route prefix, its readForm(), and the delete-confirm / status
// strings. onEdit just re-renders the preview — Trials has no widgets whose changes skip the
// form's native "input" event (the set-card radios bubble it).
const preview = initPostPreview({
  routePrefix: "trials",
  readForm,
  accentColor: BOOT.accent_color,
});
function onEdit() {
  preview.schedule();
}
initPostForm({
  routePrefix: "trials",
  readForm,
  preview,
  onEdit,
  labels: {
    deleteDraft:
      "Delete the in-channel draft post? Your form data stays — Create re-creates it.",
    deletePublished:
      "Delete the PUBLISHED Trials post? This removes it from the channel and from" +
      " every server that follows this feed. Your form data stays — Create re-posts it.",
    deleted: "Post deleted — reset to draft.",
  },
});
