// Statistics dashboard client. Framework-free vanilla JS, served from /static/stats.js
// and loaded (deferred) after shared.js. Fetches GET /stats/data (JSON, DB-only) and
// renders the dashboard. Time series arrive at daily granularity; weekly/monthly
// re-bucketing and the inline-SVG charts are layered in by a later chunk. For now this
// renders the leaderboard / current-totals / populations / server tables so the page is
// fully functional.
//
// The payload shape (see dd/anchor/extensions/stats_page.py::_collect_data):
//   commands:     [[name, "YYYY-MM-DD", count], ...]
//   autoposts:    [["YYYY-MM-DD", feed, kind, count], ...]   kind: "follow" | "mirror"
//   current:      [{feed, name, follows, mirrors}, ...]  name: the feed's display name
//   populations:  [[id, population], ...]                    id is a string (snowflake)

const _byId = (id) => document.getElementById(id);

// Small DOM helper: a <tr> from an array of cell specs. A spec is a string/number (plain
// cell) or {text, num:true} for a right-aligned numeric cell.
function _row(cells) {
  const tr = document.createElement("tr");
  for (const spec of cells) {
    const td = document.createElement("td");
    if (spec && typeof spec === "object") {
      td.textContent = spec.text;
      if (spec.num) td.className = "num";
    } else {
      td.textContent = spec;
    }
    tr.appendChild(td);
  }
  return tr;
}

function _fillTable(tableId, rows) {
  const tbody = _byId(tableId).querySelector("tbody");
  tbody.replaceChildren(...rows.map(_row));
}

const _fmt = (n) => Number(n).toLocaleString();
const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Fetched payload + current time-resolution + which command the big chart is focused on
// ("__total__" = all commands summed). Shared by the chart renderers so the resolution
// toggle / row selection can re-render without re-fetching.
const STATE = {
  data: null,
  resolution: "daily",
  selectedCommand: "__total__",
  cmdIndex: null,
  selectedFeed: "__all__", // "__all__" = every feed summed
  autopostIndex: null,
};

// --- section renderers ------------------------------------------------------

// Index the raw command rows into per-command daily series + totals + a summed "total"
// series, computed once per load and reused by the chart, sparklines, and leaderboard.
function commandIndex(commands) {
  const byNameDays = new Map(); // name -> Map(iso -> count)
  const totalDays = new Map(); //  iso  -> count (all commands)
  for (const [name, iso, count] of commands) {
    if (!byNameDays.has(name)) byNameDays.set(name, new Map());
    const m = byNameDays.get(name);
    m.set(iso, (m.get(iso) || 0) + count);
    totalDays.set(iso, (totalDays.get(iso) || 0) + count);
  }
  const toSeries = (m) =>
    [...m.entries()]
      .sort((a, b) => (a[0] < b[0] ? -1 : 1))
      .map(([iso, v]) => [new Date(iso + "T00:00:00Z"), v]);
  const byName = new Map();
  const totals = new Map();
  for (const [name, m] of byNameDays) {
    byName.set(name, toSeries(m));
    totals.set(name, [...m.values()].reduce((a, b) => a + b, 0));
  }
  const order = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([n]) => n);
  return { byName, totals, order, total: toSeries(totalDays) };
}

// The daily series behind a leaderboard key ("__total__" or a command name).
function seriesFor(key) {
  const ix = STATE.cmdIndex;
  return key === "__total__" ? ix.total : ix.byName.get(key) || [];
}

// Neutral up/flat/down arrow from first-half vs second-half usage. Usage rising or
// falling isn't inherently good/bad, so this uses text tokens, not status colours.
function trendArrow(series) {
  if (series.length < 2) return { sym: "→", cls: "flat" };
  const mid = Math.floor(series.length / 2);
  const first = series.slice(0, mid).reduce((s, [, v]) => s + v, 0);
  const second = series.slice(mid).reduce((s, [, v]) => s + v, 0);
  if (second > first * 1.1) return { sym: "▲", cls: "up" };
  if (second < first * 0.9) return { sym: "▼", cls: "down" };
  return { sym: "→", cls: "flat" };
}

