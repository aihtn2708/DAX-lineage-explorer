"""The semantic model inside a .pbix, read without Power BI.

The same steps as docs/js/xpress9.js and docs/js/pbixmodel.js:
DataModel (XPress9) -> Analysis Services backup stream -> metadata.sqlitedb,
whose Table / Column / Measure / Relationship tables have the same columns
as the INFO.* queries. Decoding stops once that database is complete.

The XPress9 decoder is a port of Microsoft's (MIT License; published in
github.com/microsoft/Extensible-Storage-Engine, dev/ese/src/_xpress9/).
"""

from __future__ import annotations

import sqlite3
import struct
import tempfile
from pathlib import Path

MAGIC = 0x4E86D72A
SHORT_ALPHABET = 256 + ((24 + 4) << 4)
LONG_ALPHABET = 256
MAX_LONG_LENGTH = LONG_ALPHABET - 24
MAX_SHORT_LENGTH = 16
MAX_CODEWORD = 27
SMALL_TABLE_SIZE = MAX_CODEWORD + 6
FILL, ZERO_REPT, PREV, ROW_0, ROW_1 = (MAX_CODEWORD + i for i in range(1, 6))
TABLE_BITS = 12


class CorruptError(ValueError):
    def __init__(self, message: str):
        super().__init__(f"XPress9 data is corrupt: {message}")


class _Bits:
    """Least-significant-bit-first reader."""

    __slots__ = ("data", "byte", "buf", "count", "used")

    def __init__(self, data: bytes, start: int):
        self.data, self.byte, self.buf, self.count, self.used = data, start, 0, 0, 0

    def _fill(self, n: int) -> None:
        while self.count < n:
            chunk = self.data[self.byte:self.byte + 4]
            self.byte += 4
            self.buf |= int.from_bytes(chunk.ljust(4, b"\0"), "little") << self.count
            self.count += 32

    def peek(self, n: int) -> int:
        if self.count < n:
            self._fill(n)
        return self.buf & ((1 << n) - 1)

    def skip(self, n: int) -> None:
        if self.count < n:
            self._fill(n)
        self.buf >>= n
        self.count -= n
        self.used += n

    def read(self, n: int) -> int:
        value = self.peek(n)
        self.skip(n)
        return value


def _reverse(code: int, length: int) -> int:
    out = 0
    for _ in range(length):
        out = (out << 1) | (code & 1)
        code >>= 1
    return out


class _Huffman:
    __slots__ = ("bits", "table", "count", "sorted", "max_len", "single")

    def __init__(self, lengths):
        count = [0] * 32
        used, single, max_len = 0, -1, 0
        for sym, length in enumerate(lengths):
            if length:
                count[length] += 1
                used += 1
                single = sym
                max_len = max(max_len, length)
        if not used:
            raise CorruptError("empty Huffman table")
        self.bits = min(TABLE_BITS, max(1, max_len))
        self.table = [0] * (1 << self.bits)
        self.single = used == 1
        if self.single:
            self.table = [(single << 5) | lengths[single]] * (1 << self.bits)
            return
        left = 1
        for length in range(1, 32):
            left = left * 2 - count[length]
            if left < 0:
                raise CorruptError("over-subscribed Huffman table")
        if left:
            raise CorruptError("incomplete Huffman table")
        order = sorted((length, sym) for sym, length in enumerate(lengths) if length)
        self.sorted = [sym for _, sym in order]
        self.count, self.max_len = count, max_len
        code, k = 0, 0
        size = 1 << self.bits
        for length in range(1, max_len + 1):
            for _ in range(count[length]):
                if length <= self.bits:
                    entry, step = (self.sorted[k] << 5) | length, 1 << length
                    for j in range(_reverse(code, length), size, step):
                        self.table[j] = entry
                code += 1
                k += 1
            code <<= 1

    def decode(self, bits: _Bits) -> int:
        entry = self.table[bits.peek(self.bits)]
        if entry or self.single:
            bits.skip(entry & 31)
            return entry >> 5
        code = first = index = 0
        for length in range(1, self.max_len + 1):
            code |= bits.read(1)
            c = self.count[length]
            if code - first < c:
                return self.sorted[index + code - first]
            index += c
            first = (first + c) << 1
            code <<= 1
        raise CorruptError("bad Huffman code")


