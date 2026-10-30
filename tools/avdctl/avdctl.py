#!/usr/bin/env python3
"""avdctl — RanchuWoA：Windows on ARM 原生 Android 模拟器（QEMU + WHPX）的 AVD 管理工具。

用法:
  py tools/avdctl/avdctl.py prepare [--force]        从官方系统镜像 zip 生成 RanchuWoA 基础镜像（一次性）
  py tools/avdctl/avdctl.py create <名字> [选项]      创建 AVD 实例（系统盘 qcow2 写时复制 + 全新 userdata），
       --width 720 --height 1280 --density 320        并在后台无窗口跑完首次开机（--no-firstboot 跳过）
       --ram 4096 --cores 4 --data-size 8G
       --audio dsound|sdl|none（默认 dsound） --mic（加录音流，会用宿主麦克风）
       --system <盘>（自定义系统盘，如 scripts/gsi_disk.py 生成的 GSI 实验盘）
  py tools/avdctl/avdctl.py list                       列出实例及运行状态
  py tools/avdctl/avdctl.py edit <名字> [选项]        改分辨率/密度/内存/核数/声音（--width --height --density
       --ram --cores --audio --mic/--no-mic），下次启动生效
  py tools/avdctl/avdctl.py start <名字> [--headless] [--wait] [--foreground] [--no-toolbar]
       [--verbose-boot]（内核/init 日志进 logs/console.log） [--selinux permissive|enforcing]（默认 enforcing）
  py tools/avdctl/avdctl.py toolbar <名字>             重新打开侧边工具栏（start 时会自动打开）
  py tools/avdctl/avdctl.py stop <名字> [--force]     正常关机（同关窗口：system_powerdown → svc power shutdown），
                                                       --force 直接退出 QEMU
  py tools/avdctl/avdctl.py wipe <名字> [--no-firstboot]   清空 userdata（恢复出厂）
  py tools/avdctl/avdctl.py delete <名字>             删除实例
  py tools/avdctl/avdctl.py geo <名字> <纬度> <经度> [海拔]   设置 GPS 位置（运行中立即生效，并记入配置）
  py tools/avdctl/avdctl.py dist                       把补丁版 QEMU 及其 DLL 打包到 dist/qemu（脱离 MSYS2 运行）

窗口：SDL + 右侧工具栏（toolbar.py：电源/音量/截图/返回/主屏幕/最近任务/位置/窗口大小…）；
鼠标左键=触摸，右键/侧键后退=返回，中键=主屏幕，侧键前进=最近任务；Ctrl+Alt+P=电源键；键盘直通；
剪贴板与 Windows 双向同步；声音经 virtio-snd 输出到 Windows；GPS 默认位于 Google 总部（avdctl geo 或工具栏修改）。
adb：启动后自动出现在 `adb devices`（emulator-5554、5556…），也可 adb connect 127.0.0.1:<adb 端口>。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # .../Emulator
DOWNLOADS = ROOT / "downloads"
IMAGES = ROOT / "images"
BASE = IMAGES / "base" / "android-35"              # 处理过的共享基础镜像（只读使用）
AVDS = IMAGES / "avds"
SCRIPTS = ROOT / "scripts"

SYSIMG_URL = ("https://dl.google.com/android/repository/sys-img/google_apis/"
              "arm64-v8a-35_r09.zip")
SYSIMG_SIZE = 1778933980
SYSIMG_ZIP = DOWNLOADS / "android-35-arm64.zip"
ZIP_MEMBERS = ["kernel-ranchu", "ramdisk.img", "system.img", "encryptionkey.img", "build.prop",
               "source.properties", "VerifiedBootParams.textproto", "data/local.prop"]

ALPINE_REL = "https://dl-cdn.alpinelinux.org/alpine/v3.23"
ALPINE_FILES = {
    DOWNLOADS / "alpine" / "vmlinuz-virt": f"{ALPINE_REL}/releases/aarch64/netboot/vmlinuz-virt",
    DOWNLOADS / "alpine" / "initramfs-virt": f"{ALPINE_REL}/releases/aarch64/netboot/initramfs-virt",
}
ALPINE_APKS = ["e2fsprogs-1.47.3-r0", "e2fsprogs-extra-1.47.3-r0", "e2fsprogs-libs-1.47.3-r0",
               "libcom_err-1.47.3-r0", "libblkid-2.41.6-r1", "libuuid-2.41.6-r1"]

MSYS_BIN = Path("C:/msys64/clangarm64/bin")
DIST = ROOT / "dist" / "qemu"
DEV_QEMU = ROOT / "qemu-ranchu" / "build" / "qemu-system-aarch64.exe"

# gpu：新实例默认宿主 GPU（gfxstream）；没有这一项的旧实例按 swiftshader 运行（build_cmdline）
DEFAULTS = {"width": 720, "height": 1280, "density": 320, "ram": 4096, "cores": 4,
            "data_size": "8G", "gpu": "gfxstream"}

# 每次开机完成后由 QEMU（ranchu-vsock 的 boot-cmd）经 adbd 执行。正常是开机向导走完时写这两个标志
# （官方镜像由 googlesdksetup 代劳）；没有开机向导的 GSI（如 ColorOS 移植包）永远是 0，而这时 Android
# 不响应主屏幕键和最近任务（"Not going home because user setup is in progress"）。命令要幂等、不含逗号。
BOOT_CMD = "settings put global device_provisioned 1; settings put secure user_setup_complete 1"


def log(msg: str) -> None:
    print(f"[avdctl] {msg}", flush=True)


# ---------------------------------------------------------------- 工具

def qemu_exe() -> tuple[Path, dict]:
    """返回 (qemu 路径, 运行环境)。

    dist/qemu 自带 DLL；开发构建要借 MSYS2 的 DLL。两者都在时用较新的那个（免得重新编译后还在
    跑旧的 dist），环境变量 RANCHU_QEMU=dist|dev 可强制指定。
    """
    env = dict(os.environ)
    # gfxstream 宿主渲染器：rutabaga_gfx_ffi.dll → libgfxstream_backend-0.dll → ANGLE(D3D)。
    # dist 模式下这些 DLL 都拷在 exe 旁边，开发模式还依赖 hostgfx/bin 与 clangarm64（PATH）。
    env["PATH"] = str(ROOT / "hostgfx" / "bin") + os.pathsep + env.get("PATH", "")
    env.setdefault("ANDROID_EGL_ON_EGL", "1")  # gfxstream GLES 走宿主 EGL（ANGLE→D3D）
    # 目前只用 GLES：禁掉 gfxstream 的宿主 Vulkan 枚举（Windows ARM 的 vulkan-1.dll/ICD
    # 组合在 QEMU 进程内初始化即崩，指向不存在的 loader 路径可让其优雅降级）
    env.setdefault("ANDROID_EMU_VK_LOADER_PATH", "C:\\ranchuwoa-no-vulkan.dll")
    # 调试用：宿主 gfxstream 日志级别（error|warning|info|debug|verbose）
    if "GFXSTREAM_LOG_LEVEL" in os.environ:
        env["GFXSTREAM_LOG_LEVEL"] = os.environ["GFXSTREAM_LOG_LEVEL"]
    dist = DIST / "qemu-system-aarch64.exe"
    want = os.environ.get("RANCHU_QEMU", "")
    use_dist = dist.exists() and (want == "dist" or (
        want != "dev" and (not DEV_QEMU.exists() or dist.stat().st_mtime >= DEV_QEMU.stat().st_mtime)))
    if use_dist:
        return dist, env
    if not DEV_QEMU.exists():
        sys.exit("找不到补丁版 QEMU：先构建 qemu-ranchu（见 README）或使用打包好的 dist/qemu")
    env["PATH"] = str(MSYS_BIN) + os.pathsep + env.get("PATH", "")
    # 可选：hostgfx/angle/（或环境变量 RANCHU_ANGLE 指向的目录）放自选的 ANGLE（如 Electron 的 ARM64 版），
    # 排在 clangarm64 前面优先加载；默认用 MSYS2 的 angleproject
    angle = Path(os.environ.get("RANCHU_ANGLE", ROOT / "hostgfx" / "angle"))
    if (angle / "libGLESv2.dll").exists():
        env["PATH"] = str(angle) + os.pathsep + env["PATH"]
    return DEV_QEMU, env


def qemu_img() -> Path:
    for p in (DIST / "qemu-img.exe", MSYS_BIN / "qemu-img.exe"):
        if p.exists():
            return p
    sys.exit("找不到 qemu-img.exe（MSYS2 的 mingw-w64-clang-aarch64-qemu 包里有）")


def download(dst: Path, url: str, size: int | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and (size is None or dst.stat().st_size == size):
        return
    log(f"下载 {url}")
    tmp = dst.with_name(dst.name + ".part")
    t0 = last = time.time()
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 22):
            f.write(chunk)
            done += len(chunk)
            if total > (64 << 20) and time.time() - last > 10:
                last = time.time()
                log(f"  {done / 1e9:.2f}/{total / 1e9:.2f} GB  {done / 1e6 / (last - t0):.0f} MB/s")
    if size is not None and tmp.stat().st_size != size:
        tmp.unlink()
        sys.exit(f"下载大小不符：{url}")
    tmp.replace(dst)


def host_timezone() -> str:
    """Windows 时区 ID → IANA（用系统自带 ICU 的 ucal_getTimeZoneIDForWindowsID）。"""
    try:
        # 按字节取：子进程输出用的是控制台代码页，不能随 Python 的 UTF-8 模式解码（时区 ID 本身是 ASCII）
        win_id = subprocess.run(["tzutil", "/g"], capture_output=True,
                                check=True).stdout.decode("ascii", "replace").strip()
        icu = ctypes.WinDLL("icu.dll")
        fn = icu.ucal_getTimeZoneIDForWindowsID
        fn.restype = ctypes.c_int32
        buf = ctypes.create_unicode_buffer(128)
        status = ctypes.c_int(0)
        n = fn(ctypes.c_wchar_p(win_id), -1, None, buf, 128, ctypes.byref(status))
        if status.value <= 0 and n > 0:
            return buf.value[:n]
    except Exception:
        pass
    return "UTC"


def pid_alive(pid: int) -> bool:
    """pid 是否还是一个在运行的 QEMU 进程。

    直接用 Win32 API：tasklist 的输出随系统语言和代码页变（找不到进程时是本地化的提示），
    在 UTF-8 模式下按 UTF-8 解码会失败；另外 run.json 残留时 pid 可能已被别的程序复用，所以还要核对映像名。
    """
    if pid <= 0:
        return False
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return False
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return True                                 # 映像名查不到时按存活处理
        return buf.value.lower().endswith("qemu-system-aarch64.exe")
    finally:
        k32.CloseHandle(h)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def adb_exe() -> str | None:
    return shutil.which("adb")


# ---------------------------------------------------------------- AVD 元数据

def avd_dir(name: str) -> Path:
    d = AVDS / name
    if not (d / "config.json").exists():
        sys.exit(f"AVD 不存在：{name}（avdctl list 查看）")
    return d


def load_cfg(d: Path) -> dict:
    return json.loads((d / "config.json").read_text(encoding="utf-8"))


def save_cfg(d: Path, cfg: dict) -> None:
    (d / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ports_of(cfg: dict) -> dict:
    """qmp_ui 是侧边工具栏专用的 QMP 端口（工具栏一直连着，QMP 端口同时只服务一个客户端）。"""
    i = cfg["index"]
    base = 25400 + 10 * i
    return {"adb": 5555 + 2 * i, "console": base, "monitor": base + 1, "qmp": base + 2,
            "qmp_ui": base + 3}


def run_info(d: Path) -> dict | None:
    """实例在运行时返回 run.json 的内容；QEMU 已退出（如直接关了窗口）时顺手删掉残留的 run.json。"""
    f = d / "run.json"
    if not f.exists():
        return None
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        info = {}
    if pid_alive(info.get("pid", 0)):
        return info
    f.unlink(missing_ok=True)
    return None


# ---------------------------------------------------------------- prepare

def ensure_alpine() -> None:
    for dst, url in ALPINE_FILES.items():
        download(dst, url)
    for apk in ALPINE_APKS:
        download(DOWNLOADS / "alpine-apk" / f"{apk}.apk", f"{ALPINE_REL}/main/aarch64/{apk}.apk")


def all_avds() -> list[Path]:
    return sorted(p for p in AVDS.glob("*") if (p / "config.json").exists()) if AVDS.exists() else []


def system_base(cfg: dict) -> Path:
    """实例系统盘的后备：默认共享的基础镜像；create --system 指定的自定义盘（如 GSI 实验盘）。"""
    return Path(cfg["system_base"]) if cfg.get("system_base") else BASE / "system.img"


def make_system_overlay(d: Path, cfg: dict) -> None:
    """系统盘：后备盘上的 qcow2 写时复制层（Android 不写系统盘，用户数据都在 data.qcow2）。"""
    (d / "system.qcow2").unlink(missing_ok=True)
    subprocess.run([str(qemu_img()), "create", "-q", "-f", "qcow2", "-F", "raw", "-b",
                    str(system_base(cfg).resolve()), str(d / "system.qcow2")], check=True)


def do_prepare(args) -> None:
    done = BASE / "base.json"
    if done.exists() and not args.force:
        log(f"基础镜像已就绪：{BASE}（--force 重做）")
        return
    if running := [d.name for d in all_avds() if run_info(d)]:
        sys.exit(f"有 AVD 正在运行（{', '.join(running)}），它们在用基础镜像，先 stop")
    download(SYSIMG_ZIP, SYSIMG_URL, SYSIMG_SIZE)
    ensure_alpine()
    BASE.mkdir(parents=True, exist_ok=True)
    done.unlink(missing_ok=True)
    log(f"解包官方镜像 → {BASE}")
    with zipfile.ZipFile(SYSIMG_ZIP) as z:
        for m in ZIP_MEMBERS:
            dst = BASE / m
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(f"arm64-v8a/{m}") as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 22)
    log("改造 vendor 分区：扩容/去重/装入 woa gralloc、SwiftShader、配置（Alpine 辅助 VM）")
    sys.path.insert(0, str(SCRIPTS))
    import vendor_install
    rc = vendor_install.install(BASE / "system.img", prepare=True, log=BASE / "prepare.log",
                                echo=False)
    if rc != 0:
        sys.exit(f"vendor 改造失败（rc={rc}），日志：{BASE / 'prepare.log'}")
    done.write_text(json.dumps({"source": SYSIMG_URL, "prepared": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    # 基础镜像变了，已有实例的写时复制层必须重建（否则新旧数据块混在一起）；userdata 保留
    for d in all_avds():
        cfg = load_cfg(d)
        if not cfg.get("system_base"):
            make_system_overlay(d, cfg)
            log(f"已重建 {d.name} 的系统盘（userdata 保留）")
        else:
            log(f"注意：{d.name} 用自定义系统盘 {Path(cfg['system_base']).name}（基础盘的拷贝），"
                f"其中的 vendor 还是旧的：用 scripts/gsi_disk.py 重新生成，再 avdctl edit {d.name} --system <盘>")
    log("基础镜像完成。下一步：avdctl create <名字>")


# ---------------------------------------------------------------- create / wipe

def make_userdata(d: Path, size: str) -> None:
    data = d / "data.qcow2"
    data.unlink(missing_ok=True)
    subprocess.run([str(qemu_img()), "create", "-q", "-f", "qcow2", str(data), size], check=True)
    stage = IMAGES / ".alpine-job" / "userdata-payload"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copyfile(BASE / "data" / "local.prop", stage / "local.prop")
    job = r"""
