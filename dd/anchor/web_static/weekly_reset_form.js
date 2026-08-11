// Weekly Reset Overview form script. A self-contained form for editing the weekly_reset
// draft, served statically from /static/weekly_reset_form.js (no build step). The page
// (weekly_reset_form.html) is served by dd.anchor.extensions.weekly_reset, which
// substitutes {draft, options, conquest_tiers, reward_fields,
// post_this_period, crossposted} into a small inline <script> as window.__BOOTSTRAP__
// before this script runs. This script reads that global, edits the draft client-side and
// POSTs it (via the shared api() helper) to /weekly_reset/{preview,create,edit,delete}
// — auth is the weekly_reset_session cookie (sent automatically on the same-origin fetch),
// so no token is embedded here. The server
// re-resolves weapons, re-applies the business rules and re-validates, so this form is a
// convenience, not a trust boundary. The live preview and the create/edit/delete
// buttons live in shared.js (initPostPreview / initPostForm); this script supplies only the
// widgets, readForm(), and the delete-confirm / status strings.
//
// Widgets: Tom Select (vendored, window.TomSelect) backs the searchable pickers — the four
// weapon slots (option value = manifest hash, label = "name — type · rarity"), the GM
// strike + Crucible featured modes, the raid/dungeon/pantheon selects and the multi-select
// Conquest tiers. readForm() reads them via getValue(); the submitted payload shape is
// unchanged from the native-input version so the server's _context_from_payload contract
// holds.

const BOOT = window.__BOOTSTRAP__;
const { draft, options, conquest_tiers } = BOOT;

// The manifest-backed pools (weapons, GM strikes, conquests) are large and cold on a
// fresh process, so the page waits for it rather than rendering half a form. `ready`
// false is the case waiting cannot fix — the manifest could not be read at all — where
// those pickers are genuinely empty and saying so beats looking broken.
if (options.ready === false) {
  const warning = document.createElement("p");
  warning.className = "options-warning";
  warning.textContent =
    "Weapon, GM strike and Conquest options could not be read from the Destiny " +
    "manifest. Reload to retry — everything else on this form works now, and anything " +
    "already picked is kept.";
  document.querySelector("header").appendChild(warning);
}
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, kids = []) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) n.append(k);
  return n;
};

$("authNote").textContent =
  "Signed in via Discord (about 30 days). Create/Edit write straight to the live post.";

// The standalone previewer (shared.js). readForm is hoisted, so it can be referenced here
// before its definition; onEdit below schedules this on every edit.
const preview = initPostPreview({
  routePrefix: "weekly_reset",
  readForm,
  accentColor: BOOT.accent_color,
});

// Tom Select instances, keyed by element id (plus "conq_<tier>"), so readForm() can pull
// their values with getValue().
const TS = {};

// Any edit re-syncs the Iron-Banner⇒Trials gate and re-renders the debounced preview.
// Native inputs bubble "input" (handled by initPostForm); Tom Select fires this via each
// instance's onChange.
function onEdit() {
  syncEvents();
  preview.schedule();
}

// item hash (string) -> item record, for hydrating a weapon slot from its saved hash and
// for rendering the "name — type · rarity" label.
const itemByHash = new Map(options.items.map((i) => [String(i.hash), i]));

// The full weapon pool as Tom Select options (~4166 rows). Value is the manifest hash (as
// a string) so the slot submits the hash for the light.gg deep link; searching spans the
// name/type/rarity so typeahead finds a weapon by any of them.
const weaponOptions = options.items.map((i) => ({
  value: String(i.hash),
  name: i.name,
  type: i.type,
  rarity: i.rarity,
  label: `${i.name} — ${i.type} · ${i.rarity}`,
}));

// --- Tom Select builders -----------------------------------------------
// A single-select typeahead over a weapon pool. Hydrates from the saved WeaponRef: by hash
// when we have one (and it's in the pool), else by injecting the plain name as a one-off
// option so a carried-over unlinked name survives and re-submits as raw text (the server
// resolves either a hash or a name via resolve_reward_value).
function tsWeapon(id, weaponRef) {
  const ts = new TomSelect($(id), {
    options: weaponOptions,
    valueField: "value",
    labelField: "label",
    searchField: ["name", "type", "rarity"],
    maxOptions: 50,
    placeholder: "Search weapons…",
    plugins: ["clear_button"],
    onChange: onEdit,
    render: {
      option: (d, esc) => `<div>${esc(d.label || d.value)}</div>`,
      item: (d, esc) => `<div>${esc(d.label || d.value)}</div>`,
    },
  });
  if (weaponRef) {
    const hash = weaponRef.hash != null ? String(weaponRef.hash) : "";
    if (hash && itemByHash.has(hash)) {
      ts.setValue(hash, true);
    } else if (weaponRef.name) {
      ts.addOption({ value: weaponRef.name, label: weaponRef.name, name: weaponRef.name });
      ts.setValue(weaponRef.name, true);
    }
  }
  TS[id] = ts;
  return ts;
}

