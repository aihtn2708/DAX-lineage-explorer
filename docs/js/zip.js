/*
 * Read entries out of a zip without loading the whole file: .xlsx, .pbix and
 * .pbit are all zips, and a .pbix can be hundreds of MB of model data we never
 * need. Only the directory and the requested entries are sliced from the Blob.
 * Handles ZIP64. Inflates with the browser's DecompressionStream.
 * window.Zip.open(blobOrBytes) -> { names, has(name), bytes(name), text(name) }
 */
(function (root) {
  "use strict";

  const asBlob = input => (typeof Blob !== "undefined" && input instanceof Blob)
    ? input
    : new Blob([input instanceof ArrayBuffer || ArrayBuffer.isView(input) ? input : new Uint8Array(input)]);

  async function slice(blob, start, end) {
    return new Uint8Array(await blob.slice(start, end).arrayBuffer());
  }

  const u16 = (b, o) => b[o] | (b[o + 1] << 8);
  const u32 = (b, o) => (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;
  const u64 = (b, o) => u32(b, o) + u32(b, o + 4) * 0x100000000;

  async function inflateRaw(data) {
    if (typeof DecompressionStream === "undefined") {
      throw new Error("This browser cannot unzip files (no DecompressionStream). Use a current Chrome, Edge, Firefox or Safari.");
    }
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  function decode(bytes) {
    // Power BI writes Report/Layout and DataModelSchema as UTF-16LE, usually without a BOM
    if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder("utf-16le").decode(bytes.subarray(2));
    if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder("utf-16be").decode(bytes.subarray(2));
    if (bytes.length > 3 && bytes[1] === 0 && bytes[3] === 0 && bytes[0] !== 0) return new TextDecoder("utf-16le").decode(bytes);
    if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) return new TextDecoder("utf-8").decode(bytes.subarray(3));
    return new TextDecoder("utf-8").decode(bytes);
  }

  async function open(input) {
    const blob = asBlob(input);
    const size = blob.size;
    const tailLength = Math.min(size, 65557 + 20);
    const tail = await slice(blob, size - tailLength, size);
    let eocd = -1;
    for (let i = tail.length - 22; i >= 0; i--) {
      if (u32(tail, i) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("This is not a zip file.");
    let count = u16(tail, eocd + 10);
    let dirSize = u32(tail, eocd + 12);
    let dirOffset = u32(tail, eocd + 16);
    if (dirOffset === 0xffffffff || count === 0xffff || dirSize === 0xffffffff) {
      // ZIP64: the locator sits just before the classic end record
      const loc = eocd - 20;
      if (loc < 0 || u32(tail, loc) !== 0x07064b50) throw new Error("Damaged ZIP64 file.");
      const recordAt = u64(tail, loc + 8);
      const record = await slice(blob, recordAt, recordAt + 56);
      if (u32(record, 0) !== 0x06064b50) throw new Error("Damaged ZIP64 directory.");
      count = u64(record, 32);
      dirSize = u64(record, 40);
      dirOffset = u64(record, 48);
    }
    const dir = await slice(blob, dirOffset, dirOffset + dirSize);
    const entries = new Map();
    const utf8 = new TextDecoder("utf-8");
    let p = 0;
    for (let n = 0; n < count && p + 46 <= dir.length; n++) {
      if (u32(dir, p) !== 0x02014b50) throw new Error("The zip directory is damaged.");
      const method = u16(dir, p + 10);
      let compressedSize = u32(dir, p + 20);
      let uncompressedSize = u32(dir, p + 24);
      const nameLength = u16(dir, p + 28), extraLength = u16(dir, p + 30), commentLength = u16(dir, p + 32);
      let localOffset = u32(dir, p + 42);
      const name = utf8.decode(dir.subarray(p + 46, p + 46 + nameLength));
      // ZIP64 extra field: only the fields that overflowed are present, in this order
      let e = p + 46 + nameLength;
      const extraEnd = e + extraLength;
      while (e + 4 <= extraEnd) {
        const id = u16(dir, e), len = u16(dir, e + 2);
        if (id === 0x0001) {
          let q = e + 4;
          if (uncompressedSize === 0xffffffff) { uncompressedSize = u64(dir, q); q += 8; }
          if (compressedSize === 0xffffffff) { compressedSize = u64(dir, q); q += 8; }
          if (localOffset === 0xffffffff) { localOffset = u64(dir, q); q += 8; }
        }
        e += 4 + len;
      }
      entries.set(name.replace(/\\/g, "/"), { method, compressedSize, uncompressedSize, localOffset });
      p += 46 + nameLength + extraLength + commentLength;
    }

    const find = name => entries.get(name) || entries.get(name.replace(/^\//, "")) ||
      [...entries.entries()].find(([k]) => k.toLowerCase() === name.toLowerCase().replace(/^\//, ""))?.[1];

    async function bytes(name) {
      const entry = find(name);
      if (!entry) return null;
      const header = await slice(blob, entry.localOffset, entry.localOffset + 30);
      if (u32(header, 0) !== 0x04034b50) throw new Error(`Damaged zip entry: ${name}`);
      const start = entry.localOffset + 30 + u16(header, 26) + u16(header, 28);
      const raw = await slice(blob, start, start + entry.compressedSize);
      if (entry.method === 0) return raw;
      if (entry.method === 8) return inflateRaw(raw);
      throw new Error(`Unsupported zip compression (method ${entry.method}) in ${name}.`);
    }

    return {
      names: [...entries.keys()],
      has: name => !!find(name),
      bytes,
      text: async name => { const b = await bytes(name); return b == null ? null : decode(b); }
    };
  }

  const api = { open, decode, asBlob };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Zip = api;
})(typeof window !== "undefined" ? window : globalThis);
