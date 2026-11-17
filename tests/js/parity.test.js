// Run: node tests/js/parity.test.js
// Checks the browser port gives exactly the Python result on both sample models.
const { execFileSync } = require("child_process");
const path = require("path");
const assert = require("assert");
const L = require("../../docs/js/lineage.js");

const root = path.resolve(__dirname, "../..");
let failures = 0;

for (const folder of ["examples", "examples/troubled"]) {
  const dump = JSON.parse(execFileSync("python3", [path.join(__dirname, "dump_python.py"), path.join(root, folder)]));
  const py = dump.python;
  const js = L.lineageFromSources(dump.sources);
  const pick = n => JSON.stringify([n.id, n.kind, n.depth, n.exists, n.functions, n.unresolved,
    n.timeIntelligence, n.iterators, n.contextModifiers, n.variables, n.isCalculated, n.table]);
  const check = (label, fn) => {
    try { fn(); console.log(`ok    ${folder}: ${label}`); }
    catch (e) { failures++; console.log(`FAIL  ${folder}: ${label}\n      ${e.message.split("\n").slice(0, 6).join("\n      ")}`); }
  };
  check("same nodes", () => assert.deepStrictEqual(js.nodes.map(pick).sort(), py.nodes.map(pick).sort()));
  check("same edges", () => assert.deepStrictEqual(
    js.edges.map(e => `${e.source}>${e.target}:${e.kind}:${e.viaVariable}`).sort(),
    py.edges.map(e => `${e.source}>${e.target}:${e.kind}:${e.viaVariable}`).sort()));
  check("same stats", () => assert.deepStrictEqual(js.stats, py.stats));
  check("same tables and folders", () => {
    assert.deepStrictEqual(js.tables, py.tables);
    assert.deepStrictEqual(js.folders, py.folders);
  });
}

// with a report: every status, every where-used entry, every stat
{
  const os = require("os"), fs = require("fs");
  globalThis.Zip = require("../../docs/js/zip.js");
  const R = require("../../docs/js/report.js");
  const fixtures = fs.mkdtempSync(path.join(os.tmpdir(), "dax-lineage-parity-"));
  execFileSync("python3", [path.join(root, "scripts/make_report_samples.py"), "--fixtures", fixtures]);
  const cases = [["examples", "sales.pbix"], ["examples", "sales_pbir.zip"], ["examples/troubled", "problems_layout.json"]];
  (async () => {
    for (const [folder, report] of cases) {
      const file = path.join(fixtures, report);
      const dump = JSON.parse(execFileSync("python3", [path.join(__dirname, "dump_python.py"), path.join(root, folder), file]));
      const py = dump.python;
      const read = await R.readFile(report, new Blob([fs.readFileSync(file)]));
      const js = L.lineageFromSources(dump.sources, { usages: read.usages });
      const usageOf = n => n.usage ? JSON.stringify([n.usage.status, [...n.usage.visuals].sort(),
        n.usage.via.map(v => `${v.visual}<${v.through}`).sort(), n.usage.structural]) : "";
      const pick = n => JSON.stringify([n.id, n.kind, n.depth, n.exists, n.label, n.page || "", usageOf(n)]);
      const label = `${folder} + ${report}`;
      const check = (what, fn) => {
        try { fn(); console.log(`ok    ${label}: ${what}`); }
        catch (e) { failures++; console.log(`FAIL  ${label}: ${what}\n      ${e.message.split("\n").slice(0, 8).join("\n      ")}`); }
      };
      check("same nodes and usage", () => assert.deepStrictEqual(js.nodes.map(pick).sort(), py.nodes.map(pick).sort()));
      check("same edges and roles", () => assert.deepStrictEqual(
        js.edges.map(e => `${e.source}>${e.target}:${e.kind}:${(e.roles || []).join("/")}`).sort(),
        py.edges.map(e => `${e.source}>${e.target}:${e.kind}:${(e.roles || []).join("/")}`).sort()));
      check("same stats, report stats included", () => assert.deepStrictEqual(js.stats, py.stats));
      check("same tables, folders and pages", () => {
        assert.deepStrictEqual(js.tables, py.tables);
        assert.deepStrictEqual(js.folders, py.folders);
        assert.deepStrictEqual(js.pages, py.pages);
      });
    }
    fs.rmSync(fixtures, { recursive: true, force: true });
    finishDetection();
  })();
}

function finishDetection() {
// header and sheet detection
const kind = (headers, label) => L.detectKind([Object.fromEntries(headers.map(h => [h, ""]))], label);
const cases = [
  [["ID", "Name", "Expression", "IsSimpleMeasure"], "Sheet1", "measures"],
  [["[ID]", "[Name]", "[Expression]"], "INFO.MEASURES", "measures"],
  [["ID", "TableID", "ExplicitName", "Expression"], "Sheet2", "columns"],
  [["ID", "FromTableID", "ToTableID"], "Sheet3", "relationships"],
  [["ID", "ModelID", "Name"], "Sheet4", "tables"],
  [["ID", "Name", "Description"], "INFO.TABLES", "tables"],
  [["Name", "Expression"], "Sheet9", "measures"]
];
for (const [headers, label, want] of cases) {
  const got = kind(headers, label);
  if (got === want) console.log(`ok    detect ${label} -> ${want}`);
  else { failures++; console.log(`FAIL  detect ${label}: wanted ${want}, got ${got}`); }
}

// CSV with quoted multi-line DAX and a title line above the header
const csv = 'Exported from DAX query view\nName,Expression\n"Sales PY","CALCULATE (\n    [Total Sales],\n    SAMEPERIODLASTYEAR ( \'Date\'[Date] )\n)"\nTotal Sales,SUM ( Sales[Amount] )\n';
const rows = L.parseDelimited(csv);
try {
  assert.strictEqual(rows.length, 2);
  assert.ok(rows[0].Expression.includes("SAMEPERIODLASTYEAR"));
  console.log("ok    csv with title line and multi-line quoted DAX");
} catch (e) { failures++; console.log("FAIL  csv parsing: " + e.message); }

console.log(failures ? `\n${failures} failure(s)` : "\nall parity checks passed");
process.exit(failures ? 1 : 0);
}
