// Rotation editor application script. A self-contained, dependency-free form for editing
// a rotation's JSON document, served statically from /static/editor.js (no build step).
// The page (editor.html) is served by dd.anchor.extensions.rotation_editor, which
// substitutes {type, data, vocab} into a small inline <script> as window.__BOOTSTRAP__
// before this script runs. This script reads that global, edits the document client-side
// and POSTs it (via the shared api() helper) to /rotation/preview and /rotation/edit —
// auth is the rotation_session cookie (sent automatically on the same-origin fetch), so
// no token is embedded here. The server re-validates against the JSON schema, so this
// form is a convenience, not a trust boundary.
//
// Each post type has its own bespoke form (tagged .ls-only / .xur-only / .legacy-only /
// .trials-loot-only / .iron-banner-only in the markup); on load every other type's nodes
// are removed and the matching type's block builds its fields and assigns collect().
// lost_sector is tabbed (Sectors / Planet cycles / Preview); xur_location is a simpler
// Locations / Preview; a world-activity destination is Activities (each activity's
// elements are independent editable value lists) / Preview; trials_loot is a standalone
// weapons-only set pool (Loot sets / Preview), reusing the world-activity set-pool
// building blocks; iron_banner is a date-anchored schedule (start/pool/modes per week) +
// named bonus focus pools (Iron Banner / Preview).

const BOOTSTRAP = window.__BOOTSTRAP__;
const { type, title, data, vocab } = BOOTSTRAP;
// The world-activity destinations are stored under the `world_activity_` slug prefix
// (dd.common.rotation_schema.ROTATION_SLUG_PREFIX). Server dispatch keys off
// `is_world_activity`; the form mirrors it with this one prefix test so the two can't
// drift. (The DOM/CSS markers stay named `.legacy-only` — Bungie/Kyber's own term for
// the activity category — they're just class names, not the dispatch discriminator.)
const isWorldActivity = type.startsWith("world_activity_");
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, kids = []) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) n.append(k);
  return n;
};

// The heading says the rotation's NAME ("Lost sector rotation"), which the server
// sends alongside the slug. It used to print the slug itself.
document.getElementById("typeName").textContent = title || type;
document.title = `${title || type} — Destiny Director`;
document.getElementById("authNote").textContent =
  "Signed in via Discord (about 30 days). Changes save straight to the database.";

// Each post type has its own bespoke form; drop every other type's markup so the
// tab bar and collect() only ever see this type's fields.
const activeOnly = isWorldActivity
  ? "legacy-only"
  : type === "lost_sector"
    ? "ls-only"
    : type === "xur_location"
      ? "xur-only"
      : type === "iron_banner"
        ? "iron-banner-only"
        : "trials-loot-only";
document.querySelectorAll(".ls-only, .xur-only, .legacy-only, .trials-loot-only, .iron-banner-only").forEach((n) => {
  if (!n.classList.contains(activeOnly)) n.remove();
});

// collect() is assigned by the active type's block below; the shared preview /
// save plumbing calls it without caring which post type produced the document.
let collect;
// consistencyProblems() returns a (possibly empty) list of reasons the document
// isn't internally consistent; the Save handler blocks while it's non-empty. The
// lost_sector block sets it; other types leave the no-op default.
let consistencyProblems = () => [];

// ===== shared value-list + set-pool building blocks ====================
// Used by both the world-activity form (element cycles + Dares-style set pools) and the
// standalone trials_loot set pool. Kept at module scope so neither form duplicates them.

function addValue(list, v, ac) {
  const input = el("input", { type: "text", value: v || "", className: "grow" });
  if (ac) {
    input.setAttribute("list", ac.id); // manifest weapon/armor autocomplete
    input.addEventListener("input", ac.onInput);
  }
  const up = el("button", { className: "tiny secondary", type: "button", textContent: "↑" });
  const down = el("button", { className: "tiny secondary", type: "button", textContent: "↓" });
  const del = el("button", { className: "tiny danger", type: "button", textContent: "✕" });
  const row = el("div", { className: "row" }, [input, up, down, del]);
  row._value = () => input.value;
  del.addEventListener("click", () => row.remove());
  up.addEventListener("click", () => row.previousElementSibling && row.parentNode.insertBefore(row, row.previousElementSibling));
  down.addEventListener("click", () => row.nextElementSibling && row.parentNode.insertBefore(row.nextElementSibling, row));
  list.append(row);
}

