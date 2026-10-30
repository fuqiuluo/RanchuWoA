"""hyperos_fixes.py — M7 实验：给 HyperOS（Xiaomi）GSI 的实验盘打补丁，让它能在 ranchu 虚拟机上跑起来。

用法: py scripts/hyperos_fixes.py <GSI 实验盘>        （scripts/gsi_disk.py 的产物，盘不能正被 VM 使用）
可重复运行：已经打过的补丁会跳过。

补丁 1（vendor 分区，ranchuwoa-features.xml）：保留蓝牙特性。
  小米 HyperOS 的 system_server（WifiHandlerThread）里 MiuiNetworkMonitor 的构造函数假定
  BluetoothAdapter.getDefaultAdapter() 非空。通用 vendor 的 ranchuwoa-features.xml 用
  <unavailable-feature> 去掉蓝牙特性后 getDefaultAdapter() 返回 null，
  MiuiNetworkMonitor.<init> 调 getProfileProxy 时 NullPointerException —— FATAL EXCEPTION IN
  SYSTEM PROCESS，system_server 每 ~17 秒崩一次，永远到不了开机完成。
  蓝牙默认关闭时蓝牙栈并不会启动（ranchu 的蓝牙 HAL 依赖 goldfish 通道，本来就没有），
  保留特性只是让 framework 里不再拿到空 Adapter。

补丁 2（system 分区，vold）：f2fs 空闲维护对非 device-mapper 的 /data 空指针。
  开机完成后框架触发 vold 空闲维护（RunIdleMaint），小米扩展从 /proc/mounts 里找 /data 的
  挂载设备名（真机是 /dev/block/dm-N），strstr(buf, "dm") 后直接把返回值赋给 std::string——
  我们的 /data 是裸 virtio 盘 /dev/block/vdc，不含 "dm"，strstr 返回 NULL →
  std::string(nullptr) → SIGSEGV。vold 是 reboot_on_failure 服务 → reboot(vold-failed)，
  无限重启。把 strstr 的针 "dm" 改成 "vd"（同长度；"/dev/block/vdc" 匹配到 "vdc"），
  后续拼出的 /sys/fs/f2fs/vdc/... 不存在、打开失败即跳过（我们的 /data 是 ext4，本来就没有
  f2fs sysfs 节点）。"dm" 字面量在 vold 里只有这一处引用（adrp/add 指令对校验）。

补丁 3（system 分区，build.prop）：去掉源机型（小米 15）的显示/图形硬件属性，改由模拟器决定。
  init 先加载 /system、/product 的 build.prop，后执行 vendor 的 init 脚本，ro.* 属性只能设一次：
  - /product/etc/build.prop 的 ro.sf.lcd_density=520 让 vendor 按 androidboot.lcd_density 设的
    密度失效，600x1335 的屏按 520 dpi 排版，界面大一倍；persist.miui.density_v2=520 是 MIUI 自己的
    “设备密度”（MiuiDisplayMetrics，缺省时回落到 ro.sf.lcd_density），一并去掉。
  - /system/build.prop 的 ro.opengles.version=196610（GLES 3.2）盖掉 vendor 按
    androidboot.opengles.version 设的 3.0，应用以为有 3.2；宿主 gfxstream / SwiftShader 只有 3.0。
  被去掉的行改成注释（带 RanchuWoA 标记），原值留档。
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpine_job import ROOT, run_job                      # noqa: E402
from vendor_install import _write_file_cmds               # noqa: E402

FEATURES = ROOT / "gfx" / "vendor-overlay" / "gsi" / "hyperos-features.xml"
VOLD = "system/bin/vold"
NEEDLE_OFF = 0x36227            # "dm\0" 字面量（RunIdleMaint 的 strstr 针）
XREF_OFF = 0xA0480              # 引用该字面量的 adrp x1/add x1 指令对（版本变了要重新定位）
XREF_BYTES = bytes.fromhex("a1fcffd0") + bytes.fromhex("219c0891")
# 补丁 3：system 分区里的 build.prop（ext4 内路径）→ 要注释掉的属性
PROP_EDITS = [
    ("system/build.prop", ("ro.opengles.version",)),
    ("product/etc/build.prop", ("ro.sf.lcd_density", "persist.miui.density_v2")),
]
PROP_MARK = "# RanchuWoA: 由模拟器 vendor/内核命令行决定，原值: "

JOB = r"""
D="$DISK0"
[ -n "$D" ] || { echo "找不到实验盘"; exit 1; }
set -e
# ---- vendor 分区：替换 ranchuwoa-features.xml ----
losetup -o {voff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck0.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/vendor.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
echo "--- features 里的 bluetooth 行数（应为 0）："
debugfs -R 'cat etc/permissions/ranchuwoa-features.xml' /dev/loop0 2>/dev/null | grep -c bluetooth || true
losetup -d /dev/loop0
# ---- system 分区：vold、build.prop ----
if [ -s /payload/system.cmds ]; then
losetup -o {soff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck1.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/system.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
debugfs -R 'stat system/bin/vold' /dev/loop0 2>/dev/null | grep -E 'Size:'
echo "--- build.prop 里被注释掉的属性："
for p in {props}; do debugfs -R "cat $p" /dev/loop0 2>/dev/null | grep 'RanchuWoA' || true; done
for p in {props}; do debugfs -R "ea_get $p security.selinux" /dev/loop0 2>/dev/null; done
losetup -d /dev/loop0
fi
echo HYPEROS_FIXES_OK
"""


def patch_vold(src: Path, dst: Path) -> bool:
    """返回 False 表示已经打过（跳过）。"""
    data = bytearray(src.read_bytes())
    if data[XREF_OFF:XREF_OFF + 8] != XREF_BYTES:
        sys.exit(f"vold {XREF_OFF:#x} 处的 adrp/add 引用指令与预期不符（固件版本变了，需重新定位）")
    needle = bytes(data[NEEDLE_OFF:NEEDLE_OFF + 3])
    if needle == b"vd\0":
        return False
    if needle != b"dm\0":
        sys.exit(f"vold {NEEDLE_OFF:#x} 处不是 'dm' 字面量（固件版本变了，需重新定位）")
    data[NEEDLE_OFF:NEEDLE_OFF + 2] = b"vd"
    dst.write_bytes(data)
    return True


def patch_props(src: Path, dst: Path, keys) -> list[str]:
    """把 keys 的赋值行改成注释；返回改掉的行（为空表示无需改动）。"""
    lines = src.read_bytes().decode("utf-8").split("\n")
    changed = []
    for i, ln in enumerate(lines):
        key = ln.split("=", 1)[0].strip()
        if "=" in ln and not ln.lstrip().startswith("#") and key in keys:
            lines[i] = PROP_MARK + ln
            changed.append(ln)
    if changed:
        dst.write_bytes("\n".join(lines).encode("utf-8"))
    return changed


def replace_cmds(rel, guest_src, mode, label_file):
    """debugfs 命令：保留原文件的 SELinux 标签（先 ea_get 存下），用 guest_src 覆盖 rel。"""
    return [f"ea_get -f {label_file} {rel} security.selinux", f"rm {rel}", f"write {guest_src} {rel}",
            f"sif {rel} mode 0{0o100000 | mode:o}", f"sif {rel} uid 0", f"sif {rel} gid 0",
            f"ea_set -f {label_file} {rel} security.selinux"]


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    disk = Path(sys.argv[1])
    if not FEATURES.is_file():
        sys.exit(f"缺少 {FEATURES}")
    voff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk), "vendor"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    soff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk), "system"],
                          capture_output=True, text=True, check=True).stdout.split()[0]

    stage = ROOT / "images" / ".alpine-job" / "hyperos-payload"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True)
    (stage / "labels").mkdir()

    # vendor：features.xml
    shutil.copyfile(FEATURES, stage / "files" / "features.xml")
    vcmds = _write_file_cmds(stage, "etc/permissions/ranchuwoa-features.xml",
                             "/payload/files/features.xml", 0o644, 0, 0, "vendor_configs_file")
    (stage / "vendor.cmds").write_text("".join(c + "\n" for c in vcmds), encoding="utf-8")

    # system：vold 与 build.prop（ext4tool 按文件名平铺输出，两个 build.prop 各取到单独目录）
    print("取出 vold 与 build.prop ...")
    wanted = [(VOLD, "orig")] + [(rel, f"orig/prop{i}") for i, (rel, _) in enumerate(PROP_EDITS)]
    for rel, sub in wanted:
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk), soff,
                        "/" + rel, "-o", str(stage / sub)], check=True, capture_output=True)
    scmds = []
    if patch_vold(stage / "orig" / "vold", stage / "files" / "vold"):
        (stage / "labels" / "vold_exec").write_bytes(b"u:object_r:vold_exec:s0\x00")
        scmds += [f"rm {VOLD}", f"write /payload/files/vold {VOLD}", f"sif {VOLD} mode 0100755",
                  f"sif {VOLD} uid 0", f"sif {VOLD} gid 0",
                  f"ea_set -f /payload/labels/vold_exec {VOLD} security.selinux"]
        print("vold：打 'dm' → 'vd' 补丁")
    else:
        print("vold：已打过补丁，跳过")
    for i, (rel, keys) in enumerate(PROP_EDITS):
        name = f"prop{i}"
        changed = patch_props(stage / "orig" / name / Path(rel).name, stage / "files" / name, keys)
        if changed:
            scmds += replace_cmds(rel, f"/payload/files/{name}", 0o644, f"/tmp/label_{name}")
            print(f"{rel}：注释掉 {', '.join(changed)}")
        else:
            print(f"{rel}：无需改动")
    shutil.rmtree(stage / "orig")
    (stage / "system.cmds").write_text("".join(c + "\n" for c in scmds), encoding="utf-8")

    print("写回 vendor 与 system 分区（Alpine 辅助 VM）...")
    job = (JOB.replace("{voff}", str(voff)).replace("{soff}", str(soff))
           .replace("{props}", " ".join(rel for rel, _ in PROP_EDITS)))
    rc = run_job(job, disks=[disk], payload_dir=stage,
                 log=ROOT / "images" / ".alpine-job" / "hyperos_fixes.log", echo=False)
    print("完成" if rc == 0 else f"失败（rc={rc}），日志 images/.alpine-job/hyperos_fixes.log")
    sys.exit(rc)


if __name__ == "__main__":
    main()
