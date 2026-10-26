"""
Minimal read-only reader for Jet4 / MSISAM databases (Microsoft Money .mny).

Scope: enough of the format to enumerate tables and read rows.  Writing,
indexes and relationships are not implemented.

MSISAM notes
------------
* Page size is 4096 and the on-disk layout is Jet4, but the signature at the
  start of the file is "MSISAM Database" rather than "Standard Jet DB".
* Money encrypts only pages 1..14 (the system catalogue).  Every table
  definition page and every data page for the application tables lives at page
  15 or beyond and is stored in the clear, so the tables can be read without
  the RC4 key.  Because MSysObjects is unreadable, tables are identified by
  their column signature instead of by name (see money.py).
* Money declares nearly every non-boolean column as variable length, so most
  values are located through the row's variable-column offset table.
"""

import struct
import datetime

PAGE_SIZE = 4096

# Page types
PG_DATA = 0x01
PG_TABLE_DEF = 0x02

# Table definition page layout (Jet4)
TAB_NUM_ROWS_OFF = 0x10
TAB_NUM_VAR_COLS_OFF = 43
TAB_NUM_COLS_OFF = 45
TAB_NUM_IDXS_OFF = 47
TAB_NUM_RIDXS_OFF = 51
TAB_COLS_START_OFF = 63
TAB_RIDX_ENTRY_SZ = 12
TAB_COL_ENTRY_SZ = 25

# Column entry layout (Jet4)
COL_TYPE_OFF = 0
COL_NUM_OFF = 5
COL_OFFSET_VAR_OFF = 7
COL_FLAGS_OFF = 15
COL_OFFSET_FIXED_OFF = 21
COL_SIZE_OFF = 23

COL_FLAG_FIXED = 0x01

# Data page layout (Jet4)
DATA_TDEF_OFF = 0x04
DATA_NUM_ROWS_OFF = 0x0C
DATA_ROW_OFFSETS_OFF = 0x0E

ROW_OFFSET_MASK = 0x1FFF
ROW_FLAG_DELETED = 0x8000
ROW_FLAG_OVERFLOW = 0x4000

# Column types
T_BOOL = 0x01
T_BYTE = 0x02
T_INT = 0x03
T_LONGINT = 0x04
T_MONEY = 0x05
T_FLOAT = 0x06
T_DOUBLE = 0x07
T_DATETIME = 0x08
T_BINARY = 0x09
T_TEXT = 0x0A
T_OLE = 0x0B
T_MEMO = 0x0C
T_GUID = 0x0F
T_NUMERIC = 0x10

_LONG_VALUE_INLINE = 0x80000000
_LONG_VALUE_SINGLE_PAGE = 0x40000000
_LONG_VALUE_LEN_MASK = 0x00FFFFFF

# Jet stores datetimes as days since this epoch.
_JET_EPOCH = datetime.datetime(1899, 12, 30)

# Marker introducing single-byte ("compressed") text in a Jet4 text value.
_TEXT_COMPRESSED = b"\xff\xfe"


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def i64(b, o):
    return struct.unpack_from("<q", b, o)[0]


def decode_text(raw):
    """Decode a Jet4 text value, honouring the single-byte compression flag."""
    if raw[:2] == _TEXT_COMPRESSED:
        # After the marker the value alternates between single-byte and
        # UTF-16LE runs each time the marker reappears.
        out = []
        rest = raw[2:]
        compressed = True
        while rest:
            idx = rest.find(_TEXT_COMPRESSED)
            chunk = rest if idx < 0 else rest[:idx]
            if compressed:
                out.append(chunk.decode("latin-1"))
            else:
                out.append(chunk.decode("utf-16-le", "replace"))
            if idx < 0:
                break
            rest = rest[idx + 2:]
            compressed = not compressed
        return "".join(out)
    if len(raw) % 2:
        raw = raw[:-1]
    return raw.decode("utf-16-le", "replace")


class Column:
    __slots__ = ("name", "type", "num", "off_var", "off_fixed", "size", "flags")

    @property
    def is_fixed(self):
        return bool(self.flags & COL_FLAG_FIXED)

    def __repr__(self):
        return "<Column %s type=%d>" % (self.name, self.type)


