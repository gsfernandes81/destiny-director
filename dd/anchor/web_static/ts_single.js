// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// Shared by every page that turns a <select> into a searchable single-item Tom Select
// picker over a fixed option pool (weekly_reset_form.js's tsSingle, autopost_settings.js's
// channel fields): if the field's current value isn't in that pool, inject it as a
// synthetic option first, so a stale/unrecognized value (a deleted channel, a
// renamed/removed weapon) isn't silently dropped from the picker instead of showing as
// whatever it actually still is. Each caller still builds and configures its own
// TomSelect instance — only this one repeated, easy-to-miss step is shared.
//
// No build step — a plain global, same as every other script here.

"use strict";

function tsWithCurrentOption(options, current, label) {
  const cur = current == null ? "" : String(current).trim();
  if (cur && !options.some((o) => String(o.value) === cur)) {
    return [{ value: cur, text: label ? label(cur) : cur }, ...options];
  }
  return options;
}

window.tsWithCurrentOption = tsWithCurrentOption;