def _read_lengths(bits: _Bits, alphabet: int, fill: int) -> list:
    lengths = [0] * alphabet
    kind = bits.read(3)
    if kind == 0:
        width = alphabet.bit_length() - 1
        k = (1 << (width + 1)) - alphabet
        return [width if i < k else width + 1 for i in range(alphabet)]
    if kind != 1:
        raise CorruptError(f"unknown Huffman table encoding {kind}")
    small, prev = [0] * SMALL_TABLE_SIZE, 4
    for i in range(SMALL_TABLE_SIZE):
        if bits.read(1) == 0:
            small[i] = prev
        else:
            k = bits.read(3)
            if k >= prev:
                k += 1
                if k > 8:
                    raise CorruptError("small table length")
            small[i] = prev = k
    code, prev, i = _Huffman(small), 8, 0
    while i < alphabet:
        sym = code.decode(bits)
        if sym <= MAX_CODEWORD:
            lengths[i] = sym
            i += 1
            if sym:
                prev = sym
        elif sym == FILL:
            while True:
                lengths[i] = 0
                i += 1
                if not ((i & (fill - 1)) and i < alphabet):
                    break
        elif sym == ZERO_REPT:
            k = bits.read(2)
            n = k + 5
            if k == 3:
                while True:
                    k = bits.read(3)
                    n += k
                    if i + n > alphabet or (i ^ (i + n)) >= fill:
                        raise CorruptError("zero run")
                    if k != 7:
                        break
            if i + n > alphabet or (i ^ (i + n)) >= fill:
                raise CorruptError("zero run")
            while True:
                lengths[i] = 0
                i += 1
                n -= 1
                if not (i < alphabet and n):
                    break
        elif sym == PREV:
            lengths[i] = prev
            i += 1
        elif sym in (ROW_0, ROW_1):
            if i < fill:
                raise CorruptError("row reference before first row")
            value = lengths[i - fill] + (1 if sym == ROW_1 else 0)
            if value == 0 or value > MAX_CODEWORD:
                raise CorruptError("row length")
            lengths[i] = prev = value
            i += 1
        else:
            raise CorruptError(f"length symbol {sym}")
    return lengths


