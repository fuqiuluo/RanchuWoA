"""gsi_disk.py — 用 GSI 替换系统盘（GPT：vbmeta + super）里的 system 逻辑分区，其余分区（我们的 vendor、
模拟器的 product/system_ext/system_dlkm）原样保留。用于 M6（ColorOS 等 GSI 引导实验）。

用法: py scripts/gsi_disk.py <基础 system.img> <GSI 镜像> <输出磁盘> [--no-patch]
  GSI 镜像可以是原始镜像或 Android sparse 镜像（自动展开）；基础盘一般用 images/base/android-35/system.img。
  默认最后再跑 vendor_install --gsi 给 vendor 打兼容补丁（策略补充类型、OPLUS 的 *_debug 策略文件）。
  然后：avdctl create <名字> --system <输出磁盘>。

做法：GSI 通常比原 system 大得多，super 里放不下。复制基础盘后在末尾追加空间：
  1. GPT：super 分区的 last_lba 扩到新末尾，重写主/备份 GPT（含 CRC）和保护性 MBR；
  2. liblp：所有 metadata 副本里把块设备大小改成新 super 大小、分区组上限改为不限，system 的唯一 extent
     改指向追加出来的新区域（旧 system 区域闲置），重算 header/tables SHA256；
  3. 把 GSI 数据写到新区域。
first-stage fstab 没有 avb 标志（不做 AVB 校验），所以不用动 vbmeta。
"""
import hashlib
import os
import shutil
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from superimg import LP_HEADER_MAGIC, SECTOR, gpt_partitions, parse_lp  # noqa: E402

SPARSE_MAGIC = 0xED26FF3A
ALIGN = 1 << 20


def align_up(v, a):
    return (v + a - 1) // a * a


def image_size(path):
    """GSI 展开后的字节数（sparse 镜像读头部）。"""
    with open(path, "rb") as f:
        h = f.read(28)
    if struct.unpack_from("<I", h, 0)[0] == SPARSE_MAGIC:
        blk_sz, total_blks = struct.unpack_from("<II", h, 12)
        return blk_sz * total_blks, True
    return os.path.getsize(path), False


def fs_type(head):
    if struct.unpack_from("<I", head, 1024)[0] == 0xE0F5E1E2:
        return "erofs"
    if struct.unpack_from("<H", head, 1080)[0] == 0xEF53:
        return "ext4"
    return "未知"


def write_gsi(src, dst, offset):
    """把 GSI 写到 dst 的 offset 处；目标区域是新追加的全零空间，sparse 的 DONT_CARE/零填充直接跳过。"""
    with open(src, "rb") as f:
        h = f.read(28)
        if struct.unpack_from("<I", h, 0)[0] != SPARSE_MAGIC:
            f.seek(0)
            dst.seek(offset)
            shutil.copyfileobj(f, dst, 16 << 20)
            return
        file_hdr_sz, chunk_hdr_sz, blk_sz, _, total_chunks = struct.unpack_from("<HHIII", h, 8)
        f.seek(file_hdr_sz)
        pos = offset
        for _ in range(total_chunks):
            ch = f.read(chunk_hdr_sz)
            ctype, _, chunk_sz, total_sz = struct.unpack_from("<HHII", ch, 0)
            n = chunk_sz * blk_sz
            if ctype == 0xCAC1:                         # RAW
                dst.seek(pos)
                left = n
                while left:
                    buf = f.read(min(left, 16 << 20))
                    dst.write(buf)
                    left -= len(buf)
            elif ctype == 0xCAC2:                       # FILL
                pattern = f.read(4)
                if pattern != b"\0\0\0\0":
                    dst.seek(pos)
                    block = pattern * (blk_sz // 4)
                    for _ in range(chunk_sz):
                        dst.write(block)
            elif ctype == 0xCAC3:                       # DONT_CARE
                pass
            elif ctype == 0xCAC4:                       # CRC32
                f.read(4)
            else:
                sys.exit(f"未知 sparse chunk 类型 {ctype:#x}")
            pos += n


def rewrite_gpt(f, disk_bytes, super_last_lba):
    """super 分区扩到 super_last_lba，按新的磁盘大小重写主/备份 GPT 与保护性 MBR。"""
    last_lba = disk_bytes // SECTOR - 1
    f.seek(SECTOR)
    hdr = bytearray(f.read(92))
    entries_lba, num, esize = struct.unpack_from("<QII", hdr, 72)
    f.seek(entries_lba * SECTOR)
    entries = bytearray(f.read(num * esize))
    for i in range(num):
        e = i * esize
        if entries[e + 56:e + 128].decode("utf-16-le").rstrip("\0") == "super":
            struct.pack_into("<Q", entries, e + 40, super_last_lba)
    entries_sectors = align_up(num * esize, SECTOR) // SECTOR
    ecrc = zlib.crc32(entries)

    def header(current, backup, entries_at):
        h = bytearray(hdr)
        struct.pack_into("<QQ", h, 24, current, backup)
        struct.pack_into("<Q", h, 48, last_lba - entries_sectors - 1)      # last_usable_lba
        struct.pack_into("<Q", h, 72, entries_at)
        struct.pack_into("<I", h, 88, ecrc)
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, zlib.crc32(h))
        return bytes(h)

    f.seek(SECTOR)
    f.write(header(1, last_lba, entries_lba))
    f.seek(entries_lba * SECTOR)
    f.write(entries)
    backup_entries_lba = last_lba - entries_sectors
    f.seek(backup_entries_lba * SECTOR)
    f.write(entries)
    f.seek(last_lba * SECTOR)
    f.write(header(last_lba, 1, backup_entries_lba).ljust(SECTOR, b"\0"))
    # 保护性 MBR（0xEE 分区）覆盖整盘
    f.seek(446 + 12)
    f.write(struct.pack("<I", min(last_lba, 0xFFFFFFFF)))


