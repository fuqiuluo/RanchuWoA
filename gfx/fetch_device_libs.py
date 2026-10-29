"""fetch_device_libs.py — 从 Android 系统镜像里取出交叉编译要链接的设备库，放到 gfx/device-libs/。

gralloc（libbinder_ndk/liblog）和平台版 SwiftShader（libnativewindow/libsync）链接的是设备上的
LLNDK 库：NDK 里没有对应的平台符号（AServiceManager_*、ANativeWindowBuffer_getHardwareBuffer 等），
直接链设备库最省事，运行时由 guest 的同名库接管。

用法: py gfx/fetch_device_libs.py [system.img]    默认 images/base/android-35/system.img
依赖: pip 包 ext4（scripts/ext4tool.py 用）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "gfx" / "device-libs"
# (逻辑分区, 分区内目录, 库名列表)
LIBS = [
    ("system", "system/lib64", ["libandroid.so", "libbinder_ndk.so", "libEGL.so", "libGLESv2.so",
                                "liblog.so", "libnativewindow.so", "libsync.so", "libui.so",
                                "libvndksupport.so"]),
    ("vendor", "lib64", ["libbase.so", "libcutils.so", "libdrm.so", "libhardware.so", "libutils.so"]),
]


def main():
    img = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "images/base/android-35/system.img"
    for part, d, names in LIBS:
        off = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(img), part],
                             capture_output=True, text=True, check=True).stdout.split()[0]
        dst = OUT / part
        dst.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(img), off,
                        *[f"{d}/{n}" for n in names], "-o", str(dst)], check=True)
    print(f"完成 → {OUT}")


if __name__ == "__main__":
    main()