class Table:
    """A single Jet4 table, located by the page number of its definition."""

    def __init__(self, db, tdef_page):
        self.db = db
        self.tdef_page = tdef_page
        self.name = "tdef%d" % tdef_page
        self.data_pages = []
        # Rows this reader could not decode.  Callers should treat a non-zero
        # count as data loss rather than ignore it.
        self.read_errors = 0

        buf, self.tdef_pages = self._read_tdef(tdef_page)
        self.num_rows = u32(buf, TAB_NUM_ROWS_OFF)
        self.num_var_cols = u16(buf, TAB_NUM_VAR_COLS_OFF)
        self.num_cols = u16(buf, TAB_NUM_COLS_OFF)
        self.num_real_idxs = u32(buf, TAB_NUM_RIDXS_OFF)
        self.columns = self._read_columns(buf)
        self.by_name = {c.name: c for c in self.columns}
        self.colnames = [c.name for c in self.columns]

    def _read_tdef(self, first_page):
        """Concatenate the definition, which may be chained over several pages."""
        buf = bytearray()
        pages = []
        pg = first_page
        while pg and pg < self.db.num_pages and pg not in pages:
            raw = self.db.page(pg)
            pages.append(pg)
            buf += raw if len(pages) == 1 else raw[8:]
            pg = u32(raw, 0x04)
        return bytes(buf), pages

    def _read_columns(self, buf):
        pos = TAB_COLS_START_OFF + self.num_real_idxs * TAB_RIDX_ENTRY_SZ
        cols = []
        for _ in range(self.num_cols):
            e = buf[pos:pos + TAB_COL_ENTRY_SZ]
            c = Column()
            c.type = e[COL_TYPE_OFF]
            c.num = u16(e, COL_NUM_OFF)
            c.off_var = u16(e, COL_OFFSET_VAR_OFF)
            c.flags = e[COL_FLAGS_OFF]
            c.off_fixed = u16(e, COL_OFFSET_FIXED_OFF)
            c.size = u16(e, COL_SIZE_OFF)
            cols.append(c)
            pos += TAB_COL_ENTRY_SZ
        for c in cols:
            n = u16(buf, pos)
            pos += 2
            c.name = buf[pos:pos + n].decode("utf-16-le", "replace")
            pos += n
        return cols

    # -- row access ------------------------------------------------------

    def rows(self):
        """Yield every live row as a dict of column name -> Python value."""
        self.read_errors = 0
        for page_no in self.data_pages:
            pg = self.db.page(page_no)
            count = u16(pg, DATA_NUM_ROWS_OFF)
            offsets = [u16(pg, DATA_ROW_OFFSETS_OFF + 2 * i) for i in range(count)]
            for i, raw_off in enumerate(offsets):
                if raw_off & ROW_FLAG_DELETED:
                    continue
                start = raw_off & ROW_OFFSET_MASK
                # Rows are packed from the end of the page downwards, so a
                # row ends where the previous entry in the array begins.
                end = PAGE_SIZE if i == 0 else (offsets[i - 1] & ROW_OFFSET_MASK)
                if raw_off & ROW_FLAG_OVERFLOW:
                    row = self._follow_overflow(pg[start:end])
                    if row is None:
                        self.read_errors += 1
                        continue
                else:
                    row = pg[start:end]
                try:
                    cracked = self._crack_row(row)
                except (struct.error, IndexError, ValueError):
                    self.read_errors += 1
                    continue
                yield cracked

    def _follow_overflow(self, ptr):
        """An overflow entry points at a row stored on another page."""
        if len(ptr) < 4:
            return None
        row_num = ptr[0]
        page_no = int.from_bytes(ptr[1:4], "little")
        if page_no >= self.db.num_pages:
            return None
        pg = self.db.page(page_no)
        count = u16(pg, DATA_NUM_ROWS_OFF)
        if row_num >= count:
            return None
        offsets = [u16(pg, DATA_ROW_OFFSETS_OFF + 2 * i) for i in range(count)]
        raw_off = offsets[row_num]
        if raw_off & ROW_FLAG_OVERFLOW:
            return None  # refuse to chase a pointer to another pointer
        # The target slot carries the deleted flag so that a plain scan of the
        # overflow page skips it; the row is only valid via this pointer.
        start = raw_off & ROW_OFFSET_MASK
        end = PAGE_SIZE if row_num == 0 else (offsets[row_num - 1] & ROW_OFFSET_MASK)
        return pg[start:end]

    def _crack_row(self, row):
        n_cols = u16(row, 0)
        bitmask_sz = (n_cols + 7) // 8
        null_mask = row[len(row) - bitmask_sz:]
        n_var = u16(row, len(row) - bitmask_sz - 2)

        var_offsets = []
        if n_var:
            tbl = len(row) - bitmask_sz - 2 - (n_var + 1) * 2
            words = [u16(row, tbl + 2 * i) for i in range(n_var + 1)]
            var_offsets = words[::-1]  # stored last-to-first

        def is_null(col_num):
            byte = col_num // 8
            return byte >= len(null_mask) or not (null_mask[byte] >> (col_num % 8)) & 1

        out = {}
        for c in self.columns:
            if c.num >= n_cols:
                out[c.name] = None
                continue
            if c.type == T_BOOL:
                # A boolean has no payload; the null-mask bit *is* the value.
                out[c.name] = not is_null(c.num)
                continue
            if is_null(c.num):
                out[c.name] = None
                continue
            if c.is_fixed:
                raw = row[2 + c.off_fixed:2 + c.off_fixed + c.size]
            elif c.off_var + 1 < len(var_offsets):
                raw = row[var_offsets[c.off_var]:var_offsets[c.off_var + 1]]
            else:
                out[c.name] = None
                continue
            out[c.name] = self._decode(c, raw)
        return out

    def _decode(self, col, raw):
        t = col.type
        if not raw:
            return None
        if t == T_BYTE:
            return raw[0]
        if t == T_INT:
            return struct.unpack_from("<h", raw, 0)[0]
        if t == T_LONGINT:
            return i32(raw, 0)
        if t == T_MONEY:
            return i64(raw, 0) / 10000.0
        if t == T_FLOAT:
            return struct.unpack_from("<f", raw, 0)[0]
        if t == T_DOUBLE:
            return struct.unpack_from("<d", raw, 0)[0]
        if t == T_DATETIME:
            days = struct.unpack_from("<d", raw, 0)[0]
            try:
                return _JET_EPOCH + datetime.timedelta(days=days)
            except OverflowError:
                return None
        if t == T_TEXT:
            return decode_text(raw)
        if t == T_GUID:
            return raw.hex()
        if t in (T_MEMO, T_OLE):
            data = self._read_long_value(raw)
            if data is None:
                return None
            return decode_text(data) if t == T_MEMO else data
        # T_BINARY and T_NUMERIC are handed back raw; Money's schema does not
        # use them for anything this tool reads.
        return raw

    def _read_long_value(self, raw):
        """Resolve a MEMO/OLE descriptor into its bytes."""
        if len(raw) < 12:
            return raw
        header = u32(raw, 0)
        length = header & _LONG_VALUE_LEN_MASK
        if header & _LONG_VALUE_INLINE:
            return raw[12:12 + length]
        ptr = u32(raw, 4)
        row_num = ptr & 0xFF
        page_no = ptr >> 8
        single = bool(header & _LONG_VALUE_SINGLE_PAGE)
        out = bytearray()
        seen = set()
        while page_no and page_no < self.db.num_pages and len(out) < length:
            if (page_no, row_num) in seen:
                break
            seen.add((page_no, row_num))
            pg = self.db.page(page_no)
            count = u16(pg, DATA_NUM_ROWS_OFF)
            if row_num >= count:
                break
            offsets = [u16(pg, DATA_ROW_OFFSETS_OFF + 2 * i) for i in range(count)]
            cur = offsets[row_num]
            start = cur & ROW_OFFSET_MASK
            end = PAGE_SIZE if row_num == 0 else (offsets[row_num - 1] & ROW_OFFSET_MASK)
            chunk = pg[start:end]
            if single or not (cur & ROW_FLAG_OVERFLOW):
                out += chunk
                break
            # Multi-page value: first 4 bytes point at the next chunk.
            nxt = u32(chunk, 0)
            out += chunk[4:]
            row_num = nxt & 0xFF
            page_no = nxt >> 8
        return bytes(out[:length]) if length else bytes(out)


