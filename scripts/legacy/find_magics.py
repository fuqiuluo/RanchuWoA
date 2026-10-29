"""定位镜像内部分区/文件系统魔数。"""
from pathlib import Path

IMG = Path(r"C:\Users\admin\Documents\Emulator\images\android-35\arm64-v8a")

MAGICS = {
    b"EFI PART": "GPT header",
    b"\x67\x44\x6c\x61": "Android super geometry magic (gDla)",
    b"\x4c\x50": None,  # 占位，LP 太短不精确
    b"\xe0\xf5\xe1\xe2": "EROFS superblock",
    b"\x53\xef": None,  # ext 魔数太短，单独看
    b"\xfa\xff\xff\xff": "qcow2? (no)",
    b"AVB0": "AVB vbmeta header",
    b"\x00\x00\x10\x20": None,
}

for name in ["vendor.img", "system.img", "encryptionkey.img"]:
    p = IMG / name
    with open(p, "rb") as f:
        first_mb = f.read(1 << 20)
    print(f"===== {name} =====")
    first_nonzero = next((i for i, b in enumerate(first_mb) if b), None)
    print(f"  首个非零字节偏移: {first_nonzero} (0x{first_nonzero:x})" if first_nonzero is not None else "  前 1MB 全零")
    for magic, label in MAGICS.items():
        if not label:
            continue
        idx = first_mb.find(magic)
        if idx >= 0:
            print(f"  {label} @ 0x{idx:x}")
    # erofs 魔数在 superblock 偏移 1024 处
    if first_mb[1024:1028] == b"\xe0\xf5\xe1\xe2":
        print("  EROFS superblock @ 1024")
    # ext4 superblock @ 1024
    if first_mb[1024:1026] == b"\x53\xef":
        print("  ext2/3/4 superblock @ 1024")
    print(f"  上下文 @ 首非零: {first_mb[first_nonzero:first_nonzero+32].hex() if first_nonzero is not None else ''}")
