"""LTP layer of a Unicode PST file ([MS-PST] section 2.3).

Builds Heap-on-Node (HN), BTree-on-Heap (BTH), Property Context (PC) and Table
Context (TC) structures as in-memory node contents, which ``write_node`` then
streams through the NDB writer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .ndb import (
    MAX_BLOCK_DATA,
    NID_TYPE_LTP,
    NdbWriter,
    PstFormatError,
    SubnodeEntry,
    make_nid,
)

# Property types ([MS-OXCDATA] 2.11.1).
PT_SHORT = 0x0002
PT_LONG = 0x0003
PT_FLOAT = 0x0004
PT_DOUBLE = 0x0005
PT_CURRENCY = 0x0006
PT_APPTIME = 0x0007
PT_ERROR = 0x000A
PT_BOOLEAN = 0x000B
PT_LONGLONG = 0x0014
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_CLSID = 0x0048
PT_BINARY = 0x0102
PT_MV_LONG = 0x1003
PT_MV_UNICODE = 0x101F
PT_MV_BINARY = 0x1102

_FIXED_SIZES = {
    PT_SHORT: 2,
    PT_LONG: 4,
    PT_FLOAT: 4,
    PT_ERROR: 4,
    PT_BOOLEAN: 1,
    PT_DOUBLE: 8,
    PT_CURRENCY: 8,
    PT_APPTIME: 8,
    PT_LONGLONG: 8,
    PT_SYSTIME: 8,
}

MAX_HEAP_ITEM = 3580  # larger values are stored in a subnode (section 2.3.3.3)
HN_SIG = 0xEC
CLIENT_TC = 0x7C
CLIENT_BTH = 0xB5
CLIENT_PC = 0xBC
MAX_HEAP_ITEMS_PER_BLOCK = 2047  # hidIndex is 11 bits


def prop_type(tag: int) -> int:
    return tag & 0xFFFF


def encode_value(tag: int, value: object) -> bytes:
    """Encode a Python value as the raw bytes of a MAPI property."""
    t = prop_type(tag)
    if t == PT_UNICODE:
        return str(value).encode("utf-16-le")
    if t == PT_STRING8:
        return value if isinstance(value, bytes) else str(value).encode("latin-1", "replace")
    if t in (PT_BINARY, PT_CLSID):
        return bytes(value)  # type: ignore[arg-type]
    if t == PT_BOOLEAN:
        return b"\x01" if value else b"\x00"
    if t == PT_SHORT:
        return struct.pack("<h", int(value))  # type: ignore[arg-type]
    if t in (PT_LONG, PT_ERROR):
        return struct.pack("<I", int(value) & 0xFFFFFFFF)  # type: ignore[arg-type]
    if t in (PT_LONGLONG, PT_SYSTIME, PT_CURRENCY):
        return struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)  # type: ignore[arg-type]
    if t in (PT_DOUBLE, PT_APPTIME):
        return struct.pack("<d", float(value))  # type: ignore[arg-type]
    if t == PT_FLOAT:
        return struct.pack("<f", float(value))  # type: ignore[arg-type]
    if t == PT_MV_LONG:
        return b"".join(struct.pack("<I", int(v) & 0xFFFFFFFF) for v in value)  # type: ignore[union-attr]
    if t in (PT_MV_UNICODE, PT_MV_BINARY):
        items = [v.encode("utf-16-le") if isinstance(v, str) else bytes(v) for v in value]  # type: ignore[union-attr]
        offset = 4 + 4 * len(items)
        offsets = []
        for item in items:
            offsets.append(offset)
            offset += len(item)
        return struct.pack(f"<I{len(items)}I", len(items), *offsets) + b"".join(items)
    raise PstFormatError(f"unsupported property type 0x{t:04x}")


@dataclass
class NodeData:
    """Contents of a node or subnode: its data stream plus nested subnodes."""

    chunks: list[bytes]
    subnodes: list[tuple[int, "NodeData"]] = field(default_factory=list)


def write_node(ndb: NdbWriter, node: NodeData) -> tuple[int, int]:
    """Write a node's subnodes and data; return (bidData, bidSub)."""
    entries = [SubnodeEntry(nid, *write_node(ndb, child)) for nid, child in node.subnodes]
    bid_sub = ndb.write_subnodes(entries)
    bid_data = ndb.write_data_tree(node.chunks) if node.chunks else 0
    return bid_data, bid_sub


def split_stream(data: bytes) -> list[bytes]:
    if not data:
        return [b""]
    return [data[i:i + MAX_BLOCK_DATA] for i in range(0, len(data), MAX_BLOCK_DATA)]


class SubnodeAllocator:
    """Hands out subnode NIDs that are unique within one parent node."""

    def __init__(self, first_index: int = 0x100):
        self._next = first_index
        self.subnodes: list[tuple[int, NodeData]] = []

    def add(self, node: NodeData, nid_type_: int = NID_TYPE_LTP, nid: int | None = None) -> int:
        if nid is None:
            nid = make_nid(nid_type_, self._next)
            self._next += 1
        self.subnodes.append((nid, node))
        return nid


