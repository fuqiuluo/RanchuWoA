"""vendor_install.py — 把 RanchuWoA 的 vendor 组件写进 system.img 里 super 的 vendor 逻辑分区。

用法:
  py scripts/vendor_install.py [--image system.img] [--prepare [字节数]] [--dry-run]
  py scripts/vendor_install.py --image <GSI 实验盘> --gsi      只打 GSI 兼容补丁（见 GSI_SEPOLICY_ADDONS）

--prepare（一次性）：在 liblp 里把 vendor 扩到指定大小（默认 400 MiB）并去掉 READONLY 属性，
    然后在 Alpine 辅助 VM 里 resize2fs + e2fsck -E unshare_blocks，把去重过的只读 ext4
    变成普通可写 ext4（之后 guest 里可以 mount -o remount,rw /vendor 直接改）。
每次运行都会：先 e2fsck -fy 修复、确保有日志，按 INSTALL 表写入文件（mode/uid/gid/SELinux 标签），
    按 REMOVE 表删除文件，最后 e2fsck -fn 校验。镜像正被运行中的 VM 使用时不要跑。
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpine_job import ROOT, run_job  # noqa: E402

IMAGE = ROOT / "images" / "android-35" / "arm64-v8a" / "system.img"
GFX = ROOT / "gfx"

# (vendor 内路径, 源文件, mode, uid, gid, SELinux 类型)
INSTALL = [
    ("bin/hw/android.hardware.graphics.allocator-service.woa",
     GFX / "gralloc/out/android.hardware.graphics.allocator-service.woa", 0o755, 0, 2000,
     "hal_graphics_allocator_default_exec"),
    ("lib64/hw/mapper.woa.so", GFX / "gralloc/out/mapper.woa.so", 0o644, 0, 0, "same_process_hal_file"),
    ("lib64/egl/libEGL_swiftshader.so", GFX / "egl-out/libEGL_swiftshader.so", 0o644, 0, 0,
     "same_process_hal_file"),
    ("lib64/egl/libGLESv2_swiftshader.so", GFX / "egl-out/libGLESv2_swiftshader.so", 0o644, 0, 0,
     "same_process_hal_file"),
    ("lib64/egl/libGLESv1_CM_swiftshader.so", GFX / "egl-out/libGLESv1_CM_swiftshader.so", 0o644, 0, 0,
     "same_process_hal_file"),
    # gfxstream（宿主 GPU 加速；virtio-gpu-pipe 传输，androidboot.hardwareegl=emulation 启用）。
    # 用 _emulation 命名覆盖镜像里原有的 goldfish-pipe 版（eglDisplay.cpp 按该名加载）；
    # 公共层是共享库（HostConnection/ThreadInfo 的 TLS 三个 HAL 必须共用一份）。
    ("lib64/egl/libEGL_emulation.so", GFX / "gs-out/libEGL_emulation.so", 0o644, 0, 0,
     "same_process_hal_file"),
    ("lib64/egl/libGLESv2_emulation.so", GFX / "gs-out/libGLESv2_emulation.so", 0o644, 0, 0,
     "same_process_hal_file"),
    ("lib64/egl/libGLESv1_CM_emulation.so", GFX / "gs-out/libGLESv1_CM_emulation.so", 0o644, 0, 0,
     "same_process_hal_file"),
    ("lib64/libOpenglSystemCommon.so", GFX / "gs-out/libOpenglSystemCommon.so", 0o644, 0, 0,
     "same_process_hal_file"),
    ("etc/init/android.hardware.graphics.allocator-service.woa.rc",
     GFX / "vendor-overlay/etc/init/android.hardware.graphics.allocator-service.woa.rc", 0o644, 0, 0,
     "vendor_configs_file"),
    ("etc/vintf/manifest/android.hardware.graphics.allocator-service.woa.xml",
     GFX / "vendor-overlay/etc/vintf/manifest/android.hardware.graphics.allocator-service.woa.xml",
     0o644, 0, 0, "vendor_configs_file"),
    ("etc/init/init.ranchuwoa.rc", GFX / "vendor-overlay/etc/init/init.ranchuwoa.rc", 0o644, 0, 0,
     "vendor_configs_file"),
    ("etc/fstab.ranchu", GFX / "vendor-overlay/etc/fstab.ranchu", 0o644, 0, 0, "vendor_configs_file"),
    ("etc/permissions/ranchuwoa-features.xml",
     GFX / "vendor-overlay/etc/permissions/ranchuwoa-features.xml", 0o644, 0, 0,
     "vendor_configs_file"),
    # 原文件 + "mapper/woa → hal_graphics_mapper_service"：libui 用 openDeclaredPassthroughHal 找 IMapper v5
    # 时 servicemanager 要做 find 权限检查，没有这行就落到 default_android_service，enforcing 下被拒
    ("etc/selinux/vendor_service_contexts", GFX / "vendor-overlay/etc/selinux/vendor_service_contexts",
     0o644, 0, 0, "vendor_service_contexts_file"),
]

# vendor 策略补充：追加到 /vendor/etc/selinux/vendor_sepolicy.cil 末尾（文件首行 "; @ranchuwoa-addon: <名>"
# 是标记，重复执行不会重复追加），并删掉 precompiled_sepolicy 让 init 开机现编（约 0.5 s）。
SEPOLICY_ADDONS = [GFX / "vendor-overlay/etc/selinux/ranchuwoa_sepolicy.cil"]
# --gsi：只给 GSI 实验盘（scripts/gsi_disk.py 的产物）打的兼容补丁（RESEARCH §21）：
#   1. 再追加 gsi_sepolicy.cil（GSI 上下文文件引用、只在其原厂 vendor 策略里定义的类型）；
#   2. 生成 vendor_sepolicy_debug.cil、plat_pub_versioned_debug.cil：OPLUS（ColorOS）的 init 在
#      secValue=0 时编译 *_debug 版策略，真机 vendor 自带，没有就 "Unable to open SELinux policy" 重启。
GSI_SEPOLICY_ADDONS = [GFX / "vendor-overlay/gsi/gsi_sepolicy.cil"]


def _sepolicy_patch(stage, addons, debug_copies):
    """返回 (debugfs 之前执行的 shell 片段, debugfs 命令, 校验命令)。"""
    sh = ["debugfs -R 'dump etc/selinux/vendor_sepolicy.cil /tmp/vsp.cil' /dev/loop0 2>/dev/null || true",
          "test -s /tmp/vsp.cil"]       # test 单独一行：sh -e 下 && / || 列表里的失败不会终止作业
    for i, path in enumerate(addons):
        marker = Path(path).read_text(encoding="utf-8").splitlines()[0]
        if not marker.startswith("; @ranchuwoa-addon:"):
            raise ValueError(f"{path} 首行应是 '; @ranchuwoa-addon: <名>' 标记")
        shutil.copyfile(path, stage / f"addon{i}.cil")
        sh.append(f"grep -qF '{marker}' /tmp/vsp.cil || cat /payload/addon{i}.cil >> /tmp/vsp.cil")
    files = [("etc/selinux/vendor_sepolicy.cil", "/tmp/vsp.cil")]
    if debug_copies:
        sh += ["debugfs -R 'dump etc/selinux/plat_pub_versioned.cil /tmp/ppv.cil' /dev/loop0 2>/dev/null || true",
               "test -s /tmp/ppv.cil"]
        files += [("etc/selinux/vendor_sepolicy_debug.cil", "/tmp/vsp.cil"),
                  ("etc/selinux/plat_pub_versioned_debug.cil", "/tmp/ppv.cil")]
    cmds, verify = [], []
    for rel, src in files:
        cmds += _write_file_cmds(stage, rel, src, 0o644, 0, 0, "vendor_configs_file")
        verify.append(f"debugfs -R 'stat {rel}' /dev/loop0 2>/dev/null | grep -E 'Size:' | head -1")
    cmds.append("rm etc/selinux/precompiled_sepolicy")
    return "".join(s + "\n" for s in sh), cmds, verify

# 旧 nullgfx 残留，以及依赖宿主渲染器的 ranchu gralloc（其 mapper@3.0 会把进程挂死在 HostConnection）
REMOVE = [
    "etc/init/nullgfx.rc",
    "etc/vintf/manifest/nullgfx.xml",
    "lib64/hw/mapper.nullgfx.so",
    "etc/init/android.hardware.graphics.allocator@3.0-service.ranchu.rc",
    "etc/vintf/manifest/android.hardware.graphics.gralloc3.ranchu.xml",
    # 没有蓝牙控制器：特性在 handheld_core_hardware.xml 里也有声明，由 ranchuwoa-features.xml 统一去掉
    "etc/permissions/android.hardware.bluetooth.xml",
    "etc/permissions/android.hardware.bluetooth_le.xml",
    "etc/permissions/ranchuwoa-unavailable-features.xml",   # 旧文件名
    # 模拟器的连接资源 overlay 把 eth0 配成受限网络（官方经 virtio-wifi 上网，eth0 只做回程），
    # 我们直接用 eth0 上网，去掉它 EthernetTracker 才会给 eth0 默认（非受限）能力
    "overlay/goldfish_overlay_connectivity_google.apk",
]

JOB = r"""
SYS="$DISK0"
[ -n "$SYS" ] || { echo "找不到 system 盘"; exit 1; }
losetup -o {voff} /dev/loop0 "$SYS"
show() { dumpe2fs -h /dev/loop0 2>/dev/null | grep -E '^(Filesystem features|Block count|Free blocks|Free inodes)'; }
show
# guest 里 remount rw 改过 vendor 又被强行关机时会留下不一致（孤儿 inode/位图），先修复
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck0.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ] || { tail -20 /tmp/fsck0.log; exit 2; }
if [ "{prepare}" = 1 ]; then
  resize2fs /dev/loop0 {blocks}
  rc=0; e2fsck -fy -E unshare_blocks /dev/loop0 || rc=$?; echo "unshare_rc=$rc"; [ $rc -le 1 ]
  show
