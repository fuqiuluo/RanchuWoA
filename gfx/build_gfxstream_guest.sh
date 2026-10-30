#!/bin/bash
# build_gfxstream_guest.sh — 构建 guest 内的 gfxstream EGL/GLES（virtio-gpu-pipe 传输），
#   产物放 gfx/gs-out：libEGL_emulation.so / libGLESv2_emulation.so / libGLESv1_CM_emulation.so
#   （_emulation 后缀与上游 goldfish/gfxstream 命名一致：eglDisplay.cpp 按该名加载，
#    直接覆盖镜像里原有的 goldfish-pipe 版；androidboot.hardwareegl=emulation 启用）
# 源码：gfxstream（guest/，GLES 编码器栈）、mesa-aosp（稀疏克隆，src/gfxstream/guest 的
#   VirtGpu DRM 后端与 ANativeWindow/gralloc 适配）、libdrm（仅头文件，链接设备 libdrm.so）。
#   gfxstream    https://github.com/google/gfxstream @0ab696d + patches/gfxstream-0ab696d-ranchuwoa.patch
#   mesa-aosp    https://android.googlesource.com/platform/external/mesa3d @224e91e
#                （--filter=blob:none --sparse；sparse-checkout：include/drm-uapi src/gfxstream src/util）
#                + patches/mesa-aosp-224e91e-ranchuwoa.patch
#   libdrm       https://gitlab.freedesktop.org/mesa/drm.git @33fab8c（原版）
# 依赖：MSYS2 CLANGARM64 的 cmake/ninja、NDK r27c、gfx/device-libs、protoc 21.12。
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/c/msys64/clangarm64/bin:$PATH"
NDK_BIN=android-ndk-r27c/toolchains/llvm/prebuilt/windows-x86_64/bin
PROTOC=../downloads/protoc21/bin/protoc.exe

# ProgramBinary protobuf（LITE 运行时，静态编入）
if [ ! -f gfxstream-guest/gen/ProgramBinary.pb.cc ] || [ gfxstream-guest/ProgramBinary.proto -nt gfxstream-guest/gen/ProgramBinary.pb.cc ]; then
    mkdir -p gfxstream-guest/gen
    sed 's/^syntax = "proto3";/syntax = "proto3";\n\noption optimize_for = LITE_RUNTIME;/' \
        gfxstream/guest/GLESv2_enc/ProgramBinary.proto > gfxstream-guest/ProgramBinary.proto
    "$PROTOC" --proto_path=gfxstream-guest --cpp_out=gfxstream-guest/gen gfxstream-guest/ProgramBinary.proto
fi

if [ ! -f gsg-build/build.ninja ]; then
    cmake -G Ninja -S gfxstream-guest -B gsg-build \
        -DCMAKE_TOOLCHAIN_FILE="$PWD/android-ndk-r27c/build/cmake/android.toolchain.cmake" \
        -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 -DANDROID_STL=c++_static \
        -DCMAKE_BUILD_TYPE=Release
fi
ninja -C gsg-build

mkdir -p gs-out
for lib in libEGL_emulation libGLESv2_emulation libGLESv1_CM_emulation libOpenglSystemCommon; do
    "$NDK_BIN/llvm-strip.exe" --strip-unneeded -o "gs-out/${lib}.so" "gsg-build/${lib}.so"
done
ls -la gs-out
