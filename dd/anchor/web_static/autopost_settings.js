// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Both settings pages — /feeds and /settings: the save button and the colour/channel
// pickers.
//
// One script for two pages, because what the two have in common is nearly all of it: the
// save protocol (see the baseline map below) is identical, and the channel picker appears
// on both.
//
// What is deliberately NOT here any more is the Preview / Send now pair each feed row
// carries. /send drives the same buttons and the same confirmation, so that machinery
// moved to feed_actions.js (with its markup in feed_modals.html and its look in
// feed_actions.css) — one send dialog for the whole panel rather than a copy per page.
// This file keeps only what a *settings* page does.
//
// Extracted from an inline <script> so `script-src 'self'` holds (see SECURITY_HEADERS
// in dd/anchor/web.py). Loaded deferred after shared.js (window.api) and Tom Select
// (window.TomSelect — see the vendored-widgets note by initChannelPickers).

"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const byId = (id) => document.getElementById(id);

  // --- colour pickers ----------------------------------------------------------------
  //
  // Each .colorrow carries a paired swatch (input[type=color], the visual picker) and
  // text field (the actual data-slug input Save reads, and the only one a hand-typed
  // hex reaches). They mirror each other on every change so either can drive the other.

  document.querySelectorAll(".colorswatch").forEach((swatch) => {
    const field = document.querySelector(
      `.colorfield[data-slug="${swatch.dataset.for}"]`,
    );
    if (!field) return;
    swatch.addEventListener("input", () => {
      field.value = swatch.value;
    });
    field.addEventListener("input", () => {
      if (/^#[0-9a-fA-F]{6}$/.test(field.value)) swatch.value = field.value;
    });
  });

  // --- channel pickers -----------------------------------------------------------------
  //
  // Widgets: Tom Select (vendored, window.TomSelect, no build step — same widget and
  // dark-theme override as weekly_reset_form.js) turns each .channelfield <select> into
  // a searchable single-item picker, for exactly one item (never a multi-select) — the
  // channel a feed/setting posts to. One fetch of /autopost_settings/channels supplies
  // every field; a field's own data-scope then filters to the guild(s) it may pick from
  // (a followable's own channel is feed-guild-only; the log/alerts channels may be in a
  // feed guild or the control server), and data-announce-only filters to announcement —
  // a followable's post channel must be one for Discord's native "Follow Channel" to
  // work at all, unlike log_channel_id/alerts_channel_id, which the bot only ever sends
  // to directly and so may be a plain text channel. A stored id the live fetch didn't
  // return (the bot cannot see that channel right now, or it was deleted) is kept as a
  // synthetic "unknown" option rather than silently dropped — via ts_single.js's
  // tsWithCurrentOption, shared with weekly_reset_form.js's tsSingle.
  async function initChannelPickers() {
    const fields = document.querySelectorAll("select.channelfield");
    if (!fields.length) return;

    let data;
    try {
      const res = await fetch("/autopost_settings/channels");
      data = await res.json();
    } catch (_) {
      data = null;
    }
    const channels = (data && data.channels) || [];
    // The guild(s) a feed may post in: Kyber in production, the TEST_ENV servers on a
    // test deployment, which is why this is a list the server sends rather than the one
    // id it used to be (see autopost_settings.py's _feed_guild_ids).
    const feedGuilds = (data && data.feedGuildIds) || [];
    const controlId = data && data.controlGuildId;

    fields.forEach((select) => {
      const scope = select.dataset.scope;
      const announceOnly = select.dataset.announceOnly === "true";
      const allowedGuilds =
        scope === "kyber_control" ? [...feedGuilds, controlId] : feedGuilds;
      const rawOptions = channels
        .filter((c) => allowedGuilds.includes(c.guildId))
        .filter((c) => !announceOnly || c.announce)
        .map((c) => ({ value: c.id, text: c.name }));

      const current = select.value || "";
      const options = tsWithCurrentOption(
        rawOptions,
        current,
        (id) => `Unknown channel (${id})`,
      );

      // data-required marks a field the save gate refuses to clear (a followable's
      // post channel — see _UNCLEARABLE_CHANNEL_SLUGS). Offering an X there would only
      // produce a rejected save, so the clear button is for the clearable fields.
      const required = select.dataset.required === "true";
      // An empty picker says what is missing rather than sitting blank — a blank control
      // is indistinguishable from one that failed to load, and "no channel" is a real
      // state a feed can be in. The server renders the same words into the pre-JS
      // <option> (autopost_settings.py's _NO_CHANNEL_LABEL), so the page does not change
      // its mind about them as Tom Select mounts.
      const ts = new TomSelect(select, {
        options,
        maxOptions: 200,
        placeholder: current ? "Search channels…" : "No channel set",
        plugins: required ? [] : ["clear_button"],
        allowEmptyOption: !required,
      });
      if (current) ts.setValue(current, true);
    });
  }
  initChannelPickers();

  // --- save ------------------------------------------------------------------------
  //
  // The page submits every field it renders, but sends `null` for the ones the
  // operator did not touch: 0-signal and no-signal are different answers. The server
  // skips a null outright — it is neither validated nor rewritten — which is what lets
  // one invalid or deliberately-empty field coexist with a save of something unrelated,
  // and what keeps an already-unconfigured feed from reading as an attempt to *clear*
  // its (unclearable) channel. Baselines are captured synchronously at load, before
  // Tom Select rewrites the channel selects, so they reflect what was rendered.

  const FIELD_SELECTOR =
    "input[type=checkbox][data-slug], input.urlfield[data-slug], " +
    "input.colorfield[data-slug], select.selectfield[data-slug], " +
    "select.channelfield[data-slug]";

  /** The value a field currently holds: checked for a toggle, value for the rest. */
  const readField = (el) => (el.type === "checkbox" ? el.checked : el.value);

  const baseline = new Map();
  document
    .querySelectorAll(FIELD_SELECTOR)
    .forEach((el) => baseline.set(el, readField(el)));

  const btn = byId("save");
  const status = byId("status");
  btn.addEventListener("click", async () => {
    const settings = {};
    document.querySelectorAll(FIELD_SELECTOR).forEach((el) => {
      const value = readField(el);
      settings[el.dataset.slug] = value === baseline.get(el) ? null : value;
    });
    btn.disabled = true;
    busy(status, "Saving…");
    try {
      const res = await window.api("/autopost_settings/save", { settings });
      if (res.ok) {
        // What is on the page is now what is in the DB, so the next save starts from
        // a clean slate — otherwise every later save would resubmit these same fields.
        document
          .querySelectorAll(FIELD_SELECTOR)
          .forEach((el) => baseline.set(el, readField(el)));
        say(status, "Saved.", false);
      } else {
        let msg = "Save failed.";
        try {
          const data = await res.json();
          if (data && data.error) msg = data.error;
        } catch (_) {}
        say(status, msg, true);
      }
    } catch (_) {
      say(status, "Network error — try again.", true);
    } finally {
      btn.disabled = false;
    }
  });

});
