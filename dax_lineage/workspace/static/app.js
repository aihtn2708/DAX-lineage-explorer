// Power BI workspace page. Everything from a .pbix (names, DAX, titles) is put
// into the page as text, never as HTML.
"use strict";

const STATUS = {
  visual: "used in visuals",
  indirect: "used through other measures",
  structural: "needed by the model",
  deadChain: "unused (only unused measures read it)",
  unused: "unused",
};
const STATUS_HELP = {
  visual: "A visual or filter in this report reads it directly.",
  indirect: "No visual reads it, but a measure that reaches a visual does.",
  structural: "Nothing reads it, but it is a relationship key or a sort-by column.",
  deadChain: "Only measures that are themselves unused read it.",
  unused: "Nothing in this model or its report reads it.",
};
const view = document.getElementById("view");
let summaryCache = null;
let lastScanLog = [];

// -- helpers ----------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok && response.status !== 409) throw new Error(data.error || response.statusText);
  return data;
}

const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};
const n = (value) => (value === null || value === undefined ? "" : Number(value).toLocaleString());
const fieldLabel = (o) => (o.kind === "measure" || o.kind === "table" || !o.table_name ? o.name : `${o.table_name}[${o.name}]`);
const badge = (status, text) => h("span", { class: `badge ${status}`, title: STATUS_HELP[status] || "" }, text || STATUS[status] || status);
const kindTag = (kind) => h("span", { class: `kind ${kind}` }, kind);
const objLink = (o) => (o.id ? h("a", { href: `#/object/${o.id}` }, fieldLabel(o)) : h("span", { class: "muted" }, fieldLabel(o)));
const tile = (value, label, alert) => h("div", { class: `tile${alert ? " alert" : ""}` }, h("b", {}, n(value)), h("span", {}, label));
const me = () => { try { return localStorage.getItem("workspace.me") || ""; } catch { return ""; } };
const setMe = (name) => { try { localStorage.setItem("workspace.me", name); } catch { /* storage off */ } };

function show(...nodes) {
  view.replaceChildren(...nodes.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false));
  window.scrollTo(0, 0);
}