D="$DISK0"
[ -n "$D" ] || { echo "找不到 userdata 盘"; exit 1; }
mke2fs -q -t ext4 -L userdata -b 4096 -I 256 "$D"
debugfs -w -R "write /payload/local.prop local.prop" "$D"
debugfs -w -R "sif local.prop mode 0100644" "$D"
e2fsck -fn "$D" >/dev/null
echo USERDATA_OK
"""
    sys.path.insert(0, str(SCRIPTS))
    from alpine_job import run_job
    # qcow2 文件大小不等于虚拟大小：把虚拟大小告诉 alpine_job，guest 里按扇区数认出这块盘
    log(f"格式化 userdata（{size}，Alpine 辅助 VM）")
    rc = run_job(job, disks=[(data, "qcow2", size_bytes(size))], payload_dir=stage,
                 log=d / "logs" / "userdata.log", echo=False)
    if rc != 0:
        sys.exit(f"创建 userdata 失败（rc={rc}），日志：{d / 'logs' / 'userdata.log'}")


def size_bytes(s: str) -> int:
    s = s.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(s[-1:], 1)
    return int(float(s[:-1] if s[-1:] in "KMG" else s) * mult)


def do_create(args) -> None:
    if not (BASE / "base.json").exists():
        sys.exit("基础镜像未准备：先运行 avdctl prepare")
    d = AVDS / args.name
    if d.exists():
        sys.exit(f"AVD 已存在：{d}")
    used = {load_cfg(p)["index"] for p in all_avds()}
    index = next(i for i in range(64) if i not in used)
    cfg = dict(DEFAULTS)
    for k in ("width", "height", "density", "ram", "cores", "data_size", "audio", "gpu"):
        v = getattr(args, k)
        if v is not None:
            cfg[k] = v
    if args.mic:
        cfg["mic"] = True
    if args.system:
        if not Path(args.system).is_file():
            sys.exit(f"找不到系统盘 {args.system}")
        cfg["system_base"] = str(Path(args.system).resolve())
    cfg["index"] = index
    (d / "logs").mkdir(parents=True)
    log(f"创建 {args.name}（{cfg['width']}x{cfg['height']}@{cfg['density']}dpi，"
        f"{cfg['ram']}MB，{cfg['cores']} 核，adb 端口 {ports_of(cfg)['adb']}）")
    try:
        make_system_overlay(d, cfg)
        shutil.copyfile(BASE / "encryptionkey.img", d / "encryptionkey.img")
        make_userdata(d, cfg["data_size"])
    except BaseException:
        # 半成品目录会挡住同名重试；日志挪到 AVDS 下留给排查
        for f in (d / "logs").glob("*.log"):
            shutil.copyfile(f, AVDS / f"{args.name}-create-failed-{f.name}")
        shutil.rmtree(d, ignore_errors=True)
        raise
    save_cfg(d, cfg)
    if not args.no_firstboot:
        first_boot(args.name, d, cfg)
    log(f"已创建 {d}；启动：avdctl start {args.name}")


EDITABLE = ("width", "height", "density", "ram", "cores", "audio", "gpu")


def do_edit(args) -> None:
    """改实例的硬件配置（分辨率/密度/内存/核数/声音/GPU），下次启动生效。userdata 大小改不了（要重建）。
    --system：换（或在原路径重新生成后重新挂上）自定义系统盘，只重建写时复制层，userdata 保留。"""
    d = avd_dir(args.name)
    if run_info(d):
        sys.exit("AVD 正在运行，先 stop")
    cfg = load_cfg(d)
    changed = {k: getattr(args, k) for k in EDITABLE if getattr(args, k) is not None}
    if args.mic is not None:
        changed["mic"] = args.mic
    if args.system:
        if not Path(args.system).exists():
            sys.exit(f"找不到系统盘 {args.system}")
        changed["system_base"] = str(Path(args.system).resolve())
    if not changed:
        sys.exit("没有要改的项（--width/--height/--density/--ram/--cores/--audio/--gpu/--mic/--no-mic/--system）")
    for k, v in changed.items():
        log(f"{k}: {cfg.get(k)} → {v}")
    cfg.update(changed)
    save_cfg(d, cfg)
    if args.system:
        make_system_overlay(d, cfg)
        log("已按新系统盘重建写时复制层（userdata 保留）")
    log(f"已保存；{args.name} 现在是 {cfg['width']}x{cfg['height']}@{cfg['density']}dpi，"
        f"{cfg['ram']}MB，{cfg['cores']} 核")


def do_wipe(args) -> None:
    d = avd_dir(args.name)
    if run_info(d):
        sys.exit("AVD 正在运行，先 stop")
    cfg = load_cfg(d)
    shutil.copyfile(BASE / "encryptionkey.img", d / "encryptionkey.img")
    make_userdata(d, cfg["data_size"])
    if not args.no_firstboot:
        first_boot(args.name, d, cfg)
    log(f"已清空 {args.name} 的 userdata")


def do_delete(args) -> None:
    d = avd_dir(args.name)
    if run_info(d):
        sys.exit("AVD 正在运行，先 stop")
    shutil.rmtree(d)
    log(f"已删除 {args.name}")


def do_list(args) -> None:
    if not AVDS.exists() or not any(AVDS.iterdir()):
        print("（没有 AVD；avdctl create <名字> 创建）")
        return
    for d in sorted(AVDS.iterdir()):
        if not (d / "config.json").exists():
            continue
        cfg = load_cfg(d)
        p = ports_of(cfg)
        info = run_info(d)
        state = f"运行中 pid={info['pid']}" if info else "已停止"
        system = f"  系统盘 {Path(cfg['system_base']).name}" if cfg.get("system_base") else ""
        print(f"{d.name:20} {cfg['width']}x{cfg['height']}@{cfg['density']}  "
              f"emulator-{p['adb'] - 1}  {state}{system}")


# ---------------------------------------------------------------- start / stop

def build_cmdline(name: str, d: Path, cfg: dict, headless: bool, verbose: bool = False) -> list[str]:
    """verbose：内核/init 日志全部打到串口控制台（logs/console.log），排查开机问题用。"""
    p = ports_of(cfg)
    # GPU：gfxstream = 宿主 GPU（virtio-gpu rutabaga 设备，新实例默认），swiftshader = guest 内 CPU 渲染
    # （没有 gpu 配置项的旧实例按它运行）。gfxstream 下
    # guest 的 EGL/GLES 编码后经 x-gfxstream-gles capset 转发给宿主 gfxstream → ANGLE → D3D
    gpu = cfg.get("gpu", "swiftshader")
    egl_flags = ("androidboot.hardwareegl=emulation androidboot.hardware.gralloc=minigbm "
                 "androidboot.hardware.gltransport=virtio-gpu-pipe") if gpu == "gfxstream" \
        else "androidboot.hardwareegl=swiftshader"
    append = " ".join([
        "console=ttyAMA0",
        "loglevel=7 printk.devkmsg=on" if verbose else "loglevel=3",
        "qemu=1 androidboot.qemu=1 androidboot.hardware=ranchu",
        "androidboot.boot_devices=a003a00.virtio_mmio,a003c00.virtio_mmio rootwait init=/init",
        f"8250.nr_uarts=1 {egl_flags}",
        # SELinux 默认 enforcing（与官方模拟器相同）；permissive 只在 userdebug 镜像上生效，
        # GSI 等 user 版本总是 enforcing
        f"androidboot.selinux={cfg.get('selinux', 'enforcing')}",
        "androidboot.opengles.version=196608",
        f"androidboot.lcd_density={cfg['density']}",
        "androidboot.logcat=*:I",
    ])
    tz = host_timezone()
    vsock = (f"ranchu-vsock-device,id=vsock0,forward={p['adb']}:5555,boot-props=qemu.timezone={tz},"
             f"boot-cmd={BOOT_CMD}")
    if cfg.get("gps"):
        vsock += f",gps={cfg['gps']}"
    # 声音：virtio-snd（guest 内核自带 virtio_snd 模块，goldfish 音频 HAL 走 tinyalsa card0/pcm0）。
    # 默认只有放音流；mic=true 时再加一路录音流（会用到宿主麦克风）。
    audio = []
    if cfg.get("audio", "dsound") != "none":
        audio = ["-audiodev", f"{cfg.get('audio', 'dsound')},id=snd0",
                 "-device", f"virtio-sound-pci,audiodev=snd0,streams={2 if cfg.get('mic') else 1}"]
    exe, _ = qemu_exe()
    return [
        str(exe), "-name", f"{name} - RanchuWoA",
        "-M", "virt,ranchu-devices=on,gic-version=3,ranchu-system-disk=sysdisk,"
              "ranchu-metadata-disk=metadisk,ranchu-data-disk=datadisk",
        "-accel", "whpx", "-cpu", "host", "-m", str(cfg["ram"]), "-smp", str(cfg["cores"]),
        "-kernel", str(BASE / "kernel-ranchu"), "-initrd", str(BASE / "ramdisk.img"),
        "-append", append,
        "-drive", f"file={d / 'system.qcow2'},if=none,id=sysdisk,format=qcow2",
        "-drive", f"file={d / 'encryptionkey.img'},if=none,id=metadisk,format=raw",
        "-drive", f"file={d / 'data.qcow2'},if=none,id=datadisk,format=qcow2",
        "-object", "rng-builtin,id=rng0", "-device", "virtio-rng-device,rng=rng0",
        "-netdev", "user,id=n0", "-device", "virtio-net-device,netdev=n0",
        # dpi：EDID 里的物理尺寸按屏幕密度报（与官方模拟器 xdpi = 密度一致）；不给时 QEMU 按 100 dpi，
        # 手机屏成了十几英寸，HyperOS 的 AutoDensity 据此把应用缩到一半以下（RESEARCH §26）
        "-device", (
            f"virtio-gpu-rutabaga-pci,x-gfxstream-gles=on,wsi=surfaceless,hostmem=2G,"
            f"xres={cfg['width']},yres={cfg['height']},dpi={cfg['density']}" if gpu == "gfxstream"
            else f"virtio-gpu-pci,xres={cfg['width']},yres={cfg['height']},dpi={cfg['density']}"),
        "-device", "virtio-touchscreen-pci", "-device", "virtio-keyboard-pci",
        *audio,
        "-device", "virtio-serial-pci,id=vser0",
        "-chardev", "null,id=hvc0", "-device", "virtconsole,bus=vser0.0,chardev=hvc0",
        "-chardev", f"file,id=hvc1,path={d / 'logs' / 'logcat.txt'}",
        "-device", "virtconsole,bus=vser0.0,chardev=hvc1",
        "-device", vsock,
        "-display", "none" if headless else "sdl",
        # 串口控制台：TCP 可连（guest shell），同时全部输出记到 logs/console.log
        "-chardev", f"socket,id=con0,host=127.0.0.1,port={p['console']},server=on,wait=off,"
                    f"logfile={d / 'logs' / 'console.log'}",
        "-serial", "chardev:con0",
        "-monitor", f"tcp:127.0.0.1:{p['monitor']},server,nowait",
        "-qmp", f"tcp:127.0.0.1:{p['qmp']},server,nowait",
        "-qmp", f"tcp:127.0.0.1:{p['qmp_ui']},server,nowait",
    ]


def busy_ports(cfg: dict, wait: float = 0) -> list[str]:
    """被占用的端口。wait 秒内会重试：刚 stop 的实例，QEMU 退出后端口要过一小会儿才放出来。"""
    deadline = time.time() + wait
    while True:
        busy = [f"{k}:{v}" for k, v in ports_of(cfg).items() if not port_free(v)]
        if not busy or time.time() >= deadline:
            return busy
        time.sleep(0.5)


def launch(name: str, d: Path, cfg: dict, headless: bool, qlog: Path,
           verbose: bool = False) -> subprocess.Popen:
    """后台启动 QEMU（不随 avdctl 退出），stdout/stderr 写 qlog。"""
    cmd = build_cmdline(name, d, cfg, headless, verbose)
    _, env = qemu_exe()
    flags = 0x00000008 | 0x00000200       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(qlog, "wb") as out:
        return subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)


def wait_boot(proc: subprocess.Popen, qlog: Path, timeout: int = 300) -> float:
    """等 guest 经 QemuMiscPipe 报告开机完成，返回用时（秒）；QEMU 退出或超时抛 RuntimeError。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"QEMU 已退出（{proc.returncode}），见 {qlog}")
        if b"guest boot completed" in qlog.read_bytes():
            return time.time() - t0
        time.sleep(1)
    raise RuntimeError(f"等待开机超时，见 {qlog}")


