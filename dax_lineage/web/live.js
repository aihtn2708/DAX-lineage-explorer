// Live lineage: shows what the measure just edited in Power BI Desktop reads, what reads it, and the
// visuals that show it. Updates arrive from the local daxlin live server as server-sent events.
"use strict";

const $ = (id) => document.getElementById(id);
const S = { token: "", models: [], events: [], source: "", shown: null, follow: true, seq: 0 };

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
const label = (x) => (x.kind === "column" ? `${x.table}[${x.name}]` : `[${x.name}]`);
const model = () => S.models.find((m) => m.source === S.source) || S.models[0];

async function api(path, body) {
  const options = body ? { method: "POST", headers: { "Content-Type": "application/json", "X-Live-Token": S.token }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, options);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// -- the edited object ----------------------------------------------------------------------

const CHANGE = { edited: "DAX changed", renamed: "renamed", added: "new", removed: "removed", moved: "moved to another table",
  format: "format changed", description: "description changed", folder: "folder changed", hidden: "shown / hidden",
  broke: "stopped working", fixed: "works again" };

function nameLink(x) {
  if (!x.exists && x.exists !== undefined) return h("span", { class: "missing", title: "not in the model" }, label(x));
  return h("a", { href: "#", onclick: (e) => { e.preventDefault(); explore(x.id); } }, label(x));
}

function showFocus(f, event) {
  const view = $("view");
  view.replaceChildren();
  if (!f || !f.found && !f.change) {
    view.append(h("p", { class: "muted" }, "That is not in the model any more."));
    return;
  }
  const head = h("div", { class: "focus-head" },
    h("span", { class: `kind ${f.kind}` }, f.kind === "column" ? "calculated column" : "measure"),
    h("h1", {}, f.name), f.table ? h("span", { class: "muted" }, ` in ${f.table}`) : null,
    f.change ? h("span", { class: `change ${f.change}` }, (f.parts || [f.change]).map((p) => CHANGE[p] || p).join(", ")) : null,
    f.oldName && f.oldName !== f.name ? h("span", { class: "muted" }, ` (was [${f.oldName}])`) : null,
    h("span", { class: "grow" }),
    event ? h("time", { datetime: event.at }, clock(event.at)) : h("span", { class: "muted" }, "looked up"));
  view.append(head);

  const warnings = f.warnings || [];
  if (warnings.length) {
    view.append(h("ul", { class: "warnings" }, warnings.map((w) => h("li", { class: w.level },
      h("span", { class: "icon", "aria-hidden": "true" }, w.level === "error" ? "✗" : w.level === "warning" ? "!" : "i"), w.text))));
  } else if (f.change && f.change !== "removed") {
    view.append(h("ul", { class: "warnings" }, h("li", { class: "ok" }, h("span", { class: "icon", "aria-hidden": "true" }, "✓"),
      "Nothing that reads it broke.")));
  }

  if (f.before !== undefined && f.change && (f.before !== f.after)) view.append(diffBlock(f.before, f.after));
  else view.append(h("pre", { class: "dax" }, f.after ?? f.expression ?? ""));

  const upstream = f.upstream || [], downstream = f.downstream || [], visuals = f.visuals || [];
  view.append(h("div", { class: "columns" },
    h("section", {}, h("h2", {}, "Reads ", h("span", { class: "count" }, upstream.length)),
      upstream.length ? h("ul", { class: "tree" }, upstream.map((u) => h("li", { class: `lvl${Math.min(u.level, 4)}` },
        nameLink(u), u.level > 1 ? h("span", { class: "muted small" }, ` · ${u.level} steps`) : null)))
        : h("p", { class: "muted" }, "Nothing: it reads no measure or column.")),
    h("section", {}, h("h2", {}, "Read by ", h("span", { class: "count" }, downstream.length)),
      downstream.length ? h("ul", { class: "tree" }, downstream.map((d) => h("li", { class: `lvl${Math.min(d.level, 4)}${d.newError ? " broke" : ""}` },
        nameLink(d), d.via ? h("span", { class: "muted small" }, ` via [${d.via}]`) : null,
        d.error ? h("div", { class: "err small" }, (d.newError ? "Stopped working: " : "Broken: ") + d.error) : null)))
        : h("p", { class: "muted" }, "No measure reads it.")),
    h("section", {}, h("h2", {}, "On visuals ", h("span", { class: "count" }, f.hasReport ? visuals.length : "–")),
      !f.hasReport ? h("p", { class: "muted" }, "Visuals come from the saved report file: see the bottom of the page.")
        : visuals.length ? h("ul", { class: "visuals" }, visuals.map((v) => h("li", {},
          h("span", { class: "page" }, v.page), " › ", h("b", {}, v.visual),
          h("span", { class: "muted small" }, v.direct ? " · shows it" : ` · through [${v.via}]`))))
          : h("p", { class: "muted" }, "No visual in the saved report shows it."))));
  S.shown = { event, focus: f };
}

// line-by-line comparison of the DAX before and after
function diffBlock(before, after) {
  const a = (before || "").split("\n"), b = (after || "").split("\n");
  if (!before) return h("pre", { class: "dax" }, after);
  if (!after) return h("pre", { class: "dax removed" }, before);
  const n = a.length, m = b.length, L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const rows = [];
  let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) { rows.push(["same", b[j]]); i++; j++; }
    else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { rows.push(["add", b[j]]); j++; }
    else { rows.push(["del", a[i]]); i++; }
  }
  return h("pre", { class: "dax diff" }, rows.map(([k, line]) => h("div", { class: k }, h("span", { class: "sign", "aria-hidden": "true" }, k === "add" ? "+" : k === "del" ? "−" : " "), line || " ")));
}

