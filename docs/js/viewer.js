/*
 * The lineage viewer: layered graph, trace, detail panel.
 * Shared with dax_lineage/template.html; here it boots from data built in the
 * browser instead of data embedded by the Python CLI.
 * window.LineageViewer.mount(graph, meta)
 */
(function () {
"use strict";

let DATA = null;
let NODES = new Map();
let EDGES = [];
let OUT = new Map(), IN = new Map();
let BY_LABEL = new Map(), BY_BARE = new Map();

function setData(data) {
  DATA = data;
  NODES = new Map(DATA.nodes.map(n => [n.id, n]));
  EDGES = DATA.edges;
  OUT = new Map(); IN = new Map();
  for (const n of DATA.nodes) { OUT.set(n.id, []); IN.set(n.id, []); }
  for (const e of EDGES) {
    if (OUT.has(e.source)) OUT.get(e.source).push(e.target);
    if (IN.has(e.target)) IN.get(e.target).push(e.source);
  }
  BY_LABEL = new Map(); BY_BARE = new Map();
  for (const n of DATA.nodes) {
    BY_LABEL.set(n.label.toLowerCase(), n.id);
    const bare = n.name.toLowerCase();
    if (!BY_BARE.has(bare)) BY_BARE.set(bare, []);
    BY_BARE.get(bare).push(n.id);
  }
}

const KIND_COLOR = { measure: "var(--measure)", column: "var(--column)", table: "var(--table)" };
const NODE_W = 182, NODE_H = 30, GAP_X = 96, GAP_Y = 13;

const state = {
  selected: null,
  hover: null,
  query: "",
  table: "",
  sort: "model",
  collapsed: new Set(),
  showColumns: true,
  showTables: false,
  showHidden: true,
  showUnreferenced: false,
  focusOnly: false,
  view: { x: 40, y: 40, k: 1 },
  positions: new Map(),
  visible: new Set()
};

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function closure(id, map) {
  const seen = new Set([id]), stack = [id], out = [];
  while (stack.length) {
    for (const next of map.get(stack.pop()) || []) {
      if (!seen.has(next)) { seen.add(next); out.push(next); stack.push(next); }
    }
  }
  return out;
}
const upstreamOf = id => closure(id, OUT);
const downstreamOf = id => closure(id, IN);

/* ---------- header ---------- */

function renderHeader() {
  const s = DATA.stats;
  const issues = s.cycles.length + s.unresolvedReferences.length + s.missingObjects;
  const tiles = [
    ["Measures", s.measures], ["Columns", s.columns], ["Tables", s.tables],
    ["Dependencies", s.edges], ["Deepest chain", s.maxDepth],
    ["Issues", issues, issues > 0]
  ];
  $("stats").innerHTML = tiles.map(([label, value, alert]) =>
    `<div class="stat${alert ? " alert" : ""}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`).join("");
  $("subtitle").textContent = DATA.meta.subtitle;

  const bits = [];
  if (s.cycles.length) bits.push(`<b>${s.cycles.length} circular dependency chain(s)</b><ul>${
    s.cycles.slice(0, 4).map(c => `<li>${c.map(id => esc((NODES.get(id) || { label: id }).label)).join(" → ")}</li>`).join("")}</ul>`);
  if (s.missingObjects) bits.push(`<b>${s.missingObjects} reference(s) to objects not in the export</b>`);
  if (s.unresolvedReferences.length)
    bits.push(`${s.unresolvedReferences.length} unresolved name(s): ${s.unresolvedReferences.slice(0, 6).map(esc).join(", ")}`);
  if (!bits.length) bits.push(`Every reference resolved. ${esc(s.baseMeasures.length)} base measure(s) read the model directly.`);
  $("issues").innerHTML = bits.join("<br>");
}

/* ---------- sidebar list ---------- */

function listNodes() {
  const q = state.query.toLowerCase();
  return DATA.nodes.filter(n => {
    if (n.kind === "column" && !state.showColumns) return false;
    if (n.kind === "table" && !state.showTables) return false;
    if (n.isHidden && !state.showHidden) return false;
    if (state.table && n.table !== state.table && n.name !== state.table) return false;
    if (q && !(n.label.toLowerCase().includes(q) ||
               (n.description || "").toLowerCase().includes(q) ||
               (n.expression || "").toLowerCase().includes(q))) return false;
    return true;
  });
}

function depthMeta(n) {
  if (!n.depth) return "";
  const what = n.kind === "measure" ? "measure" : "column";
  const tip = `Chain depth ${n.depth}: the longest path from this ${what} down to the model's source columns is ${n.depth} step${n.depth === 1 ? "" : "s"}.` +
    (n.depth === 1 ? " It reads columns directly." : ` It is built on other ${n.kind === "measure" ? "measures" : "calculations"}.`);
  return `<span class="depth" title="${esc(tip)}">${n.depth}</span>`;
}

function itemRow(n, indent) {
  const used = (IN.get(n.id) || []).length;
  const meta = state.sort === "used"
    ? `<span class="depth" title="Read by ${used} other object${used === 1 ? "" : "s"}">${used}&times;</span>`
    : depthMeta(n);
  // inside a table group the table name is already the heading
  const label = indent && n.kind === "column" ? n.name : n.label;
  return `<button class="item${state.selected === n.id ? " sel" : ""}" data-id="${esc(n.id)}"
      title="${esc(n.label)}${n.description ? " — " + esc(n.description) : ""}" style="padding-left:${14 + indent * 14}px">
    <span class="dot" style="background:${KIND_COLOR[n.kind]}${n.exists ? "" : ";opacity:.4"}"></span>
    <span class="nm">${esc(label)}</span>
    ${n.isHidden ? `<span class="tag" title="Hidden in the model">hidden</span>` : ""}
    ${meta}
  </button>`;
}

const byKindThenName = (a, b) =>
  (a.kind === b.kind ? 0 : a.kind === "measure" ? -1 : b.kind === "measure" ? 1 : 0) ||
  a.name.localeCompare(b.name);

// Power BI's field list: Table > Display folder (nested with \) > measures, then columns
function renderModelTree(nodes) {
  const searching = !!state.query;
  const tables = new Map();
  const tableOf = n => n.kind === "table" ? n.name : (n.table || (n.exists ? "(no table)" : "(not in the export)"));
  for (const n of nodes) {
    const t = tableOf(n);
    if (!tables.has(t)) tables.set(t, { folders: new Map(), items: [], measures: 0, columns: 0 });
    const group = tables.get(t);
    if (n.kind === "measure") group.measures++;
    if (n.kind === "column") group.columns++;
    if (n.kind === "table") continue;
    const paths = (n.displayFolder || "").split(";").map(p => p.trim()).filter(Boolean);
    if (!paths.length) { group.items.push(n); continue; }
    for (const path of paths) {
      let level = group;
      for (const part of path.split("\\").map(p => p.trim()).filter(Boolean)) {
        if (!level.folders.has(part)) level.folders.set(part, { folders: new Map(), items: [] });
        level = level.folders.get(part);
      }
      level.items.push(n);
    }
  }
  const html = [];
  // top: the table itself, whose loose items the caller lists after its folders
  const folderHtml = (folder, key, depth, top) => {
    for (const [name, sub] of [...folder.folders.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const subKey = `${key}\\${name}`;
      const open = searching || !state.collapsed.has(subKey);
      html.push(`<button class="fgroup" data-key="${esc(subKey)}" style="padding-left:${14 + depth * 14}px" aria-expanded="${open}">
        <span class="caret">${open ? "▾" : "▸"}</span><span class="folder-icon" aria-hidden="true"></span>${esc(name)}</button>`);
      if (open) folderHtml(sub, subKey, depth + 1, false);
    }
    if (!top) for (const n of folder.items.sort(byKindThenName)) html.push(itemRow(n, depth));
  };
  const order = [...tables.keys()].sort((a, b) =>
    (a.startsWith("(") - b.startsWith("(")) ||
    (tables.get(b).measures > 0) - (tables.get(a).measures > 0) || a.localeCompare(b));
  for (const t of order) {
    const g = tables.get(t);
    const key = `t\\${t}`;
    const open = searching || !state.collapsed.has(key);
    const counts = [g.measures && `${g.measures} measure${g.measures === 1 ? "" : "s"}`,
                    g.columns && `${g.columns} column${g.columns === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
    html.push(`<button class="tgroup" data-key="${esc(key)}" aria-expanded="${open}">
      <span class="caret">${open ? "▾" : "▸"}</span><span class="table-icon" aria-hidden="true"></span>
      <span class="nm">${esc(t)}</span><span class="count">${counts}</span></button>`);
    if (!open) continue;
    folderHtml(g, key, 1, true);
    for (const n of g.items.sort(byKindThenName)) html.push(itemRow(n, 1));
  }
  return html.join("");
}

function renderList() {
  const nodes = listNodes();
  let body;
  if (state.sort === "model") {
    body = renderModelTree(nodes);
  } else if (state.sort === "folder") {
    // measures by display folder; columns by table, labelled so a folder and a
    // table that share a name never merge into one group
    const map = new Map();
    for (const n of nodes) {
      const key = n.kind === "measure"
        ? `0${n.displayFolder || "No display folder"}`
        : `1Columns of ${n.table || n.name}`;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(n);
    }
    body = [...map.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([key, items]) =>
      `<div class="group">${key[0] === "0" ? '<span class="folder-icon" aria-hidden="true"></span>' : ""}${esc(key.slice(1).replace(/\\/g, " \u203A "))}</div>` +
      items.sort(byKindThenName).map(n => itemRow(n, 0)).join("")).join("");
  } else {
    const score = {
      depth: n => -n.depth,
      used: n => -(IN.get(n.id) || []).length,
      name: n => n.name.toLowerCase()
    }[state.sort];
    const sorted = [...nodes].sort((a, b) => {
      const x = score(a), y = score(b);
      return x < y ? -1 : x > y ? 1 : a.name.localeCompare(b.name);
    });
    body = `<div class="group">${nodes.length} object${nodes.length === 1 ? "" : "s"}</div>` +
      sorted.map(n => itemRow(n, 0)).join("");
  }
  $("list").innerHTML = body || `<div class="empty">Nothing matches that search.</div>`;
}

/* ---------- graph layout ---------- */

function visibleNodes() {
  let ids = DATA.nodes.filter(n => {
    if (n.kind === "column" && !state.showColumns) return false;
    if (n.kind === "table" && !state.showTables) return false;
    if (n.isHidden && !state.showHidden) return false;
    if (!state.showUnreferenced && n.kind !== "measure" &&
        !(IN.get(n.id) || []).length && !(OUT.get(n.id) || []).length) return false;
    return true;
  }).map(n => n.id);

  if (state.focusOnly && state.selected) {
    const keep = new Set([state.selected, ...upstreamOf(state.selected), ...downstreamOf(state.selected)]);
    ids = ids.filter(id => keep.has(id));
  } else if (state.table) {
    const keep = new Set();
    for (const n of DATA.nodes) {
      if (n.table === state.table || n.name === state.table) {
        keep.add(n.id);
        for (const id of OUT.get(n.id) || []) keep.add(id);
      }
    }
    ids = ids.filter(id => keep.has(id));
  }
  return new Set(ids);
}

function layout() {
  const visible = visibleNodes();
  state.visible = visible;
  const layers = new Map();
  for (const id of visible) {
    const depth = NODES.get(id).depth;
    if (!layers.has(depth)) layers.set(depth, []);
    layers.get(depth).push(id);
  }
  const depths = [...layers.keys()].sort((a, b) => a - b);
  for (const d of depths) {
    layers.get(d).sort((a, b) => {
      const x = NODES.get(a), y = NODES.get(b);
      return (x.table || "").localeCompare(y.table || "") || x.name.localeCompare(y.name);
    });
  }

  // barycentre sweeps: pull each node towards the average row of its neighbours
  const index = new Map();
  const reindex = () => {
    index.clear();
    for (const d of depths) layers.get(d).forEach((id, i) => index.set(id, i));
  };
  reindex();
  for (let pass = 0; pass < 6; pass++) {
    const order = pass % 2 ? [...depths].reverse() : depths;
    for (const d of order) {
      const ids = layers.get(d);
      const bary = new Map(ids.map(id => {
        const neighbours = [...(OUT.get(id) || []), ...(IN.get(id) || [])]
          .filter(n => visible.has(n) && index.has(n));
        const mean = neighbours.length
          ? neighbours.reduce((sum, n) => sum + index.get(n), 0) / neighbours.length
          : index.get(id);
        return [id, mean];
      }));
      ids.sort((a, b) => bary.get(a) - bary.get(b) || index.get(a) - index.get(b));
      reindex();
    }
  }

  const tallest = Math.max(1, ...depths.map(d => layers.get(d).length));
  const height = tallest * (NODE_H + GAP_Y);
  const positions = new Map();
  depths.forEach((d, column) => {
    const ids = layers.get(d);
    const offset = (height - ids.length * (NODE_H + GAP_Y)) / 2;
    ids.forEach((id, row) => positions.set(id, {
      x: column * (NODE_W + GAP_X),
      y: offset + row * (NODE_H + GAP_Y)
    }));
  });
  state.positions = positions;
  state.extent = { w: depths.length * (NODE_W + GAP_X), h: height };
}

/* ---------- graph render ---------- */

function truncate(text, max) {
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

function edgePath(a, b) {
  // a = dependency (left), b = dependent (right)
  const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
  const x2 = b.x, y2 = b.y + NODE_H / 2;
  const dx = Math.max(28, (x2 - x1) * 0.5);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

function render() {
  layout();
  const { positions, visible, selected } = state;
  const up = selected ? new Set([selected, ...upstreamOf(selected)]) : null;
  const down = selected ? new Set([selected, ...downstreamOf(selected)]) : null;
  const related = selected ? new Set([...up, ...down]) : null;

  const edgeSvg = [];
  for (const e of EDGES) {
    if (!visible.has(e.source) || !visible.has(e.target)) continue;
    const a = positions.get(e.target), b = positions.get(e.source);
    if (!a || !b) continue;
    let cls = "edge";
    let marker = "arrow";
    if (selected) {
      if (up.has(e.source) && up.has(e.target)) { cls += " up"; marker = "arrow-up"; }
      else if (down.has(e.source) && down.has(e.target)) { cls += " down"; marker = "arrow-down"; }
      else cls += " dim";
    }
    if (e.kind === "relationship") cls += " rel";
    edgeSvg.push(`<path class="${cls}" d="${edgePath(a, b)}" marker-end="url(#${marker})"></path>`);
  }
  $("edges").innerHTML =
    `<defs>
       ${["arrow:var(--axis)", "arrow-up:var(--measure)", "arrow-down:var(--column)"].map(spec => {
          const [id, fill] = spec.split(":");
          return `<marker id="${id}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7"
                    orient="auto-start-reverse"><path d="M0,1 L7,4 L0,7 z" fill="${fill}"></path></marker>`;
       }).join("")}
     </defs>` + edgeSvg.join("");

  const nodeSvg = [];
  for (const id of visible) {
    const n = NODES.get(id), p = positions.get(id);
    if (!p) continue;
    const color = KIND_COLOR[n.kind];
    const radius = n.kind === "measure" ? 9 : n.kind === "table" ? 15 : 3;
    const dim = related && !related.has(id);
    const isSel = id === selected;
    const dash = !n.exists ? ' stroke-dasharray="4 3"' : n.isHidden ? ' stroke-dasharray="2 2"' : "";
    const stroke = !n.exists ? "var(--critical)" : n.state === "Error" ? "var(--critical)" : color;
    nodeSvg.push(`<g class="node${dim ? " dim" : ""}" data-id="${esc(id)}" transform="translate(${p.x},${p.y})">
      <title>${esc(n.label)}${n.description ? " — " + esc(n.description) : ""}</title>
      <rect width="${NODE_W}" height="${NODE_H}" rx="${radius}" ry="${radius}"
            fill="color-mix(in srgb, ${color} ${isSel ? 26 : 12}%, var(--surface-1))"
            stroke="${stroke}"${dash} stroke-width="${isSel ? 2.4 : 1.5}"></rect>
      <text x="11" y="${NODE_H / 2 + 4}" fill="var(--text-primary)">${esc(truncate(n.label, 27))}</text>
      ${n.depth ? `<text x="${NODE_W - 9}" y="${NODE_H / 2 + 4}" text-anchor="end"
            fill="var(--muted)" font-size="10">${n.depth}</text>` : ""}
    </g>`);
  }
  $("nodes").innerHTML = nodeSvg.join("");
  applyView();
  renderList();
  renderDetail();
}

function applyView() {
  const { x, y, k } = state.view;
  $("viewport").setAttribute("transform", `translate(${x},${y}) scale(${k})`);
}

function fit() {
  const svg = $("svg").getBoundingClientRect();
  if (!svg.width || !svg.height) { state.needsFit = true; return; }
  state.needsFit = false;
  const { w, h } = state.extent || { w: 1, h: 1 };
  const k = Math.min(1.1, Math.max(0.18, Math.min((svg.width - 80) / Math.max(w, 1),
                                                  (svg.height - 80) / Math.max(h, 1))));
  state.view = { k, x: (svg.width - w * k) / 2, y: (svg.height - h * k) / 2 };
  applyView();
}

/* ---------- DAX highlighting ---------- */

const DAX_KEYWORDS = new Set(["var", "return", "define", "measure", "evaluate", "order", "by",
  "start", "at", "asc", "desc", "in", "not", "and", "or", "true", "false"]);

function resolveRef(name, owner) {
  const bare = name.toLowerCase();
  if (owner) {
    const id = BY_LABEL.get(`${owner.toLowerCase()}[${bare}]`);
    if (id) return id;
  }
  if (BY_LABEL.has(`[${bare}]`)) return BY_LABEL.get(`[${bare}]`);
  const measure = "m:" + bare;
  if (NODES.has(measure)) return measure;
  const hits = (BY_BARE.get(bare) || []).filter(id => NODES.get(id).kind === "column");
  return hits.length === 1 ? hits[0] : null;
}

function highlightDax(text) {
  let out = "", i = 0, lastWord = null, lastOwner = null;
  const n = text.length;
  while (i < n) {
    const rest = text.slice(i);
    let match;
    if ((match = rest.match(/^(\/\/|--)[^\n]*/)) || (match = rest.match(/^\/\*[\s\S]*?(\*\/|$)/))) {
      out += `<span class="cmt">${esc(match[0])}</span>`; i += match[0].length; continue;
    }
    if (text[i] === '"') {
      const m = rest.match(/^"(?:[^"]|"")*"?/);
      out += `<span class="str">${esc(m[0])}</span>`; i += m[0].length; continue;
    }
    if (text[i] === "'") {
      const m = rest.match(/^'(?:[^']|'')*'?/);
      lastOwner = m[0].replace(/^'|'$/g, "").replace(/''/g, "'");
      out += esc(m[0]); i += m[0].length; continue;
    }
    if (text[i] === "[") {
      const m = rest.match(/^\[(?:[^\]]|\]\])*\]?/);
      const name = m[0].replace(/^\[|\]$/g, "").replace(/\]\]/g, "]");
      const target = resolveRef(name, lastOwner || lastWord);
      out += target
        ? `<span class="ref" data-goto="${esc(target)}">${esc(m[0])}</span>`
        : esc(m[0]);
      lastOwner = null; lastWord = null;
      i += m[0].length; continue;
    }
    if ((match = rest.match(/^[A-Za-z_À-￿][A-Za-z0-9_.À-￿]*/))) {
      const word = match[0];
      const isFunction = /^\s*\(/.test(rest.slice(word.length));
      if (DAX_KEYWORDS.has(word.toLowerCase())) { out += `<span class="kw">${esc(word)}</span>`; lastWord = null; }
      else if (isFunction) { out += `<span class="fn">${esc(word)}</span>`; lastWord = null; }
      else { out += esc(word); lastWord = word; }
      lastOwner = null;
      i += word.length; continue;
    }
    if ((match = rest.match(/^\d+(\.\d+)?/))) {
      out += `<span class="num">${esc(match[0])}</span>`; i += match[0].length; continue;
    }
    if (!/\s/.test(text[i])) { lastWord = null; }
    out += esc(text[i]); i += 1;
  }
  return out;
}

/* ---------- detail panel ---------- */

function nodeLink(id) {
  const n = NODES.get(id);
  if (!n) return "";
  return `<button class="link" data-id="${esc(id)}">
    <span class="dot" style="width:8px;height:8px;border-radius:2px;background:${KIND_COLOR[n.kind]}"></span>
    <span class="nm">${esc(n.label)}</span></button>`;
}

function renderDetail() {
  const panel = $("detail");
  $("app").classList.toggle("detail-open", !!state.selected);
  if (!state.selected) { panel.innerHTML = ""; return; }
  const n = NODES.get(state.selected);
  const reads = (OUT.get(n.id) || []);
  const readBy = (IN.get(n.id) || []);
  const up = upstreamOf(n.id), down = downstreamOf(n.id);

  const badges = [];
  if (!n.exists) badges.push(`<span class="badge crit">not in export</span>`);
  if (n.isHidden) badges.push(`<span class="badge">hidden</span>`);
  if (n.isCalculated) badges.push(`<span class="badge">calculated column</span>`);
  if (n.state && n.state !== "Ready") badges.push(`<span class="badge warn">${esc(n.state)}</span>`);
  if (n.timeIntelligence.length) badges.push(`<span class="badge">time intelligence</span>`);
  if (n.iterators.length) badges.push(`<span class="badge">iterator</span>`);
  if (n.contextModifiers.length) badges.push(`<span class="badge">context modifier</span>`);
  if (n.unresolved.length) badges.push(`<span class="badge warn">${n.unresolved.length} unresolved ref</span>`);

  panel.innerHTML = `<div class="pad">
    <h2>${esc(n.name)}</h2>
    <div class="sub">${esc(n.kind)}${n.table ? " on " + esc(n.table) : ""}${
      n.displayFolder ? " &middot; " + esc(n.displayFolder) : ""}</div>
    ${badges.length ? `<div class="chips">${badges.join("")}</div>` : ""}
    ${n.description ? `<h3>Description</h3><p>${esc(n.description)}</p>` : ""}
    ${n.errorMessage ? `<h3>Error</h3><p style="color:var(--critical)">${esc(n.errorMessage)}</p>` : ""}

    <h3>Lineage</h3>
    <dl class="kv">
      <dt>Chain depth</dt><dd>${n.depth}</dd>
      <dt>Reads directly</dt><dd>${reads.length}</dd>
      <dt>Reads in total</dt><dd>${up.length}</dd>
      <dt>Read by directly</dt><dd>${readBy.length}</dd>
      <dt>Read by in total</dt><dd>${down.length}</dd>
      ${n.formatString ? `<dt>Format</dt><dd>${esc(n.formatString)}</dd>` : ""}
      ${n.dataType ? `<dt>Data type</dt><dd>${esc(n.dataType)}</dd>` : ""}
    </dl>

    ${n.expression ? `<h3>DAX</h3><pre class="dax">${highlightDax(n.expression)}</pre>` : ""}

    ${reads.length ? `<h3>Reads (${reads.length})</h3>${reads.map(nodeLink).join("")}` : ""}
    ${readBy.length ? `<h3>Read by (${readBy.length})</h3>${readBy.map(nodeLink).join("")}` : ""}
    ${n.functions.length ? `<h3>Functions</h3><div class="chips">${
      n.functions.map(f => `<span class="badge">${esc(f)}</span>`).join("")}</div>` : ""}
    ${n.variables.length ? `<h3>Variables</h3><div class="chips">${
      n.variables.map(v => `<span class="badge">${esc(v)}</span>`).join("")}</div>` : ""}

    <h3>Export</h3>
    <div class="row" style="display:flex;gap:6px;flex-wrap:wrap">
      <button id="copyMermaid">Copy subgraph as Mermaid</button>
      <button id="clearSel">Clear selection</button>
    </div>
  </div>`;

  $("copyMermaid").onclick = () => copyMermaid(n.id);
  $("clearSel").onclick = () => select(null);
}

function copyMermaid(id) {
  const keep = new Set([id, ...upstreamOf(id), ...downstreamOf(id)]);
  const safe = key => "n" + [...keep].indexOf(key);
  const lines = ["graph LR"];
  for (const key of keep) {
    const n = NODES.get(key);
    const open = n.kind === "measure" ? "([" : n.kind === "table" ? "[(" : "[";
    const close = n.kind === "measure" ? "])" : n.kind === "table" ? ")]" : "]";
    lines.push(`  ${safe(key)}${open}"${n.label.replace(/"/g, "'")}"${close}`);
  }
  for (const e of EDGES) {
    if (keep.has(e.source) && keep.has(e.target)) lines.push(`  ${safe(e.target)} --> ${safe(e.source)}`);
  }
  const text = lines.join("\n");
  navigator.clipboard?.writeText(text).then(
    () => flash("Mermaid copied to the clipboard"),
    () => flash("Copy failed; the browser blocked clipboard access")
  );
}

let flashTimer = null;
function flash(message) {
  const hint = $("hint");
  hint.textContent = message;
  hint.style.opacity = "1";
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    hint.textContent = "Click a node to trace what it reads and what reads it";
  }, 2600);
}

