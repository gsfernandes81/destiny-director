// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Preview and Send now, for any page that renders feed action buttons.
//
// The pair replaced the `/<feed> show` and `send` slash commands. Two pages carry them
// now — /feeds, where they sit on the feed's settings row, and /send, which is nothing
// BUT those buttons — and both drive the same two endpoints
// (`GET /feed/{name}/preview`, `POST /feed/{name}/send`) through the same two dialogs.
// This file is that machinery, extracted from autopost_settings.js when the second page
// arrived: the send confirmation is the one place an irreversible click is armed, and a
// second copy of it would be a second set of words about what sending means.
//
// It wires ITSELF on DOMContentLoaded when the page carries the dialogs, so a page opts
// in by including the markup (see feed_modals.html, spliced in server-side) and loading
// this script — there is no init call to forget. The three ids it reaches for outside
// the dialogs are `.feedaction` buttons (server-rendered — see feed_actions.py's
// `actions_html`) and `#status`, the page's own status line, which is where a started
// send reports because both dialogs are closed by then.
//
// A button carries its own `data-label`: the two pages draw a feed's name into different
// DOM, and a shared module that went looking for it would have to know both shapes.
//
// Loaded deferred, after shared.js (window.api/say/busy), cv2_model.js and cv2_render.js
// (window.CV2Render draws the post). Its styles are feed_actions.css — the pairing is
// enforced by web_static/tests/asset_links.test.js.

"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const byId = (id) => document.getElementById(id);

  // A page with no dialogs is a page that renders no feed actions: bail before the first
  // addEventListener, which would otherwise throw out of DOMContentLoaded and take any
  // listener registered after this one down with it. Bail on the DIALOGS rather than on
  // the buttons: a page can legitimately have the modals and no rows to act on (nothing
  // is registered at boot), and that is not this condition.
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
  // Where "the send started" lands once the dialog has closed. /feeds shares it with the
  // Save button's line, /send has one of its own; either way the page owns it.
  const status = byId("status");

  let pending = null; // the slug the send dialog is currently confirming
  // Bumped whenever a preview starts or a dialog closes. A build takes seconds (live
  // API), so a slow one can land after its dialog was cancelled — and would otherwise
  // draw into a closed dialog, or worse, into the NEXT feed's. Each draw checks the
  // token it started with and abandons if it is no longer current.
  let drawToken = 0;

  /** The feed's name as its row spells it, for modal copy. */
  function labelFor(el) {
    return el.dataset.label || el.dataset.slug;
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
      const label = labelFor(el);

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
