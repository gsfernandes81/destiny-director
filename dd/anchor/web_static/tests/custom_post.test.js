// Copyright © 2019-present gsfernandes81
//
// This file is part of "dd" henceforth referred to as "destiny-director".
// Licensed under the GNU AGPL v3 or later; see the project LICENSE.

// The one-off post page's channel ordering (custom_post.js's channelOptions).
//
// Worth pinning because it differs from every other channel picker in this app on
// purpose, and both directions of getting it wrong are silent. If announcement-only
// filtering leaked in here, usable channels would simply be missing from a list nobody
// can check against anything. If the sort were dropped, the rule would still "work" on
// a list that happened to be in the right order already — which is most small servers.
//
// Run with `make test-js` (node --test); no browser, no bundler, no DOM (the page script
// returns early when `document` is undefined, exporting only the pure part).

const test = require("node:test");
const assert = require("node:assert/strict");

const { channelOptions, OPTGROUPS } = require("../custom_post.js");

const FEED = "1000";
const OTHER = "2000";

const chan = (id, name, announce, guildId) => ({
  id: id,
  name: name,
  announce: !!announce,
  guildId: guildId || FEED,
});

const payload = (channels, feedGuildIds) => ({
  channels: channels,
  feedGuildIds: feedGuildIds || [FEED],
  controlGuildId: OTHER,
});

const names = (options) => options.map((o) => o.text);

test("announcement channels sort before text channels", () => {
  const options = channelOptions(
    payload([
      chan("1", "#aaa-text", false),
      chan("2", "#zzz-news", true),
      chan("3", "#bbb-text", false),
      chan("4", "#mmm-news", true),
    ]),
  );

  assert.deepEqual(names(options), [
    "#mmm-news",
    "#zzz-news",
    "#aaa-text",
    "#bbb-text",
  ]);
});

test("text channels stay selectable", () => {
  // The rule this page exists to bend. The feed pickers drop non-announcement channels
  // entirely, because a feed posted to a text channel cannot be followed at all; a
  // one-off is sent directly, so the same filter here would only hide working channels.
  const options = channelOptions(payload([chan("1", "#general", false)]));

  assert.equal(options.length, 1);
  assert.equal(options[0].value, "1");
});

test("each option carries the group the widget renders it under", () => {
  const options = channelOptions(
    payload([chan("1", "#general", false), chan("2", "#news", true)]),
  );
  const declared = OPTGROUPS.map((g) => g.value);

  assert.deepEqual(
    options.map((o) => o.group),
    [declared[0], declared[1]],
    "the announcement group must be the first one declared, or the widget's " +
      "lockOptgroupOrder pins the wrong one to the top",
  );
});

test("channels outside the feed guild(s) are not offered", () => {
  // Must match the guild set the mint route vets against
  // (autopost_settings.allowed_guild_ids(), whose default excludes the control server).
  // Offering more than the server will accept turns a legal-looking pick into a
  // refusal one step later.
  const options = channelOptions(
    payload([chan("1", "#feed-news", true, FEED), chan("2", "#control", true, OTHER)]),
  );

  assert.deepEqual(names(options), ["#feed-news"]);
});

test("every feed guild is offered, not just the first", () => {
  // TEST_ENV may name several servers, which is why the payload carries a list.
  const second = "3000";
  const options = channelOptions(
    payload(
      [chan("1", "#a", true, FEED), chan("2", "#b", true, second)],
      [FEED, second],
    ),
  );

  assert.deepEqual(names(options), ["#a", "#b"]);
});

test("an empty or unusable payload yields no options rather than throwing", () => {
  // The page reads this straight off a fetch that may have failed; a throw here would
  // leave the picker un-built and the button disabled forever, with no reason shown.
  for (const data of [null, undefined, {}, { channels: [] }, { channels: [chan("1", "#x", true)] }]) {
    assert.ok(Array.isArray(channelOptions(data)));
  }
  // The last case above has channels but no feedGuildIds — nothing is in scope.
  assert.deepEqual(channelOptions({ channels: [chan("1", "#x", true)] }), []);
});
