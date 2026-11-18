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
let session = { mode: "none", signed_in: true, user: null };
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
  if (response.status === 401 && path !== "/api/login") {
    session = { ...session, signed_in: false, user: null };
    if (!location.hash.startsWith("#/signin")) location.hash = "#/signin";
    throw new Error(data.error || "sign in first");
  }
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
const RANK = { viewer: 0, author: 1, reviewer: 2, admin: 3 };
const can = (role) => !session.user || (RANK[session.user.role] ?? -1) >= RANK[role];
const signedName = () => (session.mode !== "none" && session.user ? (session.user.display_name || session.user.username) : "");
const me = () => { if (signedName()) return signedName(); try { return localStorage.getItem("workspace.me") || ""; } catch { return ""; } };
const setMe = (name) => { try { localStorage.setItem("workspace.me", name); } catch { /* storage off */ } };

function nameFields(root) {
  // signed in: notes, reviews and changes carry the sign-in name, so no one types a name
  if (!signedName()) return;
  root.querySelectorAll("input[placeholder='your name']").forEach((input) => {
    input.value = signedName();
    (input.closest("label") || input).hidden = true;
  });
}

function fill(el, ...nodes) {
  el.replaceChildren(...nodes.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false));
  nameFields(el);
  return el;
}

function show(...nodes) {
  view.replaceChildren(...nodes.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false));
  nameFields(view);
  window.scrollTo(0, 0);
}

