#!/bin/bash
# boot_woa.sh — 开发用启动脚本：补丁版 QEMU + WHPX 引导 android-35（woa 图形栈）。
#
# 用法: scripts/boot_woa.sh <实验编号> [sdl|none]
#   串口控制台 tcp:127.0.0.1:4444，HMP 监视器 tcp:127.0.0.1:4450（screendump/sendkey/quit），QMP 4451。
#   QEMU stdout/stderr 写到 logs/qemu-m<编号>.log；guest logcat 经 virtconsole（/dev/hvc1）
#   持续写到 logs/logcat-m<编号>.txt。日常使用请用 avdctl（tools/avdctl），这个脚本只给开发镜像
#   images/android-35/arm64-v8a 用（vendor 可在 guest 里 remount rw 直接改）。
#   ranchu-vsock 把 127.0.0.1:5555 转发到 guest vsock:5555（adbd），adb server 会自动识别成 emulator-5554；
#   virtserialport ranchu.xfer（tcp:127.0.0.1:4447）是二进制文件传输通道，见 scripts/vxfer.py。
set -u
cd "$(dirname "$0")/.."
N=${1:?实验编号}
DISPLAY_MODE=${2:-none}
IMG=images/android-35/arm64-v8a
export PATH="/c/msys64/clangarm64/bin:$PATH"

APPEND="console=ttyAMA0 loglevel=3 qemu=1 androidboot.qemu=1 androidboot.hardware=ranchu"
APPEND+=" androidboot.boot_devices=a003a00.virtio_mmio,a003c00.virtio_mmio rootwait init=/init 8250.nr_uarts=1"
APPEND+=" androidboot.selinux=permissive androidboot.hardwareegl=swiftshader"
APPEND+=" androidboot.opengles.version=196608 androidboot.lcd_density=320"
APPEND+=" androidboot.logcat=*:D"   # init.ranchu.rc 的 goldfish-logcat 把 logcat 写到 /dev/hvc1

exec ./qemu-ranchu/build/qemu-system-aarch64.exe -name "RanchuWoA m$N" \
  -M virt,ranchu-devices=on,gic-version=3,ranchu-system-disk=sysdisk,ranchu-metadata-disk=metadisk,ranchu-data-disk=datadisk \
  -accel whpx -cpu host -m 4096 -smp 4 \
  -kernel $IMG/kernel-ranchu -initrd $IMG/ramdisk.img \
  -append "$APPEND" \
  -drive file=$IMG/system.img,if=none,id=sysdisk,format=raw \
  -drive file=$IMG/encryptionkey.img,if=none,id=metadisk,format=raw \
  -drive file=$IMG/data.img,if=none,id=datadisk,format=raw \
  -object rng-builtin,id=rng0 -device virtio-rng-device,rng=rng0 \
  -netdev user,id=n0 -device virtio-net-device,netdev=n0 \
  -device virtio-gpu-pci,xres=720,yres=1280 \
  -device virtio-touchscreen-pci -device virtio-keyboard-pci \
  -audiodev dsound,id=snd0 -device virtio-sound-pci,audiodev=snd0,streams=1 \
  -device virtio-serial-pci,id=vser0 \
  -chardev null,id=hvc0 -device virtconsole,bus=vser0.0,chardev=hvc0 \
  -chardev file,id=hvc1,path=logs/logcat-m$N.txt -device virtconsole,bus=vser0.0,chardev=hvc1 \
  -chardev socket,id=xfer,host=127.0.0.1,port=4447,server=on,wait=off \
  -device virtserialport,bus=vser0.0,chardev=xfer,name=ranchu.xfer \
  -device ranchu-vsock-device,forward=5555:5555,boot-props=qemu.timezone=Asia/Shanghai,debug=on \
  -display "$DISPLAY_MODE" \
  -serial tcp:127.0.0.1:4444,server,nowait \
  -monitor tcp:127.0.0.1:4450,server,nowait \
  -qmp tcp:127.0.0.1:4451,server,nowait \
  -no-reboot > "logs/qemu-m$N.log" 2>&1