function _cmdRow(key, label, total) {
  const tr = document.createElement("tr");
  tr.dataset.cmd = key;
  tr.className = "row-select" + (key === STATE.selectedCommand ? " active" : "");

  const name = document.createElement("td");
  name.textContent = label;
  const tot = document.createElement("td");
  tot.className = "num";
  tot.textContent = _fmt(total);

  const trend = document.createElement("td");
  const t = trendArrow(seriesFor(key));
  trend.className = "trend " + t.cls;
  trend.textContent = t.sym;

  const spark = document.createElement("td");
  spark.className = "spark-cell";
  spark.dataset.spark = key; // filled/refreshed by renderCommandSparklines()

  tr.append(name, tot, trend, spark);
  tr.addEventListener("click", () => selectCommand(key));
  return tr;
}

function renderCommands(commands) {
  STATE.cmdIndex = commandIndex(commands);
  const { order, totals, total } = STATE.cmdIndex;
  const tbody = _byId("commandsTable").querySelector("tbody");
  const rows = [
    _cmdRow("__total__", "All commands", total.reduce((s, [, v]) => s + v, 0)),
    ...order.map((name) => _cmdRow(name, name, totals.get(name))),
  ];
  tbody.replaceChildren(...rows);
  _byId("section-commands").classList.remove("hidden");
}

function selectCommand(key) {
  STATE.selectedCommand = key;
  document.querySelectorAll("#commandsTable .row-select").forEach((tr) =>
    tr.classList.toggle("active", tr.dataset.cmd === key),
  );
  renderCommandsChart();
}

// Index the raw autopost snapshot rows into per-feed follow/mirror/reach daily series
// plus an all-feeds total, computed once per load and reused by the focused chart, the
// per-feed sparklines, and the trend arrows. Reach = followers + mirrors.
function autopostIndex(autoposts) {
  const byFeedDays = new Map(); // feed -> Map(iso -> {follow, mirror})
  const totalDays = new Map(); //  iso  -> {follow, mirror} (all feeds)
  for (const [iso, feed, kind, count] of autoposts) {
    if (!byFeedDays.has(feed)) byFeedDays.set(feed, new Map());
    const fm = byFeedDays.get(feed);
    const g = fm.get(iso) || { follow: 0, mirror: 0 };
    g[kind] = (g[kind] || 0) + count;
    fm.set(iso, g);
    const tg = totalDays.get(iso) || { follow: 0, mirror: 0 };
    tg[kind] = (tg[kind] || 0) + count;
    totalDays.set(iso, tg);
  }
  const toSeries = (m, pick) =>
    [...m.entries()]
      .sort((a, b) => (a[0] < b[0] ? -1 : 1))
      .map(([iso, g]) => [new Date(iso + "T00:00:00Z"), pick(g)]);
  const build = (m) => ({
    follow: toSeries(m, (g) => g.follow),
    mirror: toSeries(m, (g) => g.mirror),
    reach: toSeries(m, (g) => g.follow + g.mirror),
  });
  const byFeed = new Map();
  for (const [feed, m] of byFeedDays) byFeed.set(feed, build(m));
  return { byFeed, total: build(totalDays) };
}

// The reach series ("__all__" total, or a single feed) behind a per-feed row.
function reachSeriesFor(key) {
  const ix = STATE.autopostIndex;
  return key === "__all__" ? ix.total.reach : ix.byFeed.get(key)?.reach || [];
}

// One number per feed, not two. "Followers" and "Mirrors" are the two mechanisms by
// which a post reaches a server — Discord's own channel-follow, and the bot posting a
// copy — and which of the two carried it is the bot's business, not an admin's. The
// question the page answers is "did this land, and is it growing?", so the row leads
// with the total and keeps the split as a quieter second line for whoever wants it (a
// mirror count that suddenly drops is still worth being able to see).
function _feedRow(key, label, follows, mirrors) {
  const tr = document.createElement("tr");
  tr.dataset.feed = key;
  tr.className = "row-select" + (key === STATE.selectedFeed ? " active" : "");

  const name = document.createElement("td");
  name.textContent = label;

  const reach = document.createElement("td");
  reach.className = "num";
  reach.append(
    Object.assign(document.createElement("div"), {
      textContent: _fmt(follows + mirrors),
    }),
    Object.assign(document.createElement("div"), {
      className: "reach-split",
      textContent: `${_fmt(follows)} following · ${_fmt(mirrors)} sent a copy`,
    }),
  );

  const trend = document.createElement("td");
  const t = trendArrow(reachSeriesFor(key));
  trend.className = "trend " + t.cls;
  trend.textContent = t.sym;

  const spark = document.createElement("td");
  spark.className = "spark-cell";
  spark.dataset.spark = key; // filled/refreshed by renderAutopostSparklines()

  tr.append(name, reach, trend, spark);
  tr.addEventListener("click", () => selectFeed(key));
  return tr;
}

