#!/bin/bash
# gen_aidl.sh — 由 aidl/ 下的冻结 AIDL 生成 NDK 后端 C++ 代码到 gen/（结果已入库，改 AIDL 时才需重跑）。
# 版本/哈希与 Android 15 设备上的冻结接口一致：allocator V2、graphics.common V5、common V2。
# 用 Android SDK build-tools 34 的 aidl.exe（旧版算不了 1L << 32 之类的常量，所以 BufferUsage.aidl 里
# 这几个常量已改成字面量；HdrConversionStrategy 生成失败但用不到）。
set -uo pipefail
cd "$(dirname "$0")"
AIDL="${AIDL:-$LOCALAPPDATA/Android/Sdk/build-tools/34.0.0/aidl.exe}"

gen() {   # gen <版本> <哈希> <aidl 文件>...（逐个生成，单个失败不影响其余）
    local ver=$1 hash=$2 f
    shift 2
    for f in "$@"; do
        "$AIDL" --lang=ndk --structured --stability=vintf --version="$ver" --hash="$hash" \
            -I aidl -o gen/src -h gen/include "$f" >/dev/null 2>&1 || echo "生成失败（可忽略则忽略）：$f"
    done
}

gen 2 c32ddfdeb69c6e4a8a45519e6f9a39c4b66fd99f aidl/android/hardware/common/*.aidl
gen 5 2ffe8da1136972e9b6bed7903f0d5aca289005a9 aidl/android/hardware/graphics/common/*.aidl
gen 2 9499fec09c544e9de5be3c87125721600f8ade66 aidl/android/hardware/graphics/allocator/*.aidl
echo "生成完成：gen/src、gen/include"
