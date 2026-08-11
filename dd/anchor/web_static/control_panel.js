// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// The control panel's bot administration — the web replacement for /anchor info and
// /anchor stop. The card grid above it is server-rendered and needs no script.
//
// A separate file rather than an inline <script> so `script-src 'self'` holds (see
// SECURITY_HEADERS in dd/anchor/web.py). Loaded deferred after shared.js (window.api).

"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const byId = (id) => document.getElementById(id);

  const infoDialog = byId("infoDialog");
  const infoBody = byId("infoBody");
  const stopDialog = byId("stopDialog");
  const stopStatus = byId("stopStatus");
  const stopConfirm = byId("stopConfirm");
  const botStatus = byId("botStatus");

  // say/busy/api are globals from shared.js (loaded first, deferred).

  /** A <dt>/<dd> pair. textContent throughout — ids come from config, not literals. */
  function row(list, label, value, href) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = value;
      dd.appendChild(link);
    } else {
      dd.textContent = value;
    }
    list.append(dt, dd);
  }

  byId("infoBtn").addEventListener("click", async () => {
    infoBody.replaceChildren();
    say(botStatus, "", false);
    infoDialog.showModal();
    try {
      const res = await fetch("/bot/info");
      if (!res.ok) throw new Error("could not read configuration");
      const data = await res.json();

      const dl = document.createElement("dl");
      row(dl, "Bot", data.bot);
      row(dl, "Control server", data.controlServerId);
      // An empty test-env list is what marks an environment as production, so say so
      // rather than rendering a blank.
      row(
        dl,
        "Test environment",
        data.testEnv.length ? data.testEnv.join(", ") : "(none — production)",
      );
      infoBody.appendChild(dl);

      const heading = document.createElement("h3");
      heading.textContent = "Channels";
      infoBody.appendChild(heading);

      const feeds = document.createElement("dl");
      if (data.channels.length) {
        // Prefer the name; fall back to the id only when the bot could not resolve it
        // (not in the guild, channel deleted, or still starting up).
        data.channels.forEach((c) =>
          // A link only when the bot resolved the guild — otherwise it would 404.
          row(feeds, c.feed, c.channelName || c.channelId || "(not set)", c.url),
        );
      } else {
        row(feeds, "—", "(not set)");
      }
      infoBody.appendChild(feeds);
    } catch (e) {
      infoBody.textContent = "Could not read configuration: " + e;
    }
  });

  byId("infoClose").addEventListener("click", () => infoDialog.close());

  byId("stopBtn").addEventListener("click", () => {
    say(stopStatus, "", false);
    stopConfirm.disabled = false;
    stopDialog.showModal();
  });

  byId("stopCancel").addEventListener("click", () => stopDialog.close());

  stopConfirm.addEventListener("click", async () => {
    stopConfirm.disabled = true;
    busy(stopStatus, "Stopping…");
    try {
      const res = await window.api("/bot/stop", {});
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        // The panel is served by the process that is now exiting, so there is nowhere
        // to navigate to. Say what happened and how to get back.
        stopDialog.close();
        say(
          botStatus,
          "Bot stopping. This panel is going down with it — redeploy from Railway to " +
            "bring it back.",
          false,
        );
        byId("stopBtn").disabled = true;
        byId("infoBtn").disabled = true;
      } else {
        say(stopStatus, data.error || "Shutdown failed.", toneFor(res));
        stopConfirm.disabled = false;
      }
    } catch (_) {
      say(stopStatus, "Network error — try again.", true);
      stopConfirm.disabled = false;
    }
  });
});
