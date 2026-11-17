/*
 * DAX lineage in the browser: a port of dax_lineage/dax.py, load.py and graph.py.
 * Same rules, same node ids, same numbers, so the page and `daxlin` agree.
 * No dependencies. Exposes window.DaxLineage (and module.exports under Node).
 */
(function (root) {
  "use strict";

  const norm = s => String(s == null ? "" : s).trim().toLowerCase();

  /* ---------------- headers and rows ---------------- */

  // 'Measures[Display Folder]' -> 'displayfolder'
  function headerKey(raw) {
    let text = String(raw == null ? "" : raw).trim().replace(/^﻿/, "");
    const hit = text.match(/\[([^\]]*)\]\s*$/);
    if (hit) text = hit[1];
    return text.toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  function normaliseRows(rows) {
    return rows.map(row => {
      const out = {};
      for (const [k, v] of Object.entries(row)) out[headerKey(k)] = v;
      return out;
    });
  }

  function pick(row, ...names) {
    for (const name of names) {
      const v = row[headerKey(name)];
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return null;
  }
  const text = (row, ...names) => { const v = pick(row, ...names); return v == null ? "" : String(v).trim(); };
  const int = (row, ...names) => {
    const v = pick(row, ...names);
    if (v == null) return null;
    const n = Number(String(v).trim());
    return Number.isFinite(n) ? Math.trunc(n) : null;
  };
  const bool = (row, ...names) => {
    const v = pick(row, ...names);
    if (v == null) return false;
    if (typeof v === "boolean") return v;
    const t = String(v).trim().toLowerCase();
    if (["true", "1", "yes", "y", "t"].includes(t)) return true;
    if (["false", "0", "no", "n", "f", ""].includes(t)) return false;
    return Boolean(t);
  };

  /* ---------------- CSV / TSV / JSON ---------------- */

  function parseDelimited(input) {
    const src = String(input).replace(/^﻿/, "");
    const sample = src.slice(0, 8192);
    const firstLine = sample.split(/\r?\n/)[0] || "";
    const counts = { "\t": 0, ",": 0, ";": 0, "|": 0 };
    for (const ch of firstLine) if (ch in counts) counts[ch]++;
    const delimiter = Object.entries(counts).sort((a, b) => b[1] - a[1])[0][1] > 0
      ? Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0] : ",";
    const records = [];
    let field = "", record = [], i = 0, quoted = false;
    while (i < src.length) {
      const ch = src[i];
      if (quoted) {
        if (ch === '"') {
          if (src[i + 1] === '"') { field += '"'; i += 2; continue; }
          quoted = false; i++; continue;
        }
        field += ch; i++; continue;
      }
      if (ch === '"' && field === "") { quoted = true; i++; continue; }
      if (ch === delimiter) { record.push(field); field = ""; i++; continue; }
      if (ch === "\r" && src[i + 1] === "\n") { i++; continue; }
      if (ch === "\n") { record.push(field); records.push(record); record = []; field = ""; i++; continue; }
      field += ch; i++;
    }
    if (field !== "" || record.length) { record.push(field); records.push(record); }
    return rowsFromMatrix(records.filter(r => r.some(c => String(c).trim() !== "")));
  }

  // Turn a grid into objects, finding the header row even below a title line.
  function rowsFromMatrix(matrix) {
    const known = new Set(["name", "explicitname", "inferredname", "expression", "id", "fromtableid"]);
    let headerAt = 0;
    for (let r = 0; r < Math.min(matrix.length, 10); r++) {
      const keys = (matrix[r] || []).map(headerKey);
      if (keys.some(k => known.has(k)) && keys.filter(Boolean).length >= 2) { headerAt = r; break; }
    }
    const header = (matrix[headerAt] || []).map(h => String(h == null ? "" : h));
    const rows = [];
    for (const line of matrix.slice(headerAt + 1)) {
      if (!line || !line.some(c => c !== null && c !== undefined && String(c).trim() !== "")) continue;
      const row = {};
      header.forEach((h, i) => { if (h) row[h] = line[i] === undefined ? "" : line[i]; });
      rows.push(row);
    }
    return rows;
  }

  function rowsFromJson(payload) {
    if (Array.isArray(payload)) return payload.filter(r => r && typeof r === "object");
    if (payload && typeof payload === "object") {
      if (Array.isArray(payload.results)) {
        const out = [];
        for (const result of payload.results) for (const t of result.tables || []) out.push(...(t.rows || []));
        return out;
      }
      if (Array.isArray(payload.tables)) {
        const out = [];
        for (const t of payload.tables) out.push(...(t.rows || []));
        return out;
      }
      for (const key of ["rows", "data", "value"]) if (Array.isArray(payload[key])) return payload[key];
    }
    throw new Error("could not find a list of rows in that JSON");
  }

  /* ---------------- which INFO.* query is this? ---------------- */

  function detectKind(rows, label) {
    const keys = new Set(Object.keys(rows[0] || {}).map(headerKey));
    const has = k => keys.has(k);
    const name = norm(label);
    if (has("fromtableid") || has("fromcolumnid")) return "relationships";
    if (has("explicitname") || has("inferredname")) return "columns";
    if (has("issimplemeasure") || has("kpiid") || has("detailrowsdefinitionid")) return "measures";
    if (has("modelid") || has("tablestorageid") || has("calculationgroupid")) return "tables";
    if (/relationship/.test(name)) return "relationships";
    if (/column/.test(name)) return "columns";
    if (/measure/.test(name)) return "measures";
    if (/table/.test(name)) return "tables";
    if (has("name") && has("expression")) return "measures";
    if (has("name")) return "tables";
    return null;
  }

  /* ---------------- catalogue ---------------- */

  function buildCatalogue(sources) {
    const cat = { measures: [], tables: [], columns: [], relationships: [], used: [] };
    for (const src of sources) {
      const rows = normaliseRows(src.rows || []);
      const kind = src.kind || detectKind(src.rows || [], src.label);
      if (!kind || !rows.length) { cat.used.push({ label: src.label, kind: null, rows: rows.length }); continue; }
      cat.used.push({ label: src.label, kind, rows: rows.length });
      for (const row of rows) {
        if (kind === "measures") {
          const n = text(row, "Name", "MeasureName");
          if (!n) continue;
          cat.measures.push({
            id: int(row, "ID"), tableId: int(row, "TableID"), name: n,
            expression: text(row, "Expression", "MeasureExpression"),
            tableName: text(row, "Table", "TableName"),
            description: text(row, "Description"), formatString: text(row, "FormatString"),
            displayFolder: text(row, "DisplayFolder"), isHidden: bool(row, "IsHidden"),
            state: text(row, "State"), errorMessage: text(row, "ErrorMessage"),
            dataType: text(row, "DataType")
          });
        } else if (kind === "tables") {
          const n = text(row, "Name", "TableName");
          if (!n) continue;
          cat.tables.push({ id: int(row, "ID"), name: n, description: text(row, "Description"), isHidden: bool(row, "IsHidden") });
        } else if (kind === "columns") {
          const n = text(row, "ExplicitName", "InferredName", "Name", "ColumnName");
          if (!n) continue;
          cat.columns.push({
            id: int(row, "ID"), tableId: int(row, "TableID"), name: n,
            tableName: text(row, "Table", "TableName"), expression: text(row, "Expression"),
            dataType: text(row, "DataType", "ExplicitDataType", "InferredDataType"),
            isHidden: bool(row, "IsHidden"), displayFolder: text(row, "DisplayFolder")
          });
        } else if (kind === "relationships") {
          cat.relationships.push({
            fromTableId: int(row, "FromTableID"), fromColumnId: int(row, "FromColumnID"),
            toTableId: int(row, "ToTableID"), toColumnId: int(row, "ToColumnID"),
            isActive: bool(row, "IsActive")
          });
        }
      }
    }
    // resolve TableID -> name, as Catalogue.resolve() does
    const byId = new Map(cat.tables.filter(t => t.id != null).map(t => [t.id, t.name]));
    const byName = new Map(cat.tables.map(t => [norm(t.name), t.id]));
    for (const item of [...cat.measures, ...cat.columns]) {
      if (!item.tableName && byId.has(item.tableId)) item.tableName = byId.get(item.tableId);
      if (item.tableId == null && item.tableName) item.tableId = byName.get(norm(item.tableName)) ?? null;
    }
    const known = new Set(cat.tables.map(t => norm(t.name)));
    for (const item of [...cat.measures, ...cat.columns]) {
      if (item.tableName && !known.has(norm(item.tableName))) {
        cat.tables.push({ id: item.tableId, name: item.tableName, description: "", isHidden: false });
        known.add(norm(item.tableName));
      }
    }
    const colById = new Map(cat.columns.filter(c => c.id != null).map(c => [c.id, c]));
    for (const r of cat.relationships) {
      const fc = colById.get(r.fromColumnId), tc = colById.get(r.toColumnId);
      r.fromTable = (fc && (fc.tableName || byId.get(fc.tableId))) || byId.get(r.fromTableId) || "";
      r.toTable = (tc && (tc.tableName || byId.get(tc.tableId))) || byId.get(r.toTableId) || "";
    }
    return cat;
  }

  function findColumn(cat, table, name) {
    const wantT = norm(table), wantC = norm(name);
    const hits = cat.columns.filter(c => norm(c.name) === wantC);
    if (wantT) return hits.find(c => norm(c.tableName) === wantT) || null;
    return hits.length === 1 ? hits[0] : null;
  }

  /* ---------------- DAX scanner ---------------- */

  const KEYWORDS = new Set(["var", "return", "define", "evaluate", "measure", "column", "table",
    "order", "by", "start", "at", "asc", "desc", "in", "not", "and", "or", "true", "false",
    "then", "else", "datatable", "with"]);
  const TIME_INTELLIGENCE = new Set(["dateadd", "datesbetween", "datesinperiod", "datesmtd", "datesqtd",
    "datesytd", "endofmonth", "endofquarter", "endofyear", "firstdate", "firstnonblank", "lastdate",
    "lastnonblank", "nextday", "nextmonth", "nextquarter", "nextyear", "openingbalancemonth",
    "openingbalancequarter", "openingbalanceyear", "closingbalancemonth", "closingbalancequarter",
    "closingbalanceyear", "parallelperiod", "previousday", "previousmonth", "previousquarter",
    "previousyear", "sameperiodlastyear", "startofmonth", "startofquarter", "startofyear",
    "totalmtd", "totalqtd", "totalytd"]);
  const CONTEXT_MODIFIERS = new Set(["calculate", "calculatetable", "all", "allexcept", "allselected",
    "allnoblankrow", "allcrossfiltered", "removefilters", "keepfilters", "userelationship",
    "crossfilter", "treatas"]);
  const ITERATORS = new Set(["sumx", "averagex", "minx", "maxx", "countx", "countax", "concatenatex",
    "productx", "medianx", "percentilex.inc", "percentilex.exc", "rankx", "stdevx.p", "stdevx.s",
    "varx.p", "varx.s", "generate", "generateall", "addcolumns", "selectcolumns", "filter", "topn"]);

  const IDENT_START = /[A-Za-z_À-￿]/;
  const IDENT_BODY = /[A-Za-z0-9_.À-￿]/;

  function tokenize(src) {
    const tokens = [], n = src.length;
    let i = 0;
    const quotedRun = (open, close, kind) => {
      let j = i + 1, buf = "";
      while (j < n) {
        if (src[j] === close) {
          if (src[j + 1] === close) { buf += close; j += 2; continue; }
          j++; break;
        }
        buf += src[j]; j++;
      }
      tokens.push({ kind, value: buf, start: i, end: j });
      i = j;
    };
    while (i < n) {
      const ch = src[i];
      if (/\s/.test(ch)) { i++; continue; }
      if (src.startsWith("//", i) || src.startsWith("--", i)) {
        let j = src.indexOf("\n", i); j = j < 0 ? n : j;
        tokens.push({ kind: "comment", value: src.slice(i, j), start: i, end: j }); i = j; continue;
      }
      if (src.startsWith("/*", i)) {
        let j = src.indexOf("*/", i + 2); j = j < 0 ? n : j + 2;
        tokens.push({ kind: "comment", value: src.slice(i, j), start: i, end: j }); i = j; continue;
      }
      if (ch === '"') { quotedRun('"', '"', "string"); continue; }
      if (ch === "'") { quotedRun("'", "'", "table"); continue; }
      if (ch === "[") { quotedRun("[", "]", "bracket"); continue; }
      if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(src[i + 1] || ""))) {
        let j = i;
        while (j < n && (/[0-9.eE]/.test(src[j]) || (/[+-]/.test(src[j]) && j > i && /[eE]/.test(src[j - 1])))) j++;
        tokens.push({ kind: "number", value: src.slice(i, j), start: i, end: j }); i = j; continue;
      }
      if (IDENT_START.test(ch)) {
        let j = i + 1;
        while (j < n && IDENT_BODY.test(src[j])) j++;
        while (j > i + 1 && src[j - 1] === ".") j--;
        tokens.push({ kind: "ident", value: src.slice(i, j), start: i, end: j }); i = j; continue;
      }
      tokens.push({ kind: "punct", value: ch, start: i, end: i + 1 }); i++;
    }
    return tokens;
  }

  function scan(expression, measureNames, columnNames, tableNames) {
    const measures = new Set(measureNames), columns = new Set(columnNames), tables = new Set(tableNames);
    const tokens = tokenize(expression || "").filter(t => t.kind !== "comment");
    const variables = [];
    let prev = null;
    for (const t of tokens) {
      if (prev === "var" && t.kind === "ident") variables.push(t.value);
      prev = t.kind === "ident" ? norm(t.value) : null;
    }
    const varSet = new Set(variables.map(norm));
    const refs = [], functions = [], seen = new Set();
    const add = r => {
      const key = `${r.kind}|${norm(r.table)}|${norm(r.name)}`;
      if (!seen.has(key)) { seen.add(key); refs.push(r); }
    };
    tokens.forEach((t, idx) => {
      const nxt = tokens[idx + 1], before = tokens[idx - 1];
      if (t.kind === "ident") {
        if (nxt && nxt.kind === "punct" && nxt.value === "(") {
          if (!KEYWORDS.has(norm(t.value))) functions.push(t.value);
          return;
        }
        if ((!nxt || nxt.kind !== "bracket") && tables.has(norm(t.value))) add({ kind: "table", name: t.value, table: "" });
        return;
      }
      if (t.kind === "table") {
        if (!nxt || nxt.kind !== "bracket") add({ kind: "table", name: t.value, table: "" });
        return;
      }
      if (t.kind !== "bracket") return;
      const name = t.value;
      if (before && (before.kind === "ident" || before.kind === "table")) {
        const owner = before.value;
        if (before.kind === "table" || !KEYWORDS.has(norm(owner))) {
          if (varSet.has(norm(owner))) add({ kind: "column", name, table: "", viaVariable: true });
          else {
            add({ kind: "column", name, table: owner });
            if (tables.has(norm(owner)) || before.kind === "table") add({ kind: "table", name: owner, table: "" });
          }
          return;
        }
      }
      if (measures.has(norm(name))) add({ kind: "measure", name, table: "" });
      else if (columns.has(norm(name))) add({ kind: "column", name, table: "" });
      else add({ kind: "unresolved", name, table: "" });
    });
    const uniqSorted = list => [...new Set(list)].sort();
    return {
      references: refs,
      functions,
      variables,
      timeIntelligence: uniqSorted(functions.filter(f => TIME_INTELLIGENCE.has(norm(f)))),
      iterators: uniqSorted(functions.filter(f => ITERATORS.has(norm(f)))),
      contextModifiers: uniqSorted(functions.filter(f => CONTEXT_MODIFIERS.has(norm(f))))
    };
  }

  /* ---------------- graph ---------------- */

  const measureId = n => `m:${norm(n)}`;
  const columnId = (t, n) => `c:${norm(t)}[${norm(n)}]`;
  const tableId = n => `t:${norm(n)}`;

  function blankNode(id, kind, name, extra) {
    return Object.assign({
      id, kind, name, table: "", label: name, expression: "", description: "", displayFolder: "",
      formatString: "", dataType: "", isHidden: false, isCalculated: false, state: "", errorMessage: "",
      functions: [], timeIntelligence: [], iterators: [], contextModifiers: [], variables: [],
      unresolved: [], depth: 0, exists: true
    }, extra || {});
  }

  function buildGraph(cat, options) {
    const includeRelationships = !!(options && options.includeRelationships);
    const nodes = new Map(), edges = [], edgeKeys = new Set();
    const measureNames = cat.measures.map(m => norm(m.name));
    const columnNames = cat.columns.map(c => norm(c.name));
    const tableNames = cat.tables.map(t => norm(t.name));

    for (const m of cat.measures) {
      nodes.set(measureId(m.name), blankNode(measureId(m.name), "measure", m.name, {
        table: m.tableName, expression: m.expression, description: m.description,
        displayFolder: m.displayFolder, formatString: m.formatString, dataType: m.dataType,
        isHidden: m.isHidden, state: m.state, errorMessage: m.errorMessage
      }));
    }
    for (const c of cat.columns) {
      const id = columnId(c.tableName, c.name);
      nodes.set(id, blankNode(id, "column", c.name, {
        table: c.tableName, label: c.tableName ? `${c.tableName}[${c.name}]` : `[${c.name}]`,
        expression: c.expression, displayFolder: c.displayFolder, dataType: c.dataType,
        isHidden: c.isHidden, isCalculated: !!(c.expression && c.expression.trim())
      }));
    }

    const ensureTable = name => {
      const id = tableId(name);
      if (!nodes.has(id)) {
        const known = cat.tables.find(t => norm(t.name) === norm(name));
        nodes.set(id, blankNode(id, "table", name, {
          description: known ? known.description : "", isHidden: known ? known.isHidden : false,
          exists: !!known || !cat.tables.length
        }));
      }
      return id;
    };
    const ensureMeasure = name => {
      const id = measureId(name);
      if (!nodes.has(id)) nodes.set(id, blankNode(id, "measure", name, { exists: false }));
      return id;
    };
    const ensureColumn = (table, name) => {
      const hit = findColumn(cat, table, name);
      if (hit) {
        const id = columnId(hit.tableName, hit.name);
        if (nodes.has(id)) return id;
      }
      const id = columnId(table, name);
      if (!nodes.has(id)) nodes.set(id, blankNode(id, "column", name, {
        table, label: table ? `${table}[${name}]` : `[${name}]`,
        // only call it missing when there was a column catalogue to miss it from
        exists: !cat.columns.length
      }));
      return id;
    };
    const addEdge = (source, target, kind, viaVariable) => {
      if (source === target) return;
      const key = `${source}|${target}|${kind}`;
      if (edgeKeys.has(key)) return;
      edgeKeys.add(key);
      edges.push({ source, target, kind, viaVariable: !!viaVariable });
    };
    const wire = (node, expression) => {
      const r = scan(expression, measureNames, columnNames, tableNames);
      node.functions = [...new Set(r.functions)].sort();
      node.timeIntelligence = r.timeIntelligence;
      node.iterators = r.iterators;
      node.contextModifiers = r.contextModifiers;
      node.variables = r.variables;
      node.unresolved = [...new Set(r.references.filter(x => x.kind === "unresolved").map(x => x.name))].sort();
      for (const ref of r.references) {
        if (ref.kind === "measure") addEdge(node.id, ensureMeasure(ref.name), "uses_measure");
        else if (ref.kind === "column") addEdge(node.id, ensureColumn(ref.table, ref.name), "uses_column", ref.viaVariable);
        else if (ref.kind === "table") addEdge(node.id, ensureTable(ref.name), "uses_table");
        else if (ref.kind === "unresolved" && cat.columns.length) addEdge(node.id, ensureMeasure(ref.name), "uses_measure");
      }
    };
    for (const m of cat.measures) wire(nodes.get(measureId(m.name)), m.expression);
    for (const c of cat.columns) if (c.expression && c.expression.trim()) wire(nodes.get(columnId(c.tableName, c.name)), c.expression);
    if (includeRelationships) {
      for (const r of cat.relationships) if (r.fromTable && r.toTable) addEdge(ensureTable(r.fromTable), ensureTable(r.toTable), "relationship");
    }

    // depth = longest dependency chain; cycles are recorded and cut
    const out = new Map([...nodes.keys()].map(id => [id, []]));
    for (const e of edges) out.get(e.source).push(e.target);
    const memo = new Map(), stateOf = new Map(), cycles = [];
    const walk = (id, path) => {
      if (stateOf.get(id) === "open") {
        const cycle = path.slice(path.indexOf(id)).concat([id]);
        if (!cycles.some(c => c.join() === cycle.join())) cycles.push(cycle);
        return 0;
      }
      if (memo.has(id)) return memo.get(id);
      stateOf.set(id, "open");
      let best = 0;
      for (const child of out.get(id)) best = Math.max(best, 1 + walk(child, path.concat([id])));
      stateOf.set(id, "done");
      memo.set(id, best);
      return best;
    };
    for (const id of nodes.keys()) nodes.get(id).depth = walk(id, []);

    return finish(nodes, edges, cycles);
  }

  function finish(nodes, edges, cycles) {
    const list = [...nodes.values()];
    // adjacency built once: large models have thousands of measures
    const depsMap = new Map(list.map(n => [n.id, []])), usersMap = new Map(list.map(n => [n.id, []]));
    for (const e of edges) { depsMap.get(e.source).push(e.target); usersMap.get(e.target).push(e.source); }
    const deps = id => depsMap.get(id) || [];
    const users = id => usersMap.get(id) || [];
    const measures = list.filter(n => n.kind === "measure");
    const isMeasure = id => nodes.has(id) && nodes.get(id).kind === "measure";
    const base = measures.filter(m => !deps(m.id).some(isMeasure));
    const unused = measures.filter(m => !users(m.id).some(isMeasure));
    // Python's sorted() is stable; keep the same tie order
    const stableSort = (arr, key) => arr.map((v, i) => [v, i]).sort((a, b) => key(a[0]) - key(b[0]) || a[1] - b[1]).map(x => x[0]);
    const stats = {
      measures: measures.length,
      columns: list.filter(n => n.kind === "column").length,
      tables: list.filter(n => n.kind === "table").length,
      edges: edges.length,
      missingObjects: list.filter(n => !n.exists).length,
      unresolvedReferences: [...new Set(list.flatMap(n => n.unresolved))].sort(),
      cycles,
      maxDepth: Math.max(0, ...measures.map(m => m.depth)),
      baseMeasures: base.map(m => m.name),
      unusedByOtherMeasures: unused.map(m => m.name),
      deepest: stableSort(measures, m => -m.depth).slice(0, 10).filter(m => m.depth).map(m => ({ name: m.name, depth: m.depth })),
      mostReferenced: stableSort(measures, m => -users(m.id).length).slice(0, 10)
        .filter(m => users(m.id).length).map(m => ({ name: m.name, count: users(m.id).length }))
    };
    const tables = [...new Set([...list.filter(n => n.table).map(n => n.table), ...list.filter(n => n.kind === "table").map(n => n.name)])].sort();
    const folders = [...new Set(list.filter(n => n.displayFolder).map(n => n.displayFolder))].sort();
    return { nodes: list, edges, stats, tables, folders };
  }

  /* ---------------- one call from rows to graph ---------------- */

  function lineageFromSources(sources, options) {
    const cat = buildCatalogue(sources);
    if (!cat.measures.length) {
      const seen = cat.used.map(u => `${u.label} → ${u.kind || "not recognised"} (${u.rows} rows)`).join("; ");
      throw new Error("No INFO.MEASURES rows found. A sheet or file needs at least a Name and an Expression column." +
        (seen ? ` Read: ${seen}.` : ""));
    }
    const graph = buildGraph(cat, options);
    graph.sources = cat.used;
    return graph;
  }

  const api = {
    headerKey, normaliseRows, parseDelimited, rowsFromMatrix, rowsFromJson, detectKind,
    buildCatalogue, tokenize, scan, buildGraph, lineageFromSources, measureId, columnId, tableId
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.DaxLineage = api;
})(typeof window !== "undefined" ? window : globalThis);
