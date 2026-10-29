#!/bin/bash
# build.sh — 用 NDK r27c 构建 RanchuWoA gralloc：
#   out/android.hardware.graphics.allocator-service.woa   （/vendor/bin/hw）
#   out/mapper.woa.so                                     （/vendor/lib64/hw）
# AIDL 生成代码在 gen/（由 gen_aidl.sh 生成，已入库则无需重跑）。
set -euo pipefail
cd "$(dirname "$0")"

NDK="../android-ndk-r27c/toolchains/llvm/prebuilt/windows-x86_64/bin"
CXX="$NDK/clang++.exe"
TARGET="aarch64-linux-android35"
OUT=out
mkdir -p "$OUT"

COMMON=(--target="$TARGET" -std=c++17 -O2 -fPIC -Wall -Wno-unused-parameter
        -Iinclude -Igen/include -Isrc)

GEN_SRCS=(
    gen/src/android/hardware/graphics/allocator/IAllocator.cpp
    gen/src/android/hardware/graphics/allocator/AllocationResult.cpp
    gen/src/android/hardware/graphics/allocator/BufferDescriptorInfo.cpp
    gen/src/android/hardware/common/NativeHandle.cpp
    gen/src/android/hardware/graphics/common/ExtendableType.cpp
)

"$CXX" "${COMMON[@]}" -DBINDER_STABILITY_SUPPORT -fPIE -pie -static-libstdc++ \
    -o "$OUT/android.hardware.graphics.allocator-service.woa" \
    src/allocator.cpp src/woa_format.cpp "${GEN_SRCS[@]}" \
    -L../device-libs/system -lbinder_ndk -llog -Wl,--allow-shlib-undefined

"$CXX" "${COMMON[@]}" -shared -static-libstdc++ -fvisibility=hidden \
    -Wl,--version-script=src/mapper.map -Wl,-soname,mapper.woa.so \
    -o "$OUT/mapper.woa.so" \
    src/mapper.cpp src/woa_format.cpp -llog

ls -la "$OUT"
