/*
 * XPress9 decompression in plain JavaScript, for the model inside a .pbix.
 *
 * A port of the decoder in Microsoft's XPress9 library (Copyright (c)
 * Microsoft Corporation, MIT License; published in
 * github.com/microsoft/Extensible-Storage-Engine, dev/ese/src/_xpress9/).
 * Only decoding is ported, and only what a Power BI DataModel stream needs.
 *
 * Format, as the C decoder reads it:
 *   block   = 32-byte header (8 x u32 LE) + bit stream
 *             [0] magic 0x4e86d72a  [1] decoded size  [2] encoded size in bits,
 *             header included  [3] flags  [4] 0  [5] session  [6] block index
 *             [7] header CRC
 *   flags   = 13 bits Huffman-table size in bits, 3 bits log2(window) - 16,
 *             2 bits MTF entries / 2, 1 bit pointer min match - 3,
 *             1 bit MTF min match - 2, rest 0
 *   stream  = LSB-first bits read in 32-bit little-endian words:
 *             MTF state (if any), two code-length tables, then LZ77 symbols.
 *             Huffman codes are canonical, most significant bit first.
 *   Blocks follow each other on byte boundaries; the history window and the
 *   block counter carry on from block to block and chunk to chunk.
 *
 * window.Xpress9.Decoder: new Decoder(); decoder.decodeChunk(bytes, size) -> Uint8Array
 */