function renderAutoposts(current, autoposts) {
  STATE.autopostIndex = autopostIndex(autoposts);
  // Order feeds by current total reach (followers + mirrors), busiest first.
  const feeds = [...current].sort(
    (a, b) => b.follows + b.mirrors - (a.follows + a.mirrors),
  );
  const allFollows = feeds.reduce((s, c) => s + c.follows, 0);
  const allMirrors = feeds.reduce((s, c) => s + c.mirrors, 0);
  const tbody = _byId("currentTable").querySelector("tbody");
  tbody.replaceChildren(
    _feedRow("__all__", "All feeds", allFollows, allMirrors),
    ...feeds.map((c) => _feedRow(c.feed, c.name || c.feed, c.follows, c.mirrors)),
  );
  _byId("section-autoposts").classList.remove("hidden");
}

function selectFeed(key) {
  STATE.selectedFeed = key;
  document.querySelectorAll("#currentTable .row-select").forEach((tr) =>
    tr.classList.toggle("active", tr.dataset.feed === key),
  );
  renderAutopostsChart();
}

function renderPopulations(populations) {
  const pops = populations.map(([, pop]) => pop);
  const total = pops.reduce((a, b) => a + b, 0);
  const count = pops.length;
  const summary = _byId("populationsSummary");
  summary.replaceChildren(
    ..._stats([
      ["Servers", count],
      ["Total population", total],
    ]),
  );
  _byId("section-populations").classList.remove("hidden");
}

function _stats(pairs) {
  return pairs.map(([label, value]) => {
    const wrap = document.createElement("div");
    wrap.className = "stat";
    const v = document.createElement("span");
    v.className = "value";
    v.textContent = _fmt(value);
    const l = document.createElement("span");
    l.className = "label";
    l.textContent = label;
    wrap.append(v, l);
    return wrap;
  });
}

function renderServers(populations) {
  // Keep the full list around so the search box can filter without re-fetching.
  const all = populations
    .map(([id, pop]) => ({ id: String(id), pop }))
    .sort((a, b) => b.pop - a.pop);
  _byId("serversCount").textContent = `(${_fmt(all.length)})`;

  const draw = (rows) =>
    _fillTable(
      "serversTable",
      rows.map((r) => [r.id, { text: _fmt(r.pop), num: true }]),
    );

  draw(all);
  const search = _byId("serverSearch");
  search.addEventListener("input", () => {
    const q = search.value.trim();
    draw(q ? all.filter((r) => r.id.includes(q)) : all);
  });
  _byId("section-servers").classList.remove("hidden");
}

// --- time-series charts -----------------------------------------------------

// Focused line chart for the currently-selected command (or the all-commands total).
function renderCommandsChart() {
  const key = STATE.selectedCommand;
  const label = key === "__total__" ? "All commands (total)" : key;
  _byId("commandsCaption").textContent = label;
  const points = DDCharts.bucketByResolution(
    seriesFor(key),
    STATE.resolution,
    "sum", // command usage is a FLOW — periods add up
  );
  DDCharts.lineChart(_byId("commandsChart"), {
    resolution: STATE.resolution,
    // Single series → on-brand accent, no legend (the caption names it).
    series: [{ name: label, color: cssVar("--accent"), points }],
  });
}

// Fill/refresh every per-command sparkline at the current resolution (small multiples).
function renderCommandSparklines() {
  const color = cssVar("--accent");
  document.querySelectorAll("#commandsTable [data-spark]").forEach((cell) => {
    const points = DDCharts.bucketByResolution(
      seriesFor(cell.dataset.spark),
      STATE.resolution,
      "sum",
    );
    DDCharts.sparkline(cell, points, { color, width: 150, height: 26 });
  });
}