function table(columns, rows, empty) {
  if (!rows.length) return h("div", { class: "empty" }, empty || "Nothing here.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, columns.map((c) => h("td", { class: c.num ? "num" : "" }, c.cell(r))))))));
}

// -- views --------------------------------------------------------------------

// the folders the scan reads: paste a path, or pick one with the system's dialog
function foldersPanel(summary) {
  const admin = can("admin") && !summary.read_only;
  const box = h("div", { class: summary.folders.length ? "panel" : "panel notice" });
  const path = h("input", { placeholder: "C:\\Users\\you\\OneDrive - Contoso\\BI - Reports", class: "grow" });
  const say = h("span", { class: "small" });
  const fail = (err) => { say.className = "err small"; say.textContent = err.message; };
  const add = async () => {
    say.className = "small muted";
    say.textContent = "Adding…";
    try {
      const done = await api("/api/folders", { path: path.value, scan: true });
      lastScanLog = [];
      await homeView();
      if (done.scanning) pollScan();
    } catch (err) { fail(err); }
  };
  const browse = async (event) => {
    const button = event.target;
    button.disabled = true;
    say.className = "small muted";
    say.textContent = "A folder window has opened on this PC (it may be behind the browser).";
    try {
      const picked = await api("/api/folders/browse", {});
      if (picked.path) { path.value = picked.path; say.textContent = ""; await add(); }
      else say.textContent = "No folder chosen.";
    } catch (err) { fail(err); }
    button.disabled = false;
  };
  const remove = async (folder) => {
    try { await api("/api/folders/remove", { path: folder.path }); await homeView(); } catch (err) { fail(err); }
  };
  (async () => {
    const info = await api("/api/folders").catch(() => ({ folders: summary.folders, can_browse: false }));
    fill(box,
      info.folders.length ? [h("b", {}, "Folders the scan reads"),
        h("ul", { class: "plain" }, info.folders.map((f) => h("li", {},
          h("code", {}, f.path), "  ",
          f.exists ? h("span", { class: "muted small" }, plural(f.models, "file")) : h("span", { class: "err small" }, "not found on this PC"),
          admin ? ["  ", h("button", { onclick: () => remove(f) }, "Remove")] : null)))]
        : [h("b", {}, "Start here: choose the folder with your .pbix files"),
          h("p", { class: "small" }, "Usually the SharePoint library OneDrive syncs to this PC. Its subfolders are read too, and files are only read, never changed.")],
      admin ? h("div", { class: "bar" }, path,
        info.can_browse ? h("button", { onclick: browse }, "Browse…") : null,
        h("button", { class: "primary", onclick: add }, "Add and scan")) : null,
      admin ? h("div", { class: "muted small" }, info.can_browse
        ? "Browse opens a folder window on this PC. Or paste a path: in File Explorer, click the address bar and copy it."
        : "In File Explorer, click the address bar, copy the path, and paste it here.") : null,
      say);
  })();
  return box;
}

async function homeView() {
  const [summary, models] = await Promise.all([api("/api/summary"), api("/api/models")]);
  summaryCache = summary;
  updateReviewNav();
  const run = summary.last_run;
  const scanButton = h("button", { class: "primary", disabled: summary.read_only || summary.scan.running || (session.user && session.user.role !== "admin"), onclick: startScan },
    summary.scan.running ? "Scanning…" : "Scan now");
  const live = models.filter((m) => !m.missing);
  show(
    h("h1", {}, "Models"),
    foldersPanel(summary),
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
      { label: "Score", num: true, cell: (m) => scoreText(m.score) },
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
      tile(m.broken_refs, "visual fields missing", m.broken_refs > 0),
      m.check ? h("a", { class: "tile", href: "#standards", onclick: (e) => { e.preventDefault(); document.getElementById("standards").scrollIntoView(); } },
        h("b", {}, scoreText(m.check.score)), h("span", {}, "standards score")) : null),
    h("div", { class: "grid2" },
      h("div", {},
        h("div", { class: "bar" },
          select([["measure", "Measures"], ["column", "Columns"], ["table", "Tables"], ["all", "Everything"]], state.kind, (v) => { state.kind = v; }),
          select([["", "Any usage"], ["unused", "Unused"], ["visual", "Used in visuals"], ["indirect", "Used through measures"], ["structural", "Needed by the model"]], state.status, (v) => { state.status = v; }),
          h("input", { type: "search", class: "grow", placeholder: "Filter by name or folder", oninput: (e) => { state.text = e.target.value; drawList(); } })),
        listBox,
        m.kind === "model" && !m.has_report ? h("p", { class: "muted small" }, "No report in this file, so usage is unknown.") : null,
        h("p", { class: "muted small" }, "Unused here means unused in this file's own report. Reports on the published model are listed under Models as report only; check them before removing anything."),
        h("h2", { id: "standards" }, m.check ? `Standards: score ${m.check.score}` : "Standards"),
        m.check ? [h("p", { class: "muted small" }, `${plural(m.check.errors, "error")}, ${plural(m.check.warnings, "warning")}, ${plural(m.check.infos, "note")}`
          + (m.check.accepted ? `, ${m.check.accepted} accepted as exceptions` : "") + ". Open a finding's object to accept it as an exception."),
          findingsTable(m.check.findings, false)]
          : h("p", { class: "muted" }, "Not checked: no model was read from this file.")),
      h("div", {},
        m.version_id ? h("div", { class: "bar" }, h("a", { class: "graph-link", href: `/explorer/version/${m.version_id}` }, "Open the lineage graph")) : null,
        m.kind === "model" && m.status === "ok" ? h("div", { class: "bar" }, h("a", { href: `#/model/${m.id}/edit` }, "Describe and organise measures…"),
          " · ", h("a", { href: `#/changes?status=all` }, "changes")) : null,
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
  if (params.get("view") === "standards") document.getElementById("standards").scrollIntoView();
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
    h("div", { class: "bar" }, h("a", { class: "graph-link", href: `/explorer/version/${o.version_id}?focus=${encodeURIComponent(o.node)}` }, "Show it in the lineage graph")),
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
        o.is_current ? [h("h2", {}, "Standards"), findingsPanel(o, readOnly)] : null,
        metricPanel(o),
        objectActions(o),
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
    h("p", { class: "sub" }, "Everything the team has marked, across models. Turn the removals into a change, have it approved, and the companion makes it in Power BI Desktop."),
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
    which === "remove" ? cleanupProposals(rows) : null,
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
        (r.metrics || []).length ? h("div", { class: "notice bad" }, h("b", {}, "This save changes a certified metric"),
          h("ul", {}, r.metrics.map((x) => h("li", {}, h("a", { href: `#/metric/${x.id}` }, x.name), ` (owner ${x.owner || "none"}): they should approve it, or re-certify it on the metric's page.`)))) : null,
        standardsDelta(r.standards),
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
    !r.baseline && v.status !== "error" ? h("div", { class: "bar" }, h("a", { class: "graph-link",
      href: `/explorer/version/${v.id}${against ? `?against=${encodeURIComponent(against)}` : ""}` }, "See the changes in the lineage graph")) : null,
    h("div", { class: "grid2" },
      h("div", {}, left),
      h("div", {},
        reviewable && can("reviewer") ? [h("h2", {}, "Review"),
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


// -- milestone 3: standards ---------------------------------------------------

const SEV_LABEL = { error: "error", warning: "warning", info: "note" };
const sev = (level) => h("span", { class: `sev ${level}` }, SEV_LABEL[level] || level);
const scoreText = (value, previous) => {
  if (value === null || value === undefined) return h("span", { class: "muted" }, "–");
  const cls = value >= 85 ? "high" : value >= 65 ? "mid" : "low";
  const delta = previous === null || previous === undefined || previous === value ? null
    : h("span", { class: `delta ${value > previous ? "up" : "down"}` }, `${value > previous ? "▲" : "▼"} ${Math.abs(value - previous)}`);
  return h("span", {}, h("span", { class: `score ${cls}` }, value), delta);
};

async function standardsView(params) {
  const data = await api("/api/standards");
  const readOnly = summaryCache ? summaryCache.read_only : false;
  const by = h("input", { value: me(), placeholder: "your name" });
  const status = h("span", { class: "small" });
  const change = async (rule, body) => {
    setMe(by.value);
    try {
      await api(`/api/rules/${rule.id}`, { ...body, by: by.value });
      await standardsView(params);
    } catch (err) {
      status.className = "err small";
      status.textContent = `${rule.id}: ${err.message}`;
    }
  };
  const exportRules = async () => {
    const json = JSON.stringify(await api("/api/rules/export"), null, 2);
    const link = h("a", { href: URL.createObjectURL(new Blob([json], { type: "application/json" })), download: "power-bi-rules.json" });
    document.body.append(link);
    link.click();
    link.remove();
  };
  const importInput = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: async (e) => {
    try {
      const body = JSON.parse(await e.target.files[0].text());
      await api("/api/rules/import", { ...body, by: by.value });
      await standardsView(params);
    } catch (err) {
      status.className = "err small";
      status.textContent = `Import: ${err.message}`;
    }
  } });

  const paramInputs = (rule) => Object.entries(rule.params).map(([key, value]) => {
    const input = h("input", { class: "param", value: String(value), placeholder: key, title: key, disabled: readOnly });
    input.addEventListener("change", () => change(rule, { params: { [key]: input.value } }));
    return h("div", { class: "small" }, `${key.replace("_", " ")} `, input);
  });

  show(
    h("h1", {}, "Standards"),
    h("p", { class: "sub" }, "The rules every model is checked against on each scan. Change one and every model is checked again."),
    h("div", { class: "tiles" },
      tile(data.average, "average score"),
      tile(data.models.reduce((a, m) => a + m.errors, 0), "errors", data.models.some((m) => m.errors)),
      tile(data.models.reduce((a, m) => a + m.warnings, 0), "warnings"),
      tile(data.exceptions.length, "accepted exceptions")),
    h("div", { class: "grid2" },
      h("div", {},
        h("h2", {}, "Models"),
        table([
          { label: "Model", cell: (m) => h("a", { href: `#/model/${m.id}?view=standards` }, m.name) },
          { label: "Score", num: true, cell: (m) => scoreText(m.score, m.previous_score) },
          { label: "Errors", num: true, cell: (m) => (m.errors ? h("b", { class: "err" }, m.errors) : "0") },
          { label: "Warnings", num: true, cell: (m) => m.warnings },
          { label: "Notes", num: true, cell: (m) => m.infos },
          { label: "Owner", cell: (m) => m.owner },
        ], data.models, "No checked models yet: scan a folder first."),
        h("h2", {}, "Rules"),
        table([
          { label: "On", cell: (r) => h("input", { type: "checkbox", checked: r.enabled, disabled: readOnly, "aria-label": `${r.title} on`,
            onchange: (e) => change(r, { enabled: e.target.checked }) }) },
          { label: "Rule", cell: (r) => [h("div", {}, r.title), h("div", { class: "why" }, r.why), paramInputs(r)] },
          { label: "Severity", cell: (r) => h("select", { disabled: readOnly, onchange: (e) => change(r, { severity: e.target.value }) },
            ["error", "warning", "info"].map((x) => h("option", { value: x, selected: x === r.severity }, SEV_LABEL[x]))) },
          { label: "Open", num: true, cell: (r) => (r.enabled ? (r.open ? `${r.open} in ${plural(r.models, "model")}` : "0") : "") },
        ].map((c) => ({ ...c })), data.rules),
        h("div", { class: "bar" },
          h("label", { class: "check" }, "Your name ", by),
          h("span", { class: "grow" }),
          h("button", { onclick: exportRules }, "Export rules"),
          h("button", { disabled: readOnly, onclick: () => importInput.click() }, "Import rules"), importInput,
          h("button", { disabled: readOnly, onclick: async () => { await api("/api/rules/reset", {}); await standardsView(params); } }, "Reset to defaults")),
        status),
      h("div", {},
        h("h2", {}, "Open findings by owner"),
        table([
          { label: "Owner", cell: (o) => h("a", { href: `#/standards/owner?name=${encodeURIComponent(o.owner)}` }, o.owner || "nobody yet") },
          { label: "Errors", num: true, cell: (o) => (o.errors ? h("b", { class: "err" }, o.errors) : "0") },
          { label: "Warnings", num: true, cell: (o) => o.warnings },
          { label: "Notes", num: true, cell: (o) => o.infos },
        ], data.owners, "Nothing open."),
        h("p", { class: "muted small" }, "An object's owner, or else its model's owner. Set them on the model and measure pages."),
        h("h2", {}, `Accepted exceptions (${data.exceptions.length})`),
        data.exceptions.length ? h("ul", { class: "plain panel" }, data.exceptions.map((e) => h("li", {},
          h("b", {}, e.rule_id), ` in ${e.model_name}`, h("div", { class: "small" }, e.reason),
          h("div", { class: "muted small" }, `${e.created_by || "someone"}, ${fmtDate(e.created_at)}`)))) : h("p", { class: "muted" }, "None."))),
  );
  document.querySelectorAll("tbody tr").forEach((tr) => {
    const box = tr.querySelector("input[type=checkbox]");
    if (box && !box.checked) tr.classList.add("rule-off");
  });
}

async function ownerView(params) {
  const name = params.get("name") || "";
  const rows = await api(`/api/standards/owner?name=${encodeURIComponent(name)}`);
  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/standards" }, "Standards"), " › ", name || "nobody yet"),
    h("h1", {}, name ? `Findings for ${name}` : "Findings with no owner"),
    h("p", { class: "sub" }, `${plural(rows.length, "open finding")} across ${plural(new Set(rows.map((r) => r.model_id)).size, "model")}`),
    findingsTable(rows, true),
  );
}

function findingsTable(rows, withModel) {
  return table([
    { label: "", cell: (f) => sev(f.severity) },
    { label: "Object", cell: (f) => (f.object_id ? h("a", { href: `#/object/${f.object_id}` }, f.label) : f.label) },
    withModel ? { label: "Model", cell: (f) => h("a", { href: `#/model/${f.model_id}?view=standards` }, f.model_name) } : null,
    { label: "Rule", cell: (f) => h("span", { class: "nowrap" }, f.rule_id) },
    { label: "Finding", cell: (f) => [f.message, f.accepted ? h("span", { class: "muted small" }, "  (accepted)") : null] },
  ].filter(Boolean), rows, "Nothing to fix.");
}

function findingsPanel(o, readOnly) {
  if (!o.findings || !o.findings.length) return h("p", { class: "muted small" }, "Meets every rule that is on.");
  const by = h("input", { value: me(), placeholder: "your name" });
  return h("div", { class: "panel" }, o.findings.map((f) => {
    const reason = h("input", { placeholder: "why this is fine here" });
    const send = async (body) => {
      setMe(by.value);
      await api("/api/exceptions", { model_id: o.model_id, object_key: o.object_key, rule_id: f.rule_id, by: by.value, ...body });
      route();
    };
    return h("div", { class: "finding" },
      h("div", {}, sev(f.severity), "  ", h("b", {}, f.title)),
      h("div", { class: "small" }, f.message),
      f.accepted ? h("div", { class: "small muted" }, `Accepted by ${f.created_by || "someone"}: ${f.reason}  `,
        h("button", { disabled: readOnly, onclick: () => send({ remove: true }) }, "Withdraw"))
        : h("details", {}, h("summary", { class: "small" }, "Accept as an exception"),
          h("div", { class: "form" }, reason, by, h("div", {}, h("button", { disabled: readOnly, onclick: () => send({ reason: reason.value }) }, "Accept")))));
  }));
}

function standardsDelta(st) {
  if (!st) return null;
  return [h("h2", {}, "Standards"),
    h("div", { class: "change" },
      h("div", { class: "change-head" }, "Score ", st.previous !== null ? [scoreText(st.previous), "  →  "] : null, scoreText(st.score, st.previous),
        st.previous_stale ? h("span", { class: "muted small" }, "  (the earlier score was under other rules)") : null),
      st.new.length ? [h("div", { class: "small" }, h("b", {}, `New findings (${st.new.length})`)),
        h("ul", { class: "plain" }, st.new.map((f) => h("li", { class: "small" }, sev(f.severity), "  ", f.object_id ? h("a", { href: `#/object/${f.object_id}` }, f.label) : f.label, `: ${f.message}`)))] : null,
      st.fixed.length ? [h("div", { class: "small" }, h("b", {}, `Fixed (${st.fixed.length})`)),
        h("ul", { class: "plain" }, st.fixed.map((f) => h("li", { class: "small" }, sev(f.severity), `  ${f.label}: ${f.message}`)))] : null,
      !st.new.length && !st.fixed.length ? h("div", { class: "muted small" }, "No findings new or fixed.") : null)];
}


// -- milestone 4: safe changes --------------------------------------------------

const CHANGE = { proposed: "waiting for approval", approved: "approved: apply in Desktop", rejected: "rejected",
  cancelled: "cancelled", applied: "applied: save the file", confirmed: "confirmed", failed: "failed" };
const OP_STATUS = { pending: "to do", applied: "done in Desktop", already: "already so", skipped: "skipped",
  failed: "failed", manual: "for you, in Desktop", confirmed: "in the saved file" };
const changeBadge = (state) => h("span", { class: `badge ${state}` }, CHANGE[state] || state);
const FIELD_LABEL = { description: "description", display_folder: "display folder", format_string: "format string", is_hidden: "hidden" };

function updateChangesNav() {
  const link = document.getElementById("changesNav");
  const count = summaryCache ? summaryCache.open_changes : 0;
  link.replaceChildren("Changes", count ? h("span", { class: "count", title: `${count} open` }, count) : "");
}

function opText(o) {
  const name = o.kind === "column" && o.table_name ? `${o.table_name}[${o.name}]` : o.name;
  if (o.op === "remove") return ["Remove ", h("b", {}, name)];
  if (o.op === "rename") return ["Rename ", h("b", {}, name), " to ", h("b", {}, o.new_value)];
  if (o.op === "expression") return ["Update the DAX of ", h("b", {}, name), h("span", { class: "muted" }, ` (${o.reason})`)];
  const show = (v) => (o.field === "is_hidden" ? (v === "true" ? "yes" : "no") : v || "(none)");
  return ["Set ", FIELD_LABEL[o.field] || o.field, " of ", h("b", {}, name), ": ", h("span", { class: "was" }, show(o.old_value)), "  →  ", show(o.new_value)];
}

// preview a set of edits, show what blocks them, and propose them
function proposePanel(modelId, items, title) {
  const box = h("div", { class: "panel" }, h("p", { class: "muted small" }, "Checking…"));
  const by = h("input", { value: me(), placeholder: "your name" });
  const titleInput = h("input", { value: title || "", placeholder: "a title (optional)" });
  (async () => {
    let planned;
    try {
      planned = await api("/api/changes/preview", { model_id: modelId, items });
    } catch (err) {
      box.replaceChildren(h("p", { class: "err" }, err.message));
      return;
    }
    const send = async () => {
      setMe(by.value);
      try {
        const made = await api("/api/changes", { model_id: modelId, items, title: titleInput.value, by: by.value });
        summaryCache = await api("/api/summary");
        location.hash = `#/change/${made.id}`;
      } catch (err) {
        box.append(h("p", { class: "err" }, err.message));
      }
    };
    const readOnly = summaryCache ? summaryCache.read_only : false;
    fill(box,
      planned.problems.length ? h("div", { class: "notice bad" }, h("b", {}, "Cannot be done as it is"),
        h("ul", {}, planned.problems.map((p) => h("li", {}, p)))) : null,
      planned.warnings.length ? h("div", { class: "notice" }, h("b", {}, "Check before approving"),
        h("ul", {}, planned.warnings.map((w) => h("li", {}, w)))) : null,
      planned.ops.length ? h("ol", { class: "steps" }, planned.ops.map((o) => h("li", {}, opText(o),
        o.mode === "desktop" ? h("div", { class: "small muted" }, `For you, in Desktop: ${o.reason}`) : null))) : null,
      !planned.problems.length ? h("div", { class: "form" }, h("label", {}, "Title", titleInput), h("label", {}, "Your name", by),
        h("div", {}, h("button", { class: "primary", disabled: readOnly, onclick: send }, "Propose this change"))) : null,
    );
  })();
  return box;
}

async function changesView(params) {
  const status = params.get("status") || "open";
  const rows = await api(`/api/changes?status=${encodeURIComponent(status)}`);
  const tab = (value, text) => h("a", { href: `#/changes?status=${value}` }, status === value ? h("b", {}, text) : text);
  show(
    h("h1", {}, "Changes"),
    h("p", { class: "sub" }, "Edits to a model, reviewed before anyone makes them, made in Power BI Desktop by the companion, and confirmed by the next scan."),
    h("div", { class: "bar" }, tab("open", "Open"), " · ", tab("confirmed", "Confirmed"), " · ", tab("all", "All")),
    table([
      { label: "#", num: true, cell: (r) => r.id },
      { label: "Change", cell: (r) => h("a", { href: `#/change/${r.id}` }, r.title) },
      { label: "Model", cell: (r) => h("a", { href: `#/model/${r.model_id}` }, r.model_name) },
      { label: "Edits", num: true, cell: (r) => [r.edits, r.manual ? h("span", { class: "muted small" }, ` (${r.manual} by hand)`) : null] },
      { label: "Status", cell: (r) => changeBadge(r.status) },
      { label: "Proposed", cell: (r) => [r.created_by, h("div", { class: "muted small" }, fmtDate(r.created_at))] },
    ], rows, status === "open" ? "No open changes. Start one from the Cleanup list, a model's Edit descriptions, or a measure's page." : "None."),
    h("h2", {}, "How a change is made"),
    h("ol", { class: "steps" },
      h("li", {}, "Someone proposes it; the workspace checks it against the model's latest version and refuses what would break something."),
      h("li", {}, "Someone else approves it."),
      h("li", {}, "The author opens the .pbix in Power BI Desktop and clicks External Tools › Workspace changes. The companion shows each edit and makes them when you type APPLY."),
      h("li", {}, "The author checks the model and saves. The next scan confirms every edit is in the file.")),
  );
}

async function changeView(id) {
  const r = await api(`/api/changes/${id}`);
  const readOnly = summaryCache ? summaryCache.read_only : false;
  const by = h("input", { value: me(), placeholder: "your name" });
  const note = h("textarea", { placeholder: "Optional note" });
  const say = h("span", { class: "small" });
  const post = async (path, body) => {
    setMe(by.value);
    try {
      await api(path, { ...body, by: by.value });
      summaryCache = await api("/api/summary");
      updateChangesNav();
      await changeView(id);
    } catch (err) {
      say.className = "err small";
      say.textContent = err.message;
    }
  };
  const decide = (status, text, cls) => h("button", { class: cls || "", disabled: readOnly, onclick: () => post(`/api/changes/${id}/decision`, { status, note: note.value }) }, text);
  const expressionOps = r.ops.filter((o) => o.op === "expression");
  const selfApproval = r.status === "proposed" && can("reviewer") && by.value && by.value.trim().toLowerCase() === (r.created_by || "").trim().toLowerCase();

  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/changes" }, "Changes"), " › ", `#${r.id}`),
    h("h1", {}, r.title, " ", changeBadge(r.status)),
    h("p", { class: "sub" }, h("a", { href: `#/model/${r.model_id}` }, r.model_name), `  ·  against v${r.base_number}`,
      r.current_number && r.current_number !== r.base_number ? `, the latest is v${r.current_number}` : "",
      r.confirmed_number ? `  ·  confirmed in v${r.confirmed_number}` : ""),
    h("div", { class: "bar" }, h("a", { class: "graph-link", href: `/explorer/version/${r.base_version_id}?change=${r.id}` }, "See what it touches in the lineage graph")),
    h("div", { class: "grid2" },
      h("div", {},
        r.stale.length ? h("div", { class: "notice bad" }, h("b", {}, "The model changed since this was proposed"),
          h("ul", {}, r.stale.map((x) => h("li", {}, x.why))), h("div", { class: "small" }, "Cancel it and propose it again from the latest version.")) : null,
        r.warnings.length ? h("div", { class: "notice" }, h("b", {}, "Check before approving"), h("ul", {}, r.warnings.map((w) => h("li", {}, w)))) : null,
        r.manual.length && ["approved", "applied"].includes(r.status) ? h("div", { class: "notice" }, h("b", {}, "To do by hand in Desktop's Data pane"),
          h("ul", {}, r.manual.map((o) => h("li", {}, `Rename ${o.name} to ${o.new_value}`))),
          h("div", { class: "small" }, "Visuals show these measures. A rename in Desktop updates the visuals and the DAX; one made by a tool would leave the visuals broken.")) : null,
        r.status === "approved" ? h("div", { class: "notice good" }, h("b", {}, "Ready to apply"),
          h("ol", { class: "steps" }, h("li", {}, `Open ${r.model_name}.pbix in Power BI Desktop.`),
            h("li", {}, "External Tools › Workspace changes. Check the list, type APPLY."),
            h("li", {}, "Check the model, then save. The next scan confirms it."))) : null,
        h("h2", {}, `Edits (${r.ops.length})`),
        table([
          { label: "#", num: true, cell: (o) => o.seq },
          { label: "Edit", cell: (o) => [opText(o), o.mode === "desktop" ? h("div", { class: "small muted" }, o.reason) : null, o.message && o.status !== "confirmed" ? h("div", { class: "small muted" }, o.message) : null] },
          { label: "Status", cell: (o) => h("span", { class: `badge ${o.status === "pending" ? (o.mode === "desktop" ? "manual" : "proposed") : o.status}` },
            o.status === "pending" && o.mode === "desktop" ? OP_STATUS.manual : OP_STATUS[o.status] || o.status) },
        ], r.ops),
        expressionOps.length ? [h("h2", {}, "DAX that follows the rename"), expressionOps.map((o) => h("div", { class: "change" },
          h("div", { class: "change-head" }, h("b", {}, o.name)),
          diffBlock({ lines: [...o.old_value.split("\n").map((t) => ({ op: "-", text: t })), ...o.new_value.split("\n").map((t) => ({ op: "+", text: t }))] })))] : null),
      h("div", {},
        h("h2", {}, "Decision"),
        h("div", { class: "panel form" },
          h("label", {}, "Your name", by),
          h("label", {}, "Note", note),
          selfApproval ? h("div", { class: "small muted" }, "You proposed this: ask someone else to approve it.") : null,
          r.status === "proposed" && !can("reviewer") ? h("div", { class: "small muted" }, "A reviewer approves or rejects it.") : null,
          h("div", { class: "buttons" },
            r.status === "proposed" && can("reviewer") ? [decide("approved", "Approve", "good"), decide("rejected", "Reject", "bad")] : null,
            ["proposed", "approved"].includes(r.status) ? decide("cancelled", "Cancel") : null,
            ["approved", "rejected"].includes(r.status) ? decide("proposed", "Reopen") : null),
          say),
        h("h2", {}, "History"),
        h("div", { class: "panel" }, h("div", { class: "thread" }, r.events.map((e) => h("div", { class: `msg ${e.kind}` },
          h("div", { class: "who" }, e.kind === "comment" ? "" : [changeBadge(e.kind), " "], e.author || "someone", " · ", fmtDate(e.created_at)),
          e.body ? h("p", {}, e.body) : null))),
          (() => {
            const area = h("textarea", { placeholder: "Comment" });
            return h("div", { class: "form" }, area, h("div", {}, h("button", { disabled: readOnly, onclick: () => post(`/api/changes/${id}/comments`, { body: area.value }) }, "Comment")));
          })()))),
  );
}

async function editModelView(id, params) {
  const m = await api(`/api/models/${id}`);
  const details = m.objects.filter((o) => o.kind === "measure" && o.in_model);
  const edits = new Map();
  const only = params.get("only") || "";
  const out = h("div");
  const track = (o, field, input, original) => {
    const read = () => (input.type === "checkbox" ? String(input.checked) : input.value.trim());
    input.addEventListener(input.type === "checkbox" ? "change" : "input", () => {
      const key = `${o.object_key}|${field}`;
      if (read() === original) edits.delete(key); else edits.set(key, { op: "set", object_key: o.object_key, field, value: input.type === "checkbox" ? input.checked : input.value });
      input.classList.toggle("changed", read() !== original);
      count.textContent = edits.size ? `${edits.size} edit${edits.size === 1 ? "" : "s"}` : "";
    });
    return input;
  };
  const count = h("span", { class: "muted small" });
  const visible = details.filter((d) => !only || (only === "description" ? !d.description : only === "format" ? !d.format_string : only === "folder" ? !d.display_folder : true));
  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/" }, "Models"), " › ", h("a", { href: `#/model/${id}` }, m.name), " › edit"),
    h("h1", {}, `Describe and organise: ${m.name}`),
    h("p", { class: "sub" }, "Change any cell, then preview. Nothing changes in the model until the change is approved and applied in Desktop."),
    h("div", { class: "bar" }, "Show: ",
      [["", "every measure"], ["description", "no description"], ["format", "no format string"], ["folder", "no folder"]].map(([v, t], i) =>
        [i ? " · " : "", h("a", { href: `#/model/${id}/edit${v ? `?only=${v}` : ""}` }, only === v ? h("b", {}, t) : t)])),
    h("div", { class: "table-wrap" }, h("table", { class: "edit-grid" },
      h("thead", {}, h("tr", {}, ["Measure", "Description", "Display folder", "Format string", "Hidden"].map((x) => h("th", {}, x)))),
      h("tbody", {}, visible.map((d) => h("tr", {},
        h("td", {}, h("a", { href: `#/object/${d.id}` }, d.name), h("div", { class: "muted small" }, d.table_name)),
        h("td", {}, track(d, "description", h("input", { value: d.description || "" }), (d.description || "").trim())),
        h("td", {}, track(d, "display_folder", h("input", { value: d.display_folder || "" }), (d.display_folder || "").trim())),
        h("td", {}, track(d, "format_string", h("input", { value: d.format_string || "", placeholder: "#,0.00" }), (d.format_string || "").trim())),
        h("td", {}, track(d, "is_hidden", h("input", { type: "checkbox", checked: !!d.is_hidden }), String(!!d.is_hidden)))))))),
    h("div", { class: "bar" }, h("button", { class: "primary", onclick: () => out.replaceChildren(proposePanel(Number(id), [...edits.values()], "")) }, "Preview the change"), count),
    out,
  );
}

function objectActions(o) {
  if (o.kind !== "measure" || !o.in_model || !o.is_current) return null;
  const out = h("div");
  const newName = h("input", { placeholder: "new name", value: o.name });
  return [h("h2", {}, "Change it"),
    h("div", { class: "panel form" },
      h("label", {}, "Rename to", newName),
      h("div", { class: "buttons" },
        h("button", { onclick: () => out.replaceChildren(proposePanel(o.model_id, [{ op: "rename", object_key: o.object_key, value: newName.value }], "")) }, "Preview rename"),
        h("button", { class: "bad", onclick: () => out.replaceChildren(proposePanel(o.model_id, [{ op: "remove", object_key: o.object_key }], "")) }, "Preview removal")),
      out)];
}

function cleanupProposals(rows) {
  const byModel = new Map();
  for (const r of rows) if (r.object_id && r.kind === "measure") {
    if (!byModel.has(r.model_id)) byModel.set(r.model_id, { name: r.model_name, items: [] });
    byModel.get(r.model_id).items.push({ op: "remove", object_key: r.object_key });
  }
  if (!byModel.size) return null;
  const out = h("div");
  return [h("h2", {}, "Turn it into changes"),
    h("div", { class: "bar" }, [...byModel.entries()].map(([modelId, g]) =>
      h("button", { onclick: () => out.replaceChildren(proposePanel(modelId, g.items, `Remove ${g.items.length} measure${g.items.length === 1 ? "" : "s"} marked for removal`)) },
        `Remove ${g.items.length} from ${g.name}…`))),
    out];
}


// -- milestone 5: sign-in, account, administration -------------------------------------

const ROLE_TEXT = { viewer: "reads everything", author: "notes, comments, proposes and applies changes",
  reviewer: "approves versions and changes, exceptions, rules", admin: "accounts, roles, scans, audit log" };

function updateSessionNav() {
  const account = document.getElementById("accountNav");
  const admin = document.getElementById("adminNav");
  const user = session.user;
  account.hidden = !(session.mode !== "none" && user);
  if (!account.hidden) account.replaceChildren(user.display_name || user.username, h("span", { class: "role" }, user.role));
  admin.hidden = !(user && user.role === "admin");
  const out = !session.signed_in;
  document.querySelector("header nav").hidden = out;
  document.getElementById("searchForm").hidden = out;
}

async function signinView() {
  if (session.mode === "iis" || session.mode === "windows") {
    show(h("div", { class: "signin" }, h("h1", {}, "Sign in"),
      h("p", {}, "This workspace uses Windows sign-in. Open it at the address your team was given, from a PC signed in to the company network."),
      h("p", { class: "muted small" }, "If that is where you are, ask an admin whether your account is enabled.")));
    return;
  }
  const user = h("input", { autocomplete: "username", placeholder: "user name" });
  const password = h("input", { type: "password", autocomplete: "current-password", placeholder: "password" });
  const say = h("p", { class: "err small" });
  const go = async (event) => {
    event.preventDefault();
    try {
      await api("/api/login", { username: user.value, password: password.value });
      session = await api("/api/session");
      summaryCache = null;
      location.hash = "#/";
      route();
    } catch (err) {
      say.textContent = err.message;
      password.value = "";
    }
  };
  show(h("form", { class: "signin panel form", onsubmit: go }, h("h1", {}, "Sign in"),
    h("label", {}, "User name", user), h("label", {}, "Password", password),
    h("div", {}, h("button", { class: "primary", type: "submit" }, "Sign in")), say));
  user.focus();
}

async function accountView() {
  const data = await api("/api/account");
  const user = data.user;
  const tokenBox = h("div");
  const name = h("input", { placeholder: "what it is for, e.g. companion on my laptop" });
  const say = h("span", { class: "small" });
  const createToken = async () => {
    const made = await api("/api/account/tokens", { name: name.value || "companion" });
    fill(tokenBox, h("p", {}, "Your new token. Copy it now: it is not shown again."), h("div", { class: "token" }, made.token),
      h("p", { class: "muted small" }, "On your PC, in the companion's folder: powershell -ExecutionPolicy Bypass -File .\\install-companion.ps1 -Token <the token>"));
    accountTokens(await api("/api/account"));
  };
  const list = h("div");
  const accountTokens = (d) => fill(list, table([
    { label: "Token", cell: (t) => h("code", {}, `${t.prefix}…`) },
    { label: "For", cell: (t) => t.name },
    { label: "Created", cell: (t) => fmtDate(t.created_at) },
    { label: "Last used", cell: (t) => (t.last_used_at ? fmtDate(t.last_used_at) : "never") },
    { label: "", cell: (t) => h("button", { onclick: async () => { await api("/api/account/tokens/revoke", { prefix: t.prefix }); accountTokens(await api("/api/account")); } }, "Revoke") },
  ], d.tokens, "No tokens."));
  accountTokens(data);
  const old = h("input", { type: "password", autocomplete: "current-password" });
  const fresh = h("input", { type: "password", autocomplete: "new-password" });
  show(
    h("h1", {}, user.display_name || user.username),
    h("p", { class: "sub" }, `${user.username} · ${user.role}: ${ROLE_TEXT[user.role] || ""}`),
    h("div", { class: "grid2" },
      h("div", {},
        h("h2", {}, "Tokens for the Desktop companion and scripts"),
        h("p", { class: "muted small" }, "A token acts as you. Keep it on your own PC, and revoke it when the PC goes."),
        list,
        h("div", { class: "panel form" }, h("label", {}, "New token", name), h("div", {}, h("button", { class: "primary", onclick: createToken }, "Create token")), tokenBox)),
      h("div", {},
        data.mode === "local" ? [h("h2", {}, "Password"), h("div", { class: "panel form" },
          h("label", {}, "Current password", old), h("label", {}, "New password (10 or more characters)", fresh),
          h("div", {}, h("button", { onclick: async () => {
            try { await api("/api/account/password", { old_password: old.value, new_password: fresh.value }); say.className = "saved"; say.textContent = "Changed. Sign in again on your other devices."; }
            catch (err) { say.className = "err small"; say.textContent = err.message; }
            old.value = fresh.value = "";
          } }, "Change password")), say),
        h("h2", {}, "Sign out"), h("button", { onclick: async () => { await api("/api/logout", {}); session = await api("/api/session"); location.hash = "#/signin"; route(); } }, "Sign out")] : null)),
  );
}

async function adminView(params) {
  const [users, log] = await Promise.all([api("/api/admin/users"), api(`/api/admin/audit?limit=100&user=${encodeURIComponent(params.get("user") || "")}`)]);
  const say = h("span", { class: "small" });
  const change = async (body) => {
    try { await api("/api/admin/users", body); await adminView(params); }
    catch (err) { say.className = "err small"; say.textContent = err.message; }
  };
  const newUser = h("input", { placeholder: session.mode === "local" ? "user name" : "DOMAIN\\user" });
  const newName = h("input", { placeholder: "display name (optional)" });
  const newRole = h("select", {}, ["viewer", "author", "reviewer", "admin"].map((r) => h("option", { value: r }, r)));
  const newPassword = h("input", { type: "password", placeholder: "first password (10+ characters)", autocomplete: "new-password" });
  show(
    h("h1", {}, "Admin"),
    h("p", { class: "sub" }, session.mode === "none" ? "This workspace has no sign-in, so these accounts are used only by tokens and other sign-in modes."
      : session.mode === "local" ? "People sign in with workspace passwords." : "People sign in with Windows; their account is made on their first visit, as a viewer."),
    h("h2", {}, "People"),
    table([
      { label: "User", cell: (u) => [h("div", {}, u.username), u.display_name ? h("div", { class: "muted small" }, u.display_name) : null] },
      { label: "Role", cell: (u) => h("select", { onchange: (e) => change({ username: u.username, role: e.target.value }) },
        ["viewer", "author", "reviewer", "admin"].map((r) => h("option", { value: r, selected: r === u.role }, r))) },
      { label: "Last seen", cell: (u) => (u.last_seen_at ? fmtDate(u.last_seen_at) : "never") },
      { label: "Tokens", num: true, cell: (u) => u.tokens },
      { label: "", cell: (u) => h("button", { onclick: () => change({ username: u.username, disabled: !u.disabled }) }, u.disabled ? "Enable" : "Disable") },
    ], users, "No accounts yet."),
    say,
    h("div", { class: "panel form" }, h("b", {}, "Add someone"), newUser, newName, newRole, session.mode === "local" ? newPassword : null,
      h("div", {}, h("button", { class: "primary", onclick: () => change({ create: true, username: newUser.value, display_name: newName.value, role: newRole.value, password: newPassword.value || null }) }, "Add"))),
    h("h2", {}, "Audit log"),
    table([
      { label: "When", cell: (r) => fmtDate(r.at) },
      { label: "Who", cell: (r) => h("a", { href: `#/admin?user=${encodeURIComponent(r.username)}` }, r.username || "?") },
      { label: "Did", cell: (r) => r.action },
      { label: "On", cell: (r) => r.target },
      { label: "From", cell: (r) => r.address },
      { label: "Detail", cell: (r) => h("span", { class: "small muted" }, (r.detail || "").slice(0, 120)) },
    ], log, "Nothing yet."),
  );
}


// -- governance G2: the metric glossary --------------------------------------------------

const METRIC_STATUS = { draft: "draft", certified: "certified", deprecated: "deprecated" };
const COPY = { defines: "defines it", same: "same DAX", variant: "accepted variant", drift: "drift" };
const metricBadge = (s) => h("span", { class: `badge metric-${s}` }, METRIC_STATUS[s] || s);
const copyBadge = (s) => h("span", { class: `badge copy-${s}` }, COPY[s] || s);

// a small line diff for two DAX texts (longest common subsequence of lines)
function lineDiff(a, b) {
  const x = (a || "").split("\n"), y = (b || "").split("\n");
  const n = x.length, m = y.length;
  const L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const lines = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { lines.push({ op: " ", text: x[i] }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) lines.push({ op: "-", text: x[i++] });
    else lines.push({ op: "+", text: y[j++] });
  }
  while (i < n) lines.push({ op: "-", text: x[i++] });
  while (j < m) lines.push({ op: "+", text: y[j++] });
  return { lines };
}

async function metricsView() {
  const data = await api("/api/metrics");
  const ms = data.metrics;
  const drift = ms.reduce((a, m) => a + m.counts.drift, 0);
  show(
    h("h1", {}, "Metrics"),
    h("p", { class: "sub" }, "One agreed definition per metric, and every copy of it across your files. Copies that drift from the agreed DAX count against each model's standards score."),
    h("div", { class: "tiles" },
      tile(ms.filter((m) => m.status === "certified").length, "certified metrics"),
      tile(ms.filter((m) => m.status === "draft").length, "drafts waiting for a reviewer"),
      tile(drift, "copies drifting", drift > 0),
      tile(ms.filter((m) => m.source === "changed").length, "changed at the source", ms.some((m) => m.source === "changed")),
      tile(data.candidates.length, "measures copied across files, not yet metrics")),
    h("h2", {}, "Glossary"),
    table([
      { label: "Metric", cell: (m) => [h("a", { href: `#/metric/${m.id}` }, m.name), m.aliases.length ? h("div", { class: "muted small" }, `also ${m.aliases.join(", ")}`) : null] },
      { label: "Status", cell: (m) => [metricBadge(m.status), m.source === "changed" ? h("div", { class: "err small" }, "changed at the source") : m.source === "gone" ? h("div", { class: "err small" }, "source measure gone") : null] },
      { label: "Owner", cell: (m) => m.owner },
      { label: "Defined in", cell: (m) => (m.model_name ? h("a", { href: `#/model/${m.model_id}` }, m.model_name) : "") },
      { label: "Models", num: true, cell: (m) => m.models },
      { label: "Same", num: true, cell: (m) => m.counts.same },
      { label: "Variants", num: true, cell: (m) => m.counts.variant },
      { label: "Drift", num: true, cell: (m) => (m.counts.drift ? h("b", { class: "err" }, m.counts.drift) : "0") },
      { label: "Definition", cell: (m) => h("span", { class: "small" }, (m.definition || "").slice(0, 120)) },
    ], ms, "No metrics yet. Open a measure and choose “Make it an agreed metric”, starting with the ones copied across files below."),
    h("h2", {}, `Copied across files (${data.candidates.length})`),
    h("p", { class: "muted small" }, "Measures with the same name in two or more models. Open the copy that is right and make it the agreed metric; the others are then checked against it."),
    table([
      { label: "Measure", cell: (c) => h("b", {}, c.name) },
      { label: "Models", num: true, cell: (c) => c.models },
      { label: "Different DAX", num: true, cell: (c) => (c.versions > 1 ? h("b", { class: "err" }, c.versions) : "1 (all the same)") },
      { label: "Copies", cell: (c) => h("div", { class: "chips" }, c.copies.map((x) => h("a", { class: "chip", href: `#/object/${x.object_id}` }, x.model_name))) },
    ], data.candidates, "No measure name appears in more than one model."),
  );
}

async function metricView(id) {
  const m = await api(`/api/metrics/${id}`);
  const by = h("input", { value: me(), placeholder: "your name" });
  const note = h("input", { placeholder: "note (optional)" });
  const say = h("span", { class: "small" });
  const post = async (path, body) => {
    setMe(by.value);
    try { await api(path, { ...body, by: by.value }); await metricView(id); }
    catch (err) { say.className = "err small"; say.textContent = err.message; }
  };
  const act = (action, text, cls, extra) => h("button", { class: cls || "", onclick: () => post(`/api/metrics/${id}`, { action, note: note.value, ...(extra || {}) }) }, text);
  const name = h("input", { value: m.name });
  const aliases = h("input", { value: m.aliases.join(", "), placeholder: "other names it goes by, separated by commas" });
  const definition = h("textarea", {}, m.definition);
  const owner = h("input", { value: m.owner, placeholder: "who decides what it means" });
  const src = m.source;
  const copyRow = (c) => {
    const reason = h("input", { placeholder: "why this copy may differ" });
    const extra = [];
    if (c.status === "drift") {
      extra.push(h("details", {}, h("summary", { class: "small" }, "How it differs from the agreed DAX"), diffBlock(lineDiff(m.expression, c.expression))));
      if (can("reviewer")) extra.push(h("details", {}, h("summary", { class: "small" }, "Accept it as a variant, or make it the definition"),
        h("div", { class: "form" }, reason, h("div", { class: "buttons" },
          h("button", { onclick: () => post(`/api/metrics/${id}/variants`, { model_id: c.model_id, object_key: c.object_key, reason: reason.value }) }, "Accept as variant"),
          h("button", { onclick: () => post(`/api/metrics/${id}`, { action: "define", model_id: c.model_id, object_key: c.object_key }) }, "Make this copy the definition")))));
    }
    if (c.status === "variant" && c.variant) extra.push(h("div", { class: "small muted" }, `Accepted by ${c.variant.created_by || "someone"}: ${c.variant.reason}  `,
      can("reviewer") ? h("button", { onclick: () => post(`/api/metrics/${id}/variants`, { model_id: c.model_id, object_key: c.object_key, remove: true }) }, "Withdraw") : null));
    return h("div", { class: "change" },
      h("div", { class: "change-head" }, copyBadge(c.status), h("a", { href: `#/object/${c.id}` }, h("b", {}, c.name)),
        h("span", { class: "muted small" }, ` in ${c.model_name}${c.table_name ? ` · ${c.table_name}` : ""}`), h("span", { class: "grow" }),
        c.usage_status ? badge(c.usage_status) : null),
      extra);
  };
  show(
    h("div", { class: "crumbs" }, h("a", { href: "#/metrics" }, "Metrics"), " › ", m.name),
    h("h1", {}, m.name, " ", metricBadge(m.status)),
    h("p", { class: "sub" }, m.owner ? `Owner: ${m.owner}` : "No owner yet", m.aliases.length ? `  ·  also called ${m.aliases.join(", ")}` : "",
      m.certified_at ? `  ·  certified by ${m.certified_by || "someone"}, ${fmtDate(m.certified_at)}` : ""),
    h("div", { class: "grid2" },
      h("div", {},
        m.definition ? h("div", { class: "panel" }, h("b", {}, "What it means"), h("p", {}, m.definition)) : h("div", { class: "notice" }, "No business definition yet: say what it means, in words a report reader understands."),
        src.state === "changed" ? h("div", { class: "notice bad" }, h("b", {}, "The defining measure changed since it was certified"),
          h("p", { class: "small" }, `In ${m.model_name}, the measure now says something else. Re-certify to make the new DAX the agreed one, or put it back. Until then it is a finding in that model.`),
          diffBlock(lineDiff(m.expression, src.expression)),
          can("reviewer") ? h("div", { class: "buttons" }, act("certify", "Re-certify with the new DAX", "good")) : null) : null,
        src.state === "gone" ? h("div", { class: "notice bad" }, h("b", {}, "The defining measure is no longer in its model"),
          h("p", { class: "small" }, "Make one of the copies below the definition, or deprecate the metric.")) : null,
        h("h2", {}, m.status === "certified" ? "Agreed DAX" : "DAX (agreed once certified)"),
        h("pre", {}, m.expression || "(empty)"),
        h("div", { class: "muted small" }, m.model_name ? ["Defined by the measure in ", h("a", { href: `#/model/${m.model_id}` }, m.model_name)] : ""),
        h("h2", {}, `Every copy (${m.copies.length})`),
        m.copies.length ? m.copies.map(copyRow) : h("p", { class: "muted" }, "No measure with this name in the current versions.")),
      h("div", {},
        h("h2", {}, "Decide"),
        h("div", { class: "panel form" },
          h("label", {}, "Your name", by), h("label", {}, "Note", note),
          h("div", { class: "buttons" },
            m.status === "draft" && can("reviewer") ? act("certify", "Certify", "good") : null,
            m.status !== "deprecated" && can("reviewer") ? act("deprecate", "Deprecate", "bad") : null,
            m.status === "deprecated" && can("reviewer") ? act("certify", "Certify again", "good") : null,
            m.status === "draft" ? act("delete", "Delete draft") : null),
          m.status === "draft" && !can("reviewer") ? h("div", { class: "muted small" }, "A reviewer certifies it.") : null,
          say),
        h("h2", {}, "Edit"),
        h("div", { class: "panel form" },
          h("label", {}, "Name", name), h("label", {}, "Also called", aliases), h("label", {}, "What it means", definition), h("label", {}, "Owner", owner),
          h("div", {}, h("button", { class: "primary", disabled: m.status !== "draft" && !can("reviewer"),
            onclick: () => post(`/api/metrics/${id}`, { action: "update", name: name.value, aliases: aliases.value, definition: definition.value, owner: owner.value }) }, "Save")),
          m.status !== "draft" ? h("div", { class: "muted small" }, "A certified metric is edited by a reviewer.") : null))),
  );
  if (m.status === "deleted") location.hash = "#/metrics";
}

function metricPanel(o) {
  if (o.kind !== "measure" || !o.in_model || !o.is_current) return null;
  const mt = o.metric;
  if (mt) {
    return [h("h2", {}, "Metric"),
      h("div", { class: "panel" },
        h("div", {}, h("a", { href: `#/metric/${mt.id}` }, h("b", {}, mt.name)), "  ", metricBadge(mt.status), "  ", copyBadge(mt.copy)),
        mt.owner ? h("div", { class: "small muted" }, `Owner: ${mt.owner}`) : null,
        mt.definition ? h("p", { class: "small" }, mt.definition) : null,
        mt.copy === "drift" ? [h("div", { class: "small err" }, "This copy differs from the agreed DAX:"), diffBlock(lineDiff(mt.expression, o.expression))] : null,
        mt.variant ? h("div", { class: "small muted" }, `Accepted variant: ${mt.variant.reason}`) : null)];
  }
  const definition = h("textarea", { placeholder: "What it means, in words a report reader understands" });
  const owner = h("input", { placeholder: "who decides what it means" });
  const say = h("span", { class: "small" });
  return [h("h2", {}, "Metric"),
    h("details", { class: "panel" }, h("summary", {}, "Make it an agreed metric"),
      h("div", { class: "form" },
        h("p", { class: "small muted" }, "Its DAX becomes the definition other files are checked against, once a reviewer certifies it."),
        h("label", {}, "What it means", definition), h("label", {}, "Owner", owner),
        h("div", {}, h("button", { class: "primary", onclick: async () => {
          try {
            const made = await api("/api/metrics", { model_id: o.model_id, object_key: o.object_key, definition: definition.value, owner: owner.value, by: me() });
            location.hash = `#/metric/${made.id}`;
          } catch (err) { say.className = "err small"; say.textContent = err.message; }
        } }, "Make metric")), say))];
}

// -- routing ------------------------------------------------------------------

async function route() {
  const [path, query] = (location.hash.replace(/^#/, "") || "/").split("?");
  const params = new URLSearchParams(query || "");
  try {
    session = await api("/api/session");
  } catch (err) { /* shown below */ }
  updateSessionNav();
  if (!session.signed_in) {
    await signinView();
    return;
  }
  if (path === "/signin") { location.hash = "#/"; return; }
  if (path !== "/search") document.getElementById("q").value = "";
  try {
    if (!summaryCache) summaryCache = await api("/api/summary");
    updateReviewNav();
    updateChangesNav();
    let match;
    if ((match = path.match(/^\/model\/(\d+)$/))) await modelView(match[1], params);
    else if ((match = path.match(/^\/object\/(\d+)$/))) await objectView(match[1]);
    else if (path === "/search") await searchView(params);
    else if (path === "/decisions") await decisionsView(params);
    else if (path === "/review") await reviewQueueView(params);
    else if (path === "/standards") await standardsView(params);
    else if (path === "/changes") await changesView(params);
    else if (path === "/metrics") await metricsView();
    else if ((match = path.match(/^\/metric\/(\d+)$/))) await metricView(match[1]);
    else if (path === "/account") await accountView();
    else if (path === "/admin") await adminView(params);
    else if ((match = path.match(/^\/change\/(\d+)$/))) await changeView(match[1]);
    else if ((match = path.match(/^\/model\/(\d+)\/edit$/))) await editModelView(match[1], params);
    else if (path === "/standards/owner") await ownerView(params);
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