class JetDatabase:
    """Opens a Jet4/MSISAM file and exposes its tables."""

    SIGNATURES = (b"Standard Jet DB", b"MSISAM Database", b"Standard ACE DB")

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.data = fh.read()
        if len(self.data) < PAGE_SIZE:
            raise ValueError("file is too small to be a Jet database")
        sig = self.data[4:4 + 15]
        if sig not in self.SIGNATURES:
            raise ValueError("not a Jet/MSISAM database (signature %r)" % sig)
        self.is_msisam = sig == b"MSISAM Database"
        self.num_pages = len(self.data) // PAGE_SIZE
        self.tables = self._load_tables()

    def page(self, n):
        return self.data[n * PAGE_SIZE:(n + 1) * PAGE_SIZE]

    def _load_tables(self):
        """Find table definitions and attach their data pages.

        Pages 1..14 of a Money file are encrypted, so they are skipped; the
        catalogue is not needed because callers identify tables by column
        signature.
        """
        first_clear = 15 if self.is_msisam else 1

        tdef_roots = []
        continuation = set()
        owners = {}
        for p in range(first_clear, self.num_pages):
            pg = self.page(p)
            kind = pg[0]
            if kind == PG_TABLE_DEF:
                tdef_roots.append(p)
                nxt = u32(pg, 0x04)
                if nxt and nxt < self.num_pages:
                    continuation.add(nxt)
            elif kind == PG_DATA:
                owners.setdefault(u32(pg, DATA_TDEF_OFF), []).append(p)

        tables = []
        for p in tdef_roots:
            if p in continuation:
                continue
            try:
                t = Table(self, p)
            except (struct.error, IndexError, ValueError):
                continue
            t.data_pages = owners.get(p, [])
            tables.append(t)
        return tables

    def find_table(self, required, forbidden=()):
        """Return the table whose columns cover `required` and avoid `forbidden`.

        Money's catalogue is encrypted, so tables are matched on their column
        signature.  When several match, the one with the fewest columns wins so
        that a satellite table is not mistaken for its richer parent.
        """
        req, bad = set(required), set(forbidden)
        hits = [t for t in self.tables
                if req <= set(t.colnames) and not (bad & set(t.colnames))]
        if not hits:
            return None
        hits.sort(key=lambda t: len(t.colnames))
        return hits[0]
