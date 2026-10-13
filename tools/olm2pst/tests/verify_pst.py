"""Independent structural checker for Unicode PST files.

Checks the [MS-PST] invariants that Outlook relies on but that lenient readers
(libpst, libpff) do not enforce: header and page CRCs, page/block signatures,
AMap coverage of every allocation (no leaks, no overlaps), BTree ordering and
keys, block reference counts, heap page maps, and folder/table coherence.
Run: python verify_pst.py FILE.pst
"""

from __future__ import annotations

import struct
import sys
import zlib
from collections import Counter

AMAP_INTERVAL = 253952
FIRST_AMAP = 0x4400


# Inverse permutation table for NDB_CRYPT_PERMUTE ([MS-PST] 5.1, mpbbI).
_PERMUTE_DECODE = bytes.fromhex(
    "47f1b4e60b6a7248854e9eebe2f89453e0bba002e85a09abdbe3bac67cc310dd"
    "39059630f53760828cc9134a6b1df3fb8f2697ca911701c4322d6e3195ffd923"
    "d1005e79dc443b1a28c5615720903d83b943be67d2464276c06d5b7eb20f1629"
    "3ca903540dda5ddff6b7c762cd8d06d3695c86d614f7a56675acb1e94521700c"
    "879f74a4224c6fbf1f56aa2eb3783350b0a392bccf191ca763cb1e4d3e4b1b9b"
    "4fe7f0eead3ab55904ea40552551e57a893868527bfc27aed7bdfa07f4cc8e5f"
    "ef359c842b15d5773449b6120a7f7188fd9d18417d93d8582ccefe24afdeb836"
    "c8a180a69998a82f0e816573e4c2a28ad4e111d0088b2af2ed9a643fc16cf9ec"
)


def crc32(data: bytes) -> int:
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def sig(ib: int, bid: int) -> int:
    v = (ib ^ bid) & 0xFFFFFFFF
    return ((v >> 16) ^ v) & 0xFFFF


