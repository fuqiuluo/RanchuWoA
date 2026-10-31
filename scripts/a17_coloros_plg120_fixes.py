"""coloros17_fixes.py — M10 实验：ColorOS 17（Android 17 预览，CinnamonBun/SDK 36.1）GSI 在我们
Android 15 vendor 上引导的适配补丁。在 gsi_disk.py 与 coloros_gsi_fixes.py 之后运行。

用法: py scripts/coloros17_fixes.py <GSI 实验盘>        （盘不能正被 VM 使用）

背景：A17 开发者预览（fingerprint CP21.260206，2026-03 安全补丁），MysticGSI 移植包在 Generic
目录（Coloros-OP5E11L1-CinnamonBun-20260325）。源机型 OPPO PLG120（旗舰手机，打孔屏约 1272 宽、
560 密度 → 实例 636x1390@280）。与 M9 同为向上跨版本且更省事：mapping/202404.cil 仍在、
gsi_undef_types 0 个未定义、8 级矩阵缺 6.6 内核条目（同 C16 的弹窗修法）。补丁基本是 C16 的
参数更新版：

补丁 1（system 分区，build.prop）：注释源机型抢跑的 ro.sf.lcd_density（560）、ro.opengles.version
  （GLES 3.2，宿主只有 3.0）。手机固件，没有 SF 转向属性。
补丁 1b（同文件 + my_product）：persist.sys.usb.config 改 mtp,adb（init.usb*.rc 只有这个顺序的
  触发器）；ro.mediaserver.64b.enable=true（32 位 mediaserver 在宿主 Oryon 上跑不了）。
  **my_product/build.prop 的 560 密度也要注释**（C16 起 MysticGSI 把 OPLUS my_product 打进 system
  分区，它在 vendor 之后加载抢跑，§28.2）。

补丁 2（vendor 分区，/etc/group）：定义 OPLUS 的 vendor 组 vendor_launcher / vendor_saures
  （bionic 只认分区前缀组名；缺了 zygote setgroups EINVAL，M8 的根因，与系统版本无关）。

补丁 3（system 分区，dex 指令级，两处；索引值取自 OP5E11L1-CinnamonBun-1773378663794）：
  a) framework/oplus-services.jar：OplusShellPermissionUtils.revokeShellPermission 恒 false
     （OPLUS 权限监控对 uid 2000 收权 → QEMU 开机命令静默失败，§27.3-4）。
  b) system_ext/framework/oplus-wifi-service.jar：OplusGtxController.validateExecutor 判空返回
     false（GTX 省电执行器按 qcom/MTK 创建，null 时返回 true 上层 NPE 杀 system_server）。
  注意：**ProcessListSocExtImpl.startProcess 不用补**——A17 预览里该方法本身就是 1 个代码单元的
  return-void（上游把 perfHint 路径砍了）；Telecom 的 SocDecouplingCenter 类也仍不存在。
  **A17 上给 SYSTEMSERVERCLASSPATH 上的 jar（services.jar 等）打 dex 补丁的路走不通**：zygote
  装配 classpath 时按校验和静默剔除改过的 jar（"Missing class com.android.server.SystemServer"，
  ClassLoaderContext 从 12 个掉到 10 个），zip/dex 校验全对也没用——别再试。

补丁 4（system 分区，etc/vintf/compatibility_matrix.8.xml）：补 6.6 内核条目（同 C16：8 级矩阵
  只有 5.15/6.1，缺 6.6 → 开机弹"设备内部出现了问题"，不阻塞）。

补丁 5（system 分区，system_ext/lib64/liboplusNetd.so）：initNetd() 改立即返回 0。oplusex 的
  com.oplus.NetworkAssistSys 同样被移植包掏空（0 字节占位），netd 崩溃的 onrestart 连坐杀
  zygote（§28.1；A17 里函数在 0x4050、序言无 paciasp，按 .dynsym 定位不受影响）。

补丁 6（vendor 分区，vendor_sepolicy{,_debug}.cil）：logd 写串口 /dev/hvc1 的 allow（A17 平台
  策略同 A14/A16 缺这条；没有它串口 logcat 全丢，panic 现场拿不到）。另：连接内核的 lpm_trie
  UBSAN 补丁在 scripts/kernel_fixes.py（对所有 GSI 生效，若已跑过会自动跳过）。

补丁 7（vendor 分区，build.prop）：注释 dalvik.vm.heapgrowthlimit。A17 预览的 ART 在
  FreeListSpace::ClampGrowthLimit 里 CHECK(new_capacity <= Size())，GSI 的 growthlimit(384m) 与
  vendor 的 heapsize(512m) 组合出让它扩容到 512MB/现有 192MB 的非法状态 → system_server 每 5 秒
  SIGABRT（"Check failed: new_capacity <= Size()"）。AOSP 语义：SystemServer.run 只在
  heapgrowthlimit 非空时才调 clearGrowthLimit，所以把 system/my_product/vendor 三处的该键全部
  注释，让它压根不走这条路（堆用 vendor 的 heapsize=512m）。

实测记录见 docs/RESEARCH.md §29。
"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpine_job import ROOT, run_job                      # noqa: E402
from coloros14_fixes import set_props, patch_zip_dex      # noqa: E402
from coloros_gsi_fixes import Dex                         # noqa: E402
from coloros16_fixes import elf_dynsym_value              # noqa: E402
from hyperos_fixes import patch_props, replace_cmds       # noqa: E402
from vendor_install import _write_file_cmds               # noqa: E402

# 补丁 1/1b：system 分区 build.prop → 注释掉的键 / 追加设置的键（手机固件，无转向属性）。
# dalvik.vm.heapgrowthlimit 要在三处全注释（system、my_product、vendor——见 main 里补丁 7）：
# A17 预览的 ART 在 ClearGrowthLimit→FreeListSpace::ClampGrowthLimit 里 CHECK(new_capacity<=Size())
# 会炸（GSI 的 384m growthlimit 与 vendor 的 512m heapsize 组出 512MB/192MB 的非法扩容，system_server
# 每 5 秒 abort 一次）。AOSP 里 SystemServer.run 只在 heapgrowthlimit 非空时才调 clearGrowthLimit——
# 注释干净后压根不走这条路，堆直接用 dalvik.vm.heapsize。
PROP_EDITS = [
    ("system/build.prop", ("ro.sf.lcd_density", "ro.opengles.version", "dalvik.vm.heapgrowthlimit")),
    ("my_product/build.prop", ("ro.sf.lcd_density", "ro.opengles.version", "dalvik.vm.heapgrowthlimit")),
]
PROP_SET = {
    "system/build.prop": [
        ("persist.sys.usb.config", "mtp,adb",
         "init.usb*.rc 只有 mtp,adb 的触发器，adb,mtp 不会启动 adbd"),
        ("ro.mediaserver.64b.enable", "true",
         "mediaserverwrapper 据此 exec 64 位的 mediaserver64；宿主 CPU 不支持 AArch32"),
    ],
    "my_product/build.prop": [
        # A17 libart 的 ClearGrowthLimit 用写死的 512MB 调 FreeListSpace::ClampGrowthLimit（真机
        # 内存大、堆就是 512m 所以不炸）；空间 Size() 跟 dalvik.vm.heapsize 走，OPLUS 按 RAM 档把
        # 我们的 6GB 配成 192m → 512>192 CHECK abort。在 my_product（晚于 vendor）把它提到 512m。
        # 注意：给 services.jar 等 SYSTEMSERVERCLASSPATH 上的 jar 打 dex 补丁在 A17 走不通——
        # zygote 装配 classpath 时静默剔除校验和变过的 jar（"Missing class SystemServer"），
        # zip/dex 校验全对也没用。
        ("dalvik.vm.heapsize", "512m",
         "A17 libart 的 ClampGrowthLimit 用写死的 512MB 调用，堆空间 Size() 跟本属性走："
         "OPLUS 按 RAM 档把它写成 192m → 512>192 CHECK 炸；提到 512m 让 CHECK 平衡通过"),
    ],
}
# 补丁 4：要补 6.6 内核条目的框架兼容性矩阵 (路径, 内核 FCM 等级)
FCM_KERNEL = [("system/etc/vintf/compatibility_matrix.8.xml", 8)]

# 补丁 3：dex 指令级补丁（索引值取自 OP5E11L1-CinnamonBun-1773378663794；版本不符会拒绝打）
DEX_PATCHES = {
    # 注意：oplus-services.jar 的 revokeShellPermission 补丁在 A17 上也不能打——它同样在
    # SYSTEMSERVERCLASSPATH 上，改过校验和就被 zygote 静默剔除（权限监控的后果见 §27.3-4，
    # 如开机命令失效再另想办法）。system_ext/framework 不在 classpath 上，安全。
    "system/system_ext/framework/oplus-wifi-service.jar": (
        [("Lcom/oplus/server/wifi/power/OplusGtxController;", "validateExecutor", "", "Z", 4,
          # const-string v1, "OplusGtxController"; const-string v0, "cmd executor is null";
          # invoke-static {v1, v0} Log.d —— 之后落到 const/4 v1, #0; return v1
          (0x011A, 0x59B3, 0x001A, 0x7D02, 0x2071, 0x9A08, 0x0001),
          (0x0112, 0x010F, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000))],
        ["system/system_ext/framework/oat/arm64/oplus-wifi-service.odex",
         "system/system_ext/framework/oat/arm64/oplus-wifi-service.vdex"]),
}

VENDOR_GROUPS = [b"vendor_launcher::2950:", b"vendor_saures::2951:"]

# 补丁 5：liboplusNetd 的 initNetd 立即返回 0（A17 序言没有 paciasp：sub sp; stp x29,x30）
NETD_REL = "system/system_ext/lib64/liboplusNetd.so"
NETD_SYM = b"_ZN7android4init21InitExtensionNetdImpl8initNetdEv"   # InitExtensionNetdImpl::initNetd()
NETD_OLD = bytes.fromhex("ff8301d1fd7b02a9")   # sub sp, sp, #0x60; stp x29, x30, [sp, #-0x10]!
NETD_NEW = bytes.fromhex("00008052c0035fd6")   # mov w0, #0; ret

# 补丁 8（system 分区，lib64/libandroid_runtime.so）：让 -Xmx 恒为 1g。
# A17 libart 的 Heap::ClearGrowthLimit 用**写死的 512MB** 调 FreeListSpace::ClampGrowthLimit
# （0x87dcdc: movz w1,#0x20000000；真机内存大、-Xmx 就是 512m 所以不炸）；堆空间 Size() 跟 -Xmx
# 走，而 AndroidRuntime::startVm 的 -Xmx 读 dalvik.vm.heapsize——该值在我们环境被某股力量按
# 192m 写死（全镜像无此赋值、vendor 是 512m、my_product 改了也被覆盖、RAM 8G 也没用，来源未定位）
# → 512>192 CHECK abort。libart 不读该属性（唯一读者是 libandroid_runtime），把它读的属性名换成
# 等长哑名 + 默认值 16m→1g：-Xmx 恒为 1g，幕后写入者管不着。两个串均唯一；native 库不在
# SYSTEMSERVERCLASSPATH 上，无 jar 那种校验和剔除问题（教训见补丁 3 注）。
RUNTIME_REL = "system/lib64/libandroid_runtime.so"
RUNTIME_PATCHES = [
    (b"dalvik.vm.heapsize", b"ranchuwoa.heapsize"),   # 18 字节等长
    (b"16m\x00", b"1g\x00\x00"),                      # 默认 16m → 1g（原地，长度不增）
]

# 补丁 6：logd 写串口的 allow（类型走 GSI 自带 mapping/202404.cil 的版本化名）
SEP_ADDON = ("; @ranchuwoa-addon: coloros17-diag\n"
             "; goldfish-logcat（logd 域）把 Android 日志写到串口 /dev/hvc1：A17 平台策略缺这些\n"
             "(allow logd_202404 serial_device_202404 (chr_file (append getattr ioctl open read write)))\n"
             "(allow logd_202404 device_202404 (dir (open read search)))\n"
             "(allow logd_202404 logdr_socket_202404 (sock_file (write getattr)))\n"
             "(allow logd_202404 logd_202404 (unix_stream_socket (connectto)))\n")

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
losetup -d /dev/loop0
fi
echo COLOROS17_FIXES_OK
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

    stage = ROOT / "images" / ".alpine-job" / "coloros17-payload"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True)
    (stage / "labels").mkdir()

    # 补丁 1/1b：system 与 my_product 的 build.prop
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

    # 补丁 2（vendor 分区）：/etc/group 定义 OPLUS 的 vendor 组
    vcmds = []

    # 补丁 7（vendor 分区，build.prop）：注释 dalvik.vm.heapgrowthlimit（vendor 的 192m 是最后一份
    # 赋值，不注释的话 system_server 还是会走 clearGrowthLimit；heapsize=512m 保留当默认堆）
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                    "/build.prop", "-o", str(stage / "orig-vbp")], check=True, capture_output=True)
    vp_src = stage / "orig-vbp" / "build.prop"
    changed = patch_props(vp_src, stage / "files" / "vbp", ("dalvik.vm.heapgrowthlimit",))
    if changed:
        print("vendor/build.prop：注释 dalvik.vm.heapgrowthlimit")
        vcmds += replace_cmds("build.prop", "/payload/files/vbp", 0o644, "/tmp/label_vbp")
    else:
        print("vendor/build.prop：无需改动")

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

    # 补丁 6（vendor 分区）：logd 写串口的 allow
    for rel in ("etc/selinux/vendor_sepolicy.cil", "etc/selinux/vendor_sepolicy_debug.cil"):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                        "/" + rel, "-o", str(stage / "orig-sep")], check=True, capture_output=True)
        cur = (stage / "orig-sep" / Path(rel).name).read_text(encoding="utf-8", errors="replace")
        if SEP_ADDON in cur:
            print(f"vendor/{rel}：补丁已是最新，跳过")
            continue
        base = cur.split("; @ranchuwoa-addon: coloros17-diag")[0].rstrip("\n")
        (stage / "files" / Path(rel).name).write_text(base + "\n" + SEP_ADDON, encoding="utf-8",
                                                      newline="\n")
        vcmds += replace_cmds(rel, f"/payload/files/{Path(rel).name}", 0o644,
                              f"/tmp/label_{Path(rel).name}")
        print(f"vendor/{rel}：写入 logd 串口 allow")

    # 补丁 4（system 分区）：框架兼容性矩阵补 6.6 内核条目
    for k, (rel, level) in enumerate(FCM_KERNEL):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig-fcm{k}")], check=True, capture_output=True)
        text = (stage / f"orig-fcm{k}" / Path(rel).name).read_bytes().decode("utf-8")
        if 'kernel version="6.6.0"' in text:
            print(f"{rel}：已有 6.6 内核条目，跳过")
            continue
        at = text.index("    <kernel version=")
        entry = (f'    <!-- RanchuWoA: 模拟器内核是 android15-6.6 GKI，A17 的 8 级矩阵没有 6.6 条目 -->\n'
                 f'    <kernel version="6.6.0" level="{level}"/>\n')
        (stage / "files" / f"fcm{k}").write_bytes((text[:at] + entry + text[at:]).encode("utf-8"))
        scmds += replace_cmds(rel, f"/payload/files/fcm{k}", 0o644, f"/tmp/label_fcm{k}")
        print(f"{rel}：插入 6.6 内核条目（level {level}）")

    # 补丁 3（system 分区）：dex 指令级补丁
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

    # 补丁 5（system 分区）：liboplusNetd 的 initNetd 立即返回 0
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

    # 补丁 8（system 分区）：libandroid_runtime 的 -Xmx 恒为 1g（详见 RUNTIME_PATCHES 处说明）
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                    "/" + RUNTIME_REL, "-o", str(stage / "orig-runtime")], check=True, capture_output=True)
    rt = bytearray((stage / "orig-runtime" / Path(RUNTIME_REL).name).read_bytes())
    if all(new in rt for _, new in RUNTIME_PATCHES) and \
       not any(old in rt for old, _ in RUNTIME_PATCHES):
        print(f"{RUNTIME_REL}：-Xmx 补丁已打过，跳过")
    else:
        for old, new in RUNTIME_PATCHES:
            assert len(old) == len(new)
            if rt.count(old) != 1:
                sys.exit(f"{RUNTIME_REL}：字节串 {old!r} 出现 {rt.count(old)} 次（应唯一），GSI 版本不同？")
            rt = rt.replace(old, new)
        (stage / "files" / "libandroid_runtime.so").write_bytes(bytes(rt))
        scmds += replace_cmds(RUNTIME_REL, "/payload/files/libandroid_runtime.so", 0o644,
                              "/tmp/label_runtime")
        print(f"{RUNTIME_REL}：属性名换哑名 + 默认 16m→1g（-Xmx 恒为 1g）")

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
                 log=ROOT / "images" / ".alpine-job" / "coloros17_fixes.log", echo=False)
    print("完成" if rc == 0 else f"失败（rc={rc}），日志 images/.alpine-job/coloros17_fixes.log")
    sys.exit(rc)


if __name__ == "__main__":
    main()
