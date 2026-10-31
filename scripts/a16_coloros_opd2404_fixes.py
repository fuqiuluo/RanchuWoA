"""a16_coloros_opd2404_fixes.py — M9 实验：ColorOS 16（Android 16，SDK 36）GSI 在我们 Android 15 vendor 上
引导的适配补丁。在 gsi_disk.py 与 coloros_gsi_fixes.py 之后运行。

用法: py scripts/a16_coloros_opd2404_fixes.py <GSI 实验盘>        （盘不能正被 VM 使用）

为什么只需要这几个补丁（与 M8/ColorOS 14 的根本区别）：
  M8 是向下跨版本（A14 system + A15 vendor），Treble 不保证，要逐项降级（mapping 自造、VINTF 8.0→7.0、
  libvintf 越界、vendor 塞 A15 库……）。M9 是向上跨版本（A16 system + A15 vendor）——真机 OTA 升级就是这个
  方向，Treble 原生支持：GSI 自带 mapping/202404.cil（vendor 策略版本，实测在），libvintf 认 8.0 清单与
  android15 内核等级，linkerconfig 正常生成 vendor 命名空间。gsi_undef_types.py 实测 0 个未定义类型。
  所以 M8 的降级补丁全部不需要，只要处理：

补丁 1（system 分区，build.prop）：注释掉源机型抢跑的 ro.sf.lcd_density（420）、ro.opengles.version
  （GLES 3.2，宿主只有 3.0）、ro.surface_flinger.primary_display_orientation（平板面板竖装、SF 固定
  270°，虚拟屏本身是横的，见 gsi-guide §2.2/§2.4）。源机型同一加 Pad Pro（OPD2404），与 ColorOS 14 相同。

补丁 1b（同文件）：persist.sys.usb.config 改 mtp,adb（init.usb*.rc 只有这个顺序的触发器，写成 adb,mtp
  时 adbd 永远不启动）；ro.mediaserver.64b.enable=true（OPLUS mediaserverwrapper 按它 exec 64 位
  mediaserver64，宿主 Oryon 不支持 AArch32）。

补丁 2（vendor 分区，/etc/group）：定义 OPLUS 的 vendor 组 vendor_launcher / vendor_saures。
  bionic 只认带分区前缀的组名（vendor_），标准组名内置不需要文件；缺了这俩时 SystemConfig 把 -1 放进
  附加组、zygote setgroups EINVAL，应用进程起不来（M8 的根因，与系统版本无关）。

补丁 3（system 分区，dex 指令级，三处；原指令含 string/method 索引，版本不符会拒绝打）：
  a) framework/services.jar：ProcessListSocExtImpl.startProcess 直接 return-void。高通 SoC 扩展在应用
     进程启动时发 perfHint，zygote 只在 ro.boot.hardware 以 qcom 开头时预加载 libqti_performance.so，
     UnsatisfiedLinkError 无人捕获杀死 system_server（M8 §27.3-9，A16 指令同型、寄存器改 v6）。
  b) framework/oplus-services.jar：OplusShellPermissionUtils.revokeShellPermission 恒 false。OPLUS
     权限监控对 uid 2000 收回 WRITE_SECURE_SETTINGS，QEMU 开机命令静默失败（§27.3-4）。
  c) system_ext/framework/oplus-wifi-service.jar：OplusGtxController.validateExecutor 判空返回 false。
     GTX 省电执行器按 ro.hardware == "qcom"/MTK 创建，这里是 null，返回 true 后上层 NPE 杀
     system_server（§27.3 同型；C15 已修，A16 又回到 C14 形状）。
  （M8 的 Telecom SocDecouplingCenter 补丁不需要：A16 的 Telecom 里该类已不存在。）

补丁 4（system 分区，etc/vintf/compatibility_matrix.8.xml）：补一条 6.6 内核条目。内核等级按
  android15-8 换算成 8 级，A16 的 8 级矩阵只有 5.15/6.1 条目 → isBuildConsistent() 假 → 开机弹
  "您的设备内部出现了问题"（不阻塞，与 M8 补丁 15 同理）。

补丁 5（system 分区，system_ext/lib64/liboplusNetd.so）：initNetd() 改为立即返回 0。netd 启动时
  调它 dlopen oplusex 的 com.oplus.NetworkAssistSys 模块，该模块被移植包掏空 → 句柄 NULL →
  错误分支 strlen(dlerror()=NULL) SIGSEGV → netd 每 5 秒崩 → netd.rc 的 onrestart 连坐杀 zygote，
  整个框架无声重启循环（首个开机阻塞点）。

补丁 6（vendor 分区，vendor_sepolicy{,_debug}.cil）：logd 写串口的 allow。A16 平台策略像 A14 一样
  没有这条，没有串口 logcat 就丢 panic 前的最后现场；另外连接内核的补丁见 scripts/kernel_fixes.py
  （lpm_trie UBSAN 误报 brk，A16 connectivity 的 BPF 调用会炸内核，与 GSI 盘无关）。

实测记录见 docs/RESEARCH.md §28。
"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpine_job import ROOT, run_job                      # noqa: E402
from a14_coloros_opd2404_fixes import set_props                     # noqa: E402
from coloros_gsi_fixes import Dex                         # noqa: E402
from a15_hyperos_dada_fixes import patch_props, replace_cmds       # noqa: E402
from vendor_install import _write_file_cmds               # noqa: E402
from a14_coloros_opd2404_fixes import patch_zip_dex                 # noqa: E402

# 补丁 1/1b：system 分区 build.prop → 注释掉的键 / 追加设置的键（沿用 ColorOS 14 同源机型的表）。
# C16 的移植包把 OPLUS 的 my_product 打进了 system 分区（C15 时代没有），/my_product/build.prop
# 在 vendor 之后由 OPLUS init 加载、抢在内核命令行前生效：420 密度与 270 转向都在这里（第
# 58/96/98 行），只注释 system/build.prop 不够，这里也要注释。
PROP_EDITS = [
    ("system/build.prop", ("ro.sf.lcd_density", "ro.opengles.version",
                           "ro.surface_flinger.primary_display_orientation")),
    ("my_product/build.prop", ("ro.sf.lcd_density", "ro.opengles.version",
                               "ro.surface_flinger.primary_display_orientation",
                               "persist.sys.recovery.input.rotation")),
]
PROP_SET = {
    "system/build.prop": [
        ("persist.sys.usb.config", "mtp,adb",
         "init.usb*.rc 只有 mtp,adb 的触发器，adb,mtp 不会启动 adbd"),
        ("ro.mediaserver.64b.enable", "true",
         "mediaserverwrapper 据此 exec 64 位的 mediaserver64；/system/bin/mediaserver 是 32 位，宿主 CPU 不支持 AArch32"),
    ],
}
# 补丁 4：要补 6.6 内核条目的框架兼容性矩阵 (路径, 内核 FCM 等级)
FCM_KERNEL = [("system/etc/vintf/compatibility_matrix.8.xml", 8)]

# 补丁 3：dex 指令级补丁（结构同 a14_coloros_opd2404_fixes.DEX_PATCHES；索引值取自 OP5D77L1-16-1761878977752）
DEX_PATCHES = {
    "system/framework/services.jar": (
        [("Lcom/android/server/am/ProcessListSocExtImpl;", "startProcess",
          "Lcom/android/server/am/HostingRecord;Landroid/os/Process$ProcessStartResult;"
          "Lcom/android/server/am/ProcessRecord;", "V", 0,
          (0x0062, 0x6492),                      # sget-object v6, mPerfServiceStartHint
          (0x000E, 0x0000))],                    # return-void; nop
        ["system/framework/oat/arm64/services.odex",
         "system/framework/oat/arm64/services.vdex",
         "system/framework/oat/arm64/services.art"]),
    "system/framework/oplus-services.jar": (
        [("Lcom/android/server/am/OplusShellPermissionUtils;", "revokeShellPermission",
          "Landroid/content/Context;Ljava/lang/String;I", "Z", 1,
          (0x0163, 0x29EB),                      # sget-boolean v1, IS_FORUM_VERSION
          (0x000F, 0x0000))],                    # return v0（v0 已由第 0 条置 0）; nop
        []),
    "system/system_ext/framework/oplus-wifi-service.jar": (
        [("Lcom/oplus/server/wifi/power/OplusGtxController;", "validateExecutor", "", "Z", 4,
          # const-string v1, "OplusGtxController"; const-string v0, "cmd executor is null";
          # invoke-static {v1, v0} Log.d —— 之后落到 const/4 v1, #0; return v1
          (0x011A, 0x56CD, 0x001A, 0x7AD6, 0x2071, 0x9146, 0x0001),
          (0x0112, 0x010F, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000))],
        ["system/system_ext/framework/oat/arm64/oplus-wifi-service.odex",
         "system/system_ext/framework/oat/arm64/oplus-wifi-service.vdex"]),
}

VENDOR_GROUPS = [b"vendor_launcher::2950:", b"vendor_saures::2951:"]

# 补丁 6（vendor 分区，etc/selinux/vendor_sepolicy{,_debug}.cil）：logd 写串口 /dev/hvc1 的 allow
# （goldfish-logcat → 宿主 logcat.txt）。A15 平台策略自带这条，A14/A16 都没有——A16 上没有它就
# 完全没有串口 logcat（panic 前的最后现场全丢，M9 第三轮实测 logcat.txt 0 字节）。类型名走
# mapping/202404.cil（vendor 策略版本 202404，A16 映射里都有）。
SEP_ADDON = ("; @ranchuwoa-addon: coloros16-diag\n"
             "; goldfish-logcat（logd 域）把 Android 日志写到串口 /dev/hvc1：A16 平台策略缺这些\n"
             "(allow logd_202404 serial_device_202404 (chr_file (append getattr ioctl open read write)))\n"
             "(allow logd_202404 device_202404 (dir (open read search)))\n"
             "(allow logd_202404 logdr_socket_202404 (sock_file (write getattr)))\n"
             "(allow logd_202404 logd_202404 (unix_stream_socket (connectto)))\n")

# 补丁 5（system 分区，system_ext/lib64/liboplusNetd.so）：initNetd 立即返回 0。
# netd 启动时经 OSSI_FETCH 拿 OPLUS 扩展对象并调 initNetd()，里面 dlopen
# /system_ext/lib64/oplusex/com.oplus.NetworkAssistSys/liboplusNetd.so——该模块被移植包掏空
# （目录只剩 0 字节 NetworkAssistSys.txt），句柄为 NULL 后错误分支把 dlerror() 的 NULL 直接
# 喂给 strlen → SIGSEGV，netd 每 5 秒崩一次；netd.rc 的 onrestart 又会 restart zygote，
# 整个框架每 5 秒被 init SIGKILL 一次（表现为 zygote "restarting"、system_server 无声死亡）。
# initNetd 只是可选的 OPLUS 网络扩展初始化（oplusnetd binder 服务 + iptables 扩展钩子），
# 跳过后 netd 走原生逻辑。按 .dynsym 符号定位（不靠特征码搜全文件），校验函数序言后把
# 前两条指令换成 mov w0,#0; ret（该库 .text 的 VMA 与文件偏移一致）。
NETD_REL = "system/system_ext/lib64/liboplusNetd.so"
NETD_SYM = b"_ZN7android4init21InitExtensionNetdImpl8initNetdEv"   # InitExtensionNetdImpl::initNetd()
NETD_OLD = bytes.fromhex("3f2303d5ff8301d1")   # paciasp; sub sp, sp, #0x60
NETD_NEW = bytes.fromhex("00008052c0035fd6")   # mov w0, #0; ret


def elf_dynsym_value(lib: bytes, name: bytes) -> int:
    """返回导出符号的 st_value（文件内偏移）；找不到返回 0。"""
    e_shoff = struct.unpack_from("<Q", lib, 0x28)[0]
    e_shentsize, e_shnum = struct.unpack_from("<HH", lib, 0x3A)
    sh = lambda i: lib[e_shoff + i * e_shentsize: e_shoff + (i + 1) * e_shentsize]
    for i in range(e_shnum):
        s = sh(i)
        if struct.unpack_from("<I", s, 4)[0] != 11:          # SHT_DYNSYM
            continue
        h = sh(struct.unpack_from("<I", s, 40)[0])            # sh_link → .dynstr 的节头
        strtab = lib[struct.unpack_from("<Q", h, 24)[0]:struct.unpack_from("<Q", h, 24)[0]
                     + struct.unpack_from("<Q", h, 32)[0]]    # 节头 → 节内容
        entsize, off, size = (struct.unpack_from("<Q", s, 56)[0], struct.unpack_from("<Q", s, 24)[0],
                              struct.unpack_from("<Q", s, 32)[0])
        for p in range(off, off + size, entsize):
            st_name, _, _, _, st_value, _ = struct.unpack_from("<IBBHQQ", lib, p)
            if not st_name or st_name >= len(strtab):
                continue
            end = strtab.find(b"\x00", st_name)
            if end < 0 or strtab[st_name:end] != name or not st_value:
                continue
            return st_value
    return 0


JOB = r"""
D="$DISK0"
[ -n "$D" ] || { echo "找不到实验盘"; exit 1; }
set -e
# ---- vendor 分区 ----
if [ -s /payload/vendor.cmds ]; then
losetup -o {voff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck0.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/vendor.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
debugfs -R 'cat etc/group' /dev/loop0 2>/dev/null | grep RanchuWoA -A1 || true
losetup -d /dev/loop0
fi
# ---- system 分区 ----
if [ -s /payload/system.cmds ]; then
losetup -o {soff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck1.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/system.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
echo "--- build.prop 里被注释掉的属性："
for p in {props}; do debugfs -R "cat $p" /dev/loop0 2>/dev/null | grep 'RanchuWoA' || true; done
for p in {props}; do debugfs -R "ea_get $p security.selinux" /dev/loop0 2>/dev/null; done
losetup -d /dev/loop0
fi
echo COLOROS16_FIXES_OK
"""


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    disk_arg = Path(sys.argv[1])
    soff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk_arg), "system"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    voff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk_arg), "vendor"],
                          capture_output=True, text=True, check=True).stdout.split()[0]

    stage = ROOT / "images" / ".alpine-job" / "coloros16-payload"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True)
    (stage / "labels").mkdir()

    # 补丁 1/1b：system 的 build.prop
    print("取出 build.prop ...")
    for i, (rel, _) in enumerate(PROP_EDITS):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig/prop{i}")], check=True, capture_output=True)
    scmds = []
    for i, (rel, keys) in enumerate(PROP_EDITS):
        name = f"prop{i}"
        changed = patch_props(stage / "orig" / name / Path(rel).name, stage / "files" / name, keys)
        if changed:
            print(f"{rel}：注释掉 {len(changed)} 行（{', '.join(changed[:3])}）")
        cur = (stage / "files" / name if changed else stage / "orig" / name / Path(rel).name).read_bytes()
        new = set_props(cur, PROP_SET.get(rel, ()))
        if new != cur:
            (stage / "files" / name).write_bytes(new)
            print(f"{rel}：设 {', '.join(f'{k}={v}' for k, v, _ in PROP_SET[rel])}")
        if changed or new != cur:
            scmds += replace_cmds(rel, f"/payload/files/{name}", 0o644, f"/tmp/label_{name}")
        else:
            print(f"{rel}：无需改动")
    shutil.rmtree(stage / "orig")

    # 补丁 2（vendor 分区）：/etc/group 定义 OPLUS 的 vendor 组（详见文件头说明）
    vcmds = []
    r = subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                        "/etc/group", "-o", str(stage / "orig-vgroup")], capture_output=True, text=True)
    vg = stage / "orig-vgroup" / "group"
    cur = vg.read_bytes() if r.returncode == 0 and vg.is_file() else None
    if cur is not None and all(ln in cur.splitlines() for ln in VENDOR_GROUPS):
        print("vendor/etc/group：OPLUS 组已定义，跳过")
    else:
        names = {ln.split(b":")[0] for ln in VENDOR_GROUPS}
        keep = [ln for ln in (cur or b"").splitlines() if ln.strip() and ln.split(b":")[0] not in names]
        (stage / "files" / "vgroup").write_bytes(b"".join(ln + b"\n" for ln in keep + VENDOR_GROUPS))
        if cur is not None:      # 原文件存在：保留它的 SELinux 标签
            vcmds += replace_cmds("etc/group", "/payload/files/vgroup", 0o644, "/tmp/label_vgroup")
        else:
            vcmds += _write_file_cmds(stage, "etc/group", "/payload/files/vgroup",
                                      0o644, 0, 0, "vendor_configs_file")
        print("vendor/etc/group：定义 vendor_launcher/vendor_saures")

    # 补丁 6（vendor 分区）：logd 写串口的 allow（详见 SEP_ADDON 处说明）
    for rel in ("etc/selinux/vendor_sepolicy.cil", "etc/selinux/vendor_sepolicy_debug.cil"):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                        "/" + rel, "-o", str(stage / "orig-sep")], check=True, capture_output=True)
        cur = (stage / "orig-sep" / Path(rel).name).read_text(encoding="utf-8", errors="replace")
        if SEP_ADDON in cur:
            print(f"vendor/{rel}：补丁已是最新，跳过")
            continue
        base = cur.split("; @ranchuwoa-addon: coloros16-diag")[0].rstrip("\n")
        (stage / "files" / Path(rel).name).write_text(base + "\n" + SEP_ADDON, encoding="utf-8",
                                                      newline="\n")
        vcmds += replace_cmds(rel, f"/payload/files/{Path(rel).name}", 0o644,
                              f"/tmp/label_{Path(rel).name}")
        print(f"vendor/{rel}：写入 logd 串口 allow")

    # 补丁 4（system 分区）：框架兼容性矩阵补一条 6.6 内核条目
    for k, (rel, level) in enumerate(FCM_KERNEL):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig-fcm{k}")], check=True, capture_output=True)
        text = (stage / f"orig-fcm{k}" / Path(rel).name).read_bytes().decode("utf-8")
        if 'kernel version="6.6.0"' in text:
            print(f"{rel}：已有 6.6 内核条目，跳过")
            continue
        at = text.index("    <kernel version=")
        entry = (f'    <!-- RanchuWoA: 模拟器内核是 android15-6.6 GKI，A16 的 8 级矩阵没有 6.6 条目 -->\n'
                 f'    <kernel version="6.6.0" level="{level}"/>\n')
        (stage / "files" / f"fcm{k}").write_bytes((text[:at] + entry + text[at:]).encode("utf-8"))
        scmds += replace_cmds(rel, f"/payload/files/fcm{k}", 0o644, f"/tmp/label_fcm{k}")
        print(f"{rel}：插入 6.6 内核条目（level {level}）")

    # 补丁 3（system 分区）：dex 指令级补丁（详见文件头说明）
    for k, (rel, (patches, stale)) in enumerate(DEX_PATCHES.items()):
        name = Path(rel).name
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig-dex{k}")], check=True, capture_output=True)
        blob, notes = patch_zip_dex((stage / f"orig-dex{k}" / name).read_bytes(), patches)
        shutil.rmtree(stage / f"orig-dex{k}")
        for n in notes:
            print(f"  {n}")
        if blob is None:
            print(f"{rel}：已打过，跳过")
            continue
        (stage / "files" / f"dex{k}-{name}").write_bytes(blob)
        scmds += replace_cmds(rel, f"/payload/files/dex{k}-{name}", 0o644, f"/tmp/label_dex{k}")
        scmds += [f"rm {p}" for p in stale]               # 预编译产物的 dex 校验和已对不上
        print(f"{rel}：写回，删除对应的预编译产物")

    # 补丁 5（system 分区）：liboplusNetd 的 initNetd 立即返回 0（详见 NETD_REL 处说明）
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                    "/" + NETD_REL, "-o", str(stage / "orig-netd")], check=True, capture_output=True)
    lib = bytearray((stage / "orig-netd" / Path(NETD_REL).name).read_bytes())
    at = elf_dynsym_value(bytes(lib), NETD_SYM)
    if not at:
        sys.exit(f"{NETD_REL}：找不到导出符号 initNetd（GSI 版本不同？）")
    cur = bytes(lib[at:at + 8])
    if cur == NETD_NEW:
        print(f"{NETD_REL}：initNetd 已打过，跳过")
    elif cur == NETD_OLD:
        lib[at:at + 8] = NETD_NEW
        (stage / "files" / "liboplusNetd.so").write_bytes(bytes(lib))
        scmds += replace_cmds(NETD_REL, "/payload/files/liboplusNetd.so", 0o644, "/tmp/label_netd")
        print(f"{NETD_REL}：initNetd 改为 mov w0,#0; ret @{at:#x}")
    else:
        sys.exit(f"{NETD_REL}：initNetd @{at:#x} 的序言与预期不符（{cur.hex()}，GSI 版本不同？）")

    if not scmds and not vcmds:
        print("已完成（无改动）")
        return
    (stage / "system.cmds").write_text("".join(c + "\n" for c in scmds), encoding="utf-8", newline="\n")
    if vcmds:
        (stage / "vendor.cmds").write_text("".join(c + "\n" for c in vcmds), encoding="utf-8", newline="\n")

    print("写回 vendor 与 system 分区（Alpine 辅助 VM）...")
    job = (JOB.replace("{voff}", str(voff)).replace("{soff}", str(soff))
           .replace("{props}", " ".join(rel for rel, _ in PROP_EDITS)))
    rc = run_job(job, disks=[disk_arg], payload_dir=stage,
                 log=ROOT / "images" / ".alpine-job" / "a16_coloros_opd2404_fixes.log", echo=False)
    print("完成" if rc == 0 else f"失败（rc={rc}），日志 images/.alpine-job/a16_coloros_opd2404_fixes.log")
    sys.exit(rc)


if __name__ == "__main__":
    main()
