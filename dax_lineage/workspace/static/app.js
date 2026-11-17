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
          h("b", {}, `v${v.number}`), ` saved ${fmtDate(v.file_modified_at)}`,
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
          h("a", { href: `#/object/${v.id}` }, `v${v.version}`), ` ${fmtDate(v.file_modified_at)}`,
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

// -- routing ------------------------------------------------------------------

async function route() {
  const [path, query] = (location.hash.replace(/^#/, "") || "/").split("?");
  const params = new URLSearchParams(query || "");
  if (path !== "/search") document.getElementById("q").value = "";
  try {
    if (!summaryCache) summaryCache = await api("/api/summary");
    let match;
    if ((match = path.match(/^\/model\/(\d+)$/))) await modelView(match[1], params);
    else if ((match = path.match(/^\/object\/(\d+)$/))) await objectView(match[1]);
    else if (path === "/search") await searchView(params);
    else if (path === "/decisions") await decisionsView(params);
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