function table(columns, rows, empty) {
  if (!rows.length) return h("div", { class: "empty" }, empty || "Nothing here.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, columns.map((c) => h("td", { class: c.num ? "num" : "" }, c.cell(r))))))));
}

// -- views --------------------------------------------------------------------

async function homeView() {
  const [summary, models] = await Promise.all([api("/api/summary"), api("/api/models")]);
  summaryCache = summary;
  updateReviewNav();
  const run = summary.last_run;
  const scanButton = h("button", { class: "primary", disabled: summary.read_only || summary.scan.running, onclick: startScan },
    summary.scan.running ? "Scanning…" : "Scan now");
  const live = models.filter((m) => !m.missing);
  show(
    h("h1", {}, "Models"),
    h("p", { class: "sub" }, summary.folders.length
      ? `Scanning ${summary.folders.map((f) => f.path).join(", ")}`
      : "No folders yet. Add the synced SharePoint folder with: daxlin workspace add-folder \"PATH\""),
    h("div", { class: "tiles" },
      tile(summary.models, "models and reports"),
      tile(summary.measures, "measures"),
      tile(summary.unused_measures, "unused measures", summary.unused_measures > 0),
      tile(summary.broken_refs, "visual fields missing from the model", summary.broken_refs > 0),
      tile(summary.pending_reviews, "versions waiting for review"),
      tile(summary.decisions.remove, "marked to remove"),
      tile(summary.with_problems, "files with read problems", summary.with_problems > 0)),
    h("div", { class: "bar" },
      h("span", { class: "grow muted small" }, run
        ? `Last scan ${fmtDate(run.finished_at || run.started_at)}: ${run.files} files, ${run.new_versions} new versions, ${run.failed} failed`
        : "Not scanned yet."),
      scanButton),
    h("div", { id: "scanLog" }, lastScanLog.length ? h("pre", { class: "log" }, lastScanLog.join("\n")) : null),
    table([
      { label: "Model", cell: (m) => [h("a", { href: `#/model/${m.id}` }, m.name), m.missing ? [" ", badge("missing", "file not found")] : null] },
      { label: "Kind", cell: (m) => (m.kind === "report" ? "report only" : "model") },
      { label: "Status", cell: (m) => (m.status === "ok" ? "" : badge(m.status, m.status === "partial" ? "model not read" : "could not read")) },
      { label: "Version", num: true, cell: (m) => m.version },
      { label: "Saved", cell: (m) => fmtDate(m.file_modified_at) },
      { label: "Measures", num: true, cell: (m) => n(m.measures) },
      { label: "Visuals", num: true, cell: (m) => n(m.visuals) },
      { label: "Unused measures", num: true, cell: (m) => (m.status !== "ok" || !m.has_report ? h("span", { class: "muted" }, "–")
        : m.unused_measures ? h("b", { class: "err" }, n(m.unused_measures)) : "0") },
      { label: "Owner", cell: (m) => m.owner },
    ], models, "No models yet: add a folder and scan it."),
    live.length !== models.length ? h("p", { class: "muted small" }, "Files no longer found are kept with their history.") : null,
  );
  if (summary.scan.running) pollScan();
}

async function startScan(event) {
  event.target.disabled = true;
  event.target.textContent = "Scanning…";
  await api("/api/scan", {});
  pollScan();
}

async function pollScan() {
  const state = await api("/api/scan");
  const box = document.getElementById("scanLog");
  lastScanLog = state.log;
  if (box) box.replaceChildren(state.log.length ? h("pre", { class: "log" }, state.log.join("\n")) : "");
  if (state.running) setTimeout(pollScan, 800);
  else if (box && location.hash.replace(/^#\/?/, "") === "") homeView();
}

async function searchView(params) {
  const q = params.get("q") || "";
  const dax = params.get("dax") === "1";
  document.getElementById("q").value = q;
  document.getElementById("inDax").checked = dax;
  const rows = await api(`/api/search?q=${encodeURIComponent(q)}&dax=${dax ? 1 : 0}`);
  const models = new Set(rows.map((r) => r.model_id));
  show(
    h("h1", {}, `“${q}”`),
    h("p", { class: "sub" }, `${rows.length} match${rows.length === 1 ? "" : "es"} in ${models.size} model${models.size === 1 ? "" : "s"}${dax ? ", names and DAX" : ""}`),
    table([
      { label: "Name", cell: (r) => [objLink(r), r.dax_match ? h("span", { class: "muted small" }, "  (in its DAX)") : null] },
      { label: "Kind", cell: (r) => kindTag(r.kind) },
      { label: "Table", cell: (r) => r.table_name },
      { label: "Model", cell: (r) => [h("a", { href: `#/model/${r.model_id}` }, r.model_name), r.model_kind === "report" ? h("span", { class: "muted small" }, " (report)") : null] },
      { label: "Usage", cell: (r) => (r.in_model ? (r.usage_status ? badge(r.usage_status) : "") : h("span", { class: "muted small" }, "used by this file's report")) },
      { label: "Decision", cell: (r) => (r.decision ? badge(r.decision, r.decision) : "") },
    ], rows, "No measure or column by that name in any model."),
  );
}

async function modelView(id, params) {
  const m = await api(`/api/models/${id}`);
  const readOnly = summaryCache ? summaryCache.read_only : false;
  const state = { kind: params.get("kind") || "measure", status: params.get("status") || "", text: "" };
  const listBox = h("div");
  const counts = (kind) => m.objects.filter((o) => o.kind === kind && o.in_model).length;

  const drawList = () => {
    const text = state.text.toLowerCase();
    const rows = m.objects.filter((o) => (state.kind === "all" || o.kind === state.kind)
      && (!state.status || (state.status === "unused" ? ["unused", "deadChain"].includes(o.usage_status) : o.usage_status === state.status))
      && (!text || fieldLabel(o).toLowerCase().includes(text) || (o.display_folder || "").toLowerCase().includes(text)));
    listBox.replaceChildren(table([
      { label: "Name", cell: (o) => [objLink(o), o.is_hidden ? h("span", { class: "muted small" }, "  hidden") : null, !o.in_model ? h("span", { class: "err small" }, "  not in the model") : null] },
      { label: "Kind", cell: (o) => kindTag(o.kind) },
      { label: "Table", cell: (o) => (o.kind === "table" ? "" : o.table_name) },
      { label: "Folder", cell: (o) => o.display_folder },
      { label: "Usage", cell: (o) => (o.usage_status ? badge(o.usage_status) : "") },
      { label: "Reads", num: true, cell: (o) => o.reads },
      { label: "Read by", num: true, cell: (o) => o.read_by },
      { label: "Decision", cell: (o) => (o.decision ? badge(o.decision, o.decision) : "") },
      { label: "Owner", cell: (o) => o.owner },
    ], rows, "Nothing matches these filters."));
  };

  const select = (options, value, onchange) => h("select", { onchange: (e) => { onchange(e.target.value); drawList(); } },
    options.map(([v, label]) => h("option", { value: v, selected: v === value }, label)));

  const owner = h("input", { value: m.owner, placeholder: "who looks after this model" });
  const team = h("input", { value: m.team, placeholder: "team" });
  const notes = h("textarea", {}, m.notes);
  const saved = h("span", { class: "saved" });
  const save = async () => {
    await api(`/api/models/${m.id}`, { owner: owner.value, team: team.value, notes: notes.value });
    saved.textContent = "Saved";
    setTimeout(() => { saved.textContent = ""; }, 2000);
  };

  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/" }, "Models"), " › ", m.name),
    h("h1", {}, m.name, " ", m.missing ? badge("missing", "file not found") : null),
    h("p", { class: "sub" }, m.path),
    m.status !== "ok" || m.note ? h("p", { class: m.status === "ok" ? "muted" : "err" }, m.note) : null,
    h("div", { class: "tiles" },
      tile(counts("measure"), "measures"),
      tile(counts("column"), "columns"),
      tile(m.visuals, `visuals on ${m.pages} pages`),
      tile(m.unused_measures, "unused measures", m.unused_measures > 0),
      tile(m.unused_columns, "unused columns", m.unused_columns > 0),
      tile(m.broken_refs, "visual fields missing", m.broken_refs > 0)),
    h("div", { class: "grid2" },
      h("div", {},
        h("div", { class: "bar" },
          select([["measure", "Measures"], ["column", "Columns"], ["table", "Tables"], ["all", "Everything"]], state.kind, (v) => { state.kind = v; }),
          select([["", "Any usage"], ["unused", "Unused"], ["visual", "Used in visuals"], ["indirect", "Used through measures"], ["structural", "Needed by the model"]], state.status, (v) => { state.status = v; }),
          h("input", { type: "search", class: "grow", placeholder: "Filter by name or folder", oninput: (e) => { state.text = e.target.value; drawList(); } })),
        listBox,
        m.kind === "model" && !m.has_report ? h("p", { class: "muted small" }, "No report in this file, so usage is unknown.") : null,
        h("p", { class: "muted small" }, "Unused here means unused in this file's own report. Reports on the published model are listed under Models as report only; check them before removing anything.")),
      h("div", {},
        h("h2", {}, "About this model"),
        h("div", { class: "panel form" },
          h("label", {}, "Owner", owner), h("label", {}, "Team", team), h("label", {}, "Notes", notes),
          h("div", { class: "bar" }, h("button", { class: "primary", onclick: save, disabled: readOnly }, "Save"), saved)),
        h("h2", {}, `Versions (${m.history.length})`),
        h("ul", { class: "plain panel" }, m.history.map((v) => h("li", {},
          h("a", { href: `#/version/${v.id}` }, h("b", {}, `v${v.number}`)), ` saved ${fmtDate(v.file_modified_at)}  `,
          v.review === "baseline" ? null : reviewBadge(v.review),
          h("div", { class: "muted small" }, v.status === "ok"
            ? `${n(v.measures)} measures, ${n(v.visuals)} visuals, ${n(v.unused_measures)} unused`
            : v.note))))),
    ),
  );
  drawList();
}

async function objectView(id) {
  const o = await api(`/api/objects/${id}`);
  const readOnly = summaryCache ? summaryCache.read_only : false;
  const a = o.annotation;
  const owner = h("input", { value: a.owner, placeholder: "who answers questions about it" });
  const decision = h("select", {}, [["", "No decision"], ["keep", "Keep"], ["remove", "Remove"]]
    .map(([v, label]) => h("option", { value: v, selected: v === a.decision }, label)));
  const note = h("textarea", { placeholder: "why it exists, who asked for it, what it replaces" }, a.note);
  const by = h("input", { value: me(), placeholder: "your name" });
  const saved = h("span", { class: "saved" });
  const save = async () => {
    setMe(by.value);
    try {
      await api("/api/annotations", { model_id: o.model_id, object_key: o.object_key, owner: owner.value, decision: decision.value, note: note.value, by: by.value });
      saved.className = "saved";
      saved.textContent = "Saved";
    } catch (err) {
      saved.className = "err small";
      saved.textContent = err.message;
    }
  };
  const usage = o.usage;
  const changes = [];
  o.history.forEach((v, i) => {
    const prev = o.history[i + 1];
    const what = [];
    if (!prev) what.push("first seen");
    else {
      if (fieldLabel(prev) !== fieldLabel(v)) what.push(`renamed from ${fieldLabel(prev)}`);
      if (prev.expression !== v.expression) what.push("DAX changed");
      if (prev.usage_status !== v.usage_status && v.usage_status) what.push(STATUS[v.usage_status] || v.usage_status);
    }
    if (what.length) changes.push({ ...v, what: what.join(", ") });
  });

  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/" }, "Models"), " › ", h("a", { href: `#/model/${o.model_id}` }, o.model_name), " › ", fieldLabel(o)),
    h("h1", {}, fieldLabel(o)),
    h("p", { class: "sub" }, kindTag(o.kind), o.kind !== "table" && o.table_name ? `  in ${o.table_name}` : "",
      o.display_folder ? `  ·  folder ${o.display_folder}` : "", o.is_hidden ? "  ·  hidden" : "",
      o.is_current ? "" : `  ·  version ${o.version}, not the current one`),
    h("div", { class: "grid2" },
      h("div", {},
        usage ? h("p", {}, badge(usage.status), "  ", h("span", { class: "muted" }, STATUS_HELP[usage.status])) : null,
        !o.in_model ? h("p", { class: o.version_status === "ok" && o.model_kind !== "report" ? "err" : "muted" },
          o.model_kind === "report" ? "This report uses it; the model it lives in is published elsewhere."
            : o.version_status !== "ok" ? "This file's model could not be read, so only its report's use of it is known."
              : "A visual or measure uses it, but it is not in the model: renamed or deleted?") : null,
        o.description ? h("p", {}, o.description) : null,
        o.expression ? [h("h2", {}, o.kind === "measure" ? "DAX" : "Expression"), h("pre", {}, o.expression)] : null,
        o.state && o.state !== "Ready" ? h("p", { class: "err" }, `${o.state}: ${o.error_message}`) : null,
        h("h2", {}, `Visuals that use it (${o.visuals.length})`),
        table([
          { label: "Page", cell: (v) => v.page },
          { label: "Visual", cell: (v) => v.title || v.visual_type },
          { label: "Type", cell: (v) => v.visual_type },
          { label: "As", cell: (v) => (v.roles || []).join(", ") },
        ], o.visuals, "No visual reads it directly."),
        o.via_visuals.length ? [h("h2", {}, `Reaches visuals through other measures (${o.via_visuals.length})`),
          table([
            { label: "Page", cell: (v) => v.page },
            { label: "Visual", cell: (v) => v.title || v.visual_type },
            { label: "Through", cell: (v) => objLink(v.through) },
          ], o.via_visuals)] : null,
        h("h2", {}, `Reads (${o.reads_list.length})`),
        o.reads_list.length ? h("ul", { class: "plain panel" }, o.reads_list.map((r) => h("li", {}, kindTag(r.kind || "missing"), "  ", objLink(r),
          r.via_variable ? h("span", { class: "muted small" }, "  via a variable") : null))) : h("p", { class: "muted" }, "Nothing."),
        h("h2", {}, `Read by (${o.read_by_list.length})`),
        o.read_by_list.length ? h("ul", { class: "plain panel" }, o.read_by_list.map((r) => h("li", {}, kindTag(r.kind), "  ", objLink(r),
          r.usage_status ? ["  ", badge(r.usage_status)] : null))) : h("p", { class: "muted" }, "No measure or column reads it."),
        h("h2", {}, `In other models and reports (${o.elsewhere.length})`),
        o.elsewhere.length ? table([
          { label: "Model", cell: (e) => h("a", { href: `#/object/${e.id}` }, e.model_name) },
          { label: "Table", cell: (e) => e.table_name },
          { label: "Usage", cell: (e) => (e.in_model ? (e.usage_status ? badge(e.usage_status) : "") : "used by this report") },
        ], o.elsewhere) : h("p", { class: "muted" }, "No other model has one by this name."),
      ),
      h("div", {},
        h("h2", {}, "Team notes"),
        h("div", { class: "panel form" },
          h("label", {}, "Owner", owner), h("label", {}, "Decision", decision), h("label", {}, "Note", note),
          h("label", {}, "Your name", by),
          h("div", { class: "bar" }, h("button", { class: "primary", onclick: save, disabled: readOnly }, "Save"), saved),
          a.updated_at ? h("div", { class: "muted small" }, `Last changed ${fmtDate(a.updated_at)}${a.updated_by ? ` by ${a.updated_by}` : ""}`) : null,
          h("div", { class: "muted small" }, "Notes follow this object through renames and new versions.")),
        h("h2", {}, "History"),
        h("ul", { class: "plain panel" }, changes.map((v) => h("li", {},
          h("a", { href: `#/object/${v.id}` }, `v${v.version}`), ` ${fmtDate(v.file_modified_at)}  `,
          h("a", { href: `#/version/${v.version_id}`, class: "small" }, "what changed"),
          h("div", { class: "muted small" }, v.what)))),
        o.history.length > changes.length ? h("p", { class: "muted small" }, `Unchanged in ${o.history.length - changes.length} other version(s).`) : null),
    ),
  );
}

async function decisionsView(params) {
  const which = params.get("decision") === "keep" ? "keep" : "remove";
  const rows = await api(`/api/decisions?decision=${which}`);
  show(
    h("h1", {}, "Cleanup list"),
    h("p", { class: "sub" }, "Everything the team has marked, across models. Removing happens in Power BI Desktop for now; milestone 4 applies it for you."),
    h("div", { class: "bar" },
      h("a", { href: "#/decisions?decision=remove" }, which === "remove" ? h("b", {}, "Marked to remove") : "Marked to remove"), " · ",
      h("a", { href: "#/decisions?decision=keep" }, which === "keep" ? h("b", {}, "Marked to keep") : "Marked to keep")),
    table([
      { label: "Name", cell: (r) => (r.object_id ? h("a", { href: `#/object/${r.object_id}` }, fieldLabel(r)) : h("span", { class: "muted" }, "no longer in the model")) },
      { label: "Kind", cell: (r) => (r.kind ? kindTag(r.kind) : "") },
      { label: "Model", cell: (r) => h("a", { href: `#/model/${r.model_id}` }, r.model_name) },
      { label: "Usage now", cell: (r) => (r.usage_status ? badge(r.usage_status) : "") },
      { label: "Note", cell: (r) => r.note },
      { label: "By", cell: (r) => [r.updated_by, h("div", { class: "muted small" }, fmtDate(r.updated_at))] },
    ], rows, which === "remove" ? "Nothing marked to remove yet. Open an unused measure and set its decision." : "Nothing marked to keep yet."),
  );
}


// -- milestone 2: review ------------------------------------------------------

const REVIEW = { pending: "waiting for review", approved: "approved", changes_requested: "changes requested",
  baseline: "first version", unreadable: "could not be read" };
const reviewBadge = (state) => h("span", { class: `badge ${state}` }, REVIEW[state] || state);
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
const FIELD_NAMES = { table_name: "home table", description: "description", display_folder: "display folder",
  format_string: "format string", data_type: "data type", is_hidden: "hidden", state: "state" };

function updateReviewNav() {
  const link = document.getElementById("reviewNav");
  const count = summaryCache ? summaryCache.pending_reviews : 0;
  link.replaceChildren("Review", count ? h("span", { class: "count", title: `${count} waiting` }, count) : "");
}

function changeSummary(c) {
  if (!c || c.total === undefined) return h("span", { class: "muted" }, "–");
  if (!c.total) return h("span", { class: "muted" }, "no changes");
  const parts = [];
  if (c.broken) parts.push(h("b", { class: "err" }, `${c.broken} broken`));
  if (c.dax) parts.push(`${c.dax} DAX`);
  if (c.renamed) parts.push(`${c.renamed} renamed`);
  if (c.added) parts.push(`${c.added} added`);
  if (c.removed) parts.push(`${c.removed} removed`);
  const other = c.edited - c.dax - c.follow_renames - c.formatting;
  if (other > 0) parts.push(`${other} edited`);
  if (c.formatting) parts.push(`${c.formatting} formatting`);
  if (c.visuals) parts.push(plural(c.visuals, "visual"));
  return parts.flatMap((p, i) => (i ? [", ", p] : [p]));
}

async function reviewQueueView(params) {
  const status = params.get("status") || "pending";
  const rows = await api(`/api/reviews?status=${encodeURIComponent(status)}`);
  const tab = (value, text) => h("a", { href: `#/review?status=${value}` }, status === value ? h("b", {}, text) : text);
  show(
    h("h1", {}, "Review"),
    h("p", { class: "sub" }, "Every saved version of a model, compared with the one before. Open one to see what changed, comment, and approve it."),
    h("div", { class: "bar" }, tab("pending", "Waiting"), " · ", tab("changes_requested", "Changes requested"), " · ",
      tab("approved", "Approved"), " · ", tab("all", "All")),
    table([
      { label: "Model", cell: (r) => h("a", { href: `#/version/${r.id}` }, r.model_name) },
      { label: "Version", num: true, cell: (r) => `v${r.number}` },
      { label: "Saved", cell: (r) => fmtDate(r.file_modified_at) },
      { label: "What changed", cell: (r) => changeSummary(r.changes) },
      { label: "Comments", num: true, cell: (r) => r.comments || "" },
      { label: "Review", cell: (r) => [reviewBadge(r.review), r.reviewer ? h("div", { class: "muted small" }, `${r.reviewer}, ${fmtDate(r.decided_at)}`) : null] },
    ], rows, status === "pending" ? "Nothing waiting. New versions appear here after a scan." : "None."),
  );
}

function diffBlock(dax) {
  return h("pre", { class: "diff" }, dax.lines.map((l) => h("span", { class: l.op === "+" ? "add" : l.op === "-" ? "del" : "same" }, l.text || " ")));
}

async function versionView(id, params) {
  const against = params.get("against");
  const r = await api(`/api/versions/${id}${against ? `?against=${encodeURIComponent(against)}` : ""}`);
  const v = r.version;
  const readOnly = summaryCache ? summaryCache.read_only : false;
  const comments = r.comments;
  const byObject = (key) => comments.filter((c) => c.object_key === key && c.kind === "comment");

  const by = h("input", { value: me(), placeholder: "your name" });
  const say = h("span", { class: "small" });
  const post = async (path, body) => {
    setMe(by.value);
    try {
      await api(path, { ...body, by: by.value });
      summaryCache = await api("/api/summary");
      updateReviewNav();
      await versionView(id, params);
    } catch (err) {
      say.className = "err small";
      say.textContent = err.message;
    }
  };

  const thread = (items) => h("div", { class: "thread" }, items.map((c) => h("div", { class: `msg ${c.kind}` },
    h("div", { class: "who" }, c.kind === "comment" ? "" : [reviewBadge(c.kind), " "], c.author || "someone", " · ", fmtDate(c.created_at)),
    c.body ? h("p", {}, c.body) : null)));

  const commentBox = (key) => {
    const area = h("textarea", { placeholder: key ? "Comment on this change" : "A comment on the whole version" });
    return h("div", { class: "form" }, area,
      h("div", {}, h("button", { disabled: readOnly, onclick: () => post(`/api/versions/${id}/comments`, { body: area.value, object_key: key || "" }) }, "Comment")));
  };

  const objectCard = (item, body) => {
    const own = byObject(item.key);
    const toggle = h("details", {}, h("summary", {}, own.length ? plural(own.length, "comment") : "Comment"), thread(own), commentBox(item.key));
    if (own.length) toggle.open = true;
    return h("div", { class: "change" },
      h("div", { class: "change-head" }, kindTag(item.kind), item.id ? h("a", { href: `#/object/${item.id}` }, h("b", {}, item.label)) : h("b", {}, item.label),
        h("span", { class: "grow" }), item.impact ? h("span", { class: "muted small" }, `feeds ${plural(item.impact, "visual")}`) : null),
      body, toggle);
  };

  const section = (title, items, render, open = true) => (items.length
    ? (open ? [h("h2", {}, `${title} (${items.length})`), items.map(render)]
      : h("details", {}, h("summary", {}, `${title} (${items.length})`), items.map(render)))
    : null);

  const daxEdits = r.edited.filter((e) => e.changes.includes("DAX") && !e.dax.formatting_only && !e.follows_rename);
  const followUps = r.edited.filter((e) => e.follows_rename);
  const formatting = r.edited.filter((e) => e.changes.includes("DAX") && e.dax.formatting_only);
  const props = r.edited.filter((e) => e.changes.some((c) => c !== "DAX"));
  const c = r.counts || {};
  const vis = r.visuals;

  const compareSelect = h("select", { onchange: (e) => { location.hash = `#/version/${id}${e.target.value ? `?against=${e.target.value}` : ""}`; } },
    h("option", { value: "" }, "the previous version"),
    r.versions.filter((x) => x.number < v.number && x.status !== "error").map((x) =>
      h("option", { value: x.id, selected: r.against && String(x.id) === String(against) }, `v${x.number}, ${fmtDate(x.file_modified_at)}`)));

  const left = r.baseline ? [h("div", { class: "empty" }, "The first version of this model: there is nothing earlier to compare it with.")]
    : v.status === "error" ? [h("p", { class: "err" }, `This version could not be read: ${v.note}`)]
      : [
        h("div", { class: "tiles" },
          tile(c.broken, "broken references", c.broken > 0), tile(c.dax, "DAX changes"), tile(c.renamed, "renamed"),
          tile(c.added, "added"), tile(c.removed, "removed"), tile(c.visuals, "visuals changed")),
        !c.total ? h("div", { class: "empty" }, "No change to measures, columns or visuals. Something else in the file changed, such as data or formatting.") : null,
        section("Broken: used, but no longer in the model", r.broken, (b) => h("div", { class: "change" },
          h("div", { class: "change-head" }, h("b", { class: "err" }, b.label)),
          h("div", { class: "small" }, "Still used by: ", b.used_by.join(", ")))),
        section("Renamed", r.renamed, (x) => objectCard(x, h("div", {}, h("span", { class: "was" }, x.old_label), "  →  ", h("b", {}, x.label),
          x.guessed ? h("span", { class: "muted small" }, "  (probably: same DAX, no lineage tag)") : null))),
        section("DAX changed", daxEdits, (e) => objectCard(e, [diffBlock(e.dax),
          e.reads_added.length || e.reads_removed.length ? h("div", { class: "chips" },
            e.reads_added.map((x) => h("span", { class: "chip add" }, `now reads ${x}`)),
            e.reads_removed.map((x) => h("span", { class: "chip del" }, `no longer reads ${x}`))) : null])),
        section("Removed", r.removed, (x) => objectCard(x, x.expression ? h("pre", {}, x.expression) : null)),
        section("Added", r.added, (x) => objectCard(x, x.expression ? h("pre", {}, x.expression) : null)),
        section("Other edits", props, (e) => objectCard(e, h("ul", { class: "plain" }, Object.keys(FIELD_NAMES)
          .filter((k) => e.before[k] !== e.after[k]).map((k) => h("li", {}, `${FIELD_NAMES[k]}: `,
            h("span", { class: "was" }, String(k === "is_hidden" ? (e.before[k] ? "yes" : "no") : e.before[k] || "(none)")), "  →  ",
            String(k === "is_hidden" ? (e.after[k] ? "yes" : "no") : e.after[k] || "(none)")))))),
        section("Usage", r.usage, (u) => objectCard(u, h("div", {}, u.became_unused ? h("b", { class: "err" }, "Now unused") : "Used again",
          h("span", { class: "muted" }, `: was ${STATUS[u.before] || u.before}, now ${STATUS[u.after] || u.after}`)))),
        vis.added.length || vis.removed.length || vis.changed.length ? [h("h2", {}, `Visuals (${vis.added.length + vis.removed.length + vis.changed.length})`),
          table([
            { label: "Page", cell: (x) => x.page },
            { label: "Visual", cell: (x) => x.title },
            { label: "Change", cell: (x) => (x.change === "added" ? h("span", { class: "chip add" }, "added") : x.change === "removed" ? h("span", { class: "chip del" }, "removed")
              : h("div", { class: "chips" }, x.fields_added.map((f) => h("span", { class: "chip add" }, `+ ${f}`)),
                x.fields_removed.map((f) => h("span", { class: "chip del" }, `− ${f}`)), x.roles_changed.map((f) => h("span", { class: "chip" }, `${f} moved`)))) },
          ], [...vis.removed.map((x) => ({ ...x, change: "removed" })), ...vis.added.map((x) => ({ ...x, change: "added" })), ...vis.changed])] : null,
        section("Only follow a rename", followUps, (e) => objectCard(e, diffBlock(e.dax)), false),
        section("Formatting only", formatting, (e) => objectCard(e, diffBlock(e.dax)), false),
      ];

  const reviewable = !r.baseline && v.status !== "error";
  const note = h("textarea", { placeholder: "Optional: what to fix, or why it is fine" });
  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/review" }, "Review"), " › ", h("a", { href: `#/model/${v.model_id}` }, v.model_name), " › ", `v${v.number}`),
    h("h1", {}, `${v.model_name} v${v.number} `, reviewBadge(v.review)),
    h("p", { class: "sub" }, `Saved ${fmtDate(v.file_modified_at)}`, r.against ? ["  ·  compared with ", compareSelect] : null),
    h("div", { class: "grid2" },
      h("div", {}, left),
      h("div", {},
        reviewable ? [h("h2", {}, "Review"),
          h("div", { class: "panel form" },
            v.reviewer ? h("div", { class: "small" }, reviewBadge(v.review), `  by ${v.reviewer}, ${fmtDate(v.decided_at)}`) : null,
            h("label", {}, "Your name", by),
            h("label", {}, "Note", note),
            h("div", { class: "buttons" },
              h("button", { class: "good", disabled: readOnly, onclick: () => post(`/api/versions/${id}/review`, { status: "approved", note: note.value }) }, "Approve"),
              h("button", { class: "bad", disabled: readOnly, onclick: () => post(`/api/versions/${id}/review`, { status: "changes_requested", note: note.value }) }, "Request changes"),
              v.review !== "pending" ? h("button", { disabled: readOnly, onclick: () => post(`/api/versions/${id}/review`, { status: "pending", note: note.value }) }, "Reopen") : null),
            say)] : null,
        h("h2", {}, "Discussion"),
        h("div", { class: "panel" },
          comments.filter((x) => !x.object_key).length ? thread(comments.filter((x) => !x.object_key)) : h("p", { class: "muted small" }, "No comments yet."),
          comments.some((x) => x.object_key) ? h("p", { class: "muted small" }, `${plural(comments.filter((x) => x.object_key).length, "comment")} on single changes, shown with them.`) : null,
          reviewable ? commentBox("") : null),
        h("h2", {}, "Other versions"),
        h("ul", { class: "plain panel" }, r.versions.map((x) => h("li", {},
          String(x.id) === String(id) ? h("b", {}, `v${x.number}`) : h("a", { href: `#/version/${x.id}` }, `v${x.number}`),
          ` ${fmtDate(x.file_modified_at)}`, x.status === "error" ? h("span", { class: "err small" }, "  unreadable") : null)))),
    ),
  );
}

// -- routing ------------------------------------------------------------------

async function route() {
  const [path, query] = (location.hash.replace(/^#/, "") || "/").split("?");
  const params = new URLSearchParams(query || "");
  if (path !== "/search") document.getElementById("q").value = "";
  try {
    if (!summaryCache) summaryCache = await api("/api/summary");
    updateReviewNav();
    let match;
    if ((match = path.match(/^\/model\/(\d+)$/))) await modelView(match[1], params);
    else if ((match = path.match(/^\/object\/(\d+)$/))) await objectView(match[1]);
    else if (path === "/search") await searchView(params);
    else if (path === "/decisions") await decisionsView(params);
    else if (path === "/review") await reviewQueueView(params);
    else if ((match = path.match(/^\/version\/(\d+)$/))) await versionView(match[1], params);
    else await homeView();
  } catch (err) {
    show(h("h1", {}, "Something went wrong"), h("p", { class: "err" }, err.message));
  }
}

document.getElementById("searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const q = document.getElementById("q").value.trim();
  if (q) location.hash = `#/search?q=${encodeURIComponent(q)}&dax=${document.getElementById("inDax").checked ? 1 : 0}`;
});
window.addEventListener("hashchange", route);
route();
