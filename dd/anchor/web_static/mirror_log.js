// Copyright © 2019-present gsfernandes81 — AGPL-3.0-or-later (see repo LICENSE).
//
// Mirror-log page client. Fetches GET /mirror-logs/data (recent runs + overview) and,
// per expanded run, GET /mirror-logs/data?src=<id> (that run's stats + version list),
// rendering the overview (KPIs, health bar, per-day chart via the shared DDCharts
// engine), the run list with per-run progress bars, and the expandable run detail
// (progress-card stats + the message render/diff). While any run is still in progress it
// re-polls every few seconds. No live Discord message is involved — a stateless ledger read.

"use strict";

(function () {
  const POLL_MS = 5000;

  const els = {
    loading: document.getElementById("loading"),
    error: document.getElementById("error"),
    empty: document.getElementById("empty"),
    noMatches: document.getElementById("noMatches"),
    filterBar: document.getElementById("filterBar"),
    srcFilter: document.getElementById("srcFilter"),
    table: document.getElementById("runsTable"),
    tbody: document.querySelector("#runsTable tbody"),
    windowDays: document.getElementById("windowDays"),
    overview: document.getElementById("overview"),
    overviewStats: document.getElementById("overviewStats"),
    overviewBar: document.getElementById("overviewBar"),
    overviewFails: document.getElementById("overviewFails"),
    overviewChart: document.getElementById("overviewChart"),
  };

  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const expanded = new Set(); // src_msg_ids whose detail panel is open
  let pollToken = 0; // bumped to cancel an in-flight poll chain
  let selectedSrc = ""; // "" = all; else a src_ch_id string

  const DISCORD = "https://discord.com/channels";

  async function fetchJSON(url) {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  // The shared escaper — this page loads cv2_model.js for the renderer anyway, and its
  // own copy had already drifted to a different spelling of the apostrophe. Nullish
  // collapses to "" here rather than to the string "null", which the optional fields
  // below rely on.
  const esc = (s) => window.CV2Model.esc(s ?? "");

  function statusOf(run) {
    if (run.pending > 0) return { cls: "progress", label: "In progress" };
    if (run.failed > 0 && run.delivered > 0)
      return { cls: "partial", label: "Partial" };
    if (run.failed > 0) return { cls: "failed", label: "Failed" };
    if (run.cancelled > 0 && run.delivered === 0)
      return { cls: "cancelled", label: "Cancelled" };
    return { cls: "clean", label: "Clean" };
  }

  function relTime(iso) {
    if (!iso) return "—";
    const then = new Date(iso).getTime();
    const secs = Math.max(0, (Date.now() - then) / 1000);
    if (secs < 45) return "just now";
    const mins = secs / 60;
    if (mins < 60) return `${Math.round(mins)}m ago`;
    const hrs = mins / 60;
    if (hrs < 24) return `${Math.round(hrs)}h ago`;
    return `${Math.round(hrs / 24)}d ago`;
  }

  function fmtDuration(startIso, endIso) {
    if (!startIso || !endIso) return "";
    const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
    if (ms < 0) return "";
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  function countsCell(run) {
    const parts = [
      `<span class="${run.delivered ? "ok" : "zero"}">${run.delivered}/${run.total}</span>`,
    ];
    if (run.failed) parts.push(`<span class="bad">✗${run.failed}</span>`);
    if (run.pending) parts.push(`<span class="pend">…${run.pending}</span>`);
    if (run.cancelled) parts.push(`<span class="zero">⊘${run.cancelled}</span>`);
    return `<span class="counts">${parts.join(" ")}</span>`;
  }

  function crosspostCell(run) {
    if (!run.crosspost_done && !run.crosspost_pending) return "—";
    let out = `<span class="ok">${run.crosspost_done} ✓</span>`;
    if (run.crosspost_pending)
      out += ` <span class="pend">+${run.crosspost_pending}…</span>`;
    return `<span class="counts">${out}</span>`;
  }

  function whenCell(run) {
    const dur = run.pending
      ? "running"
      : fmtDuration(run.started, run.last_at) || "";
    return `${esc(relTime(run.started))}${dur ? ` <span class="dur">· ${esc(dur)}</span>` : ""}`;
  }

  function fmtSecs(secs) {
    if (!isFinite(secs) || secs < 0) return "";
    const s = Math.round(secs);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  // A stacked bar of the run state (delivered green / failed red / cancelled grey),
  // proportional to total; the unfilled track is what's still pending. Mirrors the old
  // Discord progress card's bar. `big` renders the taller detail variant with a % label.
  function progressBar(run, big) {
    const total = run.total || 1;
    const pct = (n) => (100 * n) / total;
    const resolved = run.delivered + run.failed + run.cancelled;
    const resolvedPct = Math.round((resolved / total) * 100);
    const seg = (cls, n) =>
      n > 0 ? `<span class="pseg ${cls}" style="width:${pct(n)}%"></span>` : "";
    const bar =
      `<div class="pbar${big ? " big" : ""}" role="img" ` +
      `aria-label="${resolvedPct}% finished (${run.delivered} delivered, ` +
      `${run.failed} failed, ${run.pending || 0} still going)">` +
      seg("done", run.delivered) +
      seg("fail", run.failed) +
      seg("cancel", run.cancelled) +
      `</div>`;
    return big
      ? `<div class="pbar-row">${bar}` +
        // Labelled, not a bare percentage. This measures how much of the run has
        // FINISHED; the Success tile a line above measures how much of what finished
        // succeeded. Two different numbers, and unlabelled they read as one number
        // contradicting itself ("100%" under "90% Success").
        `<span class="pbar-pct">${resolvedPct}% finished</span></div>`
      : bar;
  }

  // Rate + ETA off resolved (excluding cancels) over elapsed time — the old card's
  // throughput line. ETA only while work remains; returns "" when not yet meaningful.
  function throughputLine(run) {
    const resolved = run.delivered + run.failed; // throughput_resolved
    const start = run.started ? new Date(run.started).getTime() : 0;
    const end = run.pending
      ? Date.now()
      : run.last_at
        ? new Date(run.last_at).getTime()
        : 0;
    const secs = (end - start) / 1000;
    if (!start || secs <= 0 || resolved === 0) return "";
    const rate = resolved / secs;
    const remaining = run.total - (run.delivered + run.failed + run.cancelled);
    let out = `${rate.toFixed(1)} ch/s`;
    if (remaining > 0) out += ` · ETA ~${fmtSecs(remaining / rate)}`;
    return out;
  }

  // The message's current/aggregate state — a health bar + count tiles, and (while a run
  // is still in flight, before it has an operation-log row) its live duration + ETA.
  // Per-operation timing / throughput / failures now live on the operation columns.
  function renderRunStats(run) {
    const remaining = run.total - (run.delivered + run.failed + run.cancelled);
    const tile = (label, value, cls) =>
      `<div class="stat-tile"><div class="stat-val ${cls || ""}">${value}</div>` +
      `<div class="stat-label">${label}</div></div>`;
    const tiles = [
      tile("Delivered", run.delivered, run.delivered ? "ok" : ""),
      tile("Failed", run.failed, run.failed ? "bad" : ""),
      tile("Remaining", remaining, remaining ? "pend" : ""),
      tile("Cancelled", run.cancelled, run.cancelled ? "muted" : ""),
      tile(
        "Pushed out",
        `${run.crosspost_done}${run.crosspost_pending ? `+${run.crosspost_pending}…` : ""}`,
      ),
      tile("Attempts", run.max_attempts),
      tile("Version", `v${run.version}`),
    ].join("");

    let live = "";
    if (run.pending) {
      const dur = fmtSecs((Date.now() - new Date(run.started).getTime()) / 1000);
      const tp = throughputLine(run);
      live =
        `<div class="stat-meta">⏳ ${esc(dur)} in flight` +
        `${tp ? ` · ⚡ ${esc(tp)}` : ""}</div>`;
    }

    return (
      `<div class="run-stats">` +
      progressBar(run, true) +
      `<div class="stat-tiles">${tiles}</div>${live}</div>`
    );
  }

  // The overview card: aggregate KPIs + a health bar over the shown runs, and a per-day
  // "channels delivered" bar chart (reusing the shared DDCharts engine, as /stats does).
  function sumRuns(runs) {
    const acc = {
      total: 0,
      delivered: 0,
      failed: 0,
      pending: 0,
      cancelled: 0,
      crosspost_done: 0,
    };
    for (const r of runs) {
      acc.total += r.total;
      acc.delivered += r.delivered;
      acc.failed += r.failed;
      acc.pending += r.pending;
      acc.cancelled += r.cancelled;
      acc.crosspost_done += r.crosspost_done;
    }
    return acc;
  }

  let lastShown = [];
  let lastOps = []; // all recorded operations in the window (from /mirror-logs/data)
  const opsByMsg = new Map(); // src_msg_id -> [op, …], rebuilt each load

  // A compact op-chip strip for a run row: one chip per recorded operation (+ create,
  // ~ update, × delete), so a failed edit/delete is spottable without expanding.
  function opChips(run) {
    const ops = opsByMsg.get(run.src_msg_id) || [];
    if (!ops.length) return "";
    const sym = { create: "+", update: "~", delete: "×" };
    const chips = ops
      .map((o) => {
        const v =
          o.op_type !== "delete" && o.version != null ? `v${esc(o.version)}` : "";
        const fail = o.failed ? `<span class="bad"> ✗${o.failed}</span>` : "";
        return `<span class="opchip ${o.op_type}">${sym[o.op_type] || "•"}${v}${fail}</span>`;
      })
      .join("");
    return `<div class="opchips">${chips}</div>`;
  }

  // Ops for the shown messages only (the overview + list track the current filter).
  function shownOps(runs) {
    const ids = new Set(runs.map((r) => r.src_msg_id));
    return lastOps.filter((o) => ids.has(o.src_msg_id));
  }

  function renderOverview(runs) {
    lastShown = runs;
    if (!runs.length) {
      els.overview.classList.add("hidden");
      return;
    }
    const s = sumRuns(runs);
    const ops = shownOps(runs);
    const resolvedTP = s.delivered + s.failed;
    const successPct = resolvedTP ? Math.round((s.delivered / resolvedTP) * 100) : 100;
    const tile = (label, value, cls) =>
      `<div class="stat-tile"><div class="stat-val ${cls || ""}">${value}</div>` +
      `<div class="stat-label">${label}</div></div>`;
    els.overviewStats.innerHTML = [
      tile("Messages", runs.length),
      tile("Changes", ops.length),
      tile("Delivered", s.delivered, "ok"),
      tile("Failed", s.failed, s.failed ? "bad" : ""),
      tile("Success", successPct + "%", s.failed ? "" : "ok"),
      tile("Pushed out", s.crosspost_done),
    ].join("");
    els.overviewBar.innerHTML = progressBar(s, true);
    // A per-op-type failure line, only when there are failures (op-type breakdown earns
    // pixels exactly when something's wrong — send/edit/delete failures differ in cause).
    els.overviewFails.innerHTML = failsByOpLine(ops);
    // Unhide before drawing so the chart reads a real container width (it sizes to
    // clientWidth; a display:none container measures 0).
    els.overview.classList.remove("hidden");
    renderOverviewChart(runs);
  }

  function failsByOpLine(ops) {
    const by = { create: 0, update: 0, delete: 0 };
    for (const o of ops) if (o.op_type in by) by[o.op_type] += o.failed || 0;
    if (!by.create && !by.update && !by.delete) return "";
    const parts = ["create", "update", "delete"]
      .filter((tp) => by[tp])
      .map((tp) => `${by[tp]} ${OP_LABEL[tp].toLowerCase()}`);
    return `failures: ${esc(parts.join(" · "))}`;
  }

  // Channels delivered per day, split into create / update / delete series — an edit
  // storm and a posting spike look identical collapsed, distinct here. Reuses DDCharts.
  function renderOverviewChart(runs) {
    if (!window.DDCharts || !els.overviewChart) return;
    const ops = shownOps(runs);
    const byType = { create: new Map(), update: new Map(), delete: new Map() };
    for (const o of ops) {
      if (!o.finished_at || !(o.op_type in byType)) continue;
      const d = new Date(o.finished_at);
      d.setHours(0, 0, 0, 0);
      const m = byType[o.op_type];
      m.set(d.getTime(), (m.get(d.getTime()) || 0) + o.delivered);
    }
    const colors = {
      create: cssVar("--accent-strong"),
      update: cssVar("--accent"),
      delete: cssVar("--text-muted"),
    };
    const series = ["create", "update", "delete"]
      .filter((tp) => byType[tp].size)
      .map((tp) => ({
        name: OP_LABEL[tp],
        color: colors[tp],
        points: [...byType[tp].entries()]
          .sort((a, b) => a[0] - b[0])
          .map(([k, v]) => [new Date(k), v]),
      }));
    window.DDCharts.lineChart(els.overviewChart, {
      resolution: "daily",
      series,
      height: 200,
    });
  }

  // A "Jump to source ↗" button for the mirrored message, when we know its source guild
  // (from the latest captured snapshot). Empty for sources predating the capture deploy.
  function sourceButton(run) {
    if (!run.src_guild_id) return "";
    const href = `${DISCORD}/${run.src_guild_id}/${run.src_ch_id}/${run.src_msg_id}`;
    return (
      `<a class="jump-source" href="${esc(href)}" target="_blank" rel="noopener">` +
      `Open the original ↗</a>`
    );
  }

  // The three things that can happen to a mirrored post, named the way the chart
  // caption already named them. One map, so the chart legend, the op chips and the
  // failure line cannot drift from each other or from the caption again.
  const OP_LABEL = { create: "Posted", update: "Edited", delete: "Removed" };

  function opDurationSecs(op) {
    const s = op.started_at ? new Date(op.started_at).getTime() : 0;
    const f = op.finished_at ? new Date(op.finished_at).getTime() : 0;
    return s && f ? Math.max(0, (f - s) / 1000) : 0;
  }

  // One operation's compact stat header for a version/delete column: a slim progress
  // bar + counts + time/throughput, expandable to its own failure breakdown. When we
  // have no recorded op for this column (pre-deploy history), say so honestly rather
  // than borrowing another op's numbers.
  function opStatHeader(op) {
    if (!op) return `<div class="op-stat none">counts not recorded</div>`;
    const secs = opDurationSecs(op);
    const rate = secs > 0 ? (op.delivered + op.failed) / secs : 0;
    const counts =
      `<span class="ok">${op.delivered} ok</span>` +
      (op.failed ? ` · <span class="bad">${op.failed} fail</span>` : "") +
      (op.cancelled ? ` · <span class="muted">${op.cancelled} ⊘</span>` : "");
    const tp = `${fmtSecs(secs)}${rate ? ` · ${rate.toFixed(1)} ch/s` : ""}`;
    const fails = op.failures || [];
    const expand = fails.length
      ? `<button type="button" class="op-expand" title="Show failures">▸ ${fails.length}</button>`
      : "";
    const breakdown = fails.length
      ? `<ul class="op-fails hidden">` +
        fails
          .map(
            (f) =>
              `<li><code>${esc(f.ref || "—")}</code> ×${f.count}` +
              (f.error_class
                ? ` <span class="muted">(${esc(f.error_class.toLowerCase())})</span>`
                : "") +
              (f.sample ? ` — ${esc(f.sample)}` : "") +
              `</li>`,
          )
          .join("") +
        `</ul>`
      : "";
    return (
      `<div class="op-stat">${progressBar(op)}` +
      `<div class="op-line"><span>${counts}</span>` +
      `<span class="op-tp">${tp}</span>${expand}</div>${breakdown}</div>`
    );
  }

  // The expandable detail's message view: every operation as its own column in a
  // horizontally-scrollable row (no vertical scroll), oldest→newest. Create/update
  // columns carry the captured version render (with a per-op stat header); a delete is a
  // dashed tombstone column (real stats, no content snapshot). A "highlight changes vs
  // previous" toggle re-renders every v2+ render as an inline diff. Plus jump-to-source.
  function renderVersionColumns(data, run) {
    const vs = data.versions || [];
    const ops = data.operations || [];
    const opByVersion = new Map(); // version -> its create/update op-event
    const deletes = [];
    for (const op of ops) {
      if (op.op_type === "delete") deletes.push(op);
      else if (op.version != null && !opByVersion.has(op.version))
        opByVersion.set(op.version, op);
    }
    const jump = sourceButton(run);
    if (!vs.length && !deletes.length) {
      return (
        `<div class="versions"><div class="version-head">` +
        `<span class="version-label">Message</span>${jump}</div>` +
        `<p class="detail-loading">No version snapshots for this source yet — ` +
        `capture began at deploy, so older runs have none.</p></div>`
      );
    }
    const control =
      vs.length > 1
        ? `<label class="diff-toggle"><input type="checkbox" class="diff-check" /> ` +
          `Highlight changes vs previous</label>`
        : vs.length === 1
          ? `<span class="version-hint">one version so far — edits are captured as ` +
            `new versions and shown as diffs</span>`
          : "";
    const cols = [];
    vs.forEach((v, i) => {
      const op = opByVersion.get(v.version);
      const opType = op ? op.op_type : i === 0 ? "create" : "update";
      const abs = v.captured_at ? new Date(v.captured_at).toLocaleString() : "";
      cols.push(
        `<div class="vcol" data-idx="${i}">` +
          `<div class="vcol-head"><span class="op-tag ${opType}">` +
          `${OP_LABEL[opType] || "Update"}</span>` +
          `<span class="vcol-ver">v${esc(v.version)}</span>` +
          `<span class="vcol-time" title="${esc(abs)}">${esc(relTime(v.captured_at))}</span>` +
          `</div>${opStatHeader(op)}` +
          // `cv2-preview` opts this pane into the shared CV2 render styling
          // (cv2_preview.css), which the builder canvas also uses. The tombstone
          // column below carries no render, so it does not need the class.
          `<div class="vcol-body cv2-preview"><p class="detail-loading">Loading…</p></div></div>`,
      );
    });
    deletes.forEach((d) => {
      const abs = d.finished_at ? new Date(d.finished_at).toLocaleString() : "";
      const ver = d.version != null ? ` v${esc(d.version)}` : "";
      cols.push(
        `<div class="vcol vcol-delete">` +
          `<div class="vcol-head"><span class="op-tag delete">${OP_LABEL.delete}</span>` +
          `<span class="vcol-time" title="${esc(abs)}">${esc(relTime(d.finished_at))}</span>` +
          `</div>${opStatHeader(d)}` +
          `<div class="vcol-body tombstone">The original was deleted — removed${ver} from ` +
          `${d.delivered} channel${d.delivered === 1 ? "" : "s"}. No content snapshot.` +
          `</div></div>`,
      );
    });
    return (
      `<div class="versions">` +
      `<div class="version-head"><span class="version-label">Operations</span>` +
      control +
      jump +
      `</div><div class="vcols">${cols.join("")}</div></div>`
    );
  }

  // Fetch each version column's render (or its diff-vs-previous when the toggle is on)
  // and wire the per-op failure expanders. Delete columns have no render to fetch. The
  // server returns pre-escaped safe HTML (cv2_render) → innerHTML; an error body is
  // untrusted → textContent. Each column carries a token so a toggle mid-fetch can't
  // land a stale render.
  function setupVersionColumns(srcId, container, versions) {
    const cols = [...container.querySelectorAll(".vcol[data-idx]")];
    const diffCheck = container.querySelector(".diff-check");
    const tokens = new WeakMap();

    async function renderCol(col) {
      const idx = Number(col.dataset.idx);
      const v = versions[idx];
      const body = col.querySelector(".vcol-body");
      const diffOn = !!diffCheck && diffCheck.checked && idx > 0;
      let url = `/mirror-logs/render?src=${encodeURIComponent(srcId)}&v=${encodeURIComponent(v.version)}`;
      if (diffOn) url += `&diff=${encodeURIComponent(versions[idx - 1].version)}`;
      const token = (tokens.get(col) || 0) + 1;
      tokens.set(col, token);
      body.innerHTML = `<p class="detail-loading">Loading…</p>`;
      try {
        const res = await fetch(url, { credentials: "same-origin" });
        if (!res.ok) {
          const text = await res.text();
          if (tokens.get(col) === token) body.textContent = `Render failed: ${text}`;
          return;
        }
        const data = await res.json();
        if (tokens.get(col) !== token) return; // superseded
        if (data.kind === "snapshot") {
          // A captured message from SOMEONE ELSE'S server — the untrusted sink. The
          // shared renderer builds real DOM: text lands via textContent, URLs are
          // http(s)-checked where they become attributes, and only renderMd output
          // reaches innerHTML.
          window.CV2Render.render(
            body,
            window.CV2Render.snapshotSpec(data.payload, data.message_kind),
            {},
          );
        } else {
          // A diff of the same, from the annotated tree the server aligned. Every run
          // is pre-split there, so nothing is diffed in the browser — the client only
          // draws, which keeps the trust story the same as a plain render.
          window.CV2Render.render(body, window.CV2Render.diffSpec(data.diff), {});
        }
      } catch (e) {
        if (tokens.get(col) === token) body.textContent = `Render error: ${e}`;
      }
    }

    cols.forEach(renderCol);
    if (diffCheck)
      diffCheck.addEventListener("change", () => cols.forEach(renderCol));

    container.querySelectorAll(".op-expand").forEach((btn) => {
      btn.addEventListener("click", () => {
        const fails = btn.closest(".op-stat").querySelector(".op-fails");
        if (!fails) return;
        const hidden = fails.classList.toggle("hidden");
        btn.textContent = `${hidden ? "▸" : "▾"} ${fails.children.length}`;
      });
    });
  }

  async function loadDetail(run, container) {
    container.innerHTML = `<p class="detail-loading">Loading message…</p>`;
    try {
      const data = await fetchJSON(
        `/mirror-logs/data?src=${encodeURIComponent(run.src_msg_id)}`,
      );
      container.innerHTML =
        renderRunStats(run) + renderVersionColumns(data, run);
      setupVersionColumns(run.src_msg_id, container, data.versions || []);
    } catch (e) {
      container.innerHTML = `<p class="detail-error">Failed to load detail: ${esc(e.message)}</p>`;
    }
  }

  // Source column: the channel name links to the source *channel*, and a separate
  // "See message" link opens the source *message*. Both need the source guild id (from
  // the latest snapshot); without it the channel name is plain text and no message link.
  function sourceCell(run) {
    const name = run.src_name ? `#${run.src_name}` : `#${run.src_ch_id}`;
    const g = run.src_guild_id;
    const chHref = g ? `${DISCORD}/${g}/${run.src_ch_id}` : null;
    const msgHref = g ? `${DISCORD}/${g}/${run.src_ch_id}/${run.src_msg_id}` : null;
    const channel = chHref
      ? `<a href="${esc(chHref)}" target="_blank" rel="noopener" ` +
        `title="Open source channel">${esc(name)}</a>`
      : esc(name);
    const msgLink = msgHref
      ? `<a class="src-msg-link" href="${esc(msgHref)}" target="_blank" ` +
        `rel="noopener">See message ↗</a>`
      : "";
    return (
      `<div class="src-channel">${channel}</div>` +
      (msgLink ? `<div class="src-msg">${msgLink}</div>` : "") +
      opChips(run)
    );
  }

  function render(runs) {
    els.tbody.replaceChildren();
    const shown = selectedSrc
      ? runs.filter((r) => r.src_ch_id === selectedSrc)
      : runs;
    renderOverview(shown);
    els.noMatches.classList.toggle("hidden", shown.length > 0 || !runs.length);
    for (const run of shown) {
      const st = statusOf(run);
      const tr = document.createElement("tr");
      tr.className = "run" + (expanded.has(run.src_msg_id) ? " open" : "");
      tr.innerHTML =
        `<td><span class="chip ${st.cls}">${esc(st.label)}</span></td>` +
        `<td>${sourceCell(run)}</td>` +
        `<td class="num">${countsCell(run)}${progressBar(run)}</td>` +
        `<td class="num">${crosspostCell(run)}</td>` +
        `<td><span class="when">${whenCell(run)}</span></td>`;
      tr.addEventListener("click", () => toggle(run.src_msg_id));
      els.tbody.appendChild(tr);

      if (expanded.has(run.src_msg_id)) {
        const dr = document.createElement("tr");
        dr.className = "detail-row";
        const td = document.createElement("td");
        td.colSpan = 5;
        const panel = document.createElement("div");
        panel.className = "detail";
        td.appendChild(panel);
        dr.appendChild(td);
        els.tbody.appendChild(dr);
        loadDetail(run, panel);
      }
    }
  }

  function toggle(srcId) {
    if (expanded.has(srcId)) expanded.delete(srcId);
    else expanded.add(srcId);
    if (lastRuns) render(lastRuns);
  }

  let lastRuns = null;

  // Rebuild the source-channel dropdown from the distinct sources in the loaded runs,
  // preserving the current selection (drop it if that source is no longer present).
  function populateFilter(runs) {
    const seen = new Map(); // src_ch_id -> label
    for (const r of runs) {
      if (!seen.has(r.src_ch_id)) {
        seen.set(r.src_ch_id, r.src_name ? `#${r.src_name}` : `#${r.src_ch_id}`);
      }
    }
    if (![...seen.keys()].includes(selectedSrc)) selectedSrc = "";
    const opts = ['<option value="">All source channels</option>'];
    for (const [id, label] of [...seen.entries()].sort((a, b) =>
      a[1].localeCompare(b[1]),
    )) {
      const sel = id === selectedSrc ? " selected" : "";
      opts.push(`<option value="${esc(id)}"${sel}>${esc(label)}</option>`);
    }
    els.srcFilter.innerHTML = opts.join("");
    els.filterBar.classList.toggle("hidden", seen.size < 2);
  }

  els.srcFilter.addEventListener("change", () => {
    selectedSrc = els.srcFilter.value;
    if (lastRuns) render(lastRuns);
  });

  async function load() {
    const token = ++pollToken;
    try {
      const data = await fetchJSON("/mirror-logs/data");
      if (token !== pollToken) return; // superseded by a newer load
      lastRuns = data.runs;
      lastOps = data.operations || [];
      opsByMsg.clear();
      for (const op of lastOps) {
        const list = opsByMsg.get(op.src_msg_id);
        if (list) list.push(op);
        else opsByMsg.set(op.src_msg_id, [op]);
      }
      if (els.windowDays) els.windowDays.textContent = String(data.window_days);
      els.loading.classList.add("hidden");
      els.error.classList.add("hidden");

      if (!data.runs.length) {
        els.empty.classList.remove("hidden");
        els.table.classList.add("hidden");
        els.filterBar.classList.add("hidden");
        els.noMatches.classList.add("hidden");
        els.overview.classList.add("hidden");
      } else {
        els.empty.classList.add("hidden");
        els.table.classList.remove("hidden");
        populateFilter(data.runs);
        render(data.runs);
      }

      // Keep the view live only while something is still in flight.
      const anyPending = data.runs.some((r) => r.pending > 0);
      if (anyPending) setTimeout(() => token === pollToken && load(), POLL_MS);
    } catch (e) {
      if (token !== pollToken) return;
      els.loading.classList.add("hidden");
      els.error.textContent = `Failed to load mirror logs: ${e.message}`;
      els.error.classList.remove("hidden");
    }
  }

  // Charts size to their container width, so re-draw the overview chart on resize
  // (debounced) — matching the /stats page's behaviour.
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (lastShown.length) renderOverviewChart(lastShown);
    }, 150);
  });

  load();
})();
