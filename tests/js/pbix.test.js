// Run: node tests/js/pbix.test.js
// The .pbix model path: XPress9, the backup stream, SQLite, the model rows.
// Set DAX_LINEAGE_PBIX=/path/to/file.pbix to also check a real file.
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");
const zlib = require("zlib");
const { execFileSync } = require("child_process");
globalThis.Zip = require("../../docs/js/zip.js");
globalThis.Xpress9 = require("../../docs/js/xpress9.js");
globalThis.SqliteReader = require("../../docs/js/sqlite.js");
globalThis.PbixModel = require("../../docs/js/pbixmodel.js");
const R = require("../../docs/js/report.js");
const L = require("../../docs/js/lineage.js");
const enc = require("./xpress9_encoder.js");

const root = path.resolve(__dirname, "../..");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dax-lineage-pbix-"));
let failures = 0;
const check = async (label, fn) => {
  try { await fn(); console.log(`ok    ${label}`); }
  catch (e) { failures++; console.log(`FAIL  ${label}\n      ${String(e.stack || e.message).split("\n").slice(0, 6).join("\n      ")}`); }
};

function sampleText(n) {
  const words = ["Sales", "Margin", "CALCULATE", "[Total Sales]", "'Date'[Date]", " ", "\n", "VAR", "RETURN", "DIVIDE", "(", ")", ","];
  let s = "", seed = 7;
  while (s.length < n) { seed = (seed * 1103515245 + 12345) & 0x7fffffff; s += words[seed % words.length]; }
  return Buffer.from(s.slice(0, n));
}

// a stored-only zip, enough for a test .pbix
function zipFiles(files) {
  const local = [], central = [];
  let offset = 0;
  for (const [name, data] of Object.entries(files)) {
    const nameBuf = Buffer.from(name), crc = zlib.crc32 ? zlib.crc32(data) : 0;
    const h = Buffer.alloc(30);
    h.writeUInt32LE(0x04034b50, 0); h.writeUInt16LE(20, 4); h.writeUInt32LE(crc >>> 0, 14);
    h.writeUInt32LE(data.length, 18); h.writeUInt32LE(data.length, 22); h.writeUInt16LE(nameBuf.length, 26);
    local.push(h, nameBuf, data);
    const c = Buffer.alloc(46);
    c.writeUInt32LE(0x02014b50, 0); c.writeUInt16LE(20, 4); c.writeUInt16LE(20, 6); c.writeUInt32LE(crc >>> 0, 16);
    c.writeUInt32LE(data.length, 20); c.writeUInt32LE(data.length, 24); c.writeUInt16LE(nameBuf.length, 28);
    c.writeUInt32LE(offset, 42);
    central.push(c, nameBuf);
    offset += 30 + nameBuf.length + data.length;
  }
  const dir = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(Object.keys(files).length, 8); end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(dir.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, dir, end]);
}