// Debounced DestinyItem autocomplete (weapons/armor) backed by /rotation/search. The
// stored value keeps the "Name (Type)" shape so emoji tagging + link baking work (the
// trials_loot producer strips the "(Type)" suffix before resolving names).
let _acSeq = 0;
function itemAutocomplete(kind) {
  const id = `ac-${kind}-${_acSeq++}`;
  const dl = el("datalist", { id });
  let timer = null;
  const onInput = (e) => {
    const q = e.target.value.trim();
    clearTimeout(timer);
    if (q.length < 2) return;
    timer = setTimeout(async () => {
      try {
        const res = await fetch(
          `/rotation/search?q=${encodeURIComponent(q)}&kind=${kind}`,
          { credentials: "same-origin" },
        );
        if (!res.ok) return;
        const items = await res.json();
        dl.replaceChildren(
          ...items.map((it) =>
            el("option", { value: it.type ? `${it.name} (${it.type})` : it.name }),
          ),
        );
      } catch (_e) {
        /* autocomplete is best-effort */
      }
    }, 200);
  };
  return { id, dl, onInput };
}

// A labelled, reorderable value list (the shared building block for element cycles, set
// weapon/armor lists, and the set schedule). `kind` enables item autocomplete.
function valueList(legend, values, addLabel, kind) {
  const list = el("div");
  const ac = kind ? itemAutocomplete(kind) : null;
  (values || []).forEach((v) => addValue(list, v, ac));
  const add = el("button", { className: "tiny secondary", type: "button", textContent: addLabel });
  add.addEventListener("click", () => addValue(list, undefined, ac));
  const children = [el("legend", { textContent: legend }), list, add];
  if (ac) children.push(ac.dl);
  const fs = el("fieldset", { className: "zone" }, children);
  return { fs, list };
}

const listValues = (list) => [...list.children].map((r) => r._value());

// A set pool: a weekly schedule of set names + a pool of named sets (each a weapon list,
// optionally an armor list). Returns the schedule + sets DOM, the models for collect(),
// and validateSets() (blank/duplicate set names + schedule weeks that name no set), so
// callers wrap them in whatever container they like. Mirrors the lost_sector sectors ↔
// schedule consistency + rename-propagation treatment.
let _setSeq = 0;
function buildSetPool({ sets, schedule, includeArmor, scheduleLegend, allowAddRemove }) {
  const listId = `setNames-${_setSeq++}`;
  const dataList = el("datalist", { id: listId });
  const scheduleBox = el("div");
  const setsWrap = el("div");
  const setModels = [];

  function addWeek(value) {
    const input = el("input", { type: "text", value: value || "", className: "grow" });
    input.setAttribute("list", listId); // autocomplete from the set names
    input.addEventListener("input", validateSets);
    const up = el("button", { className: "tiny secondary", type: "button", textContent: "↑" });
    const down = el("button", { className: "tiny secondary", type: "button", textContent: "↓" });
    const del = el("button", { className: "tiny danger", type: "button", textContent: "✕" });
    const row = el("div", { className: "row" }, [input, up, down, del]);
    row._value = () => input.value.trim();
    row._input = input;
    del.addEventListener("click", () => { row.remove(); validateSets(); });
    up.addEventListener("click", () => row.previousElementSibling && row.parentNode.insertBefore(row, row.previousElementSibling));
    down.addEventListener("click", () => row.nextElementSibling && row.parentNode.insertBefore(row.nextElementSibling, row));
    scheduleBox.append(row);
  }

  function refreshSetNames() {
    dataList.replaceChildren(
      ...setModels.map((m) => el("option", { value: m.nameInput.value.trim() })).filter((o) => o.value),
    );
  }

  // Highlight blank/duplicate set names and schedule weeks that name no set; return the
  // problems (empty ⇒ internally consistent).
  function validateSets() {
    const names = setModels.map((m) => m.nameInput.value.trim());
    const counts = {};
    for (const n of names) counts[n] = (counts[n] || 0) + 1;
    const known = new Set(names.filter(Boolean));
    const problems = new Set();
    for (const m of setModels) {
      const n = m.nameInput.value.trim();
      const bad = !n || counts[n] > 1;
      m.nameInput.classList.toggle("invalid", bad);
      if (!n) problems.add("a set has a blank name");
      else if (counts[n] > 1) problems.add(`duplicate set name "${n}"`);
    }
    for (const row of scheduleBox.children) {
      const v = row._value();
      const bad = !!v && !known.has(v);
      row._input.classList.toggle("invalid", bad);
      if (bad) problems.add(`schedule week "${v}" isn't a set`);
    }
    return [...problems];
  }

  function addSet(s = {}) {
    const nameInput = el("input", { type: "text", value: s.name || "", className: "grow", placeholder: "Set name" });
    const weapons = valueList("Weapons", s.weapons, "+ Weapon", "weapon");
    const cardChildren = [
      el("div", { className: "field" }, [el("label", { textContent: "Set name" }), nameInput]),
      weapons.fs,
    ];
    const model = { nameInput, prev: (s.name || "").trim(), weaponsList: weapons.list, armorList: null };
    if (includeArmor) {
      const armor = valueList("Armor (named once; offered for all classes)", s.armor, "+ Armor", "armor");
      model.armorList = armor.list;
      cardChildren.push(armor.fs);
    }
    // A per-set remove control (opt-in). On removal, drop the model + auto-remove any
    // schedule weeks that named this set — unless a duplicate set still holds the name —
    // mirroring the lost_sector sector-removal treatment.
    if (allowAddRemove) {
      const remove = el("button", { className: "tiny danger", type: "button", textContent: "✕ Remove set" });
      remove.addEventListener("click", () => {
        const goneName = nameInput.value.trim();
        card.remove();
        setModels.splice(setModels.indexOf(model), 1);
        if (goneName && !setModels.some((m) => m.nameInput.value.trim() === goneName))
          for (const row of [...scheduleBox.children]) if (row._value() === goneName) row.remove();
        refreshSetNames();
        validateSets();
      });
      cardChildren.unshift(el("div", { className: "card-head" }, [el("span", { className: "grow" }), remove]));
    }
    nameInput.addEventListener("input", () => { refreshSetNames(); validateSets(); });
    // On commit, propagate a rename to every schedule week that named the old set, so the
    // Sets and Weekly schedule never drift apart.
    nameInput.addEventListener("change", () => {
      const oldName = model.prev;
      const newName = nameInput.value.trim();
      if (oldName && newName && oldName !== newName)
        for (const row of scheduleBox.children) if (row._value() === oldName) row._input.value = newName;
      model.prev = newName;
      refreshSetNames();
      validateSets();
    });
    const card = el("div", { className: "card" }, cardChildren);
    setsWrap.append(card);
    setModels.push(model);
  }

  (sets || []).forEach((s) => addSet(s));
  // Opt-in "+ Add set" button (returned to the caller to place under the pool).
  const setsAdd = allowAddRemove
    ? el("button", { className: "secondary tiny", type: "button", textContent: "+ Add set" })
    : null;
  if (setsAdd)
    setsAdd.addEventListener("click", () => { addSet(); refreshSetNames(); validateSets(); });

  (schedule || []).forEach((v) => addWeek(v));
  const schedAdd = el("button", { className: "tiny secondary", type: "button", textContent: "+ Week" });
  schedAdd.addEventListener("click", () => { addWeek(); validateSets(); });
  const scheduleFs = el("fieldset", { className: "zone" }, [
    el("legend", { textContent: scheduleLegend }),
    scheduleBox,
    schedAdd,
    dataList,
  ]);
  refreshSetNames();
  return { scheduleFs, setsWrap, setsAdd, scheduleBox, setModels, validateSets };
}