class Decoder:
    """One per independently compressed stream; history carries across chunks."""

    def __init__(self):
        self.session = None
        self.block_index = 0
        self.buf = bytearray()
        self.window = 1 << 16

    def decode_chunk(self, data: bytes, size: int) -> bytes:
        out, at = bytearray(), 0
        while len(out) < size:
            if at + 32 > len(data):
                raise CorruptError("chunk ends inside a block header")
            magic, orig, encoded_bits, flags, reserved, session, index, _crc = struct.unpack_from("<8I", data, at)
            if magic != MAGIC:
                raise CorruptError(f"bad block magic 0x{magic:x}")
            if reserved or flags >> 20:
                raise CorruptError("reserved fields set")
            params = {"table_bits": flags & 0x1FFF, "window_log": ((flags >> 13) & 7) + 16,
                      "mtf": ((flags >> 16) & 3) << 1, "ptr_min": ((flags >> 18) & 1) + 3,
                      "mtf_min": ((flags >> 19) & 1) + 2}
            if params["mtf"] > 4:
                raise CorruptError("MTF entry count")
            if self.session is None:
                self.session = {"signature": session, **params}
                self.window = 1 << params["window_log"]
            elif session != self.session["signature"] or any(
                    params[k] != self.session[k] for k in ("window_log", "mtf", "ptr_min", "mtf_min")):
                raise CorruptError("block parameters changed")
            if index != self.block_index:
                raise CorruptError(f"block {index} where {self.block_index} was expected")
            if encoded_bits <= params["table_bits"] + 256:
                raise CorruptError("encoded size")
            block_bytes = (encoded_bits + 7) // 8
            if at + block_bytes > len(data):
                raise CorruptError("block runs past the chunk")
            if len(out) + orig > size:
                raise CorruptError("chunk decodes to more than its declared size")
            if len(self.buf) > 2 * self.window:
                del self.buf[:len(self.buf) - self.window]
            start = len(self.buf)
            used = self._block(data, at, orig, params)
            if used != encoded_bits:
                raise CorruptError(f"block used {used} bits, header says {encoded_bits}")
            out += self.buf[start:start + orig]
            at += block_bytes
            self.block_index += 1
        if len(out) != size:
            raise CorruptError("chunk size mismatch")
        return bytes(out)

    def _block(self, data, at, orig, p) -> int:
        bits = _Bits(data, at + 32)
        mtf, last_ptr = [0, 0, 0, 0], 0
        if p["mtf"]:
            last_ptr = bits.read(1)
            for i in range(p["mtf"]):
                msb = bits.read(5)
                if msb >= p["window_log"]:
                    raise CorruptError("MTF offset")
                mtf[i] = bits.read(msb) + (1 << msb)
        short = _Huffman(_read_lengths(bits, SHORT_ALPHABET, MAX_SHORT_LENGTH))
        long_ = _Huffman(_read_lengths(bits, LONG_ALPHABET, MAX_SHORT_LENGTH))
        if bits.used != p["table_bits"]:
            raise CorruptError("Huffman table size")
        buf, m0, m1, m2, m3 = self.buf, *mtf
        n_mtf, end = p["mtf"], len(buf) + orig
        pos = len(buf)
        while pos < end:
            sym = short.decode(bits)
            if sym < 256:
                buf.append(sym)
                pos += 1
                last_ptr = 0
                continue
            sym -= 256
            length = sym & 15
            sym >>= 4
            if length == 15:
                length = long_.decode(bits)
                if length >= MAX_LONG_LENGTH:
                    extra = length - MAX_LONG_LENGTH
                    length = bits.read(extra) + (1 << extra) + MAX_LONG_LENGTH - 1
                length += 15
            if sym < n_mtf:
                length += p["mtf_min"]
                if last_ptr:
                    if n_mtf <= 2:
                        if sym:
                            raise CorruptError("MTF symbol")
                        offset = m1
                    elif sym == 0:
                        offset = m1
                    elif sym == 1:
                        offset, m2 = m2, m1
                    elif sym == 2:
                        offset, m3, m2 = m3, m2, m1
                    else:
                        raise CorruptError("MTF symbol")
                    m1, m0 = m0, offset
                elif sym == 0:
                    offset = m0
                elif sym == 1:
                    offset = m1
                    m1, m0 = m0, offset
                elif sym == 2:
                    offset = m2
                    m2, m1, m0 = m1, m0, offset
                else:
                    offset = m3
                    m3, m2, m1, m0 = m2, m1, m0, offset
            else:
                length += p["ptr_min"]
                msb = sym - n_mtf
                offset = bits.read(msb) + (1 << msb)
                if n_mtf > 2:
                    m3, m2 = m2, m1
                if n_mtf > 0:
                    m1, m0 = m0, offset
            if offset > pos:
                raise CorruptError("offset before the start of the data")
            if pos + length > end:
                raise CorruptError("match runs past the block")
            last_ptr = 1
            src = pos - offset
            if offset >= length:
                buf += buf[src:src + length]
            else:
                for k in range(length):
                    buf.append(buf[src + k])
            pos += length
        return bits.used + 256


SINGLE = "This backup was created using XPress9 compression."
MULTI = "This backup was created using multithreaded XPrs9."
STREAM_STORAGE = "STREAM_STORAGE_SIGNATURE_)!@#$%^&*("


class _Found(Exception):
    pass


def decode_datamodel(data: bytes, on_data) -> str:
    """Feed the decompressed backup stream to on_data(bytes) in order."""
    head = data[:102].decode("utf-16-le", "ignore")

    def group(at, chunks):
        decoder, c = Decoder(), 0
        while (at < len(data)) if chunks < 0 else (c < chunks):
            size, packed = struct.unpack_from("<II", data, at)
            on_data(decoder.decode_chunk(data[at + 8:at + 8 + packed], size))
            at += 8 + packed
            c += 1
        return at

    if SINGLE in head:
        group(102, -1)
        return "XPress9"
    if MULTI in head:
        main_per, prefix_per, prefix_threads, main_threads, _ = struct.unpack_from("<5Q", data, 102)
        at = 142
        for _ in range(prefix_threads):
            at = group(at, prefix_per)
        for _ in range(main_threads):
            at = group(at, main_per)
        return "XPress9 (multithreaded)"
    if STREAM_STORAGE in data[:72].decode("utf-16-le", "ignore"):
        on_data(data)
        return "uncompressed"
    raise ValueError("the DataModel is in a format this reader does not know")


