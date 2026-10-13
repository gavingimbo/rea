"""NDB layer of a Unicode PST file ([MS-PST] section 2.2).

The writer is append-only: blocks are streamed to disk as they are produced,
while the Block BTree (BBT) and Node BTree (NBT) entries are kept in memory and
written as pages when the file is finished. Allocation metadata (AMap, PMap,
FMap, FPMap, DList) and the HEADER are produced last from the exact set of
allocated ranges, so every allocated 64-byte slot is referenced by exactly one
block or page.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Sequence

PAGE_SIZE = 512
SLOT_SIZE = 64
MAX_BLOCK_SIZE = 8192
BLOCK_TRAILER_SIZE = 16
MAX_BLOCK_DATA = MAX_BLOCK_SIZE - BLOCK_TRAILER_SIZE  # 8176

FIRST_AMAP = 0x4400
AMAP_INTERVAL = 496 * 8 * SLOT_SIZE  # 253,952 bytes of data per AMap page
SLOTS_PER_AMAP = 496 * 8
DLIST_OFFSET = 0x4200
HEADER_SIZE = 564

PTYPE_BBT = 0x80
PTYPE_NBT = 0x81
PTYPE_FMAP = 0x82
PTYPE_PMAP = 0x83
PTYPE_AMAP = 0x84
PTYPE_FPMAP = 0x85
PTYPE_DL = 0x86

# BTree page capacities for Unicode files (section 2.2.2.7.7.1).
BT_ENTRIES_BYTES = 488
NBT_LEAF_ENTRY = 32
BBT_LEAF_ENTRY = 24
BT_INTERMEDIATE_ENTRY = 24

XBLOCK_MAX_BIDS = (MAX_BLOCK_SIZE - BLOCK_TRAILER_SIZE - 8) // 8  # 1021
SLBLOCK_MAX_ENTRIES = (MAX_BLOCK_SIZE - BLOCK_TRAILER_SIZE - 8) // 24  # 340
SIBLOCK_MAX_ENTRIES = (MAX_BLOCK_SIZE - BLOCK_TRAILER_SIZE - 8) // 16  # 510

# NID types (section 2.2.2.1).
NID_TYPE_HID = 0x00
NID_TYPE_INTERNAL = 0x01
NID_TYPE_NORMAL_FOLDER = 0x02
NID_TYPE_SEARCH_FOLDER = 0x03
NID_TYPE_NORMAL_MESSAGE = 0x04
NID_TYPE_ATTACHMENT = 0x05
NID_TYPE_ASSOC_MESSAGE = 0x08
NID_TYPE_HIERARCHY_TABLE = 0x0D
NID_TYPE_CONTENTS_TABLE = 0x0E
NID_TYPE_ASSOC_CONTENTS_TABLE = 0x0F
NID_TYPE_ATTACHMENT_TABLE = 0x11
NID_TYPE_RECIPIENT_TABLE = 0x12
NID_TYPE_LTP = 0x1F

# Starting nidIndex values for HEADER.rgnid (section 2.2.2.6).
_RGNID_DEFAULTS = {
    NID_TYPE_NORMAL_FOLDER: 0x400,
    NID_TYPE_SEARCH_FOLDER: 0x4000,
    NID_TYPE_NORMAL_MESSAGE: 0x10000,
    NID_TYPE_ASSOC_MESSAGE: 0x8000,
}


def make_nid(nid_type: int, index: int) -> int:
    return (index << 5) | nid_type


def nid_type(nid: int) -> int:
    return nid & 0x1F


def nid_index(nid: int) -> int:
    return nid >> 5


def crc32(data: bytes) -> int:
    """CRC used throughout the PST format (section 5.3): seed 0, no final XOR."""
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def signature(ib: int, bid: int) -> int:
    """Block / page signature (section 5.5)."""
    value = (ib ^ bid) & 0xFFFFFFFF
    return ((value >> 16) ^ value) & 0xFFFF


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _has_pmap(section: int) -> bool:
    return section % 8 == 0


def _has_fmap(section: int) -> bool:
    # The HEADER's rgbFM covers the first 128 AMaps; each FMap page covers 496.
    return section >= 128 and (section - 128) % 496 == 0


def _has_fpmap(section: int) -> bool:
    # The HEADER's rgbFP covers 128 * 8 PMaps (8,192 AMaps); each FPMap page
    # covers 496 * 8 PMaps.
    return section >= 8192 and (section - 8192) % (496 * 8 * 8) == 0


def _map_pages(section: int) -> list[int]:
    pages = [PTYPE_AMAP]
    if _has_pmap(section):
        pages.append(PTYPE_PMAP)
    if _has_fmap(section):
        pages.append(PTYPE_FMAP)
    if _has_fpmap(section):
        pages.append(PTYPE_FPMAP)
    return pages


def _slot_bit(slot: int) -> int:
    """Mask for a 64-byte slot within its AMap byte. Slots are numbered from the
    most significant bit, as in Outlook-created files (the spec leaves the bit
    order implicit; SCANPST rejects the opposite order)."""
    return 0x80 >> (slot % 8)


def _section_start(section: int) -> int:
    return FIRST_AMAP + section * AMAP_INTERVAL


def _section_data_start(section: int) -> int:
    return _section_start(section) + PAGE_SIZE * len(_map_pages(section))


@dataclass(frozen=True)
class SubnodeEntry:
    nid: int
    bid_data: int
    bid_sub: int


class PstFormatError(Exception):
    """A value cannot be represented in the PST format."""


class NdbWriter:
    """Streams blocks to a new Unicode PST file and writes its metadata."""

    def __init__(self, stream: BinaryIO):
        self._f = stream
        self._cursor = _section_data_start(0)
        self._sections: list[bytearray] = []  # AMap bitmaps, one per section
        self._bbt: list[tuple[int, int, int, int]] = []  # bid, ib, cb, cRef
        self._nbt: dict[int, tuple[int, int, int]] = {}  # nid -> data, sub, parent
        self._next_block = 1
        self._next_page = 1
        self._rgnid = [0x400] * 32
        for t, start in _RGNID_DEFAULTS.items():
            self._rgnid[t] = start
        self._finished = False

    @property
    def bytes_written(self) -> int:
        return self._cursor

    @property
    def block_count(self) -> int:
        return len(self._bbt)

    @property
    def node_count(self) -> int:
        return len(self._nbt)

    # -- allocation -------------------------------------------------------

    def _section_bits(self, section: int) -> bytearray:
        while len(self._sections) <= section:
            bits = bytearray(496)
            # Map pages are allocated out of their own section.
            used = len(_map_pages(len(self._sections))) * (PAGE_SIZE // SLOT_SIZE)
            for slot in range(used):
                bits[slot // 8] |= _slot_bit(slot)
            self._sections.append(bits)
        return self._sections[section]

    def _alloc(self, size: int, alignment: int) -> int:
        ib = _align(self._cursor, alignment)
        section = (ib - FIRST_AMAP) // AMAP_INTERVAL
        if ib < _section_data_start(section):
            ib = _align(_section_data_start(section), alignment)
        if ib + size > _section_start(section) + AMAP_INTERVAL:
            section += 1
            ib = _align(_section_data_start(section), alignment)
        bits = self._section_bits(section)
        first = (ib - _section_start(section)) // SLOT_SIZE
        for slot in range(first, first + size // SLOT_SIZE):
            bits[slot // 8] |= _slot_bit(slot)
        self._cursor = ib + size
        return ib

    # -- blocks -----------------------------------------------------------

    def _new_block_bid(self, internal: bool) -> int:
        bid = (self._next_block << 2) | (2 if internal else 0)
        self._next_block += 1
        return bid

    def write_block(self, data: bytes, *, internal: bool = False) -> int:
        """Write one block and return its BID."""
        cb = len(data)
        if cb > MAX_BLOCK_DATA:
            raise PstFormatError(f"block data of {cb} bytes exceeds {MAX_BLOCK_DATA}")
        size = _align(cb + BLOCK_TRAILER_SIZE, SLOT_SIZE)
        bid = self._new_block_bid(internal)
        ib = self._alloc(size, SLOT_SIZE)
        trailer = struct.pack("<HHIQ", cb, signature(ib, bid), crc32(data), bid)
        self._f.seek(ib)
        self._f.write(data)
        self._f.write(b"\0" * (size - cb - BLOCK_TRAILER_SIZE))
        self._f.write(trailer)
        self._bbt.append((bid, ib, cb, 2))
        return bid

    def write_data_tree(self, chunks: Sequence[bytes]) -> int:
        """Store a node's data stream, split into the given data-block chunks.

        Returns the BID for NBTENTRY/SLENTRY.bidData: a data block, an XBLOCK
        or an XXBLOCK (section 2.2.2.8.3.2).
        """
        if not chunks:
            raise ValueError("a data tree needs at least one chunk")
        if len(chunks) == 1:
            return self.write_block(chunks[0])
        bids = [self.write_block(chunk) for chunk in chunks]
        sizes = [len(c) for c in chunks]
        if len(bids) <= XBLOCK_MAX_BIDS:
            return self._write_xblock(1, bids, sum(sizes))
        xbids = []
        for start in range(0, len(bids), XBLOCK_MAX_BIDS):
            part = bids[start:start + XBLOCK_MAX_BIDS]
            xbids.append(self._write_xblock(1, part, sum(sizes[start:start + XBLOCK_MAX_BIDS])))
        if len(xbids) > XBLOCK_MAX_BIDS:
            raise PstFormatError("data stream is too large for an XXBLOCK")
        return self._write_xblock(2, xbids, sum(sizes))

    def _write_xblock(self, level: int, bids: list[int], total: int) -> int:
        if total > 0xFFFFFFFF:
            raise PstFormatError("data stream exceeds 4 GiB")
        body = struct.pack("<BBHI", 0x01, level, len(bids), total)
        body += struct.pack(f"<{len(bids)}Q", *bids)
        return self.write_block(body, internal=True)

    def write_stream(self, data: bytes) -> int:
        """Store an opaque byte stream as a data tree."""
        if not data:
            return self.write_block(b"")
        chunks = [data[i:i + MAX_BLOCK_DATA] for i in range(0, len(data), MAX_BLOCK_DATA)]
        return self.write_data_tree(chunks)

    def write_subnodes(self, entries: Iterable[SubnodeEntry]) -> int:
        """Write a subnode BTree (SLBLOCK / SIBLOCK) and return its BID, or 0."""
        items = sorted(entries, key=lambda e: e.nid)
        if not items:
            return 0
        nids = [e.nid for e in items]
        if len(set(nids)) != len(nids):
            raise PstFormatError("duplicate subnode NID")
        leaves = []
        for start in range(0, len(items), SLBLOCK_MAX_ENTRIES):
            part = items[start:start + SLBLOCK_MAX_ENTRIES]
            body = struct.pack("<BBHI", 0x02, 0, len(part), 0)
            body += b"".join(struct.pack("<QQQ", e.nid, e.bid_data, e.bid_sub) for e in part)
            leaves.append((part[0].nid, self.write_block(body, internal=True)))
        if len(leaves) == 1:
            return leaves[0][1]
        if len(leaves) > SIBLOCK_MAX_ENTRIES:
            raise PstFormatError("too many subnodes")
        body = struct.pack("<BBHI", 0x02, 1, len(leaves), 0)
        body += b"".join(struct.pack("<QQ", nid, bid) for nid, bid in leaves)
        return self.write_block(body, internal=True)

    # -- nodes ------------------------------------------------------------

    def add_node(self, nid: int, bid_data: int, bid_sub: int = 0, nid_parent: int = 0) -> None:
        if nid in self._nbt:
            raise PstFormatError(f"duplicate NID 0x{nid:x}")
        self._nbt[nid] = (bid_data, bid_sub, nid_parent)
        t, index = nid_type(nid), nid_index(nid)
        if index >= self._rgnid[t]:
            self._rgnid[t] = index + 1

    # -- finishing --------------------------------------------------------

    def _write_page(self, ptype: int, body: bytes, ib: int | None = None, bid: int | None = None) -> tuple[int, int]:
        assert len(body) == 496
        if ib is None:
            ib = self._alloc(PAGE_SIZE, PAGE_SIZE)
        if bid is None:
            bid = self._next_page
            self._next_page += 1
        sig = 0 if ptype in (PTYPE_AMAP, PTYPE_PMAP, PTYPE_FMAP, PTYPE_FPMAP) else signature(ib, bid)
        trailer = struct.pack("<BBHIQ", ptype, ptype, sig, crc32(body), bid)
        self._f.seek(ib)
        self._f.write(body + trailer)
        return bid, ib

    def _write_btree(self, ptype: int, leaf_size: int, leaves: list[tuple[int, bytes]]) -> tuple[int, int]:
        """Write a BTree from sorted (key, packed leaf entry) pairs; return its root BREF."""
        level = 0
        max_leaf = BT_ENTRIES_BYTES // leaf_size
        size, capacity = leaf_size, max_leaf
        entries = leaves
        if not entries:
            body = bytes(BT_ENTRIES_BYTES) + struct.pack("<BBBBI", 0, max_leaf, leaf_size, 0, 0)
            return self._write_page(ptype, body)
        while True:
            pages = []
            for start in range(0, len(entries), capacity):
                part = entries[start:start + capacity]
                data = b"".join(e for _, e in part).ljust(BT_ENTRIES_BYTES, b"\0")
                body = data + struct.pack("<BBBBI", len(part), capacity, size, level, 0)
                bid, ib = self._write_page(ptype, body)
                pages.append((part[0][0], bid, ib))
            if len(pages) == 1:
                return pages[0][1], pages[0][2]
            level += 1
            if level > 8:
                raise PstFormatError("BTree too deep")
            size = BT_INTERMEDIATE_ENTRY
            capacity = BT_ENTRIES_BYTES // size
            entries = [(key, struct.pack("<QQQ", key, bid, ib)) for key, bid, ib in pages]

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        nbt_leaves = [
            (nid, struct.pack("<QQQII", nid, data, sub, parent, 0))
            for nid, (data, sub, parent) in sorted(self._nbt.items())
        ]
        bbt_leaves = [
            (bid, struct.pack("<QQHHI", bid, ib, cb, cref, 0))
            for bid, ib, cb, cref in sorted(self._bbt)
        ]
        # Each BT page written below may itself need a fresh section, which is
        # handled by the allocator; the AMaps are generated afterwards.
        nbt_root = self._write_btree(PTYPE_NBT, NBT_LEAF_ENTRY, nbt_leaves)
        bbt_root = self._write_btree(PTYPE_BBT, BBT_LEAF_ENTRY, bbt_leaves)

        last_section = (self._cursor - 1 - FIRST_AMAP) // AMAP_INTERVAL
        self._section_bits(last_section)
        free_slots = []
        for section, bits in enumerate(self._sections):
            start = _section_start(section)
            self._write_page(PTYPE_AMAP, bytes(bits), ib=start, bid=start)
            free_slots.append(SLOTS_PER_AMAP - sum(bin(b).count("1") for b in bits))
            offset = start + PAGE_SIZE
            if _has_pmap(section):
                # PMaps are deprecated; every page is reported as in use.
                self._write_page(PTYPE_PMAP, b"\xff" * 496, ib=offset, bid=offset)
                offset += PAGE_SIZE
            if _has_fmap(section):
                body = bytearray(496)
                for i in range(496):
                    target = section + i
                    if target < len(self._sections):
                        body[i] = min(255, _longest_free_run(self._sections[target]))
                self._write_page(PTYPE_FMAP, bytes(body), ib=offset, bid=offset)
                offset += PAGE_SIZE
            if _has_fpmap(section):
                self._write_page(PTYPE_FPMAP, b"\xff" * 496, ib=offset, bid=offset)
        eof = _section_start(len(self._sections))
        self._f.truncate(eof)

        self._write_dlist(free_slots)
        self._write_header(
            eof=eof,
            amap_last=_section_start(len(self._sections) - 1),
            amap_free=sum(free_slots) * SLOT_SIZE,
            nbt_root=nbt_root,
            bbt_root=bbt_root,
        )
        self._f.flush()

    def _write_dlist(self, free_slots: list[int]) -> None:
        order = sorted(range(len(free_slots)), key=lambda i: -free_slots[i])[: 476 // 4]
        entries = b"".join(struct.pack("<I", (i & 0xFFFFF) | (min(free_slots[i], 0xFFF) << 20)) for i in order)
        body = struct.pack("<BBHI", 0x01, len(order), 0, order[0] if order else 0)
        body += entries.ljust(476, b"\0") + bytes(12)
        self._write_page(PTYPE_DL, body, ib=DLIST_OFFSET)

    def _write_header(self, *, eof: int, amap_last: int, amap_free: int,
                      nbt_root: tuple[int, int], bbt_root: tuple[int, int]) -> None:
        root = struct.pack(
            "<IQQQQQQQQBBH",
            0, eof, amap_last, amap_free, 0,
            nbt_root[0], nbt_root[1], bbt_root[0], bbt_root[1],
            0x02, 0, 0,
        )
        assert len(root) == 72
        h = bytearray(HEADER_SIZE)
        h[0:4] = b"!BDN"
        struct.pack_into("<HHHBBII", h, 8, 0x4D53, 23, 19, 1, 1, 0, 0)
        struct.pack_into("<QQI", h, 0x18, 0, self._next_page, 1)
        struct.pack_into("<32I", h, 0x2C, *self._rgnid)
        h[0xB4:0xB4 + 72] = root
        h[0x100:0x180] = b"\xff" * 128
        h[0x180:0x200] = b"\xff" * 128
        h[0x200] = 0x80
        h[0x201] = 0x00  # NDB_CRYPT_NONE
        struct.pack_into("<Q", h, 0x204, self._next_block << 2)
        struct.pack_into("<I", h, 4, crc32(bytes(h[8:8 + 471])))
        struct.pack_into("<I", h, 0x20C, crc32(bytes(h[8:8 + 516])))
        self._f.seek(0)
        self._f.write(bytes(h))


def _longest_free_run(bits: bytearray) -> int:
    best = run = 0
    for byte in bits:
        if byte == 0:
            run += 8
            best = max(best, run)
            continue
        if byte == 0xFF:
            run = 0
            continue
        for slot in range(8):
            if byte & _slot_bit(slot):
                run = 0
            else:
                run += 1
                best = max(best, run)
    return best