// A single-select typeahead over a bounded string pool; the option value IS the name, so
// the slot submits the plain name. A carried-over current value not in the pool is
// injected up front (see ts_single.js) so it isn't silently dropped.
function tsSingle(id, values, current) {
  const cur = (current || "").trim();
  const pool = tsWithCurrentOption(
    values.map((v) => ({ value: v, text: v })),
    cur,
  );
  const ts = new TomSelect($(id), {
    options: pool,
    maxOptions: 500,
    placeholder: "Search…",
    plugins: ["clear_button"],
    onChange: onEdit,
  });
  ts.setValue(cur, true);
  TS[id] = ts;
  return ts;
}

// --- populate from the draft -------------------------------------------
const R = draft.rotator_raids || ["", ""];
const D = draft.rotator_dungeons || ["", ""];

$("resetAt").value = draft.reset_ts
  ? new Date(draft.reset_ts * 1000).toISOString().slice(0, 16)
  : "";
$("ironBanner").checked = !!draft.iron_banner;
$("trialsActive").checked = !!draft.trials_active;
$("updateLabel").value = (draft.update_link && draft.update_link.label) || "";
$("updateUrl").value = (draft.update_link && draft.update_link.url) || "";
$("eventsNarrative").value = draft.events_narrative || "";
$("crucible1v6").value = draft.crucible_1v6 || "";
$("imageUrl").value = draft.image_url || "";
// Pre-check "use as default" when this week's image already is the saved default.
$("imageDefault").checked =
  !!BOOT.default_image_url && (draft.image_url || "") === BOOT.default_image_url;
$("notesText").value = (draft.notes || []).join("\n");
$("linksText").value = (draft.extra_links || [])
  .map((l) => `${l.label} | ${l.url}`)
  .join("\n");

// The featured (second) mode out of a "First, Second" crucible value.
function featured(value) {
  const parts = (value || "").split(", ");
  return parts.length > 1 ? parts.slice(1).join(", ") : "";
}

// Weapon pickers.
tsWeapon("gmBonusFocus", draft.gm_bonus_focus);
tsWeapon("gmWeapon", draft.gm_weapon);
tsWeapon("quickplayBonusFocus", draft.quickplay_bonus_focus);
tsWeapon("quickplayWeapon", draft.quickplay_weapon);
tsWeapon("controlWeapon", draft.control_weapon);
tsWeapon("zavalaWeapon", draft.zavala_weapon);

// Bounded-pool single-selects.
tsSingle("gmStrike", options.strikes, draft.gm_strike);
tsSingle("rotRaid1", options.raids, R[0]);
tsSingle("rotRaid2", options.raids, R[1]);
tsSingle("rotDun1", options.dungeons, D[0]);
tsSingle("rotDun2", options.dungeons, D[1]);
tsSingle("pantheonReprise", options.pantheon, draft.pantheon_reprise);
tsSingle("pantheonEncore", options.pantheon, draft.pantheon_encore);
tsSingle("crucible3v3", options.crucible_modes, featured(draft.crucible_3v3));
tsSingle("crucible6v6", options.crucible_modes, featured(draft.crucible_6v6));

// --- conquests: a Tom Select multi per tier ----------------------------
for (const tier of conquest_tiers) {
  const chosen = (draft.conquests || {})[tier] || [];
  // Union of the manifest pool + anything already in the draft, so a carried-over pick that
  // isn't in the current pool isn't silently dropped.
  const pool = [...new Set([...(options.conquests[tier] || []), ...chosen])].sort();
  const sel = el("select", { id: "conq_" + tier, multiple: true });
  $("conquests").append(
    el("div", { className: "field" }, [
      el("label", { htmlFor: "conq_" + tier, textContent: tier }),
      sel,
    ]),
  );
  const ts = new TomSelect(sel, {
    options: pool.map((v) => ({ value: v, text: v })),
    plugins: ["remove_button"],
    maxOptions: 500,
    hideSelected: true,
    placeholder: "Add featured activities…",
    onChange: onEdit,
  });
  ts.setValue(chosen, true);
  TS["conq_" + tier] = ts;
}

