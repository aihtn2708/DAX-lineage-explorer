// Run: node tests/js/report.test.js
// Every report format gives the same usage, and the usage gives the expected statuses.
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");
const { execFileSync } = require("child_process");
const { JSDOM } = require("jsdom");
globalThis.DOMParser = new JSDOM("").window.DOMParser;
globalThis.Zip = require("../../docs/js/zip.js");
const X = require("../../docs/js/xlsx.js");
const L = require("../../docs/js/lineage.js");
const R = require("../../docs/js/report.js");

const root = path.resolve(__dirname, "../..");
const fixtures = fs.mkdtempSync(path.join(os.tmpdir(), "dax-lineage-reports-"));
execFileSync("python3", [path.join(root, "scripts/make_report_samples.py"), "--fixtures", fixtures]);
const blob = file => new Blob([fs.readFileSync(path.join(fixtures, file))]);
let failures = 0;
const check = async (label, fn) => {
  try { await fn(); console.log(`ok    ${label}`); }
  catch (e) { failures++; console.log(`FAIL  ${label}\n      ${String(e.message).split("\n").slice(0, 8).join("\n      ")}`); }
};
const key = u => [u.page, u.title, u.visualType, u.role, u.kind, u.table, u.name].join(" | ");

(async () => {
  const readers = {
    "layout json": () => R.readFile("Sales report.json", blob("sales_layout.json")),
    ".pbix": () => R.readFile("Sales report.pbix", blob("sales.pbix")),
    ".pbit": () => R.readFile("Sales report.pbit", blob("sales.pbit")),
    "PBIR zip": () => R.readFile("Sales report.zip", blob("sales_pbir.zip"))
  };
  const results = {};
  for (const [name, read] of Object.entries(readers)) results[name] = await read();
  const reference = results["layout json"].usages.map(key).sort();

  await check("layout: the expected visuals, roles and filters", () => {
    const got = new Set(reference);
    for (const want of [
      "Overview | Net sales | card | Values | measure | Measures | Total Sales",
      "Overview | Sales by month | lineChart | Axis | column | Date | Month",
      "Overview | Orders over time | lineChart | Axis | column | Sales | OrderDate",
      "Overview | Page filters | page filters | Page filter | column | Date | Year",
      "(all pages) | Report filters | report filters | Report filter | column | Date | IsCurrentYear",
      "Customers | Customer detail | tableEx | Visual filter | column | Customer | Segment",
      "Customers | Share by segment | clusteredBarChart | Conditional formatting | measure | Measures | Gross Margin %",
      "Customers |  | slicer | Values | column | Customer | Country",
      "Products | Top products | clusteredBarChart | Visual filter | measure | Measures | Total Sales",
      "Products | Margin by category | pivotTable | Values | column | Sales | Quantity"
    ]) assert.ok(got.has(want), `missing: ${want}`);
  });
  for (const name of [".pbix", ".pbit", "PBIR zip"]) {
    await check(`${name} gives exactly the same usage as the layout`, () =>
      assert.deepStrictEqual(results[name].usages.map(key).sort(), reference));
  }
  await check(".pbix says its model needs the INFO export; .pbit carries the model", () => {
    assert.ok(/binary format/.test(results[".pbix"].note));
    assert.strictEqual(results[".pbix"].model, null);
    assert.ok(results[".pbit"].model && results[".pbit"].model.find(s => s.kind === "measures").rows.length === 24);
  });

  // the model from the workbook, plus the report
  const sheets = await X.read(fs.readFileSync(path.join(root, "docs/samples/info_sample.xlsx")));
  const sources = sheets.map(s => ({ label: s.name, rows: L.rowsFromMatrix(s.matrix) }));
  const g = L.lineageFromSources(sources, { usages: results["layout json"].usages });
  const node = id => g.nodes.find(n => n.id === id);
  const status = id => node(id).usage.status;

  await check("statuses for measures", () => {
    assert.strictEqual(status("m:total sales"), "visual");
    assert.strictEqual(status("m:total cost"), "indirect");           // via Gross Margin
    assert.strictEqual(status("m:sales yoy"), "indirect");            // via Sales YoY %
    assert.strictEqual(status("m:_base sales helper"), "indirect");   // via Segment Share of Sales
    assert.strictEqual(status("m:total quantity"), "deadChain");      // only Average Unit Price and Units per Order read it
    assert.strictEqual(status("m:sales ytd"), "unused");
    assert.deepStrictEqual(g.stats.report.unusedMeasures.sort(), [
      "Average Order Value", "Average Unit Price", "Discount Impact", "Margin per Order", "Sales MTD",
      "Sales QTD", "Sales Rolling 3M", "Sales YTD", "Top Product Sales", "Total Quantity", "Units per Order"]);
  });
  await check("statuses for columns: visuals, indirect, keys, sort-by, unused", () => {
    assert.strictEqual(status("c:date[month]"), "visual");
    assert.strictEqual(status("c:sales[orderdate]"), "visual");       // through the auto date hierarchy
    assert.strictEqual(status("c:sales[unitprice]"), "indirect");     // LineAmount -> Total Sales -> visual
    assert.strictEqual(status("c:date[monthnumber]"), "structural");  // sort-by of Date[Month]
    assert.ok(node("c:date[monthnumber]").usage.structural[0].includes("sort-by"));
    assert.strictEqual(status("c:product[productkey]"), "structural"); // relationship key
    assert.strictEqual(status("c:date[quarter]"), "unused");
  });
  await check("where used: direct visuals and the measure a visual reaches it through", () => {
    const totalCost = node("m:total cost").usage;
    assert.deepStrictEqual(totalCost.visuals, []);
    const via = totalCost.via.map(v => `${node(v.visual).label} <- ${node(v.through).name}`).sort();
    assert.ok(via.includes("Products › Margin by category <- Gross Margin"), via.join("; "));
    assert.ok(via.some(v => v.startsWith("Overview › Gross margin %")), via.join("; "));
    const roles = g.edges.find(e => e.source.includes("line1") && e.target === "m:sales py").roles;
    assert.deepStrictEqual(roles, ["Values"]);
  });
  await check("visuals sit in one final layer; measure stats unchanged by the report", () => {
    const visualDepths = new Set(g.nodes.filter(n => n.kind === "visual").map(n => n.depth));
    assert.strictEqual(visualDepths.size, 1);
    assert.strictEqual([...visualDepths][0], 1 + Math.max(...g.nodes.filter(n => n.kind !== "visual").map(n => n.depth)));
    const plain = L.lineageFromSources(sources);
    for (const k of ["measures", "columns", "edges", "maxDepth", "mostReferenced", "unusedByOtherMeasures"]) {
      if (k === "edges") continue;
      assert.deepStrictEqual(g.stats[k], plain.stats[k], k);
    }
    assert.strictEqual(g.stats.report.pages, 3);
    assert.strictEqual(g.stats.report.visuals, 10);
    assert.deepStrictEqual(g.tables, plain.tables, "page names stay out of the table list");
  });
  await check(".pbit alone: model and report from one file", async () => {
    const pbit = results[".pbit"];
    const g2 = L.lineageFromSources(pbit.model, { usages: pbit.usages });
    assert.strictEqual(g2.stats.measures, 24);
    assert.deepStrictEqual(g2.stats.report.unusedMeasures.sort(), g.stats.report.unusedMeasures.sort());
    assert.strictEqual(g2.nodes.find(n => n.id === "m:sales py").expression.split("\n").length, 4, "multi-line DAX joined");
    assert.strictEqual(g2.nodes.find(n => n.id === "c:product[productkey]").usage.status, "structural", "name-based relationships");
  });
  await check("a visual that uses a field the model no longer has", async () => {
    const problems = await R.readFile("Report with problems.pbit", blob("problems.pbit"));
    const g3 = L.lineageFromSources(problems.model, { usages: problems.usages });
    assert.deepStrictEqual(g3.stats.report.brokenVisualRefs, [{ visual: "Summary › Against target", field: "Sales Target" }]);
    assert.strictEqual(g3.nodes.find(n => n.id === "m:sales target").exists, false);
  });
  await check("a PBIP folder picked in the browser", async () => {
    const zip = await Zip.open(blob("sales_pbir.zip"));
    const files = [];
    for (const name of zip.names) files.push({ path: `My project/${name}`, blob: new Blob([await zip.bytes(name)]) });
    const folder = await R.readFolder(files);
    assert.deepStrictEqual(folder.usages.map(key).sort(), reference);
    assert.ok(folder.found[0].startsWith("Sales report"));
  });
  await check("non-report JSON is left for the INFO loader", async () => {
    const r = await R.readFile("measures.json", new Blob([JSON.stringify([{ Name: "A", Expression: "1" }])]));
    assert.strictEqual(r, null);
  });

  fs.rmSync(fixtures, { recursive: true, force: true });
  console.log(failures ? `\n${failures} failure(s)` : "\nall report checks passed");
  process.exit(failures ? 1 : 0);
})();
