#!/bin/bash
# build_swiftshader.sh — 构建 guest 内用的 SwiftShader（EGL + GLES，Android 平台版），剥符号后放到 gfx/egl-out：
#   libEGL_swiftshader.so / libGLESv2_swiftshader.so / libGLESv1_CM_swiftshader.so（空库）
# 源码是 gfx/swiftshader（停在仍有 GLES 前端的旧提交，带本地改动），构建目录 gfx/ss-build。
# 依赖：MSYS2 CLANGARM64 的 cmake/ninja、NDK r27c、gfx/device-libs（fetch_device_libs.py）。
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/c/msys64/clangarm64/bin:$PATH"
NDK_BIN=android-ndk-r27c/toolchains/llvm/prebuilt/windows-x86_64/bin

if [ ! -f ss-build/build.ninja ]; then
    cmake -G Ninja -S swiftshader -B ss-build \
        -DCMAKE_TOOLCHAIN_FILE="$PWD/android-ndk-r27c/build/cmake/android.toolchain.cmake" \
        -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF -DSWIFTSHADER_BUILD_VULKAN=OFF \
        -DSWIFTSHADER_BUILD_EGL=ON -DSWIFTSHADER_BUILD_GLESv2=ON -DSWIFTSHADER_BUILD_GLES_CM=ON \
        -DSWIFTSHADER_BUILD_TESTS=OFF -DSWIFTSHADER_BUILD_SAMPLES=OFF \
        -DCMAKE_CXX_FLAGS="-Wno-error -Wno-deprecated-declarations -Wno-unused-but-set-variable"
fi
# CMake 目标名带 _deprecated（GLES 前端在上游已废弃），输出文件名由 OUTPUT_NAME/SUFFIX 定为 *_swiftshader.so
ninja -C ss-build libEGL_deprecated libGLESv2_deprecated

mkdir -p egl-out
for lib in libEGL libGLESv2; do
    "$NDK_BIN/llvm-strip.exe" --strip-unneeded -o "egl-out/${lib}_swiftshader.so" "ss-build/${lib}_swiftshader.so"
done
# Android 的 EGL loader 按 ro.hardware.egl 找齐 EGL/GLESv1_CM/GLESv2 三个库；不提供 GLES1，给一个空库
: > ss-build/empty.c
"$NDK_BIN/clang.exe" --target=aarch64-linux-android28 -shared -Wl,-soname,libGLESv1_CM_swiftshader.so \
    -o egl-out/libGLESv1_CM_swiftshader.so ss-build/empty.c
ls -la egl-out