// --- Iron Banner => Trials off + no Control reward, reflected live in the UI --------
// IB replaces the 6v6 Control playlist, so on top of forcing Trials off we grey out the
// Control Challenge Reward picker (the server drops it on save/preview) and note that the
// 6v6 first mode becomes Iron Banner.
const CRUCIBLE_6V6_HINT = $("crucible6v6Hint").textContent;
const TRIALS_GATE_NOTE = $("trialsGateNote").textContent;
// What "Trials active" was before Iron Banner forced it off, or null when the gate isn't
// holding a value. The gate is a rule, not an edit: ticking Iron Banner — to see the
// preview, or by mistake — must not quietly cost a setting whose loss is only visible if
// you happen to look at a greyed checkbox. Untick and the old value comes back. It cannot
// go stale: while IB is on the box is disabled, so no competing choice can be made.
let trialsBeforeIb = null;
function syncEvents() {
  const ib = $("ironBanner").checked;
  const trials = $("trialsActive");
  if (ib) {
    if (trialsBeforeIb === null) trialsBeforeIb = trials.checked;
    trials.checked = false;
  } else if (trialsBeforeIb !== null) {
    trials.checked = trialsBeforeIb;
    trialsBeforeIb = null;
  }
  trials.disabled = ib;
  $("trialsGateNote").textContent = ib
    ? "Trials is off for this Iron Banner week. Untick Iron Banner to get your previous"
      + " Trials setting back."
    : TRIALS_GATE_NOTE;
  if (ib) TS.controlWeapon.disable();
  else TS.controlWeapon.enable();
  $("controlWeaponNote").hidden = !ib;
  $("crucible6v6Hint").textContent = ib
    ? "During Iron Banner weeks the 6v6 first mode is Iron Banner."
    : CRUCIBLE_6V6_HINT;
}
syncEvents();

// --- read the form into the payload the server expects -----------------
// getValue() returns the option value: a hash string (weapons) or plain name (everything
// else) for single-selects, and an array of names for the conquest multi-selects — the same
// shapes the native-input form submitted, so _context_from_payload is unaffected.
function readForm() {
  const conquests = {};
  for (const tier of conquest_tiers) conquests[tier] = TS["conq_" + tier].getValue();
  const at = $("resetAt").value;
  return {
    reset_ts: at ? Math.floor(Date.parse(at + "Z") / 1000) : draft.reset_ts,
    gm_strike: TS.gmStrike.getValue().trim(),
    gm_weapon: TS.gmWeapon.getValue(),
    gm_bonus_focus: TS.gmBonusFocus.getValue(),
    quickplay_weapon: TS.quickplayWeapon.getValue(),
    quickplay_bonus_focus: TS.quickplayBonusFocus.getValue(),
    control_weapon: TS.controlWeapon.getValue(),
    zavala_weapon: TS.zavalaWeapon.getValue(),
    rotator_raids: [TS.rotRaid1.getValue(), TS.rotRaid2.getValue()],
    rotator_dungeons: [TS.rotDun1.getValue(), TS.rotDun2.getValue()],
    pantheon_reprise: TS.pantheonReprise.getValue(),
    pantheon_encore: TS.pantheonEncore.getValue(),
    crucible_1v6: $("crucible1v6").value.trim(),
    crucible_3v3: TS.crucible3v3.getValue(),
    crucible_6v6: TS.crucible6v6.getValue(),
    conquests,
    iron_banner: $("ironBanner").checked,
    trials_active: $("trialsActive").checked,
    update_label: $("updateLabel").value.trim(),
    update_url: $("updateUrl").value.trim(),
    image_url: $("imageUrl").value.trim(),
    set_default_image: $("imageDefault").checked,
    events_narrative: $("eventsNarrative").value.trim(),
    notes_text: $("notesText").value,
    links_text: $("linksText").value,
  };
}

// --- shared lifecycle: preview + create/edit/delete -----------
// Hand the previewer and the form buttons to shared.js (initPostForm), supplying only the
// route prefix, readForm(), the previewer, onEdit (syncTrials + preview), and the
// delete-confirm / status strings.
initPostForm({
  routePrefix: "weekly_reset",
  readForm,
  preview,
  onEdit,
  labels: {
    deleteDraft:
      "Delete the in-channel draft post? Your form data stays — Create re-creates it.",
    deletePublished:
      "Delete the PUBLISHED weekly-reset post? This removes it from the channel and" +
      " from every server that follows this feed. Your form data stays — Create" +
      " re-posts it.",
    deleted: "Post deleted — create a new one when ready.",
  },
});