// ===== lost_sector form ================================================
if (type === "lost_sector") {
  $("referenceDate").value = data.reference_date || "";

  // --- checkbox group helper -------------------------------------------
  function checkGroup(options, selected) {
    const wrap = el("div", { className: "checks" });
    const boxes = [];
    for (const opt of options) {
      const cb = el("input", { type: "checkbox", value: opt });
      cb.checked = (selected || []).includes(opt);
      boxes.push(cb);
      wrap.append(el("label", {}, [cb, " " + opt]));
    }
    wrap._value = () => boxes.filter((b) => b.checked).map((b) => b.value);
    return wrap;
  }

  // --- sectors ---------------------------------------------------------
  const sectorsBox = $("sectors");
  const sectorRows = [];

  function difficultyFields(title, diff) {
    const champs = checkGroup(vocab.champions, diff.champions);
    const shields = checkGroup(vocab.shields, diff.shields);
    const box = el("fieldset", {}, [
      el("legend", { textContent: title }),
      el("div", { className: "field" }, [el("label", { textContent: "Champions" }), champs]),
      el("div", { className: "field" }, [el("label", { textContent: "Shields" }), shields]),
    ]);
    box._value = () => ({ champions: champs._value(), shields: shields._value() });
    return box;
  }

  function addSector(s = {}) {
    const name = el("input", { type: "text", value: s.name || "", placeholder: "Sector name" });
    let prevName = name.value.trim();
    name.addEventListener("input", () => { refreshSectorNames(); validateConsistency(); });
    // On commit, propagate a rename to every schedule day that referenced the old
    // name, so the Sectors and Planet-cycles tabs never drift apart.
    name.addEventListener("change", () => {
      const oldName = prevName;
      const newName = name.value.trim();
      if (oldName && newName && oldName !== newName) propagateRename(oldName, newName);
      prevName = newName;
      refreshSectorNames();
      validateConsistency();
    });
    const gfx = el("input", { type: "url", value: s.shortlink_gfx || "", placeholder: "https://…" });
    const expert = difficultyFields("Expert", s.expert || {});
    const master = difficultyFields("Master", s.master || {});
    const remove = el("button", { className: "tiny danger", type: "button", textContent: "✕" });

    const card = el("div", { className: "card" }, [
      el("div", { className: "card-head" }, [el("span", { className: "grow" }), remove]),
      el("div", { className: "field" }, [el("label", { textContent: "Name" }), name]),
      el("div", { className: "field" }, [el("label", { textContent: "Graphic link" }), gfx]),
      el("div", { className: "diff-cols" }, [el("div", {}, [expert]), el("div", {}, [master])]),
    ]);
    const entry = {
      card,
      nameInput: name,
      value: () => ({
        name: name.value.trim(),
        shortlink_gfx: gfx.value.trim(),
        expert: expert._value(),
        master: master._value(),
      }),
    };
    remove.addEventListener("click", () => {
      const goneName = name.value.trim();
      card.remove();
      sectorRows.splice(sectorRows.indexOf(entry), 1);
      // Auto-remove this sector's schedule days — unless another sector still
      // holds the same name (the duplicate case).
      if (goneName && !sectorRows.some((r) => r.value().name === goneName)) {
        for (const zone of vocab.zones)
          for (const row of [...zoneRows[zone].children])
            if (row._value() === goneName) row.remove();
      }
      refreshSectorNames();
      validateConsistency();
    });
    sectorRows.push(entry);
    sectorsBox.append(card);
  }

  function refreshSectorNames() {
    const dl = $("sectorNames");
    dl.replaceChildren(
      ...sectorRows.map((r) => el("option", { value: r.value().name })).filter((o) => o.value),
    );
  }

  // --- consistency between the Sectors and Planet-cycles tabs ----------
  function nameCounts() {
    const counts = {};
    for (const r of sectorRows) {
      const n = r.value().name;
      counts[n] = (counts[n] || 0) + 1;
    }
    return counts;
  }

  function propagateRename(oldName, newName) {
    for (const zone of vocab.zones)
      for (const row of zoneRows[zone].children)
        if (row._value() === oldName) row._input.value = newName;
  }

  // Highlight blank/duplicate sector names and schedule days that match no
  // sector; return the problems (empty means the document is internally consistent).
  function validateConsistency() {
    const counts = nameCounts();
    const names = new Set(sectorRows.map((r) => r.value().name).filter(Boolean));
    const problems = new Set();
    for (const r of sectorRows) {
      const n = r.nameInput.value.trim();
      const bad = !n || counts[n] > 1;
      r.nameInput.classList.toggle("invalid", bad);
      if (!n) problems.add("a sector has a blank name");
      else if (counts[n] > 1) problems.add(`duplicate sector name "${n}"`);
    }
    for (const zone of vocab.zones)
      for (const row of zoneRows[zone].children) {
        const v = row._value();
        const bad = !!v && !names.has(v);
        row._input.classList.toggle("invalid", bad);
        if (bad) problems.add(`"${v}" in ${zone} isn't a sector`);
      }
    return [...problems];
  }

  (data.sectors || []).forEach(addSector);
  if (!sectorRows.length) addSector();
  refreshSectorNames();
  $("addSector").addEventListener("click", () => { addSector(); refreshSectorNames(); validateConsistency(); });

  // --- schedule (per zone, ordered list of sector names) ---------------
  const scheduleBox = $("schedule");
  const zoneRows = {};
  function addScheduleEntry(list, value) {
    const input = el("input", { type: "text", value: value || "", className: "grow" });
    input.setAttribute("list", "sectorNames");
    input.addEventListener("input", validateConsistency);
    const up = el("button", { className: "tiny secondary", type: "button", textContent: "↑" });
    const down = el("button", { className: "tiny secondary", type: "button", textContent: "↓" });
    const del = el("button", { className: "tiny danger", type: "button", textContent: "✕" });
    const row = el("div", { className: "row" }, [input, up, down, del]);
    row._value = () => input.value.trim();
    row._input = input;
    del.addEventListener("click", () => { row.remove(); validateConsistency(); });
    up.addEventListener("click", () => row.previousElementSibling && row.parentNode.insertBefore(row, row.previousElementSibling));
    down.addEventListener("click", () => row.nextElementSibling && row.parentNode.insertBefore(row.nextElementSibling, row));
    list.append(row);
  }
  for (const zone of vocab.zones) {
    const list = el("div");
    const add = el("button", { className: "tiny secondary", type: "button", textContent: "+ Day" });
    add.addEventListener("click", () => { addScheduleEntry(list); validateConsistency(); });
    const fs = el("fieldset", { className: "zone" }, [el("legend", { textContent: zone }), list, add]);
    scheduleBox.append(fs);
    zoneRows[zone] = list;
    ((data.schedule || {})[zone] || []).forEach((n) => addScheduleEntry(list, n));
  }

  // Wire the shared Save gate + paint the initial highlight now the schedule exists.
  consistencyProblems = validateConsistency;
  validateConsistency();

  collect = () => {
    const schedule = {};
    for (const zone of vocab.zones)
      schedule[zone] = [...zoneRows[zone].children].map((r) => r._value()).filter(Boolean);
    return {
      version: data.version || 1,
      reference_date: $("referenceDate").value,
      schedule,
      sectors: sectorRows.map((r) => r.value()),
    };
  };
}

