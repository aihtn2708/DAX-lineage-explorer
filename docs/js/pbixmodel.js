/*
 * The semantic model inside a .pbix, read without Power BI.
 *
 * DataModel (XPress9) -> an Analysis Services backup (ABF) -> the file
 * metadata.sqlitedb inside it, whose tables (Table, Column, Measure,
 * Relationship) have the same columns as INFO.TABLES / INFO.COLUMNS /
 * INFO.MEASURES / INFO.RELATIONSHIPS.
 *
 * The backup streams by as it decompresses. SQLite databases in it are
 * recognised by their header and captured; decoding stops as soon as the
 * metadata database is complete, so a large model's column data is never
 * held in memory.
 *
 * window.PbixModel.read(dataModelBytes, label) -> { sources, format, tables, ... }
 */
(function (root) {
  "use strict";

  const X = () => root.Xpress9 || require("./xpress9.js");
  const S = () => root.SqliteReader || require("./sqlite.js");
  const MAGIC = [0x53, 0x51, 0x4c, 0x69, 0x74, 0x65, 0x20, 0x66, 0x6f, 0x72, 0x6d, 0x61, 0x74, 0x20, 0x33, 0x00];
  class Found extends Error {}

  function sqliteSize(h) {
    // a plausible database header gives its own size: page size x page count
    if (h[18] < 1 || h[18] > 2 || h[19] < 1 || h[19] > 2) return 0;
    if (h[21] !== 64 || h[22] !== 32 || h[23] !== 32) return 0;
    let pageSize = (h[16] << 8) | h[17];
    if (pageSize === 1) pageSize = 65536;
    if (pageSize < 512 || (pageSize & (pageSize - 1))) return 0;
    const pages = ((h[28] << 24) | (h[29] << 16) | (h[30] << 8) | h[31]) >>> 0;
    const counter = ((h[24] << 24) | (h[25] << 16) | (h[26] << 8) | h[27]) >>> 0;
    const validFor = ((h[92] << 24) | (h[93] << 16) | (h[94] << 8) | h[95]) >>> 0;
    if (!pages || counter !== validFor) return 0;
    return pageSize * pages;
  }

  function metadataFrom(bytes) {
    try {
      const db = S().open(bytes);
      const tables = db.tables().map(t => t.toLowerCase());
      return ["measure", "table", "column"].every(t => tables.includes(t)) ? db : null;
    } catch (_) {
      return null;
    }
  }

  function extract(dataModel) {
    let head = new Uint8Array(0);
    let pending = null;              // a database being captured
    let carry = new Uint8Array(0);   // the last bytes of the previous chunk, for headers split across chunks
    let metadata = null, format = "";

    const startCapture = (offset, firstBytes) => { pending = { offset, parts: [firstBytes], got: firstBytes.length, size: 0 }; };
    const feed = bytes => {
      if (!pending) return;
      const want = pending.size ? pending.size - pending.got : bytes.length;
      const take = bytes.subarray(0, Math.min(bytes.length, want));
      pending.parts.push(take);
      pending.got += take.length;
      if (!pending.size && pending.got >= 100) {
        const all = concat(pending.parts, pending.got);
        pending.size = sqliteSize(all);
        if (!pending.size) { pending = null; return; }
        pending.parts = [all.subarray(0, Math.min(all.length, pending.size))];
        pending.got = pending.parts[0].length;
      }
      if (pending && pending.size && pending.got >= pending.size) {
        const db = concat(pending.parts, pending.size);
        pending = null;
        const opened = metadataFrom(db);
        if (opened) { metadata = opened; throw new Found(); }
      }
    };

    const onData = (chunk, offset) => {
      if (head.length < 4096) head = concat([head, chunk.subarray(0, 4096 - head.length)], Math.min(4096, head.length + chunk.length));
      if (pending) { feed(chunk); return; }
      // look for the SQLite header, including one split across two chunks
      const window = carry.length ? concat([carry, chunk], carry.length + chunk.length) : chunk;
      const base = offset - carry.length;
      for (let i = 0; i <= window.length - MAGIC.length; i++) {
        if (window[i] !== 0x53) continue;
        let k = 1;
        while (k < MAGIC.length && window[i + k] === MAGIC[k]) k++;
        if (k < MAGIC.length) continue;
        const rest = window.subarray(i);
        startCapture(base + i, new Uint8Array(0));
        feed(rest);
        if (pending) break;        // capturing: the rest of this chunk went in
      }
      carry = chunk.subarray(Math.max(0, chunk.length - (MAGIC.length - 1))).slice();
    };

    try {
      format = X().decodeDataModel(dataModel, onData).format;
    } catch (err) {
      if (!(err instanceof Found)) throw err;
      format = "XPress9";
    }
    if (!metadata) {
      const header = new TextDecoder("utf-16le").decode(head.subarray(72)).replace(/\u0000+$/, "");
      if (/<ApplyCompression>true</.test(header)) {
        throw new Error("this model compresses each file again inside the backup, which is not supported yet: save the report as .pbit, or use the INFO.* export");
      }
      throw new Error("no model metadata found inside the DataModel");
    }
    return { db: metadata, format };
  }

  function concat(parts, size) {
    const out = new Uint8Array(size);
    let at = 0;
    for (const p of parts) {
      const take = Math.min(p.length, size - at);
      out.set(p.subarray(0, take), at);
      at += take;
      if (at >= size) break;
    }
    return out;
  }

  // the metadata tables as rows shaped like the INFO.* query results
  function read(dataModel, label) {
    const { db, format } = extract(dataModel);
    const tablesAll = db.rows("Table");
    // drop the engine's own tables: hierarchy (H$), relationship (R$) and
    // user-hierarchy (U$) storage, date templates and auto date/time tables
    const keep = new Set(tablesAll
      .filter(t => !Number(t.SystemFlags) && !Number(t.IsPrivate) && !Number(t.ShowAsVariationsOnly))
      .map(t => t.ID));
    const tables = tablesAll.filter(t => keep.has(t.ID));
    const columns = db.rows("Column").filter(c => keep.has(c.TableID) && Number(c.Type) !== 3)
      .map(c => Object.assign({}, c, { DataType: c.ExplicitDataType }));
    const measures = db.rows("Measure").filter(m => keep.has(m.TableID));
    const relationships = db.rows("Relationship").filter(r => keep.has(r.FromTableID) && keep.has(r.ToTableID));
    return {
      format,
      counts: { tables: tables.length, columns: columns.length, measures: measures.length, relationships: relationships.length,
                systemTablesSkipped: tablesAll.length - tables.length },
      sources: [
        { label: `${label} › measures`, kind: "measures", rows: measures },
        { label: `${label} › tables`, kind: "tables", rows: tables },
        { label: `${label} › columns`, kind: "columns", rows: columns },
        { label: `${label} › relationships`, kind: "relationships", rows: relationships }
      ]
    };
  }

  const api = { read, extract, sqliteSize };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PbixModel = api;
})(typeof window !== "undefined" ? window : globalThis);