(function (root) {
  "use strict";

  const MAGIC = 0x4e86d72a;
  const SHORT_ALPHABET = 256 + ((24 + 4) << 4);   // 704
  const LONG_ALPHABET = 1 << 8;                    // 256
  const MAX_LONG_LENGTH = LONG_ALPHABET - 24;      // 232
  const MAX_SHORT_LENGTH = 16;
  const MAX_CODEWORD = 27;
  const SMALL_TABLE_SIZE = MAX_CODEWORD + 6;       // 33
  const FILL = MAX_CODEWORD + 1, ZERO_REPT = MAX_CODEWORD + 2, PREV = MAX_CODEWORD + 3,
        ROW_0 = MAX_CODEWORD + 4, ROW_1 = MAX_CODEWORD + 5;
  const TABLE_BITS = 12;

  class CorruptError extends Error {
    constructor(message) { super(`XPress9 data is corrupt: ${message}`); this.name = "CorruptError"; }
  }

  /* ---------- bits, least significant first ---------- */

  class BitReader {
    constructor(bytes, byteOffset) {
      this.d = bytes;
      this.p = byteOffset * 8;
    }
    peek(n) {                      // n <= 24
      const b = this.p >>> 3, d = this.d;
      const word = (d[b] | (d[b + 1] << 8) | (d[b + 2] << 16) | (d[b + 3] << 24)) >>> (this.p & 7);
      return n === 0 ? 0 : (word & (0xffffffff >>> (32 - n))) >>> 0;
    }
    skip(n) { this.p += n; }
    read(n) {
      if (n <= 24) { const v = this.peek(n); this.p += n; return v; }
      const lo = this.read(16);
      return lo + this.read(n - 16) * 65536;
    }
  }

  /* ---------- canonical Huffman ---------- */

  function reverse(code, len) {
    let r = 0;
    for (let i = 0; i < len; i++) { r = (r << 1) | (code & 1); code >>>= 1; }
    return r;
  }

  class Huffman {
    constructor(lengths) {
      const n = lengths.length;
      let maxLen = 0, used = 0, single = -1;
      const count = new Uint32Array(32);
      for (let s = 0; s < n; s++) {
        const len = lengths[s];
        if (len) { count[len]++; used++; single = s; if (len > maxLen) maxLen = len; }
      }
      if (used === 0) throw new CorruptError("empty Huffman table");
      this.bits = Math.min(TABLE_BITS, Math.max(1, maxLen));
      this.table = new Int32Array(1 << this.bits);
      if (used === 1) {
        // one symbol: every lookup decodes it and consumes its stated length
        this.table.fill((single << 5) | lengths[single]);
        this.single = true;
        return;
      }
      // a complete prefix code, as HuffmanDecodeVerifyTree demands
      let left = 1;
      for (let len = 1; len < 32; len++) {
        left = left * 2 - count[len];
        if (left < 0) throw new CorruptError("over-subscribed Huffman table");
      }
      if (left !== 0) throw new CorruptError("incomplete Huffman table");
      // canonical order: by length, then by symbol
      const offs = new Uint32Array(32);
      for (let len = 1; len < 32; len++) offs[len] = offs[len - 1] + count[len - 1];
      const sorted = new Uint16Array(used);
      for (let s = 0; s < n; s++) if (lengths[s]) sorted[offs[lengths[s]]++] = s;
      this.count = count;
      this.sorted = sorted;
      this.maxLen = maxLen;
      let code = 0, k = 0;
      for (let len = 1; len <= maxLen; len++) {
        for (let i = 0; i < count[len]; i++, k++) {
          const sym = sorted[k];
          if (len <= this.bits) {
            const rev = reverse(code, len), step = 1 << len, entry = (sym << 5) | len;
            for (let j = rev; j < this.table.length; j += step) this.table[j] = entry;
          }
          code++;
        }
        code <<= 1;
      }
    }
    decode(br) {
      const e = this.table[br.peek(this.bits)];
      if (e !== 0 || this.single) { br.skip(e & 31); return e >>> 5; }
      // longer than the table: walk the canonical code a bit at a time
      let code = 0, first = 0, index = 0;
      for (let len = 1; len <= this.maxLen; len++) {
        code |= br.read(1);
        const c = this.count[len];
        if (code - first < c) return this.sorted[index + code - first];
        index += c; first = (first + c) << 1; code <<= 1;
      }
      throw new CorruptError("bad Huffman code");
    }
  }

  // HuffmanDecodeLengthTable
  function readLengths(br, alphabetSize, fillBoundary) {
    const lengths = new Uint8Array(alphabetSize);
    const kind = br.read(3);
    if (kind === 0) {
      const bits = 31 - Math.clz32(alphabetSize);
      const k = (1 << (bits + 1)) - alphabetSize;
      for (let i = 0; i < alphabetSize; i++) lengths[i] = i < k ? bits : bits + 1;
      return lengths;
    }
    if (kind !== 1) throw new CorruptError(`unknown Huffman table encoding ${kind}`);
    const small = new Uint8Array(SMALL_TABLE_SIZE);
    let prev = 4;
    for (let i = 0; i < SMALL_TABLE_SIZE; i++) {
      if (br.read(1) === 0) small[i] = prev;
      else {
        let k = br.read(3);
        if (k >= prev) { k += 1; if (k > 8) throw new CorruptError("small table length"); }
        small[i] = k; prev = k;
      }
    }
    const smallCode = new Huffman(small);
    prev = 8;
    let i = 0;
    while (i < alphabetSize) {
      const sym = smallCode.decode(br);
      if (sym <= MAX_CODEWORD) {
        lengths[i++] = sym;
        if (sym !== 0) prev = sym;
      } else if (sym === FILL) {
        do { lengths[i++] = 0; } while ((i & (fillBoundary - 1)) !== 0 && i < alphabetSize);
      } else if (sym === ZERO_REPT) {
        let k = br.read(2), n = k + 5;
        if (k === 3) {
          do {
            k = br.read(3); n += k;
            if (i + n > alphabetSize || (i ^ (i + n)) >= fillBoundary) throw new CorruptError("zero run");
          } while (k === 7);
        }
        if (i + n > alphabetSize || (i ^ (i + n)) >= fillBoundary) throw new CorruptError("zero run");
        do { lengths[i++] = 0; n--; } while (i < alphabetSize && n !== 0);
      } else if (sym === PREV) {
        lengths[i++] = prev;
      } else if (sym === ROW_0 || sym === ROW_1) {
        if (i < fillBoundary) throw new CorruptError("row reference before first row");
        const v = lengths[i - fillBoundary] + (sym === ROW_1 ? 1 : 0);
        if (v === 0 || v > MAX_CODEWORD) throw new CorruptError("row length");
        lengths[i++] = v; prev = v;
      } else {
        throw new CorruptError(`length symbol ${sym}`);
      }
    }
    return lengths;
  }

  /* ---------- the decoder: one per independently compressed stream ---------- */

  class Decoder {
    constructor() {
      this.session = null;      // parameters fixed by the first block
      this.blockIndex = 0;
      this.buf = new Uint8Array(1 << 20);
      this.pos = 0;             // bytes in buf; the window is the tail of it
      this.window = 1 << 16;
    }

    reserve(n) {
      if (this.pos + n <= this.buf.length) return;
      if (this.pos > this.window) {          // keep only the window as history
        this.buf.copyWithin(0, this.pos - this.window, this.pos);
        this.pos = this.window;
      }
      if (this.pos + n > this.buf.length) {
        const grown = new Uint8Array(Math.max(this.pos + n, this.buf.length * 2));
        grown.set(this.buf.subarray(0, this.pos));
        this.buf = grown;
      }
    }

    // Decode one chunk of whole blocks; returns exactly `size` bytes.
    decodeChunk(input, size) {
      const bytes = new Uint8Array(input.length + 16);   // zero padding for the bit reader
      bytes.set(input);
      const out = new Uint8Array(size);
      let produced = 0, at = 0;
      const view = new DataView(bytes.buffer);
      while (produced < size) {
        if (at + 32 > input.length) throw new CorruptError("chunk ends inside a block header");
        const u = i => view.getUint32(at + 4 * i, true);
        if (u(0) !== MAGIC) throw new CorruptError(`bad block magic 0x${u(0).toString(16)}`);
        const origSize = u(1), encodedBits = u(2), flags = u(3);
        if (u(4) !== 0) throw new CorruptError("reserved header field is not zero");
        if (flags >>> 20) throw new CorruptError("reserved flag bits set");
        const params = {
          tableBits: flags & 0x1fff,
          windowLog: ((flags >>> 13) & 7) + 16,
          mtfCount: ((flags >>> 16) & 3) << 1,
          ptrMin: ((flags >>> 18) & 1) + 3,
          mtfMin: ((flags >>> 19) & 1) + 2
        };
        if (params.mtfCount > 4) throw new CorruptError("MTF entry count");
        if (!this.session) {
          this.session = { signature: u(5), ...params };
          this.window = 1 << params.windowLog;
        } else {
          const s = this.session;
          if (u(5) !== s.signature) throw new CorruptError("session signature changed");
          if (params.windowLog !== s.windowLog || params.mtfCount !== s.mtfCount ||
              params.ptrMin !== s.ptrMin || params.mtfMin !== s.mtfMin) throw new CorruptError("block parameters changed");
        }
        if (u(6) !== this.blockIndex) throw new CorruptError(`block ${u(6)} where ${this.blockIndex} was expected`);
        if (encodedBits <= params.tableBits + 256) throw new CorruptError("encoded size");
        const blockBytes = Math.ceil(encodedBits / 8);
        if (at + blockBytes > input.length) throw new CorruptError("block runs past the chunk");
        if (produced + origSize > size) throw new CorruptError("chunk decodes to more than its declared size");

        this.reserve(origSize);
        const start = this.pos;
        const used = this.decodeBlock(bytes, at, origSize, params);
        if (used !== encodedBits) throw new CorruptError(`block used ${used} bits, header says ${encodedBits}`);
        out.set(this.buf.subarray(start, start + origSize), produced);
        produced += origSize;
        at += blockBytes;
        this.blockIndex++;
      }
      if (produced !== size) throw new CorruptError("chunk size mismatch");
      return out;
    }

    // Xpress9DecodeState + Xpress9Lz77Dec for one block; returns bits consumed
    decodeBlock(bytes, at, origSize, s) {
      const br = new BitReader(bytes, at + 32);
      const mtf = [0, 0, 0, 0];
      let lastPtr = 0;
      if (s.mtfCount > 0) {
        lastPtr = br.read(1);
        for (let i = 0; i < s.mtfCount; i++) {
          const msb = br.read(5);
          if (msb >= s.windowLog) throw new CorruptError("MTF offset");
          mtf[i] = br.read(msb) + 2 ** msb;
        }
      }
      const shortCode = new Huffman(readLengths(br, SHORT_ALPHABET, MAX_SHORT_LENGTH));
      const longCode = new Huffman(readLengths(br, LONG_ALPHABET, MAX_SHORT_LENGTH));
      if (br.p - (at + 32) * 8 !== s.tableBits) throw new CorruptError("Huffman table size");

      const buf = this.buf;
      let pos = this.pos;
      const end = pos + origSize;
      let [m0, m1, m2, m3] = mtf;
      const mtfCount = s.mtfCount;
      while (pos < end) {
        let sym = shortCode.decode(br);
        if (sym < 256) {                      // literal
          buf[pos++] = sym;
          lastPtr = 0;
          continue;
        }
        sym -= 256;
        let length = sym & (MAX_SHORT_LENGTH - 1);
        sym >>>= 4;
        if (length === MAX_SHORT_LENGTH - 1) {
          length = longCode.decode(br);
          if (length >= MAX_LONG_LENGTH) {
            const bits = length - MAX_LONG_LENGTH;
            length = br.read(bits) + 2 ** bits + MAX_LONG_LENGTH - 1;
          }
          length += MAX_SHORT_LENGTH - 1;
        }
        let offset;
        if (sym < mtfCount) {                 // a recently used offset
          length += s.mtfMin;
          if (lastPtr) {
            if (mtfCount <= 2) {
              if (sym !== 0) throw new CorruptError("MTF symbol");
              offset = m1;
            } else if (sym === 0) offset = m1;
            else if (sym === 1) { offset = m2; m2 = m1; }
            else if (sym === 2) { offset = m3; m3 = m2; m2 = m1; }
            else throw new CorruptError("MTF symbol");
            m1 = m0; m0 = offset;
          } else if (sym === 0) offset = m0;
          else if (sym === 1) { offset = m1; m1 = m0; m0 = offset; }
          else if (sym === 2) { offset = m2; m2 = m1; m1 = m0; m0 = offset; }
          else { offset = m3; m3 = m2; m2 = m1; m1 = m0; m0 = offset; }
        } else {                               // a new offset
          length += s.ptrMin;
          const msb = sym - mtfCount;
          offset = br.read(msb) + 2 ** msb;
          if (mtfCount > 2) { m3 = m2; m2 = m1; }
          if (mtfCount > 0) { m1 = m0; m0 = offset; }
        }
        if (offset > pos) throw new CorruptError("offset before the start of the data");
        if (pos + length > end) throw new CorruptError("match runs past the block");
        lastPtr = 1;
        let from = pos - offset;
        const stop = pos + length;
        if (offset >= length) { buf.copyWithin(pos, from, from + length); pos = stop; }
        else while (pos < stop) buf[pos++] = buf[from++];
      }
      this.pos = pos;
      return br.p - at * 8;
    }
  }

  /* ---------- Power BI DataModel framing ---------- */

  const SINGLE = "This backup was created using XPress9 compression.";
  const MULTI = "This backup was created using multithreaded XPrs9.";
  const STREAM_STORAGE = "STREAM_STORAGE_SIGNATURE_)!@#$%^&*(";

  function signature(bytes) {
    return new TextDecoder("utf-16le").decode(bytes.subarray(0, 102));
  }

  // Decode a DataModel stream. `onData(bytes, offset)` receives the
  // decompressed ABF in order; nothing else is kept, so memory stays bounded.
  function decodeDataModel(bytes, onData) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const head = signature(bytes);
    let offset = 0;
    const emit = chunk => { onData(chunk, offset); offset += chunk.length; };
    const runGroup = (at, chunks) => {
      const decoder = new Decoder();
      for (let c = 0; chunks < 0 ? at < bytes.length : c < chunks; c++) {
        if (at + 8 > bytes.length) throw new CorruptError("stream ends inside a chunk header");
        const size = view.getUint32(at, true), packed = view.getUint32(at + 4, true);
        if (at + 8 + packed > bytes.length) throw new CorruptError("stream ends inside a chunk");
        emit(decoder.decodeChunk(bytes.subarray(at + 8, at + 8 + packed), size));
        at += 8 + packed;
      }
      return at;
    };
    if (head.includes(SINGLE)) {
      runGroup(102, -1);
      return { format: "XPress9", size: offset };
    }
    if (head.includes(MULTI)) {
      const u64 = at => Number(view.getBigUint64(at, true));
      const mainPerThread = u64(102), prefixPerThread = u64(110);
      const prefixThreads = u64(118), mainThreads = u64(126);
      let at = 102 + 40;
      for (let g = 0; g < prefixThreads; g++) at = runGroup(at, prefixPerThread);
      for (let g = 0; g < mainThreads; g++) at = runGroup(at, mainPerThread);
      return { format: "XPress9 (multithreaded)", size: offset };
    }
    if (new TextDecoder("utf-16le").decode(bytes.subarray(0, 72)).includes(STREAM_STORAGE)) {
      emit(bytes);
      return { format: "uncompressed", size: offset };
    }
    throw new Error("The DataModel is in a format this reader does not know.");
  }

  const api = { Decoder, decodeDataModel, CorruptError };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Xpress9 = api;
})(typeof window !== "undefined" ? window : globalThis);