// -- model, history, look-up ----------------------------------------------------------------

function overview() {
  const m = model();
  const view = $("view");
  view.replaceChildren();
  if (!m) {
    view.append(h("div", { class: "empty" },
      h("h1", {}, "Waiting for Power BI Desktop"),
      h("ol", {},
        h("li", {}, "Open your .pbix in Power BI Desktop."),
        h("li", {}, "On the External Tools ribbon, click ", h("b", {}, "Live lineage"), "."),
        h("li", {}, "Keep this page on your second screen. Edit a measure and press Enter: it shows up here.")),
      h("p", { class: "muted" }, "Not on the ribbon yet? Run live_tool\\install-live.ps1 once, then restart Desktop (see the README).")));
    return;
  }
  view.append(h("div", { class: "empty" },
    h("h1", {}, `Watching ${m.name}`),
    h("p", {}, `${plural(m.measures, "measure")}, ${plural(m.columns, "column")}${m.visuals !== null ? `, ${plural(m.visuals, "visual")} in the saved report` : ""}.`),
    m.broken.length ? h("p", { class: "err" }, `Already not working: ${m.broken.join(", ")}`) : null,
    h("p", {}, "Edit a measure in Power BI Desktop and press Enter (or click the tick): what it reads, what reads it and the visuals that show it appear here."),
    h("p", { class: "muted" }, "Or look one up with the box at the top.")));
  S.shown = null;
}

function renderModel() {
  const m = model();
  const box = $("model");
  box.replaceChildren();
  if (!m) { box.append(h("span", { class: "dot off" }), "No model yet"); }
  else {
    if (S.models.length > 1) {
      box.append(h("select", { "aria-label": "Model", onchange: (e) => { S.source = e.target.value; renderModel(); overview(); renderHistory(); } },
        S.models.map((x) => h("option", { value: x.source, selected: x.source === m.source }, x.name))));
    } else box.append(h("span", { class: "dot on" }), h("b", {}, m.name));
    box.append(h("span", { class: "muted small" }, ` · ${plural(m.measures, "measure")} · updated ${clock(m.received)}`));
  }
  const foot = $("footer");
  foot.replaceChildren();
  if (m) {
    const input = h("input", { type: "text", placeholder: "C:\\…\\Report.pbix", "aria-label": "Saved report file", value: m.file || "" });
    const say = h("span", { class: "small" });
    foot.append(h("span", { class: "muted" }, m.reportNote || ""), " ",
      h("form", { class: "report", onsubmit: async (e) => {
        e.preventDefault();
        try { const st = await api("/api/report", { source: m.source, path: input.value }); apply(st); say.textContent = ""; }
        catch (err) { say.className = "err small"; say.textContent = err.message; }
      } }, h("label", {}, "Saved file for visuals ", input), h("button", { type: "submit" }, "Use"), say));
  }
}