def _sqlite_size(h: bytes) -> int:
    if len(h) < 100 or not (1 <= h[18] <= 2 and 1 <= h[19] <= 2) or h[21:24] != b"\x40\x20\x20":
        return 0
    page = int.from_bytes(h[16:18], "big")
    page = 65536 if page == 1 else page
    if page < 512 or page & (page - 1):
        return 0
    pages = int.from_bytes(h[28:32], "big")
    if not pages or h[24:28] != h[92:96]:
        return 0
    return page * pages


def _open_metadata(db: bytes):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlitedb", delete=False)
    tmp.write(db)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.row_factory = sqlite3.Row
    try:
        names = {r[0].lower() for r in con.execute("select name from sqlite_master where type = 'table'")}
    except sqlite3.DatabaseError:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)
        return None
    if not {"measure", "table", "column"} <= names:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)
        return None
    return con, tmp.name


def read_model(data_model: bytes) -> dict:
    """Rows shaped like the INFO.* queries, from a .pbix DataModel."""
    state = {"head": b"", "pending": None, "carry": b"", "found": None}
    magic = b"SQLite format 3\x00"

    def feed(chunk: bytes):
        pend = state["pending"]
        want = pend["size"] - len(pend["buf"]) if pend["size"] else len(chunk)
        pend["buf"] += chunk[:want]
        if not pend["size"] and len(pend["buf"]) >= 100:
            pend["size"] = _sqlite_size(bytes(pend["buf"][:100]))
            if not pend["size"]:
                state["pending"] = None
                return
            del pend["buf"][pend["size"]:]
        if pend["size"] and len(pend["buf"]) >= pend["size"]:
            state["pending"] = None
            opened = _open_metadata(bytes(pend["buf"]))
            if opened:
                state["found"] = opened
                raise _Found()

    def on_data(chunk: bytes):
        if len(state["head"]) < 4096:
            state["head"] += chunk[:4096 - len(state["head"])]
        if state["pending"]:
            feed(chunk)
            return
        window = state["carry"] + chunk
        i = window.find(magic)
        while i >= 0:
            state["pending"] = {"buf": bytearray(), "size": 0}
            feed(window[i:])
            if state["pending"]:
                break
            i = window.find(magic, i + 1)
        state["carry"] = chunk[-(len(magic) - 1):]

    try:
        decode_datamodel(data_model, on_data)
    except _Found:
        pass
    if not state["found"]:
        if b"<\x00A\x00p\x00p\x00l\x00y\x00C\x00o\x00m\x00p\x00r\x00e\x00s\x00s\x00i\x00o\x00n\x00>\x00t\x00r\x00u\x00e" in state["head"]:
            raise ValueError("this model compresses each file again inside the backup, which is not supported yet")
        raise ValueError("no model metadata found inside the DataModel")
    con, path = state["found"]
    try:
        rows = lambda table: [dict(r) for r in con.execute(f"select * from [{table}]")]
        all_tables = rows("Table")
        keep = {t["ID"] for t in all_tables
                if not (t.get("SystemFlags") or t.get("IsPrivate") or t.get("ShowAsVariationsOnly"))}
        columns = [{**c, "DataType": c.get("ExplicitDataType")} for c in rows("Column")
                   if c["TableID"] in keep and c.get("Type") != 3]
        return {
            "measures": [m for m in rows("Measure") if m["TableID"] in keep],
            "tables": [t for t in all_tables if t["ID"] in keep],
            "columns": columns,
            "relationships": [r for r in rows("Relationship") if r["FromTableID"] in keep and r["ToTableID"] in keep],
        }
    finally:
        con.close()
        Path(path).unlink(missing_ok=True)