// ===== xur_location form ===============================================
if (type === "xur_location") {
  const locationsBox = $("locations");
  const locationRows = [];

  function addLocation(loc = {}) {
    const api = el("input", { type: "text", value: loc.api_location_name || "", placeholder: "API location name (from Bungie)" });
    const friendly = el("input", { type: "text", value: loc.friendly_location_name || "", placeholder: "Friendly name (shown in the post)" });
    const link = el("input", { type: "url", value: loc.link || "", placeholder: "https://… (optional)" });
    const remove = el("button", { className: "tiny danger", type: "button", textContent: "✕" });

    const card = el("div", { className: "card" }, [
      el("div", { className: "card-head" }, [el("span", { className: "grow" }), remove]),
      el("div", { className: "field" }, [el("label", { textContent: "API location name" }), api]),
      el("div", { className: "field" }, [el("label", { textContent: "Friendly name" }), friendly]),
      el("div", { className: "field" }, [el("label", { textContent: "Link" }), link]),
    ]);
    const entry = {
      card,
      value: () => ({
        api_location_name: api.value.trim(),
        friendly_location_name: friendly.value.trim(),
        link: link.value.trim(),
      }),
    };
    remove.addEventListener("click", () => {
      card.remove();
      locationRows.splice(locationRows.indexOf(entry), 1);
    });
    locationRows.push(entry);
    locationsBox.append(card);
  }

  (data.locations || []).forEach(addLocation);
  if (!locationRows.length) addLocation();
  $("addLocation").addEventListener("click", () => addLocation());

  collect = () => ({
    version: data.version || 1,
    locations: locationRows
      .map((r) => r.value())
      .filter((r) => r.api_location_name)
      .map((r) => {
        const out = { api_location_name: r.api_location_name };
        if (r.friendly_location_name) out.friendly_location_name = r.friendly_location_name;
        if (r.link) out.link = r.link;
        return out;
      }),
  });
}