# -- Heap-on-Node -------------------------------------------------------------


def _header_size(block: int) -> int:
    if block == 0:
        return 12  # HNHDR
    if block >= 8 and (block - 8) % 128 == 0:
        return 66  # HNBITMAPHDR
    return 2  # HNPAGEHDR


def _fill_level(free: int) -> int:
    limits = [3584, 2560, 2048, 1792, 1536, 1280, 1024, 768, 512, 256, 128, 64, 32, 16, 8]
    for level, limit in enumerate(limits):
        if free >= limit:
            return level
    return 0xF


class Heap:
    def __init__(self, client_sig: int):
        self.client_sig = client_sig
        self._blocks: list[list[bytes]] = [[]]
        self._used: list[int] = [_header_size(0)]

    def _fits(self, block: int, size: int) -> bool:
        count = len(self._blocks[block]) + 1
        if count > MAX_HEAP_ITEMS_PER_BLOCK:
            return False
        used = self._used[block] + size
        used += used % 2
        return used + 4 + 2 * (count + 1) <= MAX_BLOCK_DATA

    def alloc(self, data: bytes) -> int:
        if len(data) > MAX_HEAP_ITEM:
            raise PstFormatError("heap item too large")
        block = len(self._blocks) - 1
        if not self._fits(block, len(data)):
            self._blocks.append([])
            self._used.append(_header_size(block + 1))
            block += 1
            if block > 0xFFFF:
                raise PstFormatError("heap has too many blocks")
        self._blocks[block].append(data)
        self._used[block] += len(data)
        index = len(self._blocks[block])
        return (block << 16) | (index << 5)

    def build(self, user_root: int) -> list[bytes]:
        bodies = []
        for block, items in enumerate(self._blocks):
            header = _header_size(block)
            offsets = [header]
            for item in items:
                offsets.append(offsets[-1] + len(item))
            content = b"".join(items)
            pad = (header + len(content)) % 2
            ib_hnpm = header + len(content) + pad
            page_map = struct.pack(f"<HH{len(offsets)}H", len(items), 0, *offsets)
            bodies.append((ib_hnpm, content + b"\0" * pad + page_map))
        sizes = [_header_size(i) + len(body) for i, (_, body) in enumerate(bodies)]
        levels = [_fill_level(MAX_BLOCK_DATA - s) for s in sizes]

        def fill_map(start: int, count: int) -> bytes:
            out = bytearray(count // 2)
            for i in range(count):
                if start + i < len(levels):
                    out[i // 2] |= levels[start + i] << (4 * (i % 2))
            return bytes(out)

        chunks = []
        for block, (ib_hnpm, body) in enumerate(bodies):
            if block == 0:
                head = struct.pack("<HBBI", ib_hnpm, HN_SIG, self.client_sig, user_root) + fill_map(0, 8)
            elif _header_size(block) == 66:
                head = struct.pack("<H", ib_hnpm) + fill_map(block, 128)
            else:
                head = struct.pack("<H", ib_hnpm)
            chunks.append(head + body)
        return chunks


# -- BTree-on-Heap ------------------------------------------------------------


def build_bth(heap: Heap, cb_key: int, cb_ent: int, records: Sequence[tuple[int, bytes]]) -> int:
    """Allocate a BTH (sorted by integer key) on ``heap``; return its header HID."""
    hid_root = 0
    levels = 0
    if records:
        ordered = sorted(records, key=lambda r: r[0])
        rec_size = cb_key + cb_ent
        per_item = MAX_HEAP_ITEM // rec_size
        nodes = []
        for start in range(0, len(ordered), per_item):
            part = ordered[start:start + per_item]
            data = b"".join(k.to_bytes(cb_key, "little") + v for k, v in part)
            nodes.append((part[0][0], heap.alloc(data)))
        idx_size = cb_key + 4
        per_index = MAX_HEAP_ITEM // idx_size
        while len(nodes) > 1:
            levels += 1
            parents = []
            for start in range(0, len(nodes), per_index):
                part = nodes[start:start + per_index]
                data = b"".join(k.to_bytes(cb_key, "little") + struct.pack("<I", hid) for k, hid in part)
                parents.append((part[0][0], heap.alloc(data)))
            nodes = parents
        hid_root = nodes[0][1]
    return heap.alloc(struct.pack("<BBBBI", CLIENT_BTH, cb_key, cb_ent, levels, hid_root))


# -- Property Context ---------------------------------------------------------


def _store_variable(heap: Heap, subnodes: SubnodeAllocator, data: bytes) -> int:
    if not data:
        return 0
    if len(data) <= MAX_HEAP_ITEM:
        return heap.alloc(data)
    return subnodes.add(NodeData(split_stream(data)))


def build_pc(props: Mapping[int, object], subnodes: SubnodeAllocator | None = None) -> NodeData:
    """Build a PC node. Large values go to subnodes of the same node."""
    own = subnodes if subnodes is not None else SubnodeAllocator()
    heap = Heap(CLIENT_PC)
    records = []
    for tag, value in props.items():
        if value is None:
            continue
        t = prop_type(tag)
        raw = encode_value(tag, value)
        size = _FIXED_SIZES.get(t)
        if size is not None and size <= 4:
            hnid = int.from_bytes(raw.ljust(4, b"\0"), "little")
        elif size is not None:
            hnid = heap.alloc(raw)
        else:
            hnid = _store_variable(heap, own, raw)
        records.append((tag >> 16, struct.pack("<HI", t, hnid)))
    root = build_bth(heap, 2, 6, records)
    return NodeData(heap.build(root), own.subnodes if subnodes is None else [])


# -- Table Context ------------------------------------------------------------

TAG_LTP_ROW_ID = 0x67F20003
TAG_LTP_ROW_VER = 0x67F30003


@dataclass(frozen=True)
class Column:
    tag: int
    ib: int
    cb: int
    ibit: int


@dataclass(frozen=True)
class TableLayout:
    columns: tuple[Column, ...]
    end_4b: int
    end_2b: int
    end_1b: int
    row_size: int

    @property
    def tags(self) -> frozenset[int]:
        return frozenset(c.tag for c in self.columns)


def _cell_size(tag: int) -> int:
    size = _FIXED_SIZES.get(prop_type(tag))
    return size if size is not None else 4  # HNID


def layout(tags: Iterable[int], fixed: Mapping[int, tuple[int, int, int]] | None = None) -> TableLayout:
    """Compute a row layout. ``fixed`` maps tag -> (ib, cb, iBit) to reuse Outlook's layouts."""
    tags = sorted(set(tags) | {TAG_LTP_ROW_ID, TAG_LTP_ROW_VER})
    if fixed:
        cols = [Column(t, *fixed[t]) for t in tags]
    else:
        cols = [Column(TAG_LTP_ROW_ID, 0, 4, 0), Column(TAG_LTP_ROW_VER, 4, 4, 1)]
        rest = [t for t in tags if t not in (TAG_LTP_ROW_ID, TAG_LTP_ROW_VER)]
        offset, ibit = 8, 2
        for group in (8, 4, 2, 1):
            for t in rest:
                if _cell_size(t) == group:
                    cols.append(Column(t, offset, group, ibit))
                    offset += group
                    ibit += 1
    end_4b = max([c.ib + c.cb for c in cols if c.cb >= 4], default=8)
    end_2b = max([c.ib + c.cb for c in cols if c.cb == 2] + [end_4b])
    end_1b = max([c.ib + c.cb for c in cols if c.cb == 1] + [end_2b])
    row_size = end_1b + (len(cols) + 7) // 8
    return TableLayout(tuple(sorted(cols, key=lambda c: c.tag)), end_4b, end_2b, end_1b, row_size)


def build_tc(table: TableLayout, rows: Sequence[tuple[int, Mapping[int, object]]],
             subnodes: SubnodeAllocator | None = None) -> NodeData:
    """Build a TC node from (row id, {tag: value}) rows."""
    own = subnodes if subnodes is not None else SubnodeAllocator()
    heap = Heap(CLIENT_TC)
    ceb_offset = table.end_1b
    matrix = []
    for index, (row_id, values) in enumerate(rows):
        row = bytearray(table.row_size)
        cells = dict(values)
        cells[TAG_LTP_ROW_ID] = row_id
        cells.setdefault(TAG_LTP_ROW_VER, index + 1)
        for col in table.columns:
            value = cells.get(col.tag)
            if value is None:
                continue
            raw = encode_value(col.tag, value)
            if prop_type(col.tag) in _FIXED_SIZES:
                cell = raw
            else:
                cell = struct.pack("<I", _store_variable(heap, own, raw))
            row[col.ib:col.ib + col.cb] = cell[: col.cb].ljust(col.cb, b"\0")
            row[ceb_offset + col.ibit // 8] |= 0x80 >> (col.ibit % 8)
        matrix.append(bytes(row))

    hnid_rows = 0
    if matrix:
        data = b"".join(matrix)
        if len(data) <= MAX_HEAP_ITEM:
            hnid_rows = heap.alloc(data)
        else:
            # Rows never span blocks, and every block but the last holds a full
            # 8176 bytes so readers can locate row N arithmetically (2.3.4.4).
            per_block = MAX_BLOCK_DATA // table.row_size
            chunks = [b"".join(matrix[i:i + per_block]) for i in range(0, len(matrix), per_block)]
            chunks = [c.ljust(MAX_BLOCK_DATA, b"\0") for c in chunks[:-1]] + chunks[-1:]
            hnid_rows = own.add(NodeData(chunks))
    index_records = [(row_id, struct.pack("<I", i)) for i, (row_id, _) in enumerate(rows)]
    hid_index = build_bth(heap, 4, 4, index_records)
    info = struct.pack(
        "<BB4HIII",
        CLIENT_TC, len(table.columns),
        table.end_4b, table.end_2b, table.end_1b, table.row_size,
        hid_index, hnid_rows, 0,
    )
    info += b"".join(struct.pack("<IHBB", c.tag, c.ib, c.cb, c.ibit) for c in table.columns)
    root = heap.alloc(info)
    return NodeData(heap.build(root), own.subnodes if subnodes is None else [])
