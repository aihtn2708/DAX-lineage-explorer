/*
 * A minimal XPress9 *encoder*, for tests only: it writes blocks the way
 * Microsoft's format lays them out (see docs/js/xpress9.js) using the fixed
 * "kind 0" code tables and a greedy LZ77 search, so the decoder can be tested
 * on literals, matches, overlapping copies, long lengths, several blocks per
 * chunk and history carried from chunk to chunk.
 */
"use strict";

class BitWriter {
  constructor() { this.bytes = []; this.acc = 0; this.n = 0; this.total = 0; }
  bits(value, count) {               // least significant bit first
    for (let i = 0; i < count; i++) this.bit((Math.floor(value / 2 ** i)) & 1);
  }
  code(code, len) {                   // Huffman codes go most significant bit first
    for (let i = len - 1; i >= 0; i--) this.bit((code >>> i) & 1);
  }
  bit(b) {
    this.acc |= b << this.n; this.n++; this.total++;
    if (this.n === 8) { this.bytes.push(this.acc); this.acc = 0; this.n = 0; }
  }
  finish() { if (this.n) this.bytes.push(this.acc); return Uint8Array.from(this.bytes); }
}

// kind-0 tables: near-uniform lengths, canonical codes
function uniformCodes(alphabet) {
  const bits = 31 - Math.clz32(alphabet), k = (1 << (bits + 1)) - alphabet;
  const codes = [];
  let code = 0;
  for (let s = 0; s < k; s++) codes.push([code++, bits]);
  code <<= 1;
  for (let s = k; s < alphabet; s++) codes.push([code++, bits + 1]);
  return codes;
}
const SHORT = uniformCodes(704), LONG = uniformCodes(256);

function encodeBlock(data, start, end, history, blockIndex) {
  const w = new BitWriter();
  w.bits(0, 3); w.bits(0, 3);                     // both tables: kind 0
  const tableBits = w.total;
  let pos = start;
  while (pos < end) {
    // greedy search, preferring the longest match within 4 KB
    let bestLen = 0, bestOff = 0;
    for (let off = 1; off <= Math.min(4096, pos - history); off++) {
      let len = 0;
      while (pos + len < end && data[pos + len - off] === data[pos + len] && len < 600) len++;
      if (len > bestLen) { bestLen = len; bestOff = off; }
    }
    if (bestLen >= 3) {
      const msb = 31 - Math.clz32(bestOff), lc = bestLen - 3;
      w.code(...SHORT[256 + (msb << 4) + Math.min(lc, 15)]);
      if (lc >= 15) {
        const v = lc - 15;
        if (v < 232) w.code(...LONG[v]);
        else {
          const bits = 31 - Math.clz32(v - 231);
          w.code(...LONG[232 + bits]);
          w.bits(v - 231 - 2 ** bits, bits);
        }
      }
      w.bits(bestOff - 2 ** msb, msb);
      pos += bestLen;
    } else {
      w.code(...SHORT[data[pos]]);
      pos++;
    }
  }
  const body = w.finish();
  const encodedBits = 256 + w.total;
  const out = new Uint8Array(32 + body.length);
  const v = new DataView(out.buffer);
  const flags = tableBits;                        // window 64 KB, no MTF, min match 3
  [0x4e86d72a, end - start, encodedBits, flags, 0, 0x1234abcd, blockIndex, 0].forEach((x, i) => v.setUint32(4 * i, x >>> 0, true));
  out.set(body, 32);
  return out;
}

// one stream: chunks of blocks, history shared across them
function encodeStream(data, chunkSize = 20000, blockSize = 7000) {
  const chunks = [];
  let block = 0;
  for (let c = 0; c < data.length; c += chunkSize) {
    const cEnd = Math.min(data.length, c + chunkSize), parts = [];
    for (let b = c; b < cEnd; b += blockSize) parts.push(encodeBlock(data, b, Math.min(cEnd, b + blockSize), 0, block++));
    const packed = Buffer.concat(parts.map(p => Buffer.from(p)));
    chunks.push({ size: cEnd - c, packed });
  }
  return chunks;
}

function dataModel(data) {
  const sig = Buffer.alloc(102);
  sig.write("This backup was created using XPress9 compression.", 0, "utf16le");
  const parts = [sig];
  for (const { size, packed } of encodeStream(data)) {
    const h = Buffer.alloc(8);
    h.writeUInt32LE(size, 0); h.writeUInt32LE(packed.length, 4);
    parts.push(h, packed);
  }
  return Buffer.concat(parts);
}

module.exports = { encodeStream, encodeBlock, dataModel };