// ===== world-activity form =============================================
// A world-activity destination is a fixed set of activities (from the loaded doc); each
// activity has fixed elements, and each element is its own independent, editable list
// of cycle values. The structure (activities/elements/names) is pinned by the spec, so
// this form only edits reference_date and the per-element value lists.
if (isWorldActivity) {
  $("referenceDate").value = data.reference_date || "";
  const box = $("legacyActivities");
  const label = (name) =>
    name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  // addValue / itemAutocomplete / valueList / buildSetPool are hoisted to module scope
  // (shared with the trials_loot form).

  const activityModels = [];
  // Each set-based activity registers a validator here; Save is gated on all of them
  // (mirrors the lost_sector consistency gate between sectors and the schedule).
  const setsValidators = [];
  for (const act of data.activities || []) {
    // ----- set-based activity (Dares loot): a schedule + a pool of sets --------------
    // The Weekly schedule references sets by name, with autocomplete + the same
    // consistency/rename treatment the lost-sector editor gives its sectors ↔ schedule.
    if (act.kind === "sets") {
      const pool = buildSetPool({
        sets: act.sets,
        schedule: act.schedule,
        includeArmor: true,
        scheduleLegend: "Weekly schedule (set names, in order, looping)",
      });
      box.append(
        el("div", { className: "card" }, [
          el("div", { className: "card-head" }, [el("strong", { className: "grow", textContent: act.title })]),
          el("p", { className: "muted", textContent: "Changes weekly" }),
          pool.scheduleFs,
          el("h3", { textContent: "Sets" }),
          pool.setsWrap,
        ]),
      );
      activityModels.push({ kind: "sets", key: act.key, title: act.title, cadence: act.cadence, scheduleBox: pool.scheduleBox, setModels: pool.setModels });
      setsValidators.push(pool.validateSets);
      continue;
    }

    // ----- element-based activity: one independent value list per element ------------
    const elementModels = [];
    const elemsWrap = el("div");
    for (const elem of act.elements || []) {
      // Weapon elements (e.g. Terminal Overload / Wellspring / Altar) get autocomplete.
      const kind = elem.name === "weapon" ? "weapon" : null;
      const { fs, list } = valueList(label(elem.name), elem.values, "+ Value", kind);
      elemsWrap.append(fs);
      elementModels.push({ name: elem.name, list });
    }
    box.append(
      el("div", { className: "card" }, [
        el("div", { className: "card-head" }, [el("strong", { className: "grow", textContent: act.title })]),
        el("p", { className: "muted", textContent: act.cadence === "daily" ? "Changes daily" : "Changes weekly" }),
        elemsWrap,
      ]),
    );
    activityModels.push({ kind: "elements", key: act.key, title: act.title, cadence: act.cadence, elements: elementModels });
  }

  collect = () => ({
    version: data.version || 1,
    reference_date: $("referenceDate").value,
    activities: activityModels.map((a) =>
      a.kind === "sets"
        ? {
            key: a.key,
            title: a.title,
            cadence: a.cadence,
            kind: "sets",
            schedule: listValues(a.scheduleBox),
            sets: a.setModels.map((s) => ({
              name: s.nameInput.value.trim(),
              weapons: listValues(s.weaponsList),
              armor: listValues(s.armorList),
            })),
          }
        : {
            key: a.key,
            title: a.title,
            cadence: a.cadence,
            elements: a.elements.map((e) => ({ name: e.name, values: listValues(e.list) })),
          },
    ),
  });

  // Gate Save on every set-based activity's consistency, and paint the initial state.
  consistencyProblems = () => setsValidators.flatMap((v) => v());
  setsValidators.forEach((v) => v());

  // Reset-to-defaults: discard this destination's stored data and re-seed it from the
  // committed defaults, then reload so the form rebuilds from the fresh doc. The manual
  // recovery path for a rotation that has gone bad (command errors alert us → reset).
  $("resetDefaults").addEventListener("click", async () => {
    if (!confirm(`Reset "${type}" to the committed defaults? This discards the current stored data for it.`))
      return;
    setStatus("Resetting…", true);
    try {
      const res = await api("/rotation/reset", { type });
      const body = await res.text();
      if (!res.ok) return setStatus("Reset failed: " + body, false);
      setStatus("Reset ✓ — reloading…", true);
      location.reload();
    } catch (e) {
      setStatus("Reset error: " + e, false);
    }
  });
}

