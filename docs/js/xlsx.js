/*
 * A small .xlsx reader with no dependencies.
 * An .xlsx is a zip of XML parts: this reads the zip's central directory,
 * inflates entries with the browser's DecompressionStream, and walks the
 * workbook, shared strings and sheet XML with DOMParser.
 * It reads values only (no styles, formulas are read as their cached value).
 * Exposes window.XlsxReader.read(blobOrBytes) -> [{ name, matrix }]
 */
(function (root) {
  "use strict";

  const Z = () => root.Zip || (typeof require === "function" ? require("./zip.js") : null);

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

  async function read(input) {
    const blob = Z().asBlob(input);
    const head = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
    if (head[0] === 0xd0 && head[1] === 0xcf && head[2] === 0x11 && head[3] === 0xe0) {
      throw new Error("This is an old .xls workbook. In Excel use File → Save As → Excel Workbook (.xlsx), or save each sheet as CSV.");
    }
    let zip;
    try { zip = await Z().open(blob); }
    catch (err) { throw new Error("This is not a zip-based .xlsx file."); }
    const workbookXml = await zip.text("xl/workbook.xml");
    if (!workbookXml) throw new Error("No workbook found inside the file. Is it an Excel .xlsx?");
    const relsXml = await zip.text("xl/_rels/workbook.xml.rels");
    const rels = new Map();
    if (relsXml) for (const r of all(parseXml(relsXml), "Relationship")) rels.set(r.getAttribute("Id"), r.getAttribute("Target"));

    const shared = [];
    const sharedXml = await zip.text("xl/sharedStrings.xml");
    if (sharedXml) for (const si of all(parseXml(sharedXml), "si")) shared.push(textOf(si));

    const sheets = [];
    let fallback = 1;
    for (const sheet of all(parseXml(workbookXml), "sheet")) {
      const name = sheet.getAttribute("name") || `Sheet${fallback}`;
      const rid = sheet.getAttribute("r:id") ||
        sheet.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id");
      const target = rels.get(rid) || `worksheets/sheet${fallback}.xml`;
      fallback++;
      const xml = await zip.text(resolveTarget(target));
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