def do_start(args) -> None:
    d = avd_dir(args.name)
    if run_info(d):
        sys.exit(f"{args.name} 已在运行（pid {run_info(d)['pid']}）")
    if not (BASE / "base.json").exists():
        sys.exit("基础镜像未准备：先运行 avdctl prepare")
    cfg = load_cfg(d)
    if args.selinux:
        cfg["selinux"] = args.selinux           # 只影响本次启动
    p = ports_of(cfg)
    if busy := busy_ports(cfg, wait=10):
        sys.exit(f"端口被占用：{', '.join(busy)}")
    (d / "logs").mkdir(exist_ok=True)
    qlog = d / "logs" / "qemu.log"
    serial = f"emulator-{p['adb'] - 1}"
    log(f"启动 {args.name}：{cfg['width']}x{cfg['height']}，adb {serial} / 127.0.0.1:{p['adb']}")
    toolbar = not args.headless and not args.no_toolbar
    if args.foreground:
        _, env = qemu_exe()
        proc = subprocess.Popen(build_cmdline(args.name, d, cfg, args.headless, args.verbose_boot),
                                env=env)
        if toolbar:
            spawn_toolbar(args.name, proc.pid)
        sys.exit(proc.wait())
    proc = launch(args.name, d, cfg, args.headless, qlog, args.verbose_boot)
    (d / "run.json").write_text(json.dumps({"pid": proc.pid, "adb_serial": serial, **p}, indent=2),
                                encoding="utf-8")
    if toolbar:
        spawn_toolbar(args.name, proc.pid)
    if not args.wait:
        log(f"已启动 pid {proc.pid}；日志 {qlog}")
        return
    try:
        log(f"开机完成，用时 {wait_boot(proc, qlog):.0f}s；adb -s {serial} shell")
    except RuntimeError as e:
        sys.exit(str(e))


