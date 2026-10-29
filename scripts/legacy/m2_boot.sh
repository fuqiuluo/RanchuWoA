#!/bin/bash
# RanchuWoA M2 验收引导测试
# 用法: bash m2_boot.sh [秒数]   (默认 90)
#
# 关键点:
#  - 自研补丁版 QEMU (-M virt,ranchu-devices=on)
#  - WHPX 硬件加速
#  - 磁盘→virtio 槽位映射(倒序): 第1个-device→slot31(vda) 第2个→slot30(metadata by-name)
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMG="$ROOT/images/android-35/arm64-v8a"
QEMU="$ROOT/qemu-ranchu/build/qemu-system-aarch64.exe"
OUT="$ROOT/images/android-35/arm64-v8a/m2_boot.log"

T=${1:-90}
echo "[m2] 引导 ${T}s, 日志 -> $OUT"
timeout "$T" "$QEMU" \
  -M virt,ranchu-devices=on,gic-version=3 \
  -accel whpx -cpu host \
  -m 4096 -smp 4 \
  -kernel "$IMG/kernel-ranchu" \
  -initrd "$IMG/ramdisk.img" \
  -append "console=ttyAMA0 qemu=1 androidboot.qemu=1 androidboot.hardware=ranchu androidboot.first_stage_console=1 rootwait init=/init 8250.nr_uarts=1" \
  -drive file="$IMG/system.img",if=none,id=d0,format=raw \
  -device virtio-blk-device,drive=d0 \
  -drive file="$IMG/encryptionkey.img",if=none,id=d1,format=raw \
  -device virtio-blk-device,drive=d1 \
  -nographic -no-reboot > "$OUT" 2>&1 || true

echo "=== 结果摘要 ==="
echo "--- goldfish 探测 ---"
grep -aE "goldfish|android-pipe" "$OUT" | head -10
echo "--- init/挂载 ---"
grep -aE "init:|fstab|mount|super|metadata|dm-" "$OUT" | tail -25
echo "--- 尾部 ---"
tail -10 "$OUT"