def rewrite_lp(f, super_off, lp, super_bytes, sys_start, sys_sectors):
    max_size, slots, _ = lp["geometry"]
    base = super_off + 4096 * 3
    patched = 0
    for copy in range(2 * slots):
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
        chk = bytearray(h[:header_size])
        chk[12:44] = b"\0" * 32
        if hashlib.sha256(chk).digest() != bytes(h[12:44]) or \
                hashlib.sha256(tables).digest() != bytes(h[48:80]):
            sys.exit(f"metadata 副本 {copy} 校验和不符，中止")
        (p_off, p_num, p_sz), (e_off, _, e_sz), (g_off, g_num, g_sz), (b_off, b_num, b_sz) = descs
        for i in range(p_num):
            r = p_off + i * p_sz
            if tables[r:r + 36].split(b"\0")[0] == b"system":
                _, first_ext, num_ext, _ = struct.unpack_from("<IIII", tables, r + 36)
                if num_ext != 1:
                    sys.exit("system 不是单 extent")
                # LpMetadataExtent: num_sectors(8) target_type(4) target_data(8) target_source(4)
                struct.pack_into("<Q", tables, e_off + first_ext * e_sz, sys_sectors)
                struct.pack_into("<Q", tables, e_off + first_ext * e_sz + 12, sys_start)
        for i in range(g_num):                          # LpMetadataPartitionGroup: name[36] flags(4) max(8)
            r = g_off + i * g_sz
            struct.pack_into("<Q", tables, r + 40, 0)    # 0 = 不限
        for i in range(b_num):                          # LpMetadataBlockDevice: first(8) align(4) align_off(4) size(8)
            struct.pack_into("<Q", tables, b_off + i * b_sz + 16, super_bytes)
        h[48:80] = hashlib.sha256(tables).digest()
        h = h[:header_size]
        h[12:44] = b"\0" * 32
        h[12:44] = hashlib.sha256(bytes(h)).digest()
        f.seek(hdr_off)
        f.write(h)
        f.seek(hdr_off + header_size)
        f.write(tables)
        patched += 1
    return patched


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:] if a != "--no-patch"]
    if len(args) != 3:
        sys.exit(__doc__)
    base, gsi, out = args
    gsi_bytes, sparse = image_size(gsi)
    with open(base, "rb") as f:
        gpt = gpt_partitions(f)
        super_off = next(off for name, off, _ in gpt if name == "super")
        lp = parse_lp(f, super_off)
    used_end = max(e[2] + e[0] for p in lp["parts"] for e in p[3])    # super 内已用到的扇区
    sys_start = align_up(used_end * SECTOR, ALIGN) // SECTOR
    sys_bytes = align_up(gsi_bytes, ALIGN)
    super_bytes = align_up(sys_start * SECTOR + sys_bytes, ALIGN)
    disk_bytes = super_off + super_bytes + ALIGN                        # 末尾留给备份 GPT
    print(f"GSI {gsi_bytes} 字节（{'sparse' if sparse else '原始'}），system 新位置 super 扇区 {sys_start}，"
          f"super {super_bytes / 2**20:.0f} MiB，磁盘 {disk_bytes / 2**30:.2f} GiB")

    print(f"复制 {base} → {out}")
    shutil.copyfile(base, out)
    old_bytes = os.path.getsize(base)
    sys_abs = super_off + sys_start * SECTOR
    with open(out, "r+b") as f:
        f.truncate(disk_bytes)
        # 新 system 区域与原盘尾部重叠（原 super 末尾空闲区、旧备份 GPT）：先清零，
        # 因为写 sparse 镜像时会跳过 DONT_CARE / 零填充块，默认目标是全零
        if old_bytes > sys_abs:
            f.seek(sys_abs)
            left = old_bytes - sys_abs
            while left:
                n = min(left, 16 << 20)
                f.write(b"\0" * n)
                left -= n
        rewrite_gpt(f, disk_bytes, (super_off + super_bytes) // SECTOR - 1)
        n = rewrite_lp(f, super_off, lp, super_bytes, sys_start, sys_bytes // SECTOR)
        print(f"已改写 {n} 份 liblp metadata")
        print("写入 GSI ...")
        write_gsi(gsi, f, sys_abs)
        f.seek(sys_abs)
        print("system 文件系统：", fs_type(f.read(4096)))
    if "--no-patch" not in sys.argv:
        print("给 vendor 打 GSI 兼容补丁（Alpine 辅助 VM）...")
        import vendor_install
        rc = vendor_install.install(out, gsi=True, echo=False)
        if rc != 0:
            sys.exit(f"GSI 补丁失败（rc={rc}），日志 images/.alpine-job/vendor_install.log")
    print("完成")


if __name__ == "__main__":
    main()