// ===== trials_loot form ================================================
// A standalone, weapons-only set pool (no armor, no reference_date): the same set-pool UI
// the Dares "Legendary Loot" activity uses, minus the world-activity envelope, plus
// add/remove-set controls (Dares' pool is Bungie-fixed; the Trials pool is owner-managed).
// The Trials producer owns a skip-aware cursor over the schedule — nothing to date-anchor.
if (type === "trials_loot") {
  const pool = buildSetPool({
    sets: data.sets,
    schedule: data.schedule,
    includeArmor: false,
    scheduleLegend: "Schedule (set names, in order, looping)",
    allowAddRemove: true,
  });
  $("trialsLootSets").append(
    pool.scheduleFs,
    el("h3", { textContent: "Sets" }),
    pool.setsWrap,
    pool.setsAdd,
  );

  // Gate Save on the set-pool consistency, and paint the initial highlight.
  consistencyProblems = pool.validateSets;
  pool.validateSets();

  collect = () => ({
    version: data.version || 1,
    schedule: listValues(pool.scheduleBox),
    sets: pool.setModels.map((s) => ({
      name: s.nameInput.value.trim(),
      weapons: listValues(s.weaponsList),
    })),
  });
}

// ===== iron_banner form ================================================
// A date-anchored schedule of Iron Banner weeks — each names a start date (a Tuesday
// reset), the bonus focus pool active that week, and the game modes — plus a pool of
// named weapon lists. Unlike Trials there's no cursor: each week is a calendar entry.
// Save is gated on every schedule week naming a defined pool + carrying a start date, and
// on pool names being non-blank + unique. Pool renames propagate to the schedule.
if (type === "iron_banner") {
  const poolNamesId = "ibPoolNames";
  const poolNamesDl = el("datalist", { id: poolNamesId });
  const scheduleBox = el("div");
  const poolsWrap = el("div");
  const poolModels = [];

  function refreshPoolNames() {
    poolNamesDl.replaceChildren(
      ...poolModels.map((m) => el("option", { value: m.nameInput.value.trim() })).filter((o) => o.value),
    );
  }

  // Highlight blank/duplicate pool names and schedule weeks that name no pool or lack a
  // start date; return the problems (empty ⇒ internally consistent).
  function validate() {
    const names = poolModels.map((m) => m.nameInput.value.trim());
    const counts = {};
    for (const n of names) counts[n] = (counts[n] || 0) + 1;
    const known = new Set(names.filter(Boolean));
    const problems = new Set();
    // The schema requires at least one pool (minItems: 1); catch it here so removing
    // every pool surfaces inline instead of as a raw server 400 on Save.
    if (!poolModels.length) problems.add("add at least one bonus focus pool");
    for (const m of poolModels) {
      const n = m.nameInput.value.trim();
      const bad = !n || counts[n] > 1;
      m.nameInput.classList.toggle("invalid", bad);
      if (!n) problems.add("a pool has a blank name");
      else if (counts[n] > 1) problems.add(`duplicate pool name "${n}"`);
    }
    for (const row of scheduleBox.children) {
      const p = row._pool();
      // A blank pool is as invalid as an unknown one — every week must name a pool
      // (the server rejects an empty/undefined pool). Flag both, distinctly.
      const poolBad = !known.has(p);
      row._poolInput.classList.toggle("invalid", poolBad);
      if (!p) problems.add("a schedule week has no pool");
      else if (poolBad) problems.add(`schedule week "${p}" isn't a pool`);
      const startBad = !row._start();
      row._startInput.classList.toggle("invalid", startBad);
      if (startBad) problems.add("a schedule week has no start date");
    }
    return [...problems];
  }

  function addWeek(w = {}) {
    const start = el("input", { type: "date", value: w.start || "" });
    start.addEventListener("input", validate);
    const pool = el("input", { type: "text", value: w.pool || "", className: "grow", placeholder: "Pool name" });
    pool.setAttribute("list", poolNamesId);
    pool.addEventListener("input", validate);
    const modes = el("input", { type: "text", value: w.modes || "", placeholder: "Control / Eruption" });
    const up = el("button", { className: "tiny secondary", type: "button", textContent: "↑" });
    const down = el("button", { className: "tiny secondary", type: "button", textContent: "↓" });
    const del = el("button", { className: "tiny danger", type: "button", textContent: "✕ Remove week" });
    const row = el("div", { className: "card" }, [
      el("div", { className: "field" }, [el("label", { textContent: "Start date (a weekly-reset Tuesday)" }), start]),
      el("div", { className: "field" }, [el("label", { textContent: "Bonus focus pool" }), pool]),
      el("div", { className: "field" }, [el("label", { textContent: "Game modes (blank = Control / Eruption)" }), modes]),
      el("div", { className: "row" }, [up, down, del]),
    ]);
    row._start = () => start.value;
    row._pool = () => pool.value.trim();
    row._modes = () => modes.value.trim();
    row._startInput = start;
    row._poolInput = pool;
    del.addEventListener("click", () => { row.remove(); validate(); });
    up.addEventListener("click", () => row.previousElementSibling && row.parentNode.insertBefore(row, row.previousElementSibling));
    down.addEventListener("click", () => row.nextElementSibling && row.parentNode.insertBefore(row.nextElementSibling, row));
    scheduleBox.append(row);
  }

  function addPool(p = {}) {
    const nameInput = el("input", { type: "text", value: p.name || "", className: "grow", placeholder: "Pool name" });
    const weapons = valueList("Weapons", p.weapons, "+ Weapon", "weapon");
    const remove = el("button", { className: "tiny danger", type: "button", textContent: "✕ Remove pool" });
    const model = { nameInput, weaponsList: weapons.list, prev: (p.name || "").trim() };
    const card = el("div", { className: "card" }, [
      el("div", { className: "card-head" }, [el("span", { className: "grow" }), remove]),
      el("div", { className: "field" }, [el("label", { textContent: "Pool name" }), nameInput]),
      weapons.fs,
    ]);
    remove.addEventListener("click", () => {
      const goneName = nameInput.value.trim();
      card.remove();
      poolModels.splice(poolModels.indexOf(model), 1);
      // Drop schedule weeks that named this pool, unless a duplicate still holds the name.
      if (goneName && !poolModels.some((m) => m.nameInput.value.trim() === goneName))
        for (const row of [...scheduleBox.children]) if (row._pool() === goneName) row.remove();
      refreshPoolNames();
      validate();
    });
    nameInput.addEventListener("input", () => { refreshPoolNames(); validate(); });
    // On commit, propagate a rename to every schedule week that named the old pool.
    nameInput.addEventListener("change", () => {
      const oldName = model.prev;
      const newName = nameInput.value.trim();
      if (oldName && newName && oldName !== newName)
        for (const row of scheduleBox.children) if (row._pool() === oldName) row._poolInput.value = newName;
      model.prev = newName;
      refreshPoolNames();
      validate();
    });
    poolsWrap.append(card);
    poolModels.push(model);
  }

  (data.pools || []).forEach(addPool);
  if (!poolModels.length) addPool();
  const poolsAdd = el("button", { className: "secondary tiny", type: "button", textContent: "+ Add pool" });
  poolsAdd.addEventListener("click", () => { addPool(); refreshPoolNames(); validate(); });

  (data.schedule || []).forEach(addWeek);
  const weekAdd = el("button", { className: "tiny secondary", type: "button", textContent: "+ Week" });
  weekAdd.addEventListener("click", () => { addWeek(); validate(); });

  $("ibSchedule").append(scheduleBox, weekAdd, poolNamesDl);
  $("ibPools").append(poolsWrap, poolsAdd);
  refreshPoolNames();

  // Gate Save on the schedule ↔ pool consistency, and paint the initial highlight.
  consistencyProblems = validate;
  validate();

  collect = () => ({
    version: data.version || 1,
    schedule: [...scheduleBox.children]
      .map((r) => {
        const out = { start: r._start(), pool: r._pool() };
        if (r._modes()) out.modes = r._modes();
        return out;
      })
      .filter((s) => s.start || s.pool),
    pools: poolModels.map((m) => ({
      name: m.nameInput.value.trim(),
      weapons: listValues(m.weaponsList),
    })),
  });
}

