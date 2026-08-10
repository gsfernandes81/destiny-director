// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// The "Custom one-off post" page (custom_post.html): pick a channel, mint a draft, go to
// the builder. Two fetches and one navigation — everything else on this page is the four
// states that surround them (loading, empty, refused, pending).
//
// Widgets: Tom Select (vendored, window.TomSelect, no build step), the same picker the
// settings page's channel fields use. say/busy come from shared.js, loaded first.
//
// The one rule this page does NOT share with those fields: announcement channels sort
// first here, but a plain text channel stays selectable. The feed pickers are strict
// because a feed posted to a text channel cannot be followed by another server at all —
// the bot would post happily and every follower would receive nothing. A one-off is sent
// directly to the channel and nothing follows it, so the distinction is a preference, not
// a constraint, and it is expressed by ORDER rather than by omission. The server agrees:
// POST /cv2-builder/new vets with announce_only off. Getting this backwards in either
// direction would be silent — a strict picker here would hide usable channels, and a
// loose picker on a feed would break followers — so both sides say why.

"use strict";

(function () {
  var ANNOUNCE_GROUP = "announce";
  var TEXT_GROUP = "text";

  // Declared in display order, and Tom Select is told to keep it (lockOptgroupOrder), so
  // announcement-first survives a search rather than holding only for the unfiltered
  // list — which is when it would matter least.
  var OPTGROUPS = [
    { value: ANNOUNCE_GROUP, label: "Announcement channels" },
    { value: TEXT_GROUP, label: "Text channels" },
  ];

  /**
   * The picker's options, from GET /autopost_settings/channels' payload.
   *
   * Scoped to the feed guild(s) and NOT the control server, matching the guild set the
   * mint route vets against (autopost_settings.allowed_guild_ids(), whose default
   * excludes the control server). A picker that offered more than the server accepts
   * would turn a legal-looking choice into a refusal one step later.
   *
   * Sorted on (!announce, name) — the whole ordering rule in one comparison. The group
   * lands on each option as `group`, which the widget reads via optgroupField; sorting
   * here as well as grouping there is what makes the order hold within a group too.
   */
  function channelOptions(data) {
    var payload = data || {};
    var channels = payload.channels || [];
    var feedGuilds = (payload.feedGuildIds || []).map(String);
    return channels
      .filter(function (c) {
        return feedGuilds.indexOf(String(c.guildId)) !== -1;
      })
      .map(function (c) {
        return {
          value: String(c.id),
          text: String(c.name),
          group: c.announce ? ANNOUNCE_GROUP : TEXT_GROUP,
        };
      })
      .sort(function (a, b) {
        var announceFirst =
          (a.group === ANNOUNCE_GROUP ? 0 : 1) - (b.group === ANNOUNCE_GROUP ? 0 : 1);
        return announceFirst || a.text.localeCompare(b.text);
      });
  }

  // Exported for `make test-js` (node --test) — the ordering is a rule worth pinning, and
  // it is pure data in and data out. Also on window, for symmetry with the other page
  // scripts; nothing else reads it.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { channelOptions: channelOptions, OPTGROUPS: OPTGROUPS };
  }
  if (typeof window !== "undefined") window.channelOptions = channelOptions;

  if (typeof document === "undefined") return;

  document.addEventListener("DOMContentLoaded", function () {
    var byId = function (id) {
      return document.getElementById(id);
    };

    var select = byId("channel");
    var go = byId("go");
    var problem = byId("problem");
    var status = byId("status");
    if (!select || !go) return; // not this page (the script is only linked from one)

    /** Show a reason, or clear it. Fill first, then reveal: the shared `.appear` rule
     *  animates arrival into an empty slot and must never animate a swap. */
    function setProblem(message) {
      if (!message) {
        problem.classList.add("hidden");
        return;
      }
      problem.textContent = message;
      problem.classList.remove("hidden");
    }

    /** The server's own sentence off a failed fetch, or a fallback. Every JSON route
     *  here answers `{"error": …}` (see web.bot_not_ready_response), and those sentences
     *  are written to be shown — check_channel's especially, which names the missing
     *  permissions. Flattening them to "failed" throws away the only actionable part. */
    async function errorFrom(res, fallback) {
      try {
        var data = await res.json();
        if (data && data.error) return String(data.error);
      } catch (_) {
        /* not JSON — fall through */
      }
      return fallback;
    }

    // --- the picker -------------------------------------------------------------------

    var picker = null;

    async function loadChannels() {
      var data = null;
      var failure = null;
      try {
        var res = await fetch("/autopost_settings/channels", {
          credentials: "same-origin",
        });
        if (res.ok) {
          data = await res.json();
        } else {
          failure = await errorFrom(res, "Couldn't load the channel list.");
        }
      } catch (_) {
        failure = "Couldn't reach the bot to load the channel list.";
      }

      var options = channelOptions(data);
      // Empty and failed are the same dead end for whoever is looking at it, and the
      // most likely cause of an empty list is the same as the most likely cause of a
      // failed fetch, so they get one line. The server's sentence wins when there is one.
      if (!options.length) {
        setProblem(
          failure ||
            "No channels to post to. The bot may still be starting — reload in a moment.",
        );
      }

      picker = new TomSelect(select, {
        options: options,
        optgroups: OPTGROUPS,
        optgroupField: "group",
        // Announcement first, always — not re-sorted by search relevance.
        lockOptgroupOrder: true,
        maxOptions: 200,
        placeholder: options.length ? "Search channels…" : "No channels available",
        onChange: function (value) {
          go.disabled = !value;
          // A fresh choice clears the last refusal: the sentence was about the channel
          // that is no longer selected, and leaving it up would read as being about
          // this one.
          setProblem("");
        },
      });
      if (!options.length) picker.disable();
    }

    // --- mint and go ------------------------------------------------------------------

    go.addEventListener("click", async function () {
      var channelId = picker ? picker.getValue() : "";
      if (!channelId) return;

      // Disabled + spinner in the same tick as the click. A pending indicator that
      // arrives late (or fades in) is worse than none: the gap is exactly when someone
      // clicks again.
      go.disabled = true;
      if (picker) picker.disable();
      setProblem("");
      busy(status, "Setting up…");

      try {
        var res = await window.api("/cv2-builder/new", { channel_id: channelId });
        if (res.ok) {
          var data = await res.json();
          if (data && data.path) {
            // A relative path from the server, which is why this is a navigation and not
            // a followed redirect — see _handle_new. The page is being replaced, so the
            // spinner stays up until it is.
            window.location.assign(data.path);
            return;
          }
        }
        // The refusal IS the payload — check_channel writes a sentence naming what is
        // wrong ("the bot is missing permissions there: Embed Links."). Show it as-is.
        setProblem(await errorFrom(res, "Couldn't start a post in that channel."));
      } catch (_) {
        setProblem("Network error — try again.");
      }
      say(status, "");
      go.disabled = false;
      if (picker) picker.enable();
    });

    loadChannels();
  });
})();
