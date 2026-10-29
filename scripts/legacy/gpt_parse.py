"""解析镜像的 GPT 分区表，列出分区名/起始/大小。"""
import struct
import sys
from pathlib import Path

IMG = Path(r"C:\Users\admin\Documents\Emulator\images\android-35\arm64-v8a")


def parse_gpt(p: Path):
    with open(p, "rb") as f:
        f.seek(512)
        hdr = f.read(92)
        assert hdr[:8] == b"EFI PART", f"{p}: 无 GPT"
        (part_lba, num_parts, part_size, _) = struct.unpack_from("<QIII", hdr, 72)
        f.seek(part_lba * 512)
        entries = f.read(num_parts * part_size)
    out = []
    for i in range(num_parts):
        e = entries[i * part_size:(i + 1) * part_size]
        if e[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le").rstrip("\0")
        out.append((name, first * 512, (last - first + 1) * 512))
    return out


for name in sys.argv[1:] or ["vendor.img", "system.img", "encryptionkey.img"]:
    p = IMG / name
    print(f"===== {p.name} ({p.stat().st_size/1e6:.1f} MB) =====")
    for pname, off, size in parse_gpt(p):
        print(f"  {pname:24s} @ 0x{off:012x}  {size/1e6:10.1f} MB")