class Checker:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        self.errors: list[str] = []
        self.allocated: list[tuple[int, int, str]] = []  # ib, size, what

    def err(self, msg: str) -> None:
        if len(self.errors) < 200:
            self.errors.append(msg)

    # -- header -----------------------------------------------------------
    def header(self) -> None:
        d = self.data
        if d[:4] != b"!BDN" or d[8:10] != b"SM":
            self.err("bad magic")
        ver, client = struct.unpack_from("<HH", d, 10)
        if ver < 23:
            self.err(f"not a Unicode PST (wVer={ver})")
        if struct.unpack_from("<I", d, 4)[0] != crc32(d[8:8 + 471]):
            self.err("dwCRCPartial mismatch")
        if struct.unpack_from("<I", d, 0x20C)[0] != crc32(d[8:8 + 516]):
            self.err("dwCRCFull mismatch")
        if d[0x200] != 0x80:
            self.err("bSentinel")
        self.crypt = d[0x201]
        if d[0x100:0x200] != b"\xff" * 256:
            self.err("rgbFM/rgbFP must be 0xFF")
        (self.bid_next_p,) = struct.unpack_from("<Q", d, 0x20)
        self.rgnid = struct.unpack_from("<32I", d, 0x2C)
        (_, self.eof, self.amap_last, self.amap_free, self.pmap_free,
         self.nbt_bid, self.nbt_ib, self.bbt_bid, self.bbt_ib, self.amap_valid) = struct.unpack_from(
            "<IQQQQQQQQB", d, 0xB4)
        (self.bid_next_b,) = struct.unpack_from("<Q", d, 0x204)
        if self.eof != len(d):
            self.err(f"ibFileEof {self.eof} != file size {len(d)}")
        if (self.eof - FIRST_AMAP) % AMAP_INTERVAL:
            self.err("file size is not a whole number of AMap intervals")
        if self.amap_valid != 2:
            self.err("fAMapValid != VALID_AMAP2")

    # -- pages ------------------------------------------------------------
    def page(self, ib: int, ptype: int, bid: int | None = None) -> bytes:
        p = self.data[ib:ib + 512]
        t, t2, wsig, crc, pbid = struct.unpack_from("<BBHIQ", p, 496)
        if t != ptype or t2 != ptype:
            self.err(f"page @0x{ib:x}: type 0x{t:x}/0x{t2:x}, expected 0x{ptype:x}")
        if crc != crc32(p[:496]):
            self.err(f"page @0x{ib:x}: CRC mismatch")
        if ptype in (0x82, 0x83, 0x84, 0x85):
            if wsig != 0 or pbid != ib:
                self.err(f"map page @0x{ib:x}: wSig/bid wrong")
        else:
            if wsig != sig(ib, pbid):
                self.err(f"page @0x{ib:x}: signature mismatch")
            if bid is not None and pbid != bid:
                self.err(f"page @0x{ib:x}: bid {pbid:x} != BREF bid {bid:x}")
            if pbid >= self.bid_next_p:
                self.err(f"page @0x{ib:x}: bid >= bidNextP")
        return p

    def maps(self) -> None:
        sections = (self.eof - FIRST_AMAP) // AMAP_INTERVAL
        self.sections = sections
        self.amap_bits = []
        free = 0
        for n in range(sections):
            start = FIRST_AMAP + n * AMAP_INTERVAL
            p = self.page(start, 0x84)
            self.amap_bits.append(p[:496])
            free += sum(8 - bin(b).count("1") for b in p[:496])
            off = start + 512
            self.allocated.append((start, 512, "AMap"))
            if n % 8 == 0:
                self.page(off, 0x83)
                self.allocated.append((off, 512, "PMap"))
                off += 512
            if n >= 128 and (n - 128) % 496 == 0:
                self.page(off, 0x82)
                self.allocated.append((off, 512, "FMap"))
                off += 512
        if self.amap_last != FIRST_AMAP + (sections - 1) * AMAP_INTERVAL:
            self.err("ibAMapLast does not point to the last AMap")
        if free * 64 != self.amap_free:
            self.err(f"cbAMapFree {self.amap_free} != {free * 64}")
        dl = self.data[0x4200:0x4400]
        if dl[496] == 0x86 and struct.unpack_from("<I", dl, 500)[0] != crc32(dl[:496]):
            self.err("DList CRC mismatch")

    # -- BTrees -----------------------------------------------------------
    def btree(self, ptype: int, bid: int, ib: int, lo: int | None, level: int | None, out: list) -> None:
        p = self.page(ib, ptype, bid)
        self.allocated.append((ib, 512, f"BT 0x{ptype:x}"))
        cent, cmax, cbent, clevel = struct.unpack_from("<BBBB", p, 488)
        leaf = 32 if ptype == 0x81 else 24
        if level is not None and clevel != level:
            self.err(f"BT page @0x{ib:x}: level {clevel} != expected {level}")
        if cent == 0 and lo is not None:
            self.err(f"BT page @0x{ib:x}: empty non-root page")
        expect = leaf if clevel == 0 else 24
        if cbent != expect or cmax != 488 // expect or cent > cmax:
            self.err(f"BT page @0x{ib:x}: cEnt/cEntMax/cbEnt {cent}/{cmax}/{cbent}")
        keys = [struct.unpack_from("<Q", p, i * cbent)[0] for i in range(cent)]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            self.err(f"BT page @0x{ib:x}: keys not strictly ascending")
        if lo is not None and keys and keys[0] != lo:
            self.err(f"BT page @0x{ib:x}: first key {keys[0]:x} != parent key {lo:x}")
        for i in range(cent):
            e = p[i * cbent:(i + 1) * cbent]
            if clevel:
                k, cbid, cib = struct.unpack_from("<QQQ", e)
                self.btree(ptype, cbid, cib, k, clevel - 1, out)
            else:
                out.append(e)

    def blocks(self) -> None:
        bbt_raw: list[bytes] = []
        self.btree(0x80, self.bbt_bid, self.bbt_ib, None, None, bbt_raw)
        self.bbt = {}
        for e in bbt_raw:
            bid, ib, cb, cref = struct.unpack_from("<QQHH", e)
            self.bbt[bid] = (ib, cb, cref)
            if bid & 1:
                self.err(f"BBT: bid {bid:x} has reserved bit set")
            if bid >= self.bid_next_b:
                self.err(f"BBT: bid {bid:x} >= bidNextB")
            size = (cb + 16 + 63) // 64 * 64
            if ib % 64:
                self.err(f"block {bid:x} not 64-byte aligned")
            alloc = size
            tcb, tsig, tcrc, tbid = struct.unpack("<HHIQ", self.data[ib + size - 16:ib + size])
            if tbid != bid or tcb != cb:
                self.err(f"block {bid:x} @0x{ib:x}: trailer does not match BBT entry")
                continue
            if tsig != sig(ib, bid):
                self.err(f"block {bid:x}: signature mismatch")
            if tcrc != crc32(self.data[ib:ib + cb]):
                self.err(f"block {bid:x}: CRC mismatch")
            self.allocated.append((ib, alloc, f"block {bid:x}"))

    def read_block(self, bid: int) -> bytes:
        ib, cb, _ = self.bbt[bid & ~1]
        raw = self.data[ib:ib + cb]
        if self.crypt == 1 and not bid & 2:  # only external blocks are encoded
            return raw.translate(_PERMUTE_DECODE)
        return raw

    def nodes(self) -> None:
        nbt_raw: list[bytes] = []
        self.btree(0x81, self.nbt_bid, self.nbt_ib, None, None, nbt_raw)
        self.nbt = {}
        for e in nbt_raw:
            nid, bdata, bsub, parent = struct.unpack_from("<QQQI", e)
            self.nbt[nid] = (bdata, bsub, parent)
            t, idx = nid & 0x1F, nid >> 5
            if idx >= self.rgnid[t] and t not in (1,):
                self.err(f"rgnid[{t}]={self.rgnid[t]:x} not above NID 0x{nid:x}")
        refs: Counter = Counter()

        def visit_data(bid: int) -> None:
            if not bid:
                return
            refs[bid] += 1
            if refs[bid] > 1:
                return
            if bid not in self.bbt:
                self.err(f"missing block {bid:x}")
                return
            if bid & 2:
                d = self.read_block(bid)
                btype, level, cent, total = struct.unpack_from("<BBHI", d)
                kids = struct.unpack_from(f"<{cent}Q", d, 8)
                if btype != 1:
                    self.err(f"block {bid:x}: bad XBLOCK type")
                got = 0
                for k in kids:
                    visit_data(k)
                    got += self.stream_len(k)
                if got != total:
                    self.err(f"XBLOCK {bid:x}: lcbTotal {total} != {got}")

        def visit_sub(bid: int) -> None:
            if not bid:
                return
            refs[bid] += 1
            d = self.read_block(bid)
            btype, level, cent = struct.unpack_from("<BBH", d)
            if btype != 2 or cent == 0:
                self.err(f"subnode block {bid:x}: bad header")
            size = 24 if level == 0 else 16
            nids = []
            for i in range(cent):
                if level == 0:
                    nid, bd, bs = struct.unpack_from("<QQQ", d, 8 + i * size)
                    visit_data(bd)
                    visit_sub(bs)
                else:
                    nid, child = struct.unpack_from("<QQ", d, 8 + i * size)
                    visit_sub(child)
                nids.append(nid)
            if nids != sorted(nids) or len(set(nids)) != len(nids):
                self.err(f"subnode block {bid:x}: NIDs not ascending")

        for nid, (bdata, bsub, _) in self.nbt.items():
            visit_data(bdata)
            visit_sub(bsub)
        for bid, (_, _, cref) in self.bbt.items():
            if refs[bid] == 0:
                self.err(f"block {bid:x} is not referenced")
            elif cref != refs[bid] + 1:
                self.err(f"block {bid:x}: cRef {cref} != {refs[bid] + 1}")

    def stream_len(self, bid: int) -> int:
        if bid & 2:
            return struct.unpack_from("<I", self.read_block(bid), 4)[0]
        return self.bbt[bid][1]

    def stream_blocks(self, bid: int) -> list[bytes]:
        if not bid:
            return []
        if bid & 2:
            d = self.read_block(bid)
            _, level, cent, _ = struct.unpack_from("<BBHI", d)
            out = []
            for k in struct.unpack_from(f"<{cent}Q", d, 8):
                out.extend(self.stream_blocks(k))
            return out
        return [self.read_block(bid)]

    # -- allocation coverage ----------------------------------------------
    def coverage(self) -> None:
        used = bytearray(self.sections * 3968)
        for ib, size, what in self.allocated:
            if ib < FIRST_AMAP:
                continue
            n = (ib - FIRST_AMAP) // AMAP_INTERVAL
            first = (ib - FIRST_AMAP - n * AMAP_INTERVAL) // 64
            if first + size // 64 > 3968:
                self.err(f"{what} @0x{ib:x} crosses an AMap boundary")
            for s in range(first, first + size // 64):
                k = n * 3968 + s
                if used[k]:
                    self.err(f"{what} @0x{ib:x} overlaps another allocation")
                    break
                used[k] = 1
        mismatches = 0
        for n, bits in enumerate(self.amap_bits):
            for s in range(3968):
                bit = (bits[s // 8] >> (s % 8)) & 1
                if bit != used[n * 3968 + s]:
                    mismatches += 1
                    if mismatches <= 5:
                        kind = "allocated but unused (leak)" if bit else "used but not allocated"
                        self.err(f"AMap {n} slot {s}: {kind}")
        if mismatches > 5:
            self.err(f"... {mismatches} AMap mismatches in total")

    # -- heaps and messaging ---------------------------------------------
    def heap(self, nid: int, blocks: list[bytes]) -> dict[int, bytes]:
        items = {}
        for i, b in enumerate(blocks):
            ibhnpm = struct.unpack_from("<H", b)[0]
            if i == 0 and (b[2] != 0xEC or b[3] not in (0x7C, 0xBC, 0xB5)):
                self.err(f"node 0x{nid:x}: bad HNHDR")
            calloc, cfree = struct.unpack_from("<HH", b, ibhnpm)
            offs = struct.unpack_from(f"<{calloc + 1}H", b, ibhnpm + 4)
            if list(offs) != sorted(offs) or offs[-1] > ibhnpm:
                self.err(f"node 0x{nid:x}: bad HNPAGEMAP in block {i}")
            for j in range(calloc):
                items[(i << 16) | ((j + 1) << 5)] = b[offs[j]:offs[j + 1]]
        return items

    def bth(self, nid: int, items: dict[int, bytes], hid: int, key_size: int, ent_size: int) -> list[tuple[int, bytes]]:
        h = items[hid]
        btype, ck, ce, levels, root = struct.unpack("<BBBBI", h)
        if btype != 0xB5 or ck != key_size or ce != ent_size:
            self.err(f"node 0x{nid:x}: bad BTHHEADER")
        if not root:
            return []

        def walk(h: int, level: int) -> list[tuple[int, bytes]]:
            d = items[h]
            if level == 0:
                n = ck + ce
                return [(int.from_bytes(d[i:i + ck], "little"), d[i + ck:i + n]) for i in range(0, len(d), n)]
            out = []
            n = ck + 4
            for i in range(0, len(d), n):
                key = int.from_bytes(d[i:i + ck], "little")
                sub = walk(struct.unpack_from("<I", d, i + ck)[0], level - 1)
                if sub and sub[0][0] != key:
                    self.err(f"node 0x{nid:x}: BTH index key mismatch")
                out.extend(sub)
            return out

        recs = walk(root, levels)
        keys = [k for k, _ in recs]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            self.err(f"node 0x{nid:x}: BTH keys not ascending/unique")
        return recs

    def tc_rows(self, nid: int, bdata: int) -> list[int]:
        items = self.heap(nid, self.stream_blocks(bdata))
        root = struct.unpack_from("<I", self.stream_blocks(bdata)[0], 4)[0]
        info = items[root]
        btype, ccols = info[0], info[1]
        rgib = struct.unpack_from("<4H", info, 2)
        hid_rows_index, hnid_rows = struct.unpack_from("<II", info, 10)
        if btype != 0x7C:
            self.err(f"node 0x{nid:x}: bad TCINFO")
        tags = [struct.unpack_from("<I", info, 22 + 8 * i)[0] for i in range(ccols)]
        if tags != sorted(tags):
            self.err(f"node 0x{nid:x}: columns not sorted by tag")
        if rgib[3] != rgib[2] + (ccols + 7) // 8:
            self.err(f"node 0x{nid:x}: rgib[TCI_bm] wrong")
        if hnid_rows & 0x1F:  # row matrix lives in a subnode
            blocks = self.stream_blocks(self.subnode_data(nid, hnid_rows))
            if any(len(b) != 8176 for b in blocks[:-1]):
                self.err(f"node 0x{nid:x}: row matrix block other than the last is not 8176 bytes")
        recs = self.bth(nid, items, hid_rows_index, 4, 4)
        indexes = sorted(struct.unpack("<I", v)[0] for _, v in recs)
        if indexes != list(range(len(recs))):
            self.err(f"node 0x{nid:x}: RowIndex does not cover rows 0..n-1")
        return [k for k, _ in recs]

    def subnode_data(self, nid: int, sub_nid: int) -> int:
        def find(bid: int) -> int:
            d = self.read_block(bid)
            _, level, cent = struct.unpack_from("<BBH", d)
            for i in range(cent):
                if level == 0:
                    n, bd, _ = struct.unpack_from("<QQQ", d, 8 + i * 24)
                    if n == sub_nid:
                        return bd
                else:
                    found = find(struct.unpack_from("<QQ", d, 8 + i * 16)[1])
                    if found:
                        return found
            return 0

        bid_sub = self.nbt[nid][1]
        found = find(bid_sub) if bid_sub else 0
        if not found:
            self.err(f"node 0x{nid:x}: subnode 0x{sub_nid:x} missing")
        return found

    def messaging(self) -> None:
        for required in (0x21, 0x61, 0x122, 0x60D, 0x60E, 0x60F, 0x671, 0x692, 0x1E1):
            if required not in self.nbt:
                self.err(f"required node 0x{required:x} missing")
        folders = [nid for nid in self.nbt if nid & 0x1F == 2]
        for f in folders:
            idx = f >> 5
            for t in (0xD, 0xE, 0xF):
                if (idx << 5 | t) not in self.nbt:
                    self.err(f"folder 0x{f:x}: TC type 0x{t:x} missing")
            children = sorted(n for n, (_, _, p) in self.nbt.items() if p == f and n & 0x1F == 2 and n != f)
            msgs = sorted(n for n, (_, _, p) in self.nbt.items() if p == f and n & 0x1F == 4)
            ht = sorted(self.tc_rows(idx << 5 | 0xD, self.nbt[idx << 5 | 0xD][0]))
            ct = sorted(self.tc_rows(idx << 5 | 0xE, self.nbt[idx << 5 | 0xE][0]))
            if ht != children:
                self.err(f"folder 0x{f:x}: hierarchy table {ht} != child folders {children}")
            if ct != msgs:
                self.err(f"folder 0x{f:x}: contents table does not match child messages")
        for nid, (bdata, bsub, _) in self.nbt.items():
            if nid & 0x1F in (2, 4) or nid in (0x21, 0x61):
                blocks = self.stream_blocks(bdata)
                items = self.heap(nid, blocks)
                root = struct.unpack_from("<I", blocks[0], 4)[0]
                self.bth(nid, items, root, 2, 6)

    def run(self) -> list[str]:
        self.header()
        self.maps()
        self.blocks()
        self.nodes()
        self.coverage()
        self.messaging()
        return self.errors


def main() -> int:
    errors = Checker(sys.argv[1]).run()
    for e in errors:
        print("ERROR:", e)
    print("OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
