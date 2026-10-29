"""扫描镜像原始字节，找 goldfish 模块与文件系统特征。"""
import re
import sys
from pathlib import Path

IMG = Path(__file__).resolve().parents[2] / "images" / "android-35" / "arm64-v8a"

for name in ["vendor.img", "system.img"]:
    p = IMG / name
    data = p.read_bytes()
    print(f"===== {name} ({len(data)/1e6:.1f} MB) =====")
    print("  erofs 魔数(e0f5e1e2):", data[:4096].count(b"\xe0\xf5\xe1\xe2"))
    print("  ext 魔数(0xef53):", data[1024:1026] == b"\x53\xef")
    print("  goldfish 出现总数:", data.count(b"goldfish"))
    print("  goldfish_pipe:", data.count(b"goldfish_pipe"))
    print("  virtio:", data.count(b"virtio"))
    # 找可打印字符串里的 .ko 文件名
    kos = set()
    for m in re.finditer(rb"[A-Za-z0-9_.+-]{4,64}\.ko", data):
        kos.add(m.group().decode())
    goldfish_kos = sorted(k for k in kos if "goldfish" in k)
    print(f"  .ko 文件名总数: {len(kos)}, goldfish 相关: {goldfish_kos[:15]}")
    if name == "vendor.img" and not goldfish_kos:
        print("  .ko 样例:", sorted(kos)[:10])