fi
# 加一个小日志：guest 里 rw 改动后若非正常关机，下次挂载会回放日志而不是留下不一致
if ! dumpe2fs -h /dev/loop0 2>/dev/null | grep -q has_journal; then
  tune2fs -O has_journal -J size=4 /dev/loop0 && echo "journal_added"
fi
{dumps}debugfs -w -f /payload/debugfs.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
show
{verify}
losetup -d /dev/loop0
echo VENDOR_INSTALL_OK
"""


def _write_file_cmds(stage, rel, guest_src, mode, uid, gid, label):
    """debugfs 命令：用 guest 里的 guest_src 覆盖 vendor 内 rel，并设 mode/uid/gid/SELinux 标签。"""
    # SELinux 标签与镜像里原有文件一致：带结尾 NUL
    (stage / "labels" / label).write_bytes(f"u:object_r:{label}:s0".encode() + b"\x00")
    return [f"rm {rel}", f"write {guest_src} {rel}",
            f"sif {rel} mode 0{0o100000 | mode:o}", f"sif {rel} uid {uid}", f"sif {rel} gid {gid}",
            f"ea_set -f /payload/labels/{label} {rel} security.selinux"]


def install(image, prepare=False, size=400 << 20, dry_run=False, log=None, echo=True, gsi=False):
    """把 INSTALL/REMOVE（gsi=True 时只打 GSI 兼容补丁）应用到 image 里的 vendor 分区；返回作业退出码。"""
    image = Path(image)
    out = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(image), "vendor"],
                         capture_output=True, text=True, check=True).stdout.split()
    voff = int(out[0])

    stage = ROOT / "images" / ".alpine-job" / "vendor-payload"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True)
    (stage / "labels").mkdir()
    cmds, verify = [], []
    if not gsi:
        for rel in REMOVE:
            cmds.append(f"rm {rel}")
        for i, (rel, src, mode, uid, gid, label) in enumerate(INSTALL):
            if not Path(src).is_file():
                raise FileNotFoundError(f"缺少源文件 {src}（先构建 gfx/gralloc 与 SwiftShader）")
            name = f"f{i}"
            shutil.copyfile(src, stage / "files" / name)
            cmds += _write_file_cmds(stage, rel, f"/payload/files/{name}", mode, uid, gid, label)
            verify.append(f"debugfs -R 'stat {rel}' /dev/loop0 2>/dev/null | grep -E 'Mode:|Size:' | head -2")
    addons = SEPOLICY_ADDONS + (GSI_SEPOLICY_ADDONS if gsi else [])
    dumps, sp_cmds, sp_verify = _sepolicy_patch(stage, addons, debug_copies=gsi)
    cmds += sp_cmds
    verify += sp_verify
    (stage / "debugfs.cmds").write_text("".join(c + "\n" for c in cmds), encoding="utf-8")

    if dry_run:
        print((stage / "debugfs.cmds").read_text(encoding="utf-8"))
        return 0
    if prepare:
        subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(image), "--grow", "vendor",
                        str(size), "--rw"], check=True)
    job = (JOB.replace("{voff}", str(voff)).replace("{prepare}", "1" if prepare else "0")
           .replace("{blocks}", str(size // 4096)).replace("{dumps}", dumps)
           .replace("{verify}", "".join(v + "\n" for v in verify)))
    return run_job(job, disks=[image], payload_dir=stage,
                   log=log or ROOT / "images" / ".alpine-job" / "vendor_install.log", echo=echo)


def main():
    args = sys.argv[1:]
    image = IMAGE
    if "--image" in args:
        image = Path(args[args.index("--image") + 1])
    prepare = "--prepare" in args
    size = 400 << 20
    if prepare:
        i = args.index("--prepare")
        if i + 1 < len(args) and args[i + 1].isdigit():
            size = int(args[i + 1])
    rc = install(image, prepare=prepare, size=size, dry_run="--dry-run" in args, gsi="--gsi" in args)
    print("job rc =", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