// Focused follow/mirror chart for the currently-selected feed (or all feeds summed).
function renderAutopostsChart() {
  const key = STATE.selectedFeed;
  _byId("autopostsCaption").textContent = key === "__all__" ? "All feeds" : key;
  const ix = STATE.autopostIndex;
  const s = key === "__all__" ? ix.total : ix.byFeed.get(key) || { follow: [], mirror: [] };
  const res = STATE.resolution;
  // Reach is a STOCK (active-channel count), so aggregate by the period's last snapshot.
  DDCharts.lineChart(_byId("autopostsChart"), {
    resolution: res,
    series: [
      { name: "Following", color: cssVar("--accent"), points: DDCharts.bucketByResolution(s.follow, res, "last") },
      { name: "Sent a copy", color: cssVar("--accent-strong"), points: DDCharts.bucketByResolution(s.mirror, res, "last") },
    ],
  });
}

// Fill/refresh every per-feed reach sparkline at the current resolution.
function renderAutopostSparklines() {
  const color = cssVar("--accent");
  document.querySelectorAll("#currentTable [data-spark]").forEach((cell) => {
    const points = DDCharts.bucketByResolution(
      reachSeriesFor(cell.dataset.spark),
      STATE.resolution,
      "last", // reach is a STOCK — take the period's last snapshot
    );
    DDCharts.sparkline(cell, points, { color, width: 150, height: 26 });
  });
}

// Re-render every time-series chart at the current resolution. (Populations is a
// distribution, not a time series, so it is rendered once at load — not here.)
function renderTimeCharts() {
  if (!STATE.data) return;
  renderCommandsChart();
  renderCommandSparklines();
  renderAutopostsChart();
  renderAutopostSparklines();
}

// Server population distribution: count servers per [10^k, 10^(k+1)) band (mirrors the
// old /stats populations log breakdown), rendered as a column chart.
function populationLogBands(populations) {
  const counts = new Map();
  for (const [, pop] of populations) {
    if (pop > 0) {
      const k = Math.floor(Math.log10(pop));
      counts.set(k, (counts.get(k) || 0) + 1);
    }
  }
  const ks = [...counts.keys()];
  if (!ks.length) return [];
  const lo = Math.min(...ks), hi = Math.max(...ks);
  const compact = (n) => (n >= 1e6 ? n / 1e6 + "M" : n >= 1e3 ? n / 1e3 + "K" : String(n));
  const bands = [];
  for (let k = lo; k <= hi; k++) {
    bands.push({ label: `${compact(10 ** k)}–${compact(10 ** (k + 1))}`, value: counts.get(k) || 0 });
  }
  return bands;
}

function renderPopulationsChart() {
  DDCharts.barChart(_byId("populationsChart"), {
    bars: populationLogBands(STATE.data.populations || []),
    color: cssVar("--accent-strong"),
    unit: "servers",
  });
}

function initToolbar() {
  const tb = _byId("toolbar");
  tb.classList.remove("hidden");
  tb.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      STATE.resolution = btn.dataset.res;
      tb.querySelectorAll(".seg-btn").forEach((b) =>
        b.classList.toggle("active", b === btn),
      );
      renderTimeCharts();
    });
  });
}

// --- boot -------------------------------------------------------------------

async function load() {
  try {
    const res = await fetch("/stats/data", { credentials: "same-origin" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    STATE.data = data;

    renderCommands(data.commands || []);
    renderAutoposts(data.current || [], data.autoposts || []);
    renderPopulations(data.populations || []);
    renderServers(data.populations || []);

    initToolbar();
    renderTimeCharts();
    renderPopulationsChart(); // distribution — resolution-independent, render once

    _byId("loading").classList.add("hidden");
  } catch (e) {
    const err = _byId("error");
    err.textContent = "Failed to load statistics: " + e.message;
    err.classList.remove("hidden");
    _byId("loading").classList.add("hidden");
  }
}

document.addEventListener("DOMContentLoaded", load);

// Charts size to their container width, so re-render (debounced) on resize.
let _resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(renderTimeCharts, 150);
});