/* ---------- interaction ---------- */

// Frame the traced chain: the node plus everything it reads and everything
// that reads it, zoomed to fit but never past a readable size.
function centreOn(id) {
  const rect = $("svg").getBoundingClientRect();
  if (!rect.width || !state.positions.has(id)) return;
  const ids = [id, ...upstreamOf(id), ...downstreamOf(id)].filter(i => state.positions.has(i));
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const i of ids) {
    const p = state.positions.get(i);
    x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y);
    x1 = Math.max(x1, p.x + NODE_W); y1 = Math.max(y1, p.y + NODE_H);
  }
  const pad = 60;
  const k = Math.max(0.12, Math.min(1.15, (rect.width - pad * 2) / (x1 - x0), (rect.height - pad * 2) / (y1 - y0)));
  state.view = { k, x: rect.width / 2 - ((x0 + x1) / 2) * k, y: rect.height / 2 - ((y0 + y1) / 2) * k };
  applyView();
}

function revealInList(id) {
  const n = NODES.get(id);
  if (!n) return;
  const table = n.kind === "table" ? n.name : (n.table || (n.exists ? "(no table)" : "(not in the export)"));
  state.collapsed.delete(`t\\${table}`);
  for (const path of (n.displayFolder || "").split(";").map(p => p.trim()).filter(Boolean)) {
    let key = `t\\${table}`;
    for (const part of path.split("\\").map(p => p.trim()).filter(Boolean)) {
      key += `\\${part}`;
      state.collapsed.delete(key);
    }
  }
}

