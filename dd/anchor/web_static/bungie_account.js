// Bungie account page — link status plus an on-demand account-numbers lookup.
// Served by dd.anchor.extensions.bungie_account. The shell is static (CSP is
// script-src 'self'), so status comes from GET /bungie/data on load.

(() => {
  const byId = (id) => document.getElementById(id);
  // say/busy/api are globals from shared.js (loaded first, deferred).
  const dot = byId("dot");
  const state = byId("state");
  const fetchBtn = byId("fetchBtn");
  const numbersStatus = byId("numbersStatus");
  const numbers = byId("numbers");

  async function loadStatus() {
    try {
      const res = await fetch("/bungie/data");
      if (!res.ok) throw new Error("status unavailable");
      const data = await res.json();

      if (!data.linked) {
        dot.className = "dot bad";
        state.textContent = "Not linked — log in to enable the vendor-backed feeds.";
      } else if (data.expired) {
        dot.className = "dot bad";
        state.textContent = "Link expired — log in again.";
      } else {
        dot.className = "dot ok";
        state.textContent = "Linked.";
      }
      // Deliberately not rendered: an expiry date is not something anyone should have
      // to read or reason about. It hides in the hover title for troubleshooting, and
      // lives in /bungie/data for anyone poking at the API.
      state.title = data.expires ? "Token expiry on record: " + data.expires : "";
    } catch (_) {
      dot.className = "dot bad";
      state.textContent = "Could not read link status.";
    }
  }

  fetchBtn.addEventListener("click", async () => {
    fetchBtn.disabled = true;
    busy(numbersStatus, "Revealing…");
    numbers.textContent = "";
    try {
      const res = await fetch("/bungie/account");
      const data = await res.json();
      if (data.error) {
        say(numbersStatus, data.error, true);
      } else {
        // textContent, not innerHTML — these are ids from a remote API.
        numbers.textContent =
          "Destiny Character ID:   " + data.characterId + "\n" +
          "Destiny Membership ID:  " + data.membershipId + "\n" +
          "Destiny Membership Type:" + " " + data.membershipType;
        say(numbersStatus, "", false);
      }
    } catch (_) {
      say(numbersStatus, "Network error — try again.", true);
    } finally {
      fetchBtn.disabled = false;
    }
  });

  const logoutDialog = byId("logoutDialog");
  byId("logoutBtn").addEventListener("click", () => logoutDialog.showModal());
  byId("logoutCancel").addEventListener("click", () => logoutDialog.close());
  byId("logoutConfirm").addEventListener("click", async () => {
    const confirmBtn = byId("logoutConfirm");
    confirmBtn.disabled = true;
    try {
      const res = await window.api("/bungie/logout", {});
      logoutDialog.close();
      if (res.ok) {
        // Re-read rather than assuming: the status line is driven by /bungie/data
        // everywhere else, and this keeps one source of truth for it.
        numbers.textContent = "";
        say(numbersStatus, "", false);
        await loadStatus();
      } else {
        say(numbersStatus, "Log out failed.", true);
      }
    } catch (_) {
      logoutDialog.close();
      say(numbersStatus, "Network error — try again.", true);
    } finally {
      confirmBtn.disabled = false;
    }
  });

  loadStatus();
})();
