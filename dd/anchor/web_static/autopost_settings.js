// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// The autopost settings page: the save button, plus each feed row's two actions.
//
// Preview and Send now replaced the `/<feed> show` and `send` slash commands. They live
// on the row rather than a per-feed page — a feed has no state a page could show that
// the row does not — and the rendered post appears in a modal, so the list itself stays
// a list of toggles.
//
// Extracted from an inline <script> so `script-src 'self'` holds (see SECURITY_HEADERS
// in dd/anchor/web.py). Loaded deferred after shared.js (window.api) and cv2_render.js.

"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const byId = (id) => document.getElementById(id);

  // --- save ------------------------------------------------------------------------

  const btn = byId("save");
  const status = byId("status");
  btn.addEventListener("click", async () => {
    const settings = {};
    document
      .querySelectorAll("input[type=checkbox][data-slug]")
      .forEach((el) => {
        settings[el.dataset.slug] = el.checked;
      });
    document
      .querySelectorAll("input.urlfield[data-slug]")
      .forEach((el) => {
        settings[el.dataset.slug] = el.value;
      });
    btn.disabled = true;
    busy(status, "Saving…");
    try {
      const res = await window.api("/autopost_settings/save", { settings });
      if (res.ok) {
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

  const previewDialog = byId("previewDialog");
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
          "Send started — it continues even if you leave. Check Mirror logs for " +
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