(async () => {
  await check("XPress9: literals, matches, long and overlapping copies, many blocks and chunks", () => {
    const data = Buffer.concat([sampleText(30000), Buffer.alloc(3000, 0x41), sampleText(25000)]);
    const chunks = enc.encodeStream(data);
    assert.ok(chunks.length >= 3);
    const d = new Xpress9.Decoder();
    const out = Buffer.concat(chunks.map(c => Buffer.from(d.decodeChunk(c.packed, c.size))));
    assert.ok(out.equals(data), "round trip");
  });

  await check("XPress9: structural damage is refused (the format has no data checksum)", () => {
    const [chunk] = enc.encodeStream(sampleText(5000), 5000, 2500);
    const badMagic = Buffer.from(chunk.packed); badMagic[0] ^= 1;
    assert.throws(() => new Xpress9.Decoder().decodeChunk(badMagic, chunk.size), /magic/);
    const truncated = chunk.packed.subarray(0, chunk.packed.length - 40);
    assert.throws(() => new Xpress9.Decoder().decodeChunk(truncated, chunk.size), /corrupt/);
    const badIndex = Buffer.from(chunk.packed); badIndex.writeUInt32LE(5, 24);
    assert.throws(() => new Xpress9.Decoder().decodeChunk(badIndex, chunk.size), /block 5/);
    const badSize = Buffer.from(chunk.packed); badSize.writeUInt32LE(badSize.readUInt32LE(8) + 9, 8);
    assert.throws(() => new Xpress9.Decoder().decodeChunk(badSize, chunk.size), /corrupt/);
  });

  const dbPath = path.join(tmp, "metadata.sqlitedb");
  execFileSync("python3", [path.join(__dirname, "make_metadata_db.py"), dbPath]);
  const dbBytes = fs.readFileSync(dbPath);

  await check("SQLite reader returns exactly what sqlite3 does", () => {
    const db = SqliteReader.open(new Uint8Array(dbBytes));
    const py = JSON.parse(execFileSync("python3", ["-I", "-c", `
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.row_factory = sqlite3.Row
fix = lambda v: str(v) if isinstance(v, int) and abs(v) > 2**53 - 1 else v
print(json.dumps({t: [{k: fix(v) for k, v in dict(r).items()} for r in con.execute(f"select * from [{t}] order by ID")]
                  for t in ["Table", "Column", "Measure", "Relationship", "Model"]}))`, dbPath]));
    for (const t of Object.keys(py)) {
      const js = db.rows(t).sort((a, b) => a.ID - b.ID).map(r => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, v])));
      assert.deepStrictEqual(js, py[t], t);
    }
    assert.ok(db.rows("Measure").find(m => m.Name === "Discount Impact").Expression.length > 2500, "overflow pages followed");
  });

  // an ABF-shaped stream: signature, padding, the database, more padding
  const abf = Buffer.concat([
    Buffer.from("﻿STREAM_STORAGE_SIGNATURE_)!@#$%^&*(", "utf16le"),
    Buffer.alloc(4096 - 72), sampleText(9000), dbBytes, sampleText(7000)
  ]);
  const layout = fs.readFileSync(path.join(root, "docs/samples/sample_report.json"), "utf8");
  const pbix = zipFiles({
    "Version": Buffer.from("1.28", "utf16le"),
    "Report/Layout": Buffer.from(JSON.stringify(JSON.parse(layout)), "utf16le"),
    "DataModel": enc.dataModel(abf)
  });

  if (process.argv[2] === "--write") {
    fs.writeFileSync(process.argv[3], pbix);
    fs.rmSync(tmp, { recursive: true, force: true });
    process.exit(0);
  }

  await check("Python reads the same .pbix to the same answer", async () => {
    const file = path.join(tmp, "Sales.pbix");
    fs.writeFileSync(file, pbix);
    const py = JSON.parse(execFileSync("python3", ["-m", "dax_lineage", "json", "-s", file, "-o", "-"], { cwd: root }));
    const result = await R.readFile("Sales.pbix", new Blob([pbix]));
    const js = L.lineageFromSources(result.model, { usages: result.usages });
    assert.deepStrictEqual(js.stats, py.stats);
  });

  await check(".pbix alone: report and model read in the browser path", async () => {
    const result = await R.readFile("Sales.pbix", new Blob([pbix]));
    assert.ok(!result.note, result.note);
    assert.ok(result.model, "model found");
    const g = L.lineageFromSources(result.model, { usages: result.usages });
    assert.strictEqual(g.stats.measures, 24);
    assert.strictEqual(g.stats.edges, 68, "same lineage as the CSV export");
    assert.strictEqual(g.stats.report.unusedMeasures.length, 11);
    assert.ok(!g.nodes.some(n => /^H\$|DateTableTemplate|RowNumber/.test(n.name)), "system tables and RowNumber columns left out");
    assert.ok(g.nodes.every(n => n.kind !== "measure" || !n.state || n.state === "Ready"), "State 1 reads as Ready");
    assert.strictEqual(g.nodes.find(n => n.id === "c:date[monthnumber]").usage.status, "structural", "sort-by from SortByColumnID");
  });

  await check("a .pbix without a model says so", async () => {
    const thin = zipFiles({ "Report/Layout": Buffer.from(layout, "utf16le") });
    const result = await R.readFile("Thin.pbix", new Blob([thin]));
    assert.ok(/thin report/.test(result.note), result.note);
  });

  await check("a damaged DataModel gives a reason", async () => {
    const dm = enc.dataModel(abf); dm[400] ^= 0xff;
    const broken = zipFiles({ "Report/Layout": Buffer.from(layout, "utf16le"), "DataModel": dm });
    const result = await R.readFile("Broken.pbix", new Blob([broken]));
    assert.ok(!result.model && /could not be read/.test(result.note), result.note);
  });

  if (process.env.DAX_LINEAGE_PBIX) {
    await check(`real file: ${path.basename(process.env.DAX_LINEAGE_PBIX)}`, async () => {
      const result = await R.readFile(path.basename(process.env.DAX_LINEAGE_PBIX), new Blob([fs.readFileSync(process.env.DAX_LINEAGE_PBIX)]));
      assert.ok(result.model, result.note);
      const g = L.lineageFromSources(result.model, { usages: result.usages });
      console.log(`      ${g.stats.measures} measures, ${g.stats.columns} columns, ${g.stats.report.visuals} visuals, ${g.stats.report.unusedMeasures.length} unused`);
    });
  }

  fs.rmSync(tmp, { recursive: true, force: true });
  console.log(failures ? `\n${failures} failure(s)` : "\nall pbix checks passed");
  process.exit(failures ? 1 : 0);
})();