// --- submit helpers ----------------------------------------------------
function setStatus(msg, ok) {
  const s = $("status");
  s.textContent = msg;
  s.className = ok ? "ok" : "err";
}

// Delegates to the shared api() wrapper (shared.js); auth is the rotation_session cookie
// (same-origin fetch sends it). Same request shape as before the html/js split.
async function post(path) {
  return api(path, { type, data: collect() });
}

// --- tabs --------------------------------------------------------------
const tabBar = $("tabBar");
function showTab(name) {
  for (const b of tabBar.querySelectorAll("button"))
    b.classList.toggle("active", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".panel"))
    p.classList.toggle("active", p.dataset.panel === name);
  if (name === "preview") runPreview();
}
tabBar.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) showTab(btn.dataset.tab);
});

// --- preview -----------------------------------------------------------
async function runPreview() {
  const box = $("previewBox");
  setStatus("Rendering preview…", true);
  box.textContent = "Rendering…";
  try {
    const res = await post("/rotation/preview");
    if (!res.ok) {
      box.textContent = "Preview failed:\n" + (await res.text());
      return setStatus("Preview failed — see the Preview tab.", false);
    }
    const data = await res.json();
    if (data.kind === "html") {
      // xur_location / trials_loot are data views of a document that never posts on
      // its own — a small server-rendered table, not a message.
      box.innerHTML = data.html;
    } else {
      // A wall of real posts, each drawn from the node tree build_cv2 would send. The
      // .post-wall wrapper is what spaces the cards — the labelled-card layout stayed
      // behind when the post *rendering* moved to the shared renderer.
      //
      // Described in the renderer's own spec vocabulary rather than assembled by hand:
      // the cards are chrome around a post, so they may as well go through the same
      // one-pass draw, which also keeps each label in a `text` field (textContent).
      const R = window.CV2Render;
      const posts = data.posts || [];
      R.render(
        box,
        posts.length
          ? R.el("div", "post-wall", {
              children: posts.map((item) =>
                R.el("article", "post-wall-item", {
                  children: [
                    R.el("h3", "sectionhead post-wall-label", { text: item.label }),
                    R.el("div", "cv2-preview", {
                      children: [R.nodesSpec(item.nodes || [])],
                    }),
                  ],
                }),
              ),
            })
          : R.el("p", "post-wall-empty", { text: "No upcoming posts to show." }),
        { emoji: data.emoji || {} },
      );
    }
    setStatus("Preview updated.", true);
  } catch (e) {
    box.textContent = "Preview error: " + e;
    setStatus("Preview error.", false);
  }
}
$("refreshPreview").addEventListener("click", runPreview);

// --- save --------------------------------------------------------------
$("saveBtn").addEventListener("click", async () => {
  const problems = consistencyProblems();
  if (problems.length) {
    return setStatus("Fix before saving: " + problems.slice(0, 4).join("; "), false);
  }
  setStatus("Saving…", true);
  try {
    const res = await post("/rotation/edit");
    const body = await res.text();
    if (!res.ok) return setStatus("Save failed: " + body, false);
    setStatus("Saved ✓ — changes are live. Keep editing or close the tab.", true);
  } catch (e) { setStatus("Save error: " + e, false); }
});

// Activate the initial tab. lost_sector keeps its static default (Sectors);
// the other types' default tab lived in the removed markup, so activate it here.
if (type === "xur_location") showTab("locations");
if (isWorldActivity) showTab("activities");
if (type === "trials_loot") showTab("trials_loot");
if (type === "iron_banner") showTab("iron_banner");