function select(id) {
  state.selected = id && NODES.has(id) ? id : null;
  if (state.selected) revealInList(state.selected);
  render();
  if (state.selected) {
    centreOn(state.selected);
    const item = document.querySelector(`.item[data-id="${CSS.escape(state.selected)}"]`);
    if (item && typeof item.scrollIntoView === "function") item.scrollIntoView({ block: "nearest" });
  }
}

$("list").addEventListener("click", event => {
  const header = event.target.closest(".tgroup, .fgroup");
  if (header) {
    const key = header.dataset.key;
    state.collapsed.has(key) ? state.collapsed.delete(key) : state.collapsed.add(key);
    renderList();
    return;
  }
  const item = event.target.closest(".item");
  if (item) select(item.dataset.id);
});
$("nodes").addEventListener("click", event => {
  const node = event.target.closest(".node");
  if (node) select(node.dataset.id);
});
$("detail").addEventListener("click", event => {
  const link = event.target.closest("[data-id], [data-goto]");
  if (link) select(link.dataset.id || link.dataset.goto);
});

$("search").addEventListener("input", event => {
  state.query = event.target.value.trim();
  renderList();
});
$("tableFilter").addEventListener("change", event => { state.table = event.target.value; render(); });
$("sort").addEventListener("change", event => { state.sort = event.target.value; renderList(); });
for (const key of ["showColumns", "showTables", "showHidden", "showUnreferenced", "focusOnly"]) {
  $(key).addEventListener("change", event => { state[key] = event.target.checked; render(); });
}

