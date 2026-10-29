"""superimg.py — 解析 system.img（GPT 裸盘）里的 super 分区与 liblp 逻辑分区表。

用法:
  py scripts/superimg.py <system.img>            列出 GPT 分区、super 内逻辑分区与空闲空间
  py scripts/superimg.py <system.img> <分区名>    只打印该逻辑分区的 (字节偏移, 字节数)
  py scripts/superimg.py <system.img> --grow <分区名> <新字节数> [--rw]
      原地扩大单 extent 逻辑分区（其后必须是空闲区），--rw 同时清掉 READONLY 属性；
      改写全部 metadata 副本（各 slot 的主/备份）并重算校验和。分区内文件系统需另行扩容。

liblp 布局（system/core/fs_mgr/liblp/include/liblp/metadata_format.h）：
  super+0      4096 字节保留区
  super+4096   LpMetadataGeometry（主），super+8192 备份
  super+12288  metadata slot 0（header + tables），之后是备份 slot
"""
import struct
import sys

LP_GEOMETRY_MAGIC = 0x616C4467
LP_HEADER_MAGIC = 0x414C5030
SECTOR = 512


def gpt_partitions(f):
    f.seek(512)
    hdr = f.read(92)
    if hdr[:8] != b"EFI PART":
        return []
    entries_lba, num, esize = struct.unpack_from("<QII", hdr, 72)
    f.seek(entries_lba * SECTOR)
    raw = f.read(num * esize)
    out = []
    for i in range(num):
        e = raw[i * esize:(i + 1) * esize]
        if e[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le").rstrip("\0")
        out.append((name, first * SECTOR, (last - first + 1) * SECTOR))
    return out


def parse_lp(f, base):
    f.seek(base + 4096)
    g = f.read(52)
    magic, struct_size = struct.unpack_from("<II", g, 0)
    if magic != LP_GEOMETRY_MAGIC:
        raise RuntimeError(f"geometry magic 不对: {magic:#x}")
    # magic, struct_size, checksum[32], metadata_max_size, metadata_slot_count, logical_block_size
    metadata_max_size, slot_count, logical_block_size = struct.unpack_from("<III", g, 40)
    hdr_off = base + 4096 * 3
    f.seek(hdr_off)
    h = f.read(256)
    hmagic, major, minor, header_size = struct.unpack_from("<IHHI", h, 0)
    if hmagic != LP_HEADER_MAGIC:
        raise RuntimeError(f"header magic 不对: {hmagic:#x}")
    # header: magic, major, minor, header_size, header_checksum[32], tables_size, tables_checksum[32]
    # 然后 partitions/extents/groups/block_devices 四个 LpMetadataTableDescriptor(offset,num,entry_size)
    tables_size, = struct.unpack_from("<I", h, 44)
    descs = [struct.unpack_from("<III", h, 80 + 12 * i) for i in range(4)]
    f.seek(hdr_off + header_size)
    tables = f.read(tables_size)

    def rows(i):
        off, num, esz = descs[i]
        return [tables[off + k * esz: off + (k + 1) * esz] for k in range(num)]

    groups = []
    for r in rows(2):
        name = r[:36].split(b"\0")[0].decode()
        flags, maximum = struct.unpack_from("<IQ", r, 36)
        groups.append((name, flags, maximum))
    bdevs = []
    for r in rows(3):
        first_sector, alignment, alignment_offset, size = struct.unpack_from("<QIIQ", r, 0)
        name = r[24:60].split(b"\0")[0].decode()
        bdevs.append((name, first_sector, size))
    exts = []
    for r in rows(1):
        nsec, ttype, tdata, tsrc = struct.unpack_from("<QIQI", r, 0)
        exts.append((nsec, ttype, tdata, tsrc))
    parts = []
    for r in rows(0):
        name = r[:36].split(b"\0")[0].decode()
        attrs, first_ext, num_ext, grp = struct.unpack_from("<IIII", r, 36)
        pe = exts[first_ext:first_ext + num_ext]
        parts.append((name, attrs, groups[grp][0] if grp < len(groups) else grp, pe))
    return dict(geometry=(metadata_max_size, slot_count, logical_block_size), version=(major, minor),
                groups=groups, bdevs=bdevs, parts=parts)


def grow(img, part_name, new_bytes, make_rw):
    import hashlib
    if new_bytes % 4096:
        sys.exit("新大小必须是 4096 的倍数")
    f = open(img, "r+b")
    super_off = next(off for name, off, _ in gpt_partitions(f) if name == "super")
    lp = parse_lp(f, super_off)
    max_size, slots, _ = lp["geometry"]
    bdev_sectors = lp["bdevs"][0][2] // SECTOR
    new_sectors = new_bytes // SECTOR
    # 检查目标分区之后到下一个 extent 之间的空闲
    target = next((p for p in lp["parts"] if p[0] == part_name), None)
    if target is None or len(target[3]) != 1:
        sys.exit(f"{part_name} 不存在或不是单 extent")
    start = target[3][0][2]
    others = sorted(e[2] for p in lp["parts"] if p[0] != part_name for e in p[3] if e[2] > start)
    limit = others[0] if others else bdev_sectors
    if start + new_sectors > limit:
        sys.exit(f"空间不足：最多 {(limit - start) * SECTOR} 字节")
    group_used = sum(sum(e[0] for e in p[3]) for p in lp["parts"] if p[2] == target[2] and p[0] != part_name)
    group_max = next(g[2] for g in lp["groups"] if g[0] == target[2])
    if group_max and (group_used + new_sectors) * SECTOR > group_max:
        sys.exit("超过分区组上限")

    patched = 0
    base = super_off + 4096 * 3
    for copy in range(2 * slots):  # 主 slot 们，然后备份 slot 们
        hdr_off = base + copy * max_size
        f.seek(hdr_off)
        h = bytearray(f.read(256))
        if struct.unpack_from("<I", h, 0)[0] != LP_HEADER_MAGIC:
            continue
        header_size, = struct.unpack_from("<I", h, 8)
        tables_size, = struct.unpack_from("<I", h, 44)
        descs = [struct.unpack_from("<III", h, 80 + 12 * i) for i in range(4)]
        f.seek(hdr_off + header_size)
        tables = bytearray(f.read(tables_size))
        # 先验证我们对校验和的理解与现有数据一致，否则不动它
        chk = bytearray(h[:header_size])
        chk[12:44] = b"\0" * 32
        if hashlib.sha256(chk).digest() != bytes(h[12:44]) or \
                hashlib.sha256(tables).digest() != bytes(h[48:80]):
            sys.exit(f"metadata 副本 {copy} 校验和不符，中止")
        p_off, p_num, p_sz = descs[0]
        e_off, _, e_sz = descs[1]
        for i in range(p_num):
            r = p_off + i * p_sz
            name = tables[r:r + 36].split(b"\0")[0].decode()
            if name != part_name:
                continue
            attrs, first_ext, num_ext, _ = struct.unpack_from("<IIII", tables, r + 36)
            if make_rw:
                struct.pack_into("<I", tables, r + 36, attrs & ~1)
            struct.pack_into("<Q", tables, e_off + first_ext * e_sz, new_sectors)
        h[48:80] = hashlib.sha256(tables).digest()
        h = h[:header_size]
        h[12:44] = b"\0" * 32
        h[12:44] = hashlib.sha256(bytes(h)).digest()
        f.seek(hdr_off)
        f.write(h)
        f.seek(hdr_off + header_size)
        f.write(tables)
        patched += 1
    f.close()
    print(f"已改写 {patched} 份 metadata：{part_name} -> {new_bytes} 字节" + ("，已清 READONLY" if make_rw else ""))


def main():
    img = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "--grow":
        grow(img, sys.argv[3], int(sys.argv[4]), "--rw" in sys.argv)
        return
    want = sys.argv[2] if len(sys.argv) > 2 else None
    f = open(img, "rb")
    gpt = gpt_partitions(f)
    super_off = 0
    for name, off, size in gpt:
        if name == "super":
            super_off = off
    lp = parse_lp(f, super_off)
    if want:
        for name, attrs, grp, pe in lp["parts"]:
            if name == want:
                if len(pe) != 1:
                    print(f"{name} 有 {len(pe)} 个 extent", file=sys.stderr)
                nsec, ttype, tdata, _ = pe[0]
                print(super_off + tdata * SECTOR, sum(e[0] for e in pe) * SECTOR)
                return
        sys.exit(f"没有逻辑分区 {want}")
    print("GPT:")
    for name, off, size in gpt:
        print(f"  {name:12} off={off:>12} size={size:>12} ({size / 2**20:.1f} MiB)")
    print(f"liblp v{lp['version'][0]}.{lp['version'][1]} geometry(max_meta,slots,blk)={lp['geometry']}")
    for name, flags, maximum in lp["groups"]:
        print(f"  group {name:24} flags={flags} max={maximum}")
    for name, first, size in lp["bdevs"]:
        print(f"  bdev {name} first_logical_sector={first} size={size} ({size / 2**20:.1f} MiB)")
    used = []
    for name, attrs, grp, pe in lp["parts"]:
        total = sum(e[0] for e in pe) * SECTOR
        desc = ", ".join(f"[sec {e[2]}+{e[0]}]" for e in pe)
        print(f"  part {name:16} grp={grp:20} attrs={attrs} size={total:>11} ({total / 2**20:.1f} MiB) "
              f"abs_off={super_off + pe[0][2] * SECTOR if pe else '-'} {desc}")
        for e in pe:
            used.append((e[2], e[2] + e[0]))
    used.sort()
    if lp["bdevs"]:
        first, size = lp["bdevs"][0][1], lp["bdevs"][0][2]
        cur = first
        for a, b in used:
            if a > cur:
                print(f"  free: sec {cur}..{a} ({(a - cur) * SECTOR / 2**20:.1f} MiB)")
            cur = max(cur, b)
        end = size // SECTOR
        if end > cur:
            print(f"  free: sec {cur}..{end} ({(end - cur) * SECTOR / 2**20:.1f} MiB)")


if __name__ == "__main__":
    main()
