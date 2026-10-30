#!/bin/bash
# build_gfxstream_host.sh — 构建宿主 GPU 加速链路，装进 hostgfx/（bin/ 下两个 DLL + lib/pkgconfig）：
#   gfxstream（宿主渲染器，EGL-on-EGL → ANGLE(D3D11)）→ libgfxstream_backend-0.dll
#   rutabaga_gfx（C FFI，features=gfxstream）          → rutabaga_gfx_ffi.dll
# 之后 QEMU 带 rutabaga 重新配置（一次即可）：
#   PKG_CONFIG_PATH=<repo>/hostgfx/lib/pkgconfig meson configure qemu-ranchu/build -Drutabaga_gfx=enabled
#
# 源码（都不进仓库）：
#   gfxstream     https://github.com/google/gfxstream @0ab696d + patches/gfxstream-0ab696d-ranchuwoa.patch
#   rutabaga_gfx  https://github.com/magma-gpu/rutabaga_gfx @5bcb95f（原版）
# 依赖：MSYS2 CLANGARM64 的 clang/meson/ninja/rust、mingw-w64-clang-aarch64-angleproject（运行期 ANGLE）。
# 改了 gfxstream 宿主源码后重跑本脚本（先停掉所有 VM：DLL 被占用时 install 会失败），
# 改了 guest 源码则跑 gfx/build_gfxstream_guest.sh 再 avdctl prepare --force。
set -euo pipefail
cd "$(dirname "$0")/.."
# Python 3.14 在 MSYS2 shell 里没有 USERPROFILE 时 Path.home() 抛异常（meson 会用到）
export USERPROFILE="${USERPROFILE:-$(cygpath -w "$HOME")}"
export PATH="/c/msys64/clangarm64/bin:/c/msys64/usr/bin:$PATH"
PREFIX="$(cygpath -m "$PWD/hostgfx")"

# debugoptimized（-O2 -g）：崩溃时 VEH 打出的“模块+偏移”可以用 llvm-symbolizer 符号化
if [ ! -f gfxstream/host-build/build.ninja ]; then
    (cd gfxstream && meson setup host-build --prefix="$PREFIX" -Dbuildtype=debugoptimized)
fi
ninja -C gfxstream/host-build
meson install -C gfxstream/host-build --quiet

# rutabaga 的 meson 按 pkg-config 找 libc++（MSYS2 没有这个 .pc，补一个）
PC="$PREFIX/lib/pkgconfig"
mkdir -p "$PC" hostgfx/bin hostgfx/include/rutabaga_gfx
if [ ! -f "$PC/libc++.pc" ]; then
    printf '%s\n' 'prefix=/clangarm64' 'libdir=${prefix}/lib' 'includedir=${prefix}/include' '' \
        'Name: libc++' 'Description: LLVM C++ standard library (RanchuWoA shim for pkg-config lookup)' \
        'Version: 1.0' 'Libs: -L${libdir} -lc++ -lc++abi' 'Cflags: -I${includedir}/c++/v1' > "$PC/libc++.pc"
fi
if [ ! -f rutabaga_gfx/ffi-build/build.ninja ]; then
    (cd rutabaga_gfx && PKG_CONFIG_PATH="$PC" \
        meson setup ffi-build -Dfeatures=gfxstream -Dffi=true --prefix="$PREFIX")
fi
ninja -C rutabaga_gfx/ffi-build
# meson install 在 MinGW 上找 MSVC 风格的 rutabaga_gfx_ffi.dll.lib 而失败：手工安装
cp rutabaga_gfx/ffi-build/ffi/rutabaga_gfx_ffi.dll hostgfx/bin/
cp rutabaga_gfx/ffi-build/ffi/librutabaga_gfx_ffi.dll.a hostgfx/lib/
cp rutabaga_gfx/ffi/src/include/rutabaga_gfx_ffi*.h hostgfx/include/rutabaga_gfx/
printf '%s\n' "prefix=$PREFIX" 'libdir=${prefix}/lib' 'includedir=${prefix}/include' '' \
    'Name: rutabaga_gfx_ffi' 'Description: C FFI bindings to Rutabaga VGI (manual install)' \
    'Version: 0.1.85' 'Libs: -L${libdir} -lrutabaga_gfx_ffi' 'Cflags: -I${includedir}' \
    > "$PC/rutabaga_gfx_ffi.pc"
ls -la hostgfx/bin