$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const prefersDark = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const isDark = root.dataset.theme === "dark" ||
    (root.dataset.theme !== "light" && prefersDark);
  root.dataset.theme = isDark ? "light" : "dark";
});

const svg = $("svg");
let drag = null;
svg.addEventListener("pointerdown", event => {
  if (event.target.closest(".node")) return;
  drag = { x: event.clientX, y: event.clientY, vx: state.view.x, vy: state.view.y };
  svg.classList.add("panning");
  svg.setPointerCapture(event.pointerId);
});
svg.addEventListener("pointermove", event => {
  if (!drag) return;
  state.view.x = drag.vx + (event.clientX - drag.x);
  state.view.y = drag.vy + (event.clientY - drag.y);
  applyView();
});
svg.addEventListener("pointerup", event => {
  drag = null; svg.classList.remove("panning");
  try { svg.releasePointerCapture(event.pointerId); } catch (_) {}
});
svg.addEventListener("wheel", event => {
  event.preventDefault();
  const rect = svg.getBoundingClientRect();
  const px = event.clientX - rect.left, py = event.clientY - rect.top;
  const factor = Math.exp(-event.deltaY * 0.0014);
  const k = Math.min(2.4, Math.max(0.12, state.view.k * factor));
  const ratio = k / state.view.k;
  state.view.x = px - (px - state.view.x) * ratio;
  state.view.y = py - (py - state.view.y) * ratio;
  state.view.k = k;
  applyView();
}, { passive: false });

