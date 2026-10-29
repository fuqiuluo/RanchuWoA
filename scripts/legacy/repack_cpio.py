"""解析 newc cpio 归档、替换指定文件、重新输出单个归档。

用法: py repack_cpio.py <in1.cpio> [in2.cpio ...] --replace first_stage_ramdisk/fstab.ranchu=<newfile> -o out.cpio
"""
import sys
from pathlib import Path


def parse_cpio(data: bytes):
    """返回 [(name, mode, uid, gid, filesize, content)]"""
    entries = []
    pos = 0
    while pos + 110 <= len(data):
        if data[pos:pos + 6] not in (b"070701", b"070702"):
            # 跳过填充字节找下一个魔数
            nxt = data.find(b"070701", pos + 1)
            nxt2 = data.find(b"070702", pos + 1)
            cands = [n for n in (nxt, nxt2) if n >= 0]
            if not cands:
                break
            pos = min(cands)
            continue
        hdr = data[pos:pos + 110]
        f = lambda off, ln: int(hdr[off:off + ln], 16)
        ino, mode, uid, gid, nlink, mtime, fsize = (f(6, 8), f(14, 8), f(22, 8),
                                                    f(30, 8), f(38, 8), f(46, 8),
                                                    f(54, 8))
        namesize = f(94, 8)
        check = f(102, 8)
        name = data[pos + 110:pos + 110 + namesize - 1].decode()
        dstart = pos + 110 + namesize
        dstart = (dstart + 3) & ~3
        content = data[dstart:dstart + fsize]
        if name == "TRAILER!!!":
            break
        entries.append((name, mode, uid, gid, fsize, content))
        pos = (dstart + fsize + 3) & ~3
    return entries


def write_cpio(entries, out: Path):
    with open(out, "wb") as fo:
        ino = 100
        for name, mode, uid, gid, fsize, content in entries:
            nb = name.encode() + b"\0"
            hdr = ("070701"
                   + f"{ino:08X}" + f"{mode:08X}" + f"{uid:08X}" + f"{gid:08X}"
                   + f"{1:08X}" + f"{0:08X}" + f"{fsize:08X}"
                   + f"{0:08X}" + f"{0:08X}" + f"{0:08X}" + f"{0:08X}"
                   + f"{len(nb):08X}" + f"{0:08X}").encode()
            fo.write(hdr + nb)
            pad = (-(110 + len(nb))) % 4
            fo.write(b"\0" * pad)
            fo.write(content)
            if content:
                fo.write(b"\0" * ((-fsize) % 4))
            ino += 1
        fo.write(("070701" + "0" * 8 * 12 + f"{11:08X}" + "0" * 8).encode()
                 + b"TRAILER!!!\0" + b"\0" * 3 + b"\0" * 512)


def main():
    args = sys.argv[1:]
    files = []
    replaces = {}
    out = None
    i = 0
    while i < len(args):
        if args[i] == "--replace":
            k, v = args[i + 1].split("=", 1)
            replaces[k] = v
            i += 2
        elif args[i] == "-o":
            out = Path(args[i + 1])
            i += 2
        else:
            files.append(args[i])
            i += 1
    all_entries = []
    seen = set()
    for f in files:
        for e in parse_cpio(Path(f).read_bytes()):
            if e[0] in seen:
                continue
            seen.add(e[0])
            all_entries.append(e)
    for name, newfile in replaces.items():
        content = Path(newfile).read_bytes()
        hit = False
        for idx, (n, mode, uid, gid, fsize, _) in enumerate(all_entries):
            if n == name:
                all_entries[idx] = (n, mode, uid, gid, len(content), content)
                hit = True
        if not hit:
            all_entries.append((name, 0o100644, 0, 0, len(content), content))
    write_cpio(all_entries, out)
    total = sum(e[4] for e in all_entries)
    print(f"OK: {len(all_entries)} 项, 总内容 {total/1e6:.2f} MB -> {out}")


if __name__ == "__main__":
    main()
