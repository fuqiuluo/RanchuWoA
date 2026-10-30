"""hyperos_fixes.py — M7 实验：给 HyperOS（Xiaomi）GSI 的实验盘打补丁，让它能在 ranchu 虚拟机上跑起来。

用法: py scripts/hyperos_fixes.py <GSI 实验盘>        （scripts/gsi_disk.py 的产物，盘不能正被 VM 使用）

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
# ---- system 分区：写回打了补丁的 vold ----
losetup -o {soff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck1.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/system.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
debugfs -R 'stat system/bin/vold' /dev/loop0 2>/dev/null | grep -E 'Size:'
losetup -d /dev/loop0
echo HYPEROS_FIXES_OK
"""


def patch_vold(src: Path, dst: Path):
    data = bytearray(src.read_bytes())
    if data[NEEDLE_OFF:NEEDLE_OFF + 3] != b"dm\0":
        sys.exit(f"vold {NEEDLE_OFF:#x} 处不是 'dm' 字面量（固件版本变了，需重新定位）")
    if data[XREF_OFF:XREF_OFF + 8] != XREF_BYTES:
        sys.exit(f"vold {XREF_OFF:#x} 处的 adrp/add 引用指令与预期不符（固件版本变了，需重新定位）")
    data[NEEDLE_OFF:NEEDLE_OFF + 2] = b"vd"
    dst.write_bytes(data)


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

    # system：vold
    print("取出 vold ...")
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk), soff,
                    "/" + VOLD, "-o", str(stage / "orig")], check=True, capture_output=True)
    patch_vold(stage / "orig" / "vold", stage / "files" / "vold")
    shutil.rmtree(stage / "orig")
    (stage / "labels" / "vold_exec").write_bytes(b"u:object_r:vold_exec:s0\x00")
    scmds = [f"rm {VOLD}", f"write /payload/files/vold {VOLD}", f"sif {VOLD} mode 0100755",
             f"sif {VOLD} uid 0", f"sif {VOLD} gid 0",
             f"ea_set -f /payload/labels/vold_exec {VOLD} security.selinux"]
    (stage / "system.cmds").write_text("".join(c + "\n" for c in scmds), encoding="utf-8")

    print("写回 vendor 与 system 分区（Alpine 辅助 VM）...")
    job = JOB.replace("{voff}", str(voff)).replace("{soff}", str(soff))
    rc = run_job(job, disks=[disk], payload_dir=stage,
                 log=ROOT / "images" / ".alpine-job" / "hyperos_fixes.log", echo=False)
    print("完成" if rc == 0 else f"失败（rc={rc}），日志 images/.alpine-job/hyperos_fixes.log")
    sys.exit(rc)


if __name__ == "__main__":
    main()
