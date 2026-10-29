"""patch_soname.py — 原地修补 SwiftShader .so 的 SONAME 字符串。

libEGL_deprecated_swiftshader.so   -> libEGL_emulation.so
libGLESv2_deprecated_swiftshader.so -> libGLESv2_emulation.so

新名比旧名短，直接在 .dynstr 内覆盖并补 NUL，不改变任何偏移，
SONAME/NEEDED 都指向同一 dynstr，安全无损。
"""
import struct
import sys

RENAMES = {
    b"libEGL_deprecated_swiftshader.so": b"libEGL_emulation.so",
    b"libGLESv2_deprecated_swiftshader.so": b"libGLESv2_emulation.so",
    b"libGLESv1_cm_deprecated_swiftshader.so": b"libGLESv1_cm_emulation.so",
}


def find_dynstr(d):
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    e_shentsize, e_shnum = struct.unpack_from("<HH", d, 0x3A)
    secs = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        vals = struct.unpack_from("<IIQQQQIIQQ", d, o)
        secs.append(dict(zip(
            "name typ flags addr off size link info align entsize".split(), vals)))
    shstr = secs[struct.unpack_from("<H", d, 0x3E)[0]]
    tab = d[shstr["off"]:shstr["off"] + shstr["size"]]
    for s in secs:
        e = tab.index(b"\0", s["name"])
        s["sname"] = tab[s["name"]:e].decode()
    for s in secs:
        if s["sname"] == ".dynstr":
            return s["off"], s["size"]
    raise RuntimeError("no .dynstr")


def patch_file(src, dst):
    d = bytearray(open(src, "rb").read())
    off, size = find_dynstr(d)
    region = d[off:off + size]
    n = 0
    for old, new in RENAMES.items():
        if len(new) > len(old):
            raise RuntimeError(f"{new} longer than {old}")
        pos = region.find(old)
        while pos != -1:
            end = pos + len(old)
            # 必须是完整字符串（前后都是 NUL 或起始），避免误伤子串
            before_ok = pos == 0 or region[pos - 1] == 0
            after_ok = end >= size or region[end] == 0
            if before_ok and after_ok:
                region[pos:end] = new + b"\0" * (len(old) - len(new))
                n += 1
            pos = region.find(old, pos + 1)
    d[off:off + size] = region
    open(dst, "wb").write(d)
    print(f"{src} -> {dst}: {n} string(s) rewritten")


def dump_dyn(path):
    d = open(path, "rb").read()
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    e_shentsize, e_shnum = struct.unpack_from("<HH", d, 0x3A)
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        name, typ, flags, addr, off, size = struct.unpack_from("<IIQQQQ", d, o)
        if typ == 6:  # SHT_DYNAMIC
            doff = off
    dsize = 0
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        if struct.unpack_from("<I", d, o + 4)[0] == 6:  # SHT_DYNAMIC
            doff = struct.unpack_from("<Q", d, o + 24)[0]
            dsize = struct.unpack_from("<Q", d, o + 32)[0]
    if not dsize:
        raise RuntimeError("no .dynamic")
    strs_off, strs_size = find_dynstr(d)
    dynstr = d[strs_off:strs_off + strs_size]
    print(f"== {path}")
    for i in range(dsize // 16):
        tag, val = struct.unpack_from("<qQ", d, doff + i * 16)
        if tag == 0:
            break
        if tag in (1, 14):
            e = dynstr.index(b"\0", val)
            print("  DT_%s: %s" % ("NEEDED" if tag == 1 else "SONAME",
                                   dynstr[val:e].decode()))


if __name__ == "__main__":
    srcdir, dstdir = sys.argv[1], sys.argv[2]
    for old, new in RENAMES.items():
        try:
            patch_file(f"{srcdir}/{old.decode()}", f"{dstdir}/{new.decode()}")
        except FileNotFoundError:
            print(f"skip {old.decode()} (not built)")
    for old, new in RENAMES.items():
        p = f"{dstdir}/{new.decode()}"
        try:
            dump_dyn(p)
        except FileNotFoundError:
            pass
