"""coloros_gsi_fixes.py — M6 实验：给 ColorOS（OPLUS）GSI 的系统镜像打框架补丁，让它能在 ranchu 虚拟机上跑起来。

用法: py scripts/coloros_gsi_fixes.py <GSI 实验盘>        （scripts/gsi_disk.py 的产物，盘不能正被 VM 使用）

为什么要补（RESEARCH §21）：
  ColorOS 的 system_server 按真机的 CPU 簇拓扑工作。模拟器 vendor 的 dummy-cpufreq 驱动给每个 CPU 一个
  policy（最高频率都是 2 kHz），OPLUS 的 Process.getCpuClusters() 排出一个任意顺序（如 [4,2,0,5,3]），
  OplusOsenseCompressAction.calcAffinityExcludeSuperBigCore(String) 用它算 BitSet 区间时
  抛 IndexOutOfBoundsException（fromIndex 4 > toIndex 2），system_server 每分钟崩一次。
  该方法只是给后台内存压缩线程选 CPU 亲和性（避开超大核），直接改成 return-void。

做法：从镜像里取出 /system/framework/oplus-services.jar，在 dex 里按类/方法/原型定位代码，把第一条指令
改成 return-void（0x000e），重算 dex 的 SHA-1 与 adler32，重新打包并 4 字节对齐；再在 Alpine 辅助 VM 里
用 debugfs 写回 system 分区，删掉对应的预编译产物（oat/arm64/oplus-services.ocomp.*），ART 会直接跑新 dex。
"""
import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alpine_job import ROOT, run_job  # noqa: E402

JAR = "system/framework/oplus-services.jar"
# (类描述符, 方法名, 参数描述符串, 返回类型) —— 只支持 void 方法（改成 return-void）。
# 同一逻辑补丁的多版本签名并列（ColorOS 15 是 (String)V，ColorOS 16 改成了 ()V），
# 找到哪个打哪个，一个都没找到才报错。
PATCHES = [
    ("Lcom/android/server/am/OplusOsenseCompressAction;", "calcAffinityExcludeSuperBigCore",
     "Ljava/lang/String;", "V"),
    ("Lcom/android/server/am/OplusOsenseCompressAction;", "calcAffinityExcludeSuperBigCore",
     "", "V"),
]
STALE = ["system/framework/oat/arm64/oplus-services.ocomp.odex",
         "system/framework/oat/arm64/oplus-services.ocomp.vdex",
         "system/framework/oat/arm64/oplus-services.ocomp.art"]