def spawn_toolbar(name: str, pid: int) -> None:
    """拉起侧边工具栏（toolbar.py，用 pythonw 免得多一个控制台窗口；QEMU 退出时它自己退出）。"""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), "-X", "utf8",
                      str(Path(__file__).with_name("toolbar.py")), name, "--pid", str(pid)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def do_toolbar(args) -> None:
    d = avd_dir(args.name)
    info = run_info(d)
    if not info:
        sys.exit(f"{args.name} 没有在运行")
    spawn_toolbar(args.name, info["pid"])
    log(f"已打开 {args.name} 的工具栏（已经开着时不会重复打开；出错看 logs/toolbar.log）")


def console_cmd(port: int, cmd: str) -> bool:
    """经串口控制台（guest 里的 /system/bin/sh，shell 用户）执行一条命令；连不上返回 False。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"\n" + cmd.encode() + b"\n")
            time.sleep(1)
        return True
    except OSError:
        return False


def poweroff(pid: int, ports: dict, adb_serial: str, timeout: int = 40) -> None:
    """请求 Android 正常关机，超时后经 QMP 强制退出 QEMU。

    首选 QMP system_powerdown：guest 开机完成后 ranchu-vsock 会自己经 adbd 执行 svc power shutdown
    （与关窗口同一条路）；不行再试串口控制台和 adb 的 reboot -p。
    """
    def exited(sec: int) -> bool:
        for _ in range(sec):
            if not pid_alive(pid):
                return True
            time.sleep(1)
        return not pid_alive(pid)

    try:
        r = qmp(ports["qmp"], {"execute": "qom-get", "arguments": {
            "path": "/machine/peripheral/vsock0", "property": "poweroff-ready"}})
        if r and r[0].get("return") is True:
            qmp(ports["qmp"], {"execute": "system_powerdown"})
            if exited(timeout):
                return
    except (OSError, ValueError):
        pass
    if console_cmd(ports["console"], "reboot -p") and exited(timeout):
        return
    adb = adb_exe()
    if adb:
        try:
            subprocess.run([adb, "-s", adb_serial, "shell", "reboot", "-p"], capture_output=True,
                           timeout=20)
        except subprocess.TimeoutExpired:
            pass
        if exited(timeout):
            return
    log("Android 没有按时关机，强制退出 QEMU")
    qmp_quit(ports["qmp"])
    exited(10)


FIRSTBOOT_SETTLE = 10     # 开机完成后再等几秒，让 launcher 和首次开机任务跑起来


def first_boot(name: str, d: Path, cfg: dict) -> None:
    """无窗口跑一次首次开机再关机。

    首次开机时 googlesdksetup 的 HOME 跳板（写 device_provisioned 后自行禁用）先于 launcher 运行，
    launcher 接手状态栏/导航栏控制时 leash 处于隐藏态，而 launcher 不会把它显示出来（软件渲染下
    的时序问题；切换一次应用后恢复）。预先跑完首次开机，用户看到的第一次启动就是正常的再次开机，
    也省掉首次开机的额外初始化时间。失败只告警，不影响 AVD 使用。
    """
    p = ports_of(cfg)
    if busy := busy_ports(cfg):
        log(f"跳过首次开机初始化：端口被占用（{', '.join(busy)}）")
        return
    log("首次开机初始化（无窗口）...")
    qlog = d / "logs" / "firstboot.log"
    proc = launch(name, d, cfg, True, qlog)
    try:
        t = wait_boot(proc, qlog)
        time.sleep(FIRSTBOOT_SETTLE)
        # 关掉"自动"私人 DNS：NAT 后面的 DNS 不支持 DNS-over-TLS，而 eth0 路由就绪前的 DoT 校验会让
        # resolver 在 SSL_connect 上空转 1–2 秒、刷出上万行日志（RESEARCH §20.11）
        console_cmd(p["console"], "settings put global private_dns_mode off")
        poweroff(proc.pid, p, f"emulator-{p['adb'] - 1}")
        log(f"首次开机初始化完成（开机 {t:.0f}s）")
    except RuntimeError as e:
        log(f"首次开机初始化失败（{e}）；AVD 仍可直接启动")
    finally:
        if proc.poll() is None:
            proc.kill()


def qmp(port: int, *cmds: dict) -> list[dict]:
    """依次执行 QMP 命令，返回各自的响应（跳过异步事件）。"""
    out = []
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        f = s.makefile("rwb")
        f.readline()                                    # 问候语
        for c in ({"execute": "qmp_capabilities"}, *cmds):
            f.write(json.dumps(c).encode() + b"\n")
            f.flush()
            while line := f.readline():                 # quit 之后 QEMU 直接断开
                r = json.loads(line)
                if "event" not in r:
                    out.append(r)
                    break
    return out[1:]


def qmp_quit(port: int) -> None:
    try:
        qmp(port, {"execute": "quit"})
    except OSError:
        pass


def do_stop(args) -> None:
    d = avd_dir(args.name)
    info = run_info(d)
    if not info:
        log(f"{args.name} 没有在运行")
        return
    if args.force:
        log("强制退出 QEMU")
        qmp_quit(info["qmp"])
        for _ in range(10):
            if not pid_alive(info["pid"]):
                break
            time.sleep(1)
    else:
        log("请求 Android 关机 ...")
        poweroff(info["pid"], info, info["adb_serial"])
    (d / "run.json").unlink(missing_ok=True)
    log(f"{args.name} 已停止")


def do_geo(args) -> None:
    d = avd_dir(args.name)
    if not (-90 <= args.lat <= 90 and -180 <= args.lon <= 180):
        sys.exit("纬度应在 -90..90，经度应在 -180..180")
    value = f"{args.lat};{args.lon}" + (f";{args.alt}" if args.alt is not None else "")
    cfg = load_cfg(d)
    cfg["gps"] = value
    save_cfg(d, cfg)
    info = run_info(d)
    if info:
        r = qmp(info["qmp"], {"execute": "qom-set", "arguments": {
            "path": "/machine/peripheral/vsock0", "property": "gps", "value": value}})
        if "error" in r[0]:
            sys.exit(f"设置失败：{r[0]['error'].get('desc')}")
    log(f"GPS 位置 {value}" + ("（已生效）" if info else "（下次启动生效）"))


# ---------------------------------------------------------------- dist

def do_dist(args) -> None:
    if not DEV_QEMU.exists():
        sys.exit("先构建 qemu-ranchu")
    if any(run_info(d) for d in all_avds()):
        sys.exit("有 AVD 在运行（可能正占用 dist 里的 DLL），先 stop")
    shutil.rmtree(DIST, ignore_errors=True)     # 重新打包，免得旧版本遗留的 DLL 混在里面
    DIST.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # hostgfx/bin 也要在 PATH 里：ldd 找不到 rutabaga_gfx_ffi.dll 时整棵依赖树都解析不全
    env["PATH"] = f"{ROOT / 'hostgfx' / 'bin'};{MSYS_BIN};C:\\msys64\\usr\\bin;" + env.get("PATH", "")
    exes = [DEV_QEMU, MSYS_BIN / "qemu-img.exe"]
    dlls = set()
    for exe in exes:
        out = subprocess.run([r"C:\msys64\usr\bin\ldd.exe", str(exe)], capture_output=True,
                             env=env).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            parts = line.split("=>")
            if len(parts) == 2 and "/clangarm64/" in parts[1]:
                dlls.add(parts[1].split("(")[0].strip())
        shutil.copy2(exe, DIST / exe.name)
    for dll in sorted(dlls):
        src = Path("C:/msys64" + dll)
        shutil.copy2(src, DIST / src.name)
    dist_gfxstream()
    n = len(list(DIST.glob("*.dll")))
    log(f"已打包 {len(exes)} 个程序 + {n} 个 DLL → {DIST}")


def dist_gfxstream() -> None:
    """gfxstream 链路：rutabaga_gfx_ffi → libgfxstream_backend → ANGLE（按名 LoadLibrary，ldd 看不到），
    连同它们在 clangarm64 里的依赖一起放到 exe 旁边。"""
    hostbin = ROOT / "hostgfx" / "bin"
    if not (hostbin / "rutabaga_gfx_ffi.dll").exists():
        log("没有 hostgfx（gfx/build_gfxstream_host.sh 未构建），dist 不含 gfxstream 模式")
        return
    angle = Path(os.environ.get("RANCHU_ANGLE", ROOT / "hostgfx" / "angle"))
    if not (angle / "libGLESv2.dll").exists():
        angle = MSYS_BIN
    todo = [hostbin / "rutabaga_gfx_ffi.dll", hostbin / "libgfxstream_backend-0.dll",
            angle / "libEGL.dll", angle / "libGLESv2.dll"]
    readobj = MSYS_BIN / "llvm-readobj.exe"
    strip = MSYS_BIN / "llvm-strip.exe"
    done: set[str] = set()
    while todo:
        src = todo.pop()
        if src.name.lower() in done:
            continue
        done.add(src.name.lower())
        dst = DIST / src.name
        if src.name.startswith("libgfxstream_backend"):
            # 开发构建是 debugoptimized（带调试信息 78 MB），dist 里去掉调试段
            subprocess.run([str(strip), "--strip-debug", "-o", str(dst), str(src)], check=True)
        else:
            shutil.copy2(src, dst)
        out = subprocess.run([str(readobj), "--coff-imports", str(src)], capture_output=True,
                             text=True).stdout
        for name in re.findall(r"^\s*Name: (\S+\.dll)\s*$", out, re.M | re.I):
            if name.lower() not in done and (MSYS_BIN / name).exists():
                todo.append(MSYS_BIN / name)


# ---------------------------------------------------------------- main

def main() -> None:
    # Git Bash / 重定向时 stdout 默认是 ANSI 代码页，中文会乱码；子 Python 进程（superimg 等）同理
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONUTF8"] = "1"
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("create")
    p.add_argument("name")
    for k, t in (("width", int), ("height", int), ("density", int), ("ram", int), ("cores", int)):
        p.add_argument(f"--{k}", type=t)
    p.add_argument("--data-size", dest="data_size")
    p.add_argument("--audio", choices=["dsound", "sdl", "none"])
    p.add_argument("--gpu", choices=["swiftshader", "gfxstream"],
                   help="gfxstream＝宿主 GPU 加速（默认）；swiftshader＝guest 内 CPU 渲染")
    p.add_argument("--mic", action="store_true")
    p.add_argument("--system", help="自定义系统盘（GPT：vbmeta+super，如 scripts/gsi_disk.py 的产物）")
    p.add_argument("--no-firstboot", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("edit")
    p.add_argument("name")
    for k, t in (("width", int), ("height", int), ("density", int), ("ram", int), ("cores", int)):
        p.add_argument(f"--{k}", type=t)
    p.add_argument("--audio", choices=["dsound", "sdl", "none"])
    p.add_argument("--gpu", choices=["swiftshader", "gfxstream"],
                   help="切换渲染后端，下次启动生效")
    p.add_argument("--mic", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--system", help="自定义系统盘（gsi_disk.py 重新生成后用它重新挂上）")
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--foreground", action="store_true")
    p.add_argument("--verbose-boot", action="store_true",
                   help="内核/init 日志全部打到串口控制台（logs/console.log）")
    p.add_argument("--selinux", choices=["permissive", "enforcing"], help="本次启动的 SELinux 模式")
    p.add_argument("--no-toolbar", action="store_true", help="不打开侧边工具栏")
    sub.add_parser("toolbar").add_argument("name")
    p = sub.add_parser("wipe")
    p.add_argument("name")
    p.add_argument("--no-firstboot", action="store_true")
    p = sub.add_parser("stop")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="不等 Android 关机，直接退出 QEMU")
    sub.add_parser("delete").add_argument("name")
    p = sub.add_parser("geo")
    p.add_argument("name")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("alt", type=float, nargs="?")
    sub.add_parser("dist")
    args = ap.parse_args()
    {"prepare": do_prepare, "create": do_create, "list": do_list, "edit": do_edit, "start": do_start,
     "stop": do_stop, "wipe": do_wipe, "delete": do_delete, "geo": do_geo,
     "toolbar": do_toolbar, "dist": do_dist}[args.cmd](args)


if __name__ == "__main__":
    main()