function renderHistory() {
  const list = $("history");
  list.replaceChildren();
  const items = S.events.filter((e) => e.focus && e.source === (model() || {}).source).slice().reverse();
  if (!items.length) { list.append(h("li", { class: "muted" }, "None yet.")); return; }
  for (const e of items.slice(0, 40)) {
    const f = e.focus;
    const bad = (f.warnings || []).some((w) => w.level === "error");
    list.append(h("li", { class: S.shown && S.shown.event === e ? "current" : "" },
      h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); setFollow(false); showFocus(f, e); renderHistory(); } },
        h("span", { class: `mark ${bad ? "bad" : "good"}`, "aria-hidden": "true" }, bad ? "✗" : "✓"),
        h("b", {}, f.name), h("span", { class: "muted small" }, ` ${CHANGE[f.change] || f.change} · ${clock(e.at)}`))));
  }
}

async function explore(id) {
  const m = model();
  if (!m) return;
  setFollow(false);
  showFocus(await api(`/api/explore?source=${encodeURIComponent(m.source)}&id=${encodeURIComponent(id)}`), null);
  renderHistory();
}

function setFollow(on) {
  S.follow = on;
  const b = $("follow");
  b.setAttribute("aria-pressed", String(on));
  b.textContent = on ? "Following edits" : "Paused: follow edits";
}

function apply(state) {
  S.models = state.models;
  if (!S.models.some((m) => m.source === S.source)) S.source = S.models.length ? S.models[0].source : "";
  renderModel();
}

function onEvent(e) {
  S.events.push(e);
  S.seq = Math.max(S.seq, e.seq);
  const i = S.models.findIndex((m) => m.source === e.source);
  if (i >= 0) S.models[i] = e.model; else S.models.unshift(e.model);
  if (e.kind === "connected" && (!S.source || !S.models.some((m) => m.source === S.source))) S.source = e.source;
  if (e.focus && S.follow) {
    S.source = e.source;
    renderModel();
    showFocus(e.focus, e);
    document.title = `● ${e.focus.name}: live lineage`;
    $("view").classList.remove("flash"); void $("view").offsetWidth; $("view").classList.add("flash");
  } else {
    renderModel();
    if (!S.shown) overview();
  }
  renderHistory();
}

function listen() {
  const source = new EventSource(`/api/events?after=${S.seq}`);
  source.addEventListener("update", (msg) => onEvent(JSON.parse(msg.data)));
  source.onerror = () => { source.close(); setTimeout(listen, 2000); };
}

// look-up box
let timer = null;
$("q").addEventListener("input", () => {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const m = model(), text = $("q").value.trim(), hits = $("hits");
    hits.replaceChildren();
    if (!m || !text) { hits.hidden = true; return; }
    const found = await api(`/api/find?source=${encodeURIComponent(m.source)}&q=${encodeURIComponent(text)}`);
    for (const x of found) hits.append(h("li", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); hits.hidden = true; $("q").value = ""; explore(x.id); } }, label(x), h("span", { class: "muted small" }, x.kind === "measure" ? ` ${x.table}` : ""))));
    if (!found.length) hits.append(h("li", { class: "muted" }, "Nothing by that name."));
    hits.hidden = false;
  }, 150);
});
$("lookup").addEventListener("submit", (e) => { e.preventDefault(); const first = $("hits").querySelector("a"); if (first) first.click(); });
$("follow").addEventListener("click", () => {
  setFollow(!S.follow);
  if (S.follow) {
    const last = S.events.filter((e) => e.focus).pop();
    if (last) { S.source = last.source; renderModel(); showFocus(last.focus, last); } else overview();
    renderHistory();
    document.title = "Live lineage";
  }
});

(async function start() {
  const state = await api("/api/state");
  S.token = state.token;
  S.events = state.events;
  S.seq = state.seq;
  apply(state);
  const last = S.events.filter((e) => e.focus).pop();
  if (last) { S.source = last.source; renderModel(); showFocus(last.focus, last); } else overview();
  renderHistory();
  listen();
})();
