"""liblp.py — 解析 Android super 镜像的逻辑分区表（liblp 元数据）。

用法: python liblp.py <super.img> [偏移]
输出: 逻辑分区名 -> (super 内绝对偏移, 字节数)
"""
import hashlib
import struct
import sys

GEOM_MAGIC = 0x616C4467
HDR_MAGIC = 0x414C5030


def parse_super(path, base=0):
    f = open(path, "rb")

    def rd(off, n):
        f.seek(base + off)
        return f.read(n)

    g = rd(4096, 512)
    magic, struct_size = struct.unpack_from("<II", g, 0)
    if magic != GEOM_MAGIC:
        raise RuntimeError(f"not a super image (geometry magic {magic:#x})")
    metadata_max_size, = struct.unpack_from("<I", g, 0x44 - 4096 + 4096) if False else (struct.unpack_from("<I", g, 44),)
    # geometry: magic4 size4 csum32 sig32 -> 0x48 处 metadata_max_size(u32), slot_count(u16), block_size(u32)
    metadata_max_size, slot_count, block_size = struct.unpack_from("<IHI", g, 72 - 4096 + 4096) if False else struct.unpack_from("<IHI", g, 0x48)
    # metadata header 紧随 geometry（4096 大小的 geometry 之后，取 slot0）
    hdr_off = 4096 * 3
    h = rd(hdr_off, 256)
    hmagic, major, minor, hsize = struct.unpack_from("<IHHI", h, 0)
    if hmagic != HDR_MAGIC:
        raise RuntimeError(f"bad metadata header magic {hmagic:#x}")
    # header: magic4 ver2+2 size4 hdr_csum32 tbl_size4 tbl_csum32 (共 0x50)
    # 之后 tables 描述: 5 组 (offset u32, entry_size u32, num u32)
    # 描述符布局: (offset u32, count u32, entry_size u32)，相对 tables 起点（header 之后）
    tbl_base = hdr_off + hsize
    parts_off, part_num, part_sz = struct.unpack_from("<III", h, 0x50)
    exts_off, ext_num, ext_sz = struct.unpack_from("<III", h, 0x5C)

    parts_raw = rd(tbl_base + parts_off, part_sz * part_num)
    exts_raw = rd(tbl_base + exts_off, ext_sz * ext_num)
    out = []
    for i in range(part_num):
        p = parts_raw[i * part_sz:(i + 1) * part_sz]
        name = p[0:36].split(b"\0")[0].decode()
        attrs, first_ext, num_ext, grp = struct.unpack_from("<IIII", p, 36)
        first_off = None
        total_bytes = 0
        for j in range(first_ext, first_ext + num_ext):
            e = exts_raw[j * ext_sz:(j + 1) * ext_sz]
            nsec, = struct.unpack_from("<Q", e, 0)
            start_sec, = struct.unpack_from("<Q", e, 12)  # target_type u32@8 后跟 start_sector
            if first_off is None:
                first_off = start_sec * 512
            total_bytes += nsec * 512
        out.append((name, first_off, total_bytes))
    return out


if __name__ == "__main__":
    img = sys.argv[1]
    base = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    for name, off, size in parse_super(img, base):
        # 校验 ext4 魔数
        ok = False
        if off is not None:
            try:
                f = open(img, "rb")
                f.seek(base + off + 1024 + 56)
                ok = struct.unpack("<H", f.read(2))[0] == 0xEF53
            except Exception:
                ok = False
        print(f"{name:16} abs_off={(base + (off or 0)):>14} size={size:>14} ext4={ok}")