def uleb(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


class Dex:
    def __init__(self, data: bytearray):
        self.d = data
        u32 = lambda off: struct.unpack_from("<I", data, off)[0]
        self.strings_off, self.types_off = u32(0x3C), u32(0x44)
        self.protos_off, self.methods_off = u32(0x4C), u32(0x5C)
        self.n_classes, self.classes_off = u32(0x60), u32(0x64)

    def string(self, idx):
        off = struct.unpack_from("<I", self.d, self.strings_off + 4 * idx)[0]
        _, pos = uleb(self.d, off)
        end = self.d.index(0, pos)
        return bytes(self.d[pos:end]).decode("utf-8", "replace")

    def type(self, idx):
        return self.string(struct.unpack_from("<I", self.d, self.types_off + 4 * idx)[0])

    def proto(self, idx):
        _, ret, params = struct.unpack_from("<III", self.d, self.protos_off + 12 * idx)
        args = ""
        if params:
            n = struct.unpack_from("<I", self.d, params)[0]
            args = "".join(self.type(struct.unpack_from("<H", self.d, params + 4 + 2 * i)[0])
                           for i in range(n))
        return args, self.type(ret)

    def method(self, idx):
        cls, proto, name = struct.unpack_from("<HHI", self.d, self.methods_off + 8 * idx)
        return self.type(cls), self.string(name), *self.proto(proto)

    def find_code(self, cls_desc, name, args, ret):
        """返回方法 insns 的文件偏移；找不到返回 None。"""
        for c in range(self.n_classes):
            base = self.classes_off + 32 * c
            if self.type(struct.unpack_from("<I", self.d, base)[0]) != cls_desc:
                continue
            data_off = struct.unpack_from("<I", self.d, base + 24)[0]
            pos = data_off
            sf, pos = uleb(self.d, pos)
            inf, pos = uleb(self.d, pos)
            dm, pos = uleb(self.d, pos)
            vm, pos = uleb(self.d, pos)
            for _ in range(sf + inf):                      # 跳过字段
                _, pos = uleb(self.d, pos)
                _, pos = uleb(self.d, pos)
            for count in (dm, vm):
                midx = 0
                for _ in range(count):
                    diff, pos = uleb(self.d, pos)
                    _, pos = uleb(self.d, pos)             # access_flags
                    code_off, pos = uleb(self.d, pos)
                    midx += diff
                    if code_off and self.method(midx) == (cls_desc, name, args, ret):
                        return code_off + 16               # code_item 头 16 字节后是 insns
        return None

    def fix_header(self):
        self.d[12:32] = hashlib.sha1(self.d[32:]).digest()
        struct.pack_into("<I", self.d, 8, zlib.adler32(self.d[12:]) & 0xFFFFFFFF)


def patch_jar(src: Path, dst: Path):
    zin = zipfile.ZipFile(src)
    out = io.BytesIO()
    done = set()
    with zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.startswith("classes") and info.filename.endswith(".dex"):
                dex = Dex(bytearray(data))
                changed = False
                for p in PATCHES:
                    at = dex.find_code(*p)
                    if at is not None:
                        # 首指令整体换成 return-void，剩余指令单元全部填 nop：方法变空。
                        # 只换单元 0 的话，原首指令多于 1 个单元时（C16 的 ()V 变体首指令是
                        # 3 单元的 invoke-static）后续指令边界错位 → VerifyError 杀 system_server。
                        n = struct.unpack_from("<I", dex.d, at - 4)[0]      # code_item.insns_size
                        if dex.d[at:at + 2] == b"\x0e\x00" and \
                           dex.d[at + 2:at + 2 * n] == b"\x00" * (2 * n - 2):
                            print(f"  {info.filename}: {p[0]}->{p[1]}({p[2]}){p[3]} @ {at:#x} 已打过")
                        else:
                            dex.d[at:at + 2 * n] = b"\x0e\x00" + b"\x00" * (2 * n - 2)
                            print(f"  {info.filename}: {p[0]}->{p[1]}({p[2]}){p[3]} @ {at:#x}"
                                  f" → return-void（清空 {n} 个代码单元）")
                        done.add(p)
                        changed = True
                if changed:
                    dex.fix_header()
                    data = bytes(dex.d)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
    if not done:
        sys.exit(f"有方法没找到：{PATCHES}")
    unaligned = dst.with_suffix(".unaligned")
    unaligned.write_bytes(out.getvalue())
    zipalign = Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/build-tools/34.0.0/zipalign.exe"
    if zipalign.exists():
        subprocess.run([str(zipalign), "-f", "4", str(unaligned), str(dst)], check=True)
        unaligned.unlink()
    else:
        print("  （没有 zipalign，dex 未对齐；ART 会解压到内存运行）")
        unaligned.replace(dst)


JOB = r"""
D="$DISK0"
[ -n "$D" ] || { echo "找不到实验盘"; exit 1; }
losetup -o {soff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck0.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/debugfs.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
debugfs -R 'stat {jar}' /dev/loop0 2>/dev/null | grep -E 'Size:'
losetup -d /dev/loop0
echo COLOROS_FIX_OK
"""


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    disk = Path(sys.argv[1])
    soff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk), "system"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    stage = ROOT / "images" / ".alpine-job" / "coloros-payload"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    print("取出 oplus-services.jar ...")
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk), soff, JAR,
                    "-o", str(stage / "orig")], check=True, capture_output=True)
    orig = stage / "orig" / "oplus-services.jar"
    patch_jar(orig, stage / "oplus-services.jar")
    shutil.rmtree(stage / "orig")
    (stage / "label").write_bytes(b"u:object_r:system_file:s0\x00")
    cmds = [f"rm {JAR}", f"write /payload/oplus-services.jar {JAR}", f"sif {JAR} mode 0100644",
            f"sif {JAR} uid 0", f"sif {JAR} gid 0", f"ea_set -f /payload/label {JAR} security.selinux"]
    cmds += [f"rm {p}" for p in STALE]
    (stage / "debugfs.cmds").write_text("".join(c + "\n" for c in cmds), encoding="utf-8")
    print("写回 system 分区（Alpine 辅助 VM）...")
    job = JOB.replace("{soff}", soff).replace("{jar}", JAR)
    rc = run_job(job, disks=[disk], payload_dir=stage, echo=False,
                 log=ROOT / "images" / ".alpine-job" / "coloros_fix.log")
    print("完成" if rc == 0 else f"失败（rc={rc}），日志 images/.alpine-job/coloros_fix.log")
    sys.exit(rc)


if __name__ == "__main__":
    main()
