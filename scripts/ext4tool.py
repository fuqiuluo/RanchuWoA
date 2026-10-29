"""ext4tool.py — 读取（含偏移分区的）ext4 镜像：列目录 / 提取文件。

用法:
  python ext4tool.py ls <img> [分区偏移] <相对路径>     列目录（偏移可省，自动探测）
  python ext4tool.py get <img> [偏移] <相对路径>... -o <目录>   提取文件（目录递归）
"""
import sys
from pathlib import Path

from ext4 import EXT4_FT, Volume


class OffsetFile:
    """把 (文件, 偏移) 包装成 ext4.Volume 需要的流。"""

    def __init__(self, path, off):
        self.f = open(path, "rb")
        self.off = off
        self.pos = 0
        self.f.seek(0, 2)
        self.total = self.f.tell() - off

    def seek(self, p, w=0):
        if w == 0:
            self.pos = p
        elif w == 1:
            self.pos += p
        elif w == 2:
            self.pos = self.total + p
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        self.f.seek(self.off + min(self.pos, self.total))
        d = self.f.read(n) if n >= 0 else self.f.read(max(0, self.total - self.pos))
        self.pos += len(d)
        return d

    def peek(self, n):
        cur = self.pos
        d = self.read(n)
        self.pos = cur
        return d


def find_partition(img, name=b""):
    """若镜像是 GPT，返回 [(name, offset)]；否则 [(img 本身, 0)]。"""
    f = open(img, "rb")
    f.seek(512)
    if f.read(8) != b"EFI PART":
        return [("", 0)]
    import struct
    hdr = f.read(84)  # 头部其余部分（相对字段偏移已含 8 字节签名）
    part_lba, num_parts, part_size, _ = struct.unpack_from("<QIII", hdr, 72 - 8)
    f.seek(part_lba * 512)
    entries = f.read(num_parts * part_size)
    out = []
    for i in range(num_parts):
        e = entries[i * part_size:(i + 1) * part_size]
        if e[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        pname = e[56:128].decode("utf-16-le").rstrip("\0")
        out.append((pname, first * 512))
    return out


def open_volume(img, off=0):
    return Volume(OffsetFile(img, off))


def node(vol, rel):
    """按路径取 inode，返回 (inode, 是否目录) 或 None。"""
    rel = rel.strip("/")
    n = vol.root
    for part in [p for p in rel.split("/") if p]:
        try:
            n = n.inode_at(part)
        except Exception:
            return None
    is_dir = hasattr(n, "opendir")
    return n, is_dir


def entries(n):
    """目录项迭代（跳过 . 与 ..），产出 (名字, 类型字母)。"""
    for e, ft in n.opendir():
        name = e.name_str
        if name in (".", ".."):
            continue
        if ft == EXT4_FT.DIR:
            kind = "D"
        elif ft == EXT4_FT.SYMLINK:
            kind = "L"
        else:
            kind = "F"
        yield name, kind


def ls(vol, rel):
    r = node(vol, rel)
    if r is None:
        print(f"{rel}: not found")
        return
    n, is_dir = r
    if not is_dir:
        print(f"{rel}: file ({n.i_size} bytes)")
        return
    for name, kind in sorted(entries(n)):
        extra = ""
        try:
            child = n.inode_at(name)
            if kind == "F":
                extra = f" ({child.i_size})"
            elif kind == "L":
                extra = f" -> {child.readlink().decode(errors='replace')}"
        except Exception:
            pass
        print(f"{kind} {name}{extra}")


def read_file(vol, rel):
    r = node(vol, rel)
    if r is None:
        raise FileNotFoundError(rel)
    n, is_dir = r
    if is_dir:
        raise IsADirectoryError(rel)
    with n.open() as f:
        return f.read()


def extract(vol, rel, outdir):
    r = node(vol, rel)
    if r is None:
        print(f"skip (missing): {rel}")
        return
    n, is_dir = r
    dst = Path(outdir) / Path(rel).name
    if is_dir:
        dst.mkdir(parents=True, exist_ok=True)
        for name, kind in entries(n):
            if kind == "L":
                continue
            extract(vol, f"{rel.rstrip('/')}/{name}", dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    with n.open() as f, open(dst, "wb") as fo:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            fo.write(b)
    print(f"extracted {rel} -> {dst} ({n.i_size})")


def main():
    args = sys.argv[1:]
    cmd = args[0]
    img = args[1]
    rest = args[2:]
    off = 0
    if rest and rest[0].isdigit():
        off = int(rest.pop(0))
    else:
        parts = find_partition(img)
        # 自动用第一个 ext4 分区
        for pname, poff in parts:
            try:
                open_volume(img, poff)
                off = poff
                print(f"# using partition '{pname}' @ {poff}" if pname else "# raw image", file=sys.stderr)
                break
            except Exception:
                continue
    vol = open_volume(img, off)
    if cmd == "ls":
        for rel in rest or [""]:
            print(f"== {rel or '/'}")
            ls(vol, rel)
    elif cmd == "get":
        if "-o" in rest:
            i = rest.index("-o")
            outdir = rest[i + 1]
            rels = rest[:i]
        else:
            outdir, rels = ".", rest
        for rel in rels:
            extract(vol, rel, outdir)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
