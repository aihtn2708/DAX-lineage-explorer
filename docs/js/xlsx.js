/*
 * A small .xlsx reader with no dependencies.
 * An .xlsx is a zip of XML parts: this reads the zip's central directory,
 * inflates entries with the browser's DecompressionStream, and walks the
 * workbook, shared strings and sheet XML with DOMParser.
 * It reads values only (no styles, formulas are read as their cached value).
 * Exposes window.XlsxReader.read(arrayBuffer) -> [{ name, matrix }]
 */
(function (root) {
  "use strict";

  const decoder = new TextDecoder("utf-8");

  function readZipDirectory(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) {
      if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("This is not a zip-based .xlsx file.");
    const count = view.getUint16(eocd + 10, true);
    let offset = view.getUint32(eocd + 16, true);
    const entries = new Map();
    for (let n = 0; n < count; n++) {
      if (view.getUint32(offset, true) !== 0x02014b50) throw new Error("The .xlsx zip directory is damaged.");
      const method = view.getUint16(offset + 10, true);
      const compressedSize = view.getUint32(offset + 20, true);
      const nameLength = view.getUint16(offset + 28, true);
      const extraLength = view.getUint16(offset + 30, true);
      const commentLength = view.getUint16(offset + 32, true);
      const localOffset = view.getUint32(offset + 42, true);
      const name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
      entries.set(name, { method, compressedSize, localOffset });
      offset += 46 + nameLength + extraLength + commentLength;
    }
    return { view, entries };
  }

  async function inflateRaw(data) {
    if (typeof DecompressionStream === "undefined") {
      throw new Error("This browser cannot unzip files (no DecompressionStream). Use a current Chrome, Edge, Firefox or Safari, or upload CSV.");
    }
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  async function entryText(bytes, dir, name) {
    const entry = dir.entries.get(name) || dir.entries.get(name.replace(/^\//, ""));
    if (!entry) return null;
    const { view } = dir;
    const at = entry.localOffset;
    if (view.getUint32(at, true) !== 0x04034b50) throw new Error(`Damaged zip entry: ${name}`);
    const start = at + 30 + view.getUint16(at + 26, true) + view.getUint16(at + 28, true);
    const raw = bytes.subarray(start, start + entry.compressedSize);
    if (entry.method === 0) return decoder.decode(raw);
    if (entry.method === 8) return decoder.decode(await inflateRaw(raw));
    throw new Error(`Unsupported zip compression (method ${entry.method}) in ${name}.`);
  }

  function parseXml(text) {
    const doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.getElementsByTagName("parsererror").length) throw new Error("A part of the workbook is not valid XML.");
    return doc;
  }
  const all = (node, local) => Array.from(node.getElementsByTagNameNS("*", local));
  const first = (node, local) => node.getElementsByTagNameNS("*", local)[0] || null;
  const textOf = node => all(node, "t").map(t => t.textContent).join("");

  function columnIndex(ref) {
    const letters = (ref.match(/^[A-Z]+/i) || [""])[0].toUpperCase();
    let n = 0;
    for (const ch of letters) n = n * 26 + (ch.charCodeAt(0) - 64);
    return n - 1;
  }

  function resolveTarget(target) {
    if (target.startsWith("/")) return target.slice(1);
    const parts = ("xl/" + target).split("/");
    const out = [];
    for (const p of parts) { if (p === "..") out.pop(); else if (p && p !== ".") out.push(p); }
    return out.join("/");
  }

  async function read(buffer) {
    const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    if (bytes[0] === 0xd0 && bytes[1] === 0xcf && bytes[2] === 0x11 && bytes[3] === 0xe0) {
      throw new Error("This is an old .xls workbook. In Excel use File → Save As → Excel Workbook (.xlsx), or save each sheet as CSV.");
    }
    const dir = readZipDirectory(bytes);
    const workbookXml = await entryText(bytes, dir, "xl/workbook.xml");
    if (!workbookXml) throw new Error("No workbook found inside the file. Is it an Excel .xlsx?");
    const relsXml = await entryText(bytes, dir, "xl/_rels/workbook.xml.rels");
    const rels = new Map();
    if (relsXml) for (const r of all(parseXml(relsXml), "Relationship")) rels.set(r.getAttribute("Id"), r.getAttribute("Target"));

    const shared = [];
    const sharedXml = await entryText(bytes, dir, "xl/sharedStrings.xml");
    if (sharedXml) for (const si of all(parseXml(sharedXml), "si")) shared.push(textOf(si));

    const sheets = [];
    let fallback = 1;
    for (const sheet of all(parseXml(workbookXml), "sheet")) {
      const name = sheet.getAttribute("name") || `Sheet${fallback}`;
      const rid = sheet.getAttribute("r:id") ||
        sheet.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id");
      const target = rels.get(rid) || `worksheets/sheet${fallback}.xml`;
      fallback++;
      const xml = await entryText(bytes, dir, resolveTarget(target));
      if (!xml) continue;
      const doc = parseXml(xml);
      const matrix = [];
      let rowCursor = 0;
      for (const row of all(doc, "row")) {
        const r = Number(row.getAttribute("r")) - 1;
        rowCursor = Number.isFinite(r) && r >= 0 ? r : rowCursor;
        const line = [];
        let colCursor = 0;
        for (const c of Array.from(row.childNodes).filter(n => n.localName === "c")) {
          const ref = c.getAttribute("r");
          const col = ref ? columnIndex(ref) : colCursor;
          colCursor = col + 1;
          const type = c.getAttribute("t") || "n";
          const v = first(c, "v");
          let value;
          if (type === "s") value = shared[Number(v && v.textContent)] ?? "";
          else if (type === "inlineStr") value = textOf(first(c, "is") || c);
          else if (type === "b") value = v ? v.textContent === "1" : false;
          else if (type === "str" || type === "e") value = v ? v.textContent : "";
          else value = v ? (v.textContent.trim() === "" ? "" : Number(v.textContent)) : "";
          line[col] = value;
        }
        for (let i = 0; i < line.length; i++) if (line[i] === undefined) line[i] = "";
        matrix[rowCursor] = line;
        rowCursor++;
      }
      sheets.push({ name, matrix: Array.from(matrix, l => l || []) });
    }
    return sheets;
  }

  const api = { read };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.XlsxReader = api;
})(typeof window !== "undefined" ? window : globalThis);
