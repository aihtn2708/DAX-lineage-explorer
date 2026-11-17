/*
 * Which model fields a Power BI report uses, read from the report file itself.
 *
 *   .pbix   Report/Layout (legacy JSON, UTF-16LE). Its model is a binary
 *           VertiPaq blob we cannot read: pair it with the INFO.* export.
 *   .pbit   Report/Layout + DataModelSchema (the model as JSON): self-sufficient.
 *   PBIP    <name>.Report/definition/pages/<p>/visuals/<v>/visual.json (PBIR),
 *           or <name>.Report/report.json (PBIR-legacy), and
 *           <name>.SemanticModel/model.bim when the model is saved as TMSL.
 *
 * Every usage is { report, page, pageOrder, visualId, visualType, title, role,
 * kind: "measure" | "column", table, name }.
 * window.ReportReader
 */
(function (root) {
  "use strict";

  const Z = () => root.Zip || (typeof require === "function" ? require("./zip.js") : null);

  const ROLE_NAMES = {
    Category: "Axis", Y: "Values", Y2: "Secondary values", X: "X axis", Series: "Legend",
    Values: "Values", Rows: "Rows", Columns: "Columns", Tooltips: "Tooltips", Details: "Details",
    Size: "Size", Indicator: "Value", Goal: "Target", TrendLine: "Trend axis", Data: "Values",
    Fields: "Fields", Play: "Play axis", Gradient: "Color saturation", MinValue: "Minimum",
    MaxValue: "Maximum", TargetValue: "Target"
  };
  const roleName = r => ROLE_NAMES[r] || r;

  const parseMaybe = value => {
    if (typeof value !== "string") return value;
    const s = value.trim();
    if (!(s.startsWith("{") || s.startsWith("["))) return value;
    try { return JSON.parse(s); } catch (_) { return value; }
  };

  function literal(textProperty) {
    const v = textProperty && textProperty.expr && textProperty.expr.Literal && textProperty.expr.Literal.Value;
    if (typeof v !== "string") return "";
    return v.replace(/^'([\s\S]*)'$/, "$1").replace(/''/g, "'");
  }

  /* ---------- field references inside the query JSON ---------- */

  function entityOf(expression, aliases) {
    const ref = expression && expression.SourceRef;
    if (!ref) return "";
    return ref.Entity || aliases[ref.Source] || "";
  }

  function fieldRef(node, aliases) {
    if (node.Measure && typeof node.Measure.Property === "string") {
      return { kind: "measure", table: entityOf(node.Measure.Expression, aliases), name: node.Measure.Property };
    }
    if (node.Column && typeof node.Column.Property === "string") {
      return { kind: "column", table: entityOf(node.Column.Expression, aliases), name: node.Column.Property };
    }
    if (node.HierarchyLevel) {
      const h = node.HierarchyLevel.Expression && node.HierarchyLevel.Expression.Hierarchy;
      if (!h) return null;
      // auto date/time: the level hangs off a variation of a real date column
      const variation = h.Expression && h.Expression.PropertyVariationSource;
      if (variation && variation.Property) {
        return { kind: "column", table: entityOf(variation.Expression, aliases), name: variation.Property };
      }
      if (typeof node.HierarchyLevel.Level === "string") {
        return { kind: "column", table: entityOf(h.Expression, aliases), name: node.HierarchyLevel.Level };
      }
    }
    return null;
  }

  // Walk any JSON looking for field references; legacy layouts nest JSON in strings.
  function collectRefs(value, aliases, out) {
    value = parseMaybe(value);
    if (Array.isArray(value)) { for (const v of value) collectRefs(v, aliases, out); return out; }
    if (!value || typeof value !== "object") return out;
    let scope = aliases;
    if (Array.isArray(value.From)) {
      scope = Object.assign({}, aliases);
      for (const f of value.From) if (f && f.Name) scope[f.Name] = f.Entity || scope[f.Name];
    }
    const ref = fieldRef(value, scope);
    if (ref) { out.push(ref); return out; }
    for (const [k, v] of Object.entries(value)) {
      // a selector pins formatting to one data point (a colour for one
      // category value); it names a field but does not use it, and Power BI
      // keeps stale ones after a column is renamed or removed
      if (k === "selector") continue;
      collectRefs(v, scope, out);
    }
    return out;
  }

  /* ---------- legacy layout (pbix Report/Layout, PBIR-legacy report.json) ---------- */

  function parseLayout(layout, reportName) {
    layout = parseMaybe(layout);
    const usages = [];
    const push = (base, refs, role) => {
      for (const r of refs) usages.push(Object.assign({}, base, { role }, r));
    };

    push({ report: reportName, page: "(all pages)", pageOrder: -1, visualId: "report-filters",
           visualType: "report filters", title: "Report filters" },
         collectRefs(layout.filters, {}, []), "Report filter");

    (layout.sections || []).forEach((section, index) => {
      const page = section.displayName || section.name || `Page ${index + 1}`;
      const pageOrder = typeof section.ordinal === "number" ? section.ordinal : index;
      push({ report: reportName, page, pageOrder, visualId: `${section.name || index}:page-filters`,
             visualType: "page filters", title: "Page filters" },
           collectRefs(section.filters, {}, []), "Page filter");

      for (const container of section.visualContainers || []) {
        const config = parseMaybe(container.config) || {};
        const visual = config.singleVisual;
        const base = {
          report: reportName, page, pageOrder,
          visualId: `${section.name || index}:${config.name || usages.length}`,
          visualType: visual ? visual.visualType || "visual" : "group",
          title: visual ? literal(visual.vcObjects && visual.vcObjects.title &&
            visual.vcObjects.title[0] && visual.vcObjects.title[0].properties &&
            visual.vcObjects.title[0].properties.text) : ""
        };
        if (visual) {
          const roles = {};
          for (const [role, items] of Object.entries(visual.projections || {})) {
            for (const item of items || []) (roles[item.queryRef] = roles[item.queryRef] || []).push(roleName(role));
          }
          const query = visual.prototypeQuery || {};
          const aliases = {};
          for (const f of query.From || []) if (f && f.Name) aliases[f.Name] = f.Entity;
          for (const select of query.Select || []) {
            const refs = collectRefs(select, aliases, []);
            const names = roles[select.Name] || ["Query"];
            for (const role of names) push(base, refs, role);
          }
          push(base, collectRefs({ Where: query.Where, OrderBy: query.OrderBy }, aliases, []), "Sort or query filter");
          push(base, collectRefs(visual.objects, aliases, []), "Conditional formatting");
          push(base, collectRefs(visual.vcObjects, aliases, []), "Formatting");
        }
        push(base, collectRefs(container.filters, {}, []), "Visual filter");
      }
    });
    return dedupe(usages);
  }

  /* ---------- PBIR (one JSON file per page and visual) ---------- */

  const PBIR_VISUAL = /(^|\/)definition\/pages\/([^/]+)\/visuals\/([^/]+)\/visual\.json$/i;
  const PBIR_PAGE = /(^|\/)definition\/pages\/([^/]+)\/page\.json$/i;
  const PBIR_PAGES = /(^|\/)definition\/pages\/pages\.json$/i;
  const PBIR_REPORT = /(^|\/)definition\/report\.json$/i;

  async function parsePbir(paths, getText, reportName) {
    const json = async p => parseMaybe(await getText(p)) || {};
    const usages = [];
    const push = (base, refs, role) => { for (const r of refs) usages.push(Object.assign({}, base, { role }, r)); };

    const pagesFile = paths.find(p => PBIR_PAGES.test(p));
    const order = pagesFile ? ((await json(pagesFile)).pageOrder || []) : [];
    const pages = {};
    for (const p of paths.filter(p => PBIR_PAGE.test(p))) {
      const id = p.match(PBIR_PAGE)[2];
      const page = await json(p);
      pages[id] = { name: page.displayName || page.name || id, order: order.indexOf(id) };
      push({ report: reportName, page: pages[id].name, pageOrder: pages[id].order, visualId: `${id}:page-filters`,
             visualType: "page filters", title: "Page filters" },
           collectRefs(page.filterConfig, {}, []), "Page filter");
    }
    const reportFile = paths.find(p => PBIR_REPORT.test(p));
    if (reportFile) {
      push({ report: reportName, page: "(all pages)", pageOrder: -1, visualId: "report-filters",
             visualType: "report filters", title: "Report filters" },
           collectRefs((await json(reportFile)).filterConfig, {}, []), "Report filter");
    }
    for (const p of paths.filter(p => PBIR_VISUAL.test(p))) {
      const [, , pageId, visualId] = p.match(PBIR_VISUAL);
      const container = await json(p);
      const visual = container.visual;
      const page = pages[pageId] || { name: pageId, order: order.indexOf(pageId) };
      const title = visual && visual.visualContainerObjects && visual.visualContainerObjects.title &&
        visual.visualContainerObjects.title[0] && visual.visualContainerObjects.title[0].properties &&
        literal(visual.visualContainerObjects.title[0].properties.text);
      const base = { report: reportName, page: page.name, pageOrder: page.order,
        visualId: `${pageId}:${container.name || visualId}`,
        visualType: visual ? visual.visualType || "visual" : "group", title: title || "" };
      if (visual) {
        const state = (visual.query && visual.query.queryState) || {};
        for (const [role, bucket] of Object.entries(state)) {
          for (const projection of (bucket && bucket.projections) || []) {
            push(base, collectRefs(projection.field, {}, []), roleName(role));
          }
        }
        push(base, collectRefs(visual.query && visual.query.sortDefinition, {}, []), "Sort or query filter");
        push(base, collectRefs(visual.objects, {}, []), "Conditional formatting");
        push(base, collectRefs(visual.visualContainerObjects, {}, []), "Formatting");
      }
      push(base, collectRefs(container.filterConfig, {}, []), "Visual filter");
    }
    return dedupe(usages);
  }

  function dedupe(usages) {
    const seen = new Set();
    return usages.filter(u => {
      if (!u.name) return false;
      const key = [u.report, u.visualId, u.role, u.kind, u.table, u.name].join("\u0001").toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  /* ---------- the model as JSON: .pbit DataModelSchema, model.bim ---------- */

  const joinLines = e => Array.isArray(e) ? e.join("\n") : (e == null ? "" : String(e));

  function modelSources(schema, label) {
    schema = parseMaybe(schema);
    const model = schema && (schema.model || schema);
    if (!model || !Array.isArray(model.tables)) return null;
    const tables = [], columns = [], measures = [], relationships = [];
    model.tables.forEach((t, ti) => {
      tables.push({ ID: ti + 1, Name: t.name, Description: joinLines(t.description), IsHidden: !!t.isHidden });
      for (const c of t.columns || []) {
        if (c.type === "rowNumber") continue;
        columns.push({ TableID: ti + 1, ExplicitName: c.name, Expression: c.type === "calculated" ? joinLines(c.expression) : "",
          IsHidden: !!c.isHidden, DisplayFolder: c.displayFolder || "", DataType: c.dataType || "",
          SortByColumn: c.sortByColumn || "" });
      }
      for (const m of t.measures || []) {
        measures.push({ TableID: ti + 1, Name: m.name, Expression: joinLines(m.expression),
          DisplayFolder: m.displayFolder || "", IsHidden: !!m.isHidden, Description: joinLines(m.description),
          FormatString: m.formatString || "", State: "Ready" });
      }
    });
    for (const r of model.relationships || []) {
      relationships.push({ FromTable: r.fromTable, FromColumn: r.fromColumn, ToTable: r.toTable, ToColumn: r.toColumn,
        IsActive: r.isActive !== false });
    }
    return [
      { label: `${label} › measures`, kind: "measures", rows: measures },
      { label: `${label} › tables`, kind: "tables", rows: tables },
      { label: `${label} › columns`, kind: "columns", rows: columns },
      { label: `${label} › relationships`, kind: "relationships", rows: relationships }
    ];
  }

  /* ---------- entry points ---------- */

  const stem = name => name.replace(/\.(pbix|pbit|zip|json)$/i, "").replace(/^.*[\\/]/, "");
  const isLayout = obj => obj && typeof obj === "object" && Array.isArray(obj.sections);
  const isModel = obj => obj && typeof obj === "object" && (Array.isArray(obj.tables) || (obj.model && Array.isArray(obj.model.tables)));

  // Read whatever report or model is in a set of paths (a zip or a folder).
  async function fromPaths(paths, getText, label) {
    const result = { usages: [], model: null, found: [] };
    const reportName = n => (n.match(/([^/]+)\.Report\//i) || [, label])[1];

    const visualPaths = paths.filter(p => PBIR_VISUAL.test(p) || PBIR_PAGE.test(p) || PBIR_PAGES.test(p) || PBIR_REPORT.test(p));
    if (visualPaths.length) {
      const groups = {};
      for (const p of visualPaths) (groups[reportName(p)] = groups[reportName(p)] || []).push(p);
      for (const [name, group] of Object.entries(groups)) {
        result.usages.push(...await parsePbir(group, getText, name));
        result.found.push(`${name} (PBIR report)`);
      }
    }
    for (const p of paths) {
      const base = p.replace(/^.*\//, "").toLowerCase();
      if (/(^|\/)report\/layout$/i.test(p) || (base === "report.json" && !PBIR_REPORT.test(p))) {
        const layout = parseMaybe(await getText(p));
        if (isLayout(layout)) {
          const name = /\.report\//i.test(p) ? reportName(p) : label;
          result.usages.push(...parseLayout(layout, name));
          result.found.push(`${name} (report layout)`);
        }
      } else if ((base === "datamodelschema" || base === "model.bim") && !result.model) {
        const schema = parseMaybe(await getText(p));
        const sources = isModel(schema) ? modelSources(schema, label) : null;
        if (sources) { result.model = sources; result.found.push(`${label} (model from ${base === "model.bim" ? "model.bim" : "DataModelSchema"})`); }
      }
    }
    return result;
  }

  async function readFile(name, blob) {
    const lower = name.toLowerCase();
    if (/\.(pbix|pbit|zip)$/.test(lower)) {
      const zip = await Z().open(blob);
      const result = await fromPaths(zip.names, p => zip.text(p), stem(name));
      if (/\.pbix$/.test(lower) && !result.model) {
        const PM = root.PbixModel || (typeof require === "function" ? require("./pbixmodel.js") : null);
        if (!zip.has("DataModel")) {
          result.note = `${name} has no model of its own: it is a thin report on a published semantic model. Add the INFO.MEASURES export from that model.`;
        } else {
          try {
            const model = PM.read(await zip.bytes("DataModel"), stem(name));
            result.model = model.sources;
            result.modelInfo = model.counts;
            result.found.push(`${stem(name)} (model from the .pbix: ${model.counts.measures} measures, ${model.counts.columns} columns)`);
          } catch (err) {
            result.note = `${name}: its model could not be read (${err.message}). Add the INFO.MEASURES export, or save the file as .pbit.`;
          }
        }
      }
      return result;
    }
    if (/\.(json|bim)$/.test(lower) || lower === "layout") {
      const text = Z().decode(new Uint8Array(await blob.arrayBuffer()));
      let obj;
      try { obj = JSON.parse(text); } catch (_) { return null; }
      if (isLayout(obj)) return { usages: parseLayout(obj, stem(name)), model: null, found: [`${stem(name)} (report layout)`] };
      if (isModel(obj)) return { usages: [], model: modelSources(obj, stem(name)), found: [`${stem(name)} (model)`] };
      return null;
    }
    return null;
  }

  // A folder picked with <input webkitdirectory>: [{ path, blob }]
  async function readFolder(files) {
    const byPath = new Map(files.map(f => [f.path.replace(/\\/g, "/"), f.blob]));
    const top = (files[0] && files[0].path.split(/[\\/]/)[0]) || "report";
    return fromPaths([...byPath.keys()], async p => Z().decode(new Uint8Array(await byPath.get(p).arrayBuffer())), stem(top));
  }

  const api = { readFile, readFolder, parseLayout, parsePbir, modelSources, collectRefs, roleName };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ReportReader = api;
})(typeof window !== "undefined" ? window : globalThis);
