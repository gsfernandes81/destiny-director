// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Both settings pages — /feeds and /settings: the save button, the colour/channel
// pickers, plus each feed row's two actions.
//
// One script for two pages, because what the two have in common is nearly all of it: the
// save protocol (see the baseline map below) is identical, and the channel picker appears
// on both. What only /feeds has is the per-feed Preview/Send pair, which is why that
// section returns early when the page carries no modals rather than being a third file
// with its own copy of the save.
//
// Preview and Send now replaced the `/<feed> show` and `send` slash commands. They live
// on the row rather than a per-feed page — a feed has no state a page could show that
// the row does not — and the rendered post appears in a modal, so the list itself stays
// a list of toggles.
//
// Extracted from an inline <script> so `script-src 'self'` holds (see SECURITY_HEADERS
// in dd/anchor/web.py). Loaded deferred after shared.js (window.api), cv2_render.js and
// Tom Select (window.TomSelect — see the vendored-widgets note by initChannelPickers).

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

  // --- per-feed actions --------------------------------------------------------------
  //
  // /feeds only. /settings renders no feed rows and hosts no modals, so every byId below
  // would be null and the first addEventListener a TypeError — which, being thrown from
  // DOMContentLoaded, would take the save button's listener down with it if this ran
  // first. Bail on the dialogs rather than on the buttons: a page could have the modals
  // and no registered feeds (nothing is wired at boot), and that is not this condition.

  const previewDialog = byId("previewDialog");
  if (!previewDialog) return;
  const previewTitle = byId("previewTitle");
  const previewBox = byId("previewBox");
  const previewStatus = byId("previewStatus");

  const sendDialog = byId("sendDialog");
  const sendTitle = byId("sendTitle");
  const sendBody = byId("sendBody");
  const sendPreview = byId("sendPreview");
  const sendPreviewStatus = byId("sendPreviewStatus");
  const sendStatus = byId("sendStatus");
  const sendConfirm = byId("sendConfirm");
  const publish = byId("publish");

  let pending = null; // the slug the send dialog is currently confirming
  // Bumped whenever a preview starts or a dialog closes. A build takes seconds (live
  // API), so a slow one can land after its dialog was cancelled — and would otherwise
  // draw into a closed dialog, or worse, into the NEXT feed's. Each draw checks the
  // token it started with and abandons if it is no longer current.
  let drawToken = 0;

  /** The feed's label as rendered on its row, for modal copy. */
  function labelFor(slug) {
    const el = document.querySelector(`.feedaction[data-slug="${slug}"]`);
    const row = el && el.closest(".row");
    const name = row && row.querySelector(".name");
    return name ? name.textContent.trim() : slug;
  }

  // say/busy/api are globals from shared.js (loaded first, deferred).

  /** Abandon any in-flight preview draw, so a slow one cannot land after this. */
  function cancelDraw() {
    drawToken++;
  }

  /**
   * Build the feed's post and draw it into `host`.
   *
   * Resolves true when a post was drawn. A build failure is a legitimate answer — Iron
   * Banner between events raises, and the Discord `show` reported it the same way — so
   * it lands in the status line rather than throwing.
   */
  async function drawPreview(slug, host, statusEl) {
    const token = ++drawToken;
    host.replaceChildren();
    busy(statusEl, "Building the post…");
    try {
      const res = await fetch("/feed/" + encodeURIComponent(slug) + "/preview");
      const data = await res.json();
      if (token !== drawToken) return false; // superseded or cancelled — drop it
      if (data.error) {
        say(statusEl, data.error, true);
        return false;
      }
      window.CV2Render.render(
        host,
        window.CV2Render.snapshotSpec(data.payload, data.message_kind),
        {},
      );
      say(statusEl, "", false);
      return true;
    } catch (e) {
      if (token !== drawToken) return false;
      say(statusEl, "Render error: " + e, true);
      return false;
    }
  }

  document.querySelectorAll(".feedaction").forEach((el) => {
    el.addEventListener("click", async () => {
      const slug = el.dataset.slug;
      const label = labelFor(slug);

      if (el.dataset.action === "preview") {
        previewTitle.textContent = label + " — preview";
        previewDialog.showModal();
        await drawPreview(slug, previewBox, previewStatus);
        return;
      }

      // Send: confirm against the post that is actually going out, so the dialog shows
      // WHAT is being sent, not just where. The preview is context, NOT a gate — send
      // stays available while it builds and even if it fails, because a feed whose
      // preview is broken is exactly one you may still need to push.
      pending = slug;
      sendTitle.textContent = "Send the " + label + " post?";
      // The "you can close this page" line matters more than it looks: the send runs in
      // the bot, detached from this request, so closing the tab cancels nothing — and
      // without saying so, an operator sits here waiting on a post that needs no
      // watching. See plans/send_status_feedback.md.
      sendBody.textContent =
        "This posts to the " + label + " channel straight away. It cannot be recalled, " +
        "only edited or deleted afterwards. Sending continues in the bot, so you can " +
        "close this page once it starts.";
      publish.checked = true;
      sendConfirm.disabled = false;
      // Clear the previous open's send error. The preview's busy() used to do this
      // incidentally, back when both wrote the same line.
      say(sendStatus, "", false);
      sendDialog.showModal();
      await drawPreview(slug, sendPreview, sendPreviewStatus);
    });
  });

  // Closing either dialog abandons an in-flight draw — including via Escape or the
  // backdrop, which is why this listens for `close` rather than only the button.
  byId("previewClose").addEventListener("click", () => previewDialog.close());
  byId("sendCancel").addEventListener("click", () => sendDialog.close());
  previewDialog.addEventListener("close", cancelDraw);
  sendDialog.addEventListener("close", cancelDraw);

  sendConfirm.addEventListener("click", async () => {
    const slug = pending;
    const wantPublish = publish.checked;
    sendConfirm.disabled = true;
    busy(sendStatus, "Sending…");
    try {
      const res = await window.api("/feed/" + encodeURIComponent(slug) + "/send", {
        publish: wantPublish,
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        sendDialog.close();
        say(
          status,
          "Send started — it continues even if you leave. Check the Delivery log for " +
            "delivery.",
          false,
        );
      } else {
        say(sendStatus, data.error || "Send failed.", true);
        sendConfirm.disabled = false;
      }
    } catch (_) {
      say(sendStatus, "Network error — try again.", true);
      sendConfirm.disabled = false;
    }
  });
});
