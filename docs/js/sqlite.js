/*
 * A read-only SQLite table reader, enough to read the metadata database
 * (metadata.sqlitedb) that Power BI keeps inside a .pbix model.
 * Walks table b-trees, follows overflow pages, decodes records.
 * window.SqliteReader.open(bytes) -> { tables(), rows(tableName) }
 */
(function (root) {
  "use strict";

  function open(bytes) {
    const magic = new TextDecoder().decode(bytes.subarray(0, 15));
    if (magic !== "SQLite format 3") throw new Error("not a SQLite database");
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let pageSize = view.getUint16(16);
    if (pageSize === 1) pageSize = 65536;
    const reserved = bytes[20];
    const usable = pageSize - reserved;
    const enc = view.getUint32(56);
    const text = new TextDecoder(enc === 2 ? "utf-16le" : enc === 3 ? "utf-16be" : "utf-8");

    const pageAt = n => (n - 1) * pageSize;

    function varint(p) {
      let v = 0;
      for (let i = 0; i < 8; i++) {
        const b = bytes[p + i];
        v = v * 128 + (b & 0x7f);
        if (!(b & 0x80)) return [v, p + i + 1];
      }
      return [v * 256 + bytes[p + 8], p + 9];
    }

    // the whole payload of a cell, gathered from its overflow pages
    function payload(p, total) {
      const maxLocal = usable - 35;
      if (total <= maxLocal) return bytes.subarray(p, p + total);
      const minLocal = ((usable - 12) * 32 / 255 | 0) - 23;
      let local = minLocal + (total - minLocal) % (usable - 4);
      if (local > maxLocal) local = minLocal;
      const out = new Uint8Array(total);
      out.set(bytes.subarray(p, p + local));
      let filled = local, next = view.getUint32(p + local);
      while (filled < total && next) {
        const at = pageAt(next);
        const take = Math.min(usable - 4, total - filled);
        out.set(bytes.subarray(at + 4, at + 4 + take), filled);
        filled += take;
        next = view.getUint32(at);
      }
      return out;
    }

    function record(data) {
      const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
      let [headerSize, p] = varintIn(data, 0);
      const types = [];
      while (p < headerSize) { const [t, q] = varintIn(data, p); types.push(t); p = q; }
      let at = headerSize;
      return types.map(t => {
        if (t === 0) return null;
        if (t >= 1 && t <= 6) {
          const len = [0, 1, 2, 3, 4, 6, 8][t];
          if (len === 8) {
            // exact: 64-bit values past 2^53 come back as strings
            const big = dv.getBigInt64(at);
            at += 8;
            return big >= -9007199254740991n && big <= 9007199254740991n ? Number(big) : big.toString();
          }
          let v = 0;
          for (let i = 0; i < len; i++) v = v * 256 + data[at + i];
          if (data[at] & 0x80) v -= 2 ** (8 * len);      // two's complement
          at += len;
          return v;
        }
        if (t === 7) { const v = dv.getFloat64(at); at += 8; return v; }
        if (t === 8) return 0;
        if (t === 9) return 1;
        if (t >= 12) {
          const len = (t - (t % 2 ? 13 : 12)) / 2;
          const slice = data.subarray(at, at + len);
          at += len;
          return t % 2 ? text.decode(slice) : slice;
        }
        return null;
      });
    }
    function varintIn(data, p) {
      let v = 0;
      for (let i = 0; i < 8; i++) {
        const b = data[p + i];
        v = v * 128 + (b & 0x7f);
        if (!(b & 0x80)) return [v, p + i + 1];
      }
      return [v * 256 + data[p + 8], p + 9];
    }

    // every row of a table b-tree, as [rowid, values]
    function scan(rootPage) {
      const rows = [], stack = [rootPage], seen = new Set();
      while (stack.length) {
        const page = stack.pop();
        if (seen.has(page)) throw new Error("SQLite b-tree loops");
        seen.add(page);
        const base = pageAt(page), h = base + (page === 1 ? 100 : 0);
        const type = bytes[h];
        const cells = view.getUint16(h + 3);
        const ptrs = h + (type === 0x05 ? 12 : 8);
        if (type === 0x05) {
          const children = [];
          for (let i = 0; i < cells; i++) children.push(view.getUint32(base + view.getUint16(ptrs + 2 * i)));
          children.push(view.getUint32(h + 8));
          for (let i = children.length - 1; i >= 0; i--) stack.push(children[i]);
        } else if (type === 0x0d) {
          for (let i = 0; i < cells; i++) {
            let p = base + view.getUint16(ptrs + 2 * i);
            let size; [size, p] = varint(p);
            let rowid; [rowid, p] = varint(p);
            rows.push([rowid, record(payload(p, size))]);
          }
        } else {
          throw new Error(`unexpected SQLite page type ${type}`);
        }
      }
      return rows;
    }

    const schema = scan(1).map(([, r]) => ({ type: r[0], name: r[1], table: r[2], rootpage: r[3], sql: r[4] }));

    function columnsOf(sql) {
      const body = sql.slice(sql.indexOf("(") + 1, sql.lastIndexOf(")"));
      const cols = [];
      let depth = 0, cur = "";
      for (const ch of body) {
        if (ch === "(") depth++;
        if (ch === ")") depth--;
        if (ch === "," && depth === 0) { cols.push(cur); cur = ""; } else cur += ch;
      }
      if (cur.trim()) cols.push(cur);
      const parts = cols.map(c => c.trim());
      const constraint = /^(PRIMARY|UNIQUE|FOREIGN|CHECK|CONSTRAINT)\b/i;
      const out = parts.filter(c => !constraint.test(c)).map(c => {
        const m = c.match(/^(?:\[([^\]]+)\]|"([^"]+)"|`([^`]+)`|(\S+))\s*(.*)$/);
        const decl = (m[5] || "").trim();
        return { name: m[1] || m[2] || m[3] || m[4], integer: /^INTEGER\b/i.test(decl),
                 rowid: /^INTEGER\s+PRIMARY\s+KEY/i.test(decl) };
      });
      // PRIMARY KEY (col) on a single INTEGER column also makes it the rowid
      const pk = parts.find(c => /^PRIMARY\s+KEY\s*\(/i.test(c));
      if (pk) {
        const names = pk.slice(pk.indexOf("(") + 1, pk.lastIndexOf(")")).split(",")
          .map(s => s.trim().replace(/\s+(ASC|DESC)$/i, "").replace(/^[\["`]|[\]"`]$/g, ""));
        const col = names.length === 1 && out.find(c => c.name === names[0]);
        if (col && col.integer) col.rowid = true;
      }
      return out;
    }

    return {
      tables: () => schema.filter(s => s.type === "table").map(s => s.name),
      rows(name) {
        const entry = schema.find(s => s.type === "table" && s.name.toLowerCase() === name.toLowerCase());
        if (!entry) return [];
        const cols = columnsOf(entry.sql);
        return scan(entry.rootpage).map(([rowid, values]) => {
          const row = {};
          cols.forEach((c, i) => { row[c.name] = c.rowid ? rowid : (i < values.length ? values[i] : null); });
          return row;
        });
      }
    };
  }

  const api = { open };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.SqliteReader = api;
})(typeof window !== "undefined" ? window : globalThis);