$("zoomIn").onclick = () => { state.view.k = Math.min(2.4, state.view.k * 1.25); applyView(); };
$("zoomOut").onclick = () => { state.view.k = Math.max(0.12, state.view.k / 1.25); applyView(); };
$("fit").onclick = fit;

addEventListener("keydown", event => {
  if (!DATA || document.getElementById("app").hidden) return;
  if (event.key === "/" && document.activeElement !== $("search")) {
    event.preventDefault(); $("search").focus(); $("search").select();
  } else if (event.key === "Escape") {
    if (document.activeElement === $("search")) { $("search").value = ""; state.query = ""; renderList(); }
    else select(null);
  } else if (event.key === "f" && !event.metaKey && !event.ctrlKey &&
             document.activeElement !== $("search")) { fit(); }
});

/* ---------- boot ---------- */

function mount(data, meta) {
  setData(data);
  DATA.meta = meta || DATA.meta || { subtitle: "" };
  Object.assign(state, {
    selected: null, query: "", table: "", focusOnly: false,
    view: { x: 40, y: 40, k: 1 }, positions: new Map(), visible: new Set()
  });
  $("search").value = "";
  $("focusOnly").checked = false;
  $("tableFilter").innerHTML = `<option value="">All tables</option>` +
    DATA.tables.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  renderHeader();
  render();
  requestAnimationFrame(fit);
}

addEventListener("resize", () => { if (DATA) { state.needsFit ? fit() : applyView(); } });

window.LineageViewer = { mount, select, fit };
})();
