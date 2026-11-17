// Run: node tests/js/xlsx.test.js   (needs jsdom for DOMParser: npm i jsdom)
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");
globalThis.DOMParser = new JSDOM("").window.DOMParser;
const X = require("../../docs/js/xlsx.js");
const L = require("../../docs/js/lineage.js");

(async () => {
  const file = path.resolve(__dirname, "../../docs/samples/info_sample.xlsx");
  const sheets = await X.read(fs.readFileSync(file));
  assert.deepStrictEqual(sheets.map(s => s.name), ["INFO.MEASURES", "INFO.TABLES", "INFO.COLUMNS", "INFO.RELATIONSHIPS"]);
  const sources = sheets.map(s => ({ label: s.name, rows: L.rowsFromMatrix(s.matrix) }));
  const measures = sources[0].rows;
  assert.strictEqual(measures.length, 24);
  assert.ok(measures.find(r => r.Name === "Sales PY").Expression.includes("\n"), "multi-line DAX survives");
  assert.strictEqual(measures.find(r => r.Name === "_Base Sales Helper").IsHidden, true, "booleans are booleans");
  assert.strictEqual(typeof measures[0].ID, "number");
  const graph = L.lineageFromSources(sources);
  assert.strictEqual(graph.stats.measures, 24);
  assert.strictEqual(graph.stats.edges, 68);
  assert.deepStrictEqual(graph.sources.map(s => s.kind), ["measures", "tables", "columns", "relationships"]);
  console.log("ok    sample workbook: 4 sheets detected, 24 measures, 68 dependencies");

  const troubled = await X.read(fs.readFileSync(path.resolve(__dirname, "../../docs/samples/info_sample_with_problems.xlsx")));
  const g2 = L.lineageFromSources(troubled.map(s => ({ label: s.name, rows: L.rowsFromMatrix(s.matrix) })));
  assert.strictEqual(g2.stats.cycles.length, 1);
  assert.deepStrictEqual(g2.stats.unresolvedReferences, ["Refunds", "Revenu"]);
  console.log("ok    workbook with problems: 1 cycle, 2 unresolved references");

  await assert.rejects(X.read(Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0, 0, 0, 0])), /old \.xls/);
  await assert.rejects(X.read(Buffer.from("not a zip at all, just some text bytes here")), /not a zip/);
  console.log("ok    rejects .xls and non-zip input with a clear message");
  console.log("\nall xlsx checks passed");
})().catch(e => { console.error("FAIL", e); process.exit(1); });

// Shapes real exports use that openpyxl does not write
(async () => {
  const { execFileSync } = require("child_process");
  const os = require("os");
  const target = path.join(os.tmpdir(), "dax-lineage-odd.xlsx");
  execFileSync("python3", [path.join(__dirname, "make_odd_workbook.py"), target]);
  const [sheet] = await X.read(fs.readFileSync(target));
  const rows = L.rowsFromMatrix(sheet.matrix);
  assert.strictEqual(sheet.name, "Result");
  assert.deepStrictEqual(rows.map(r => r.Name), ["Total Sales", "Margin"]);
  assert.strictEqual(rows[1].Expression, "[Total Sales] * 0.3", "rich-text inline string joined");
  assert.strictEqual(rows[0].IsHidden, true);
  assert.strictEqual(L.detectKind(rows, sheet.name), "measures");
  const g = L.lineageFromSources([{ label: sheet.name, rows }]);
  assert.strictEqual(g.edges.filter(e => e.kind === "uses_measure").length, 1);
  console.log("ok    inline strings, rich text, title row, missing cell refs, stored entries, absolute rel target");
})().catch(e => { console.error("FAIL", e); process.exit(1); });
