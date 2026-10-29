#!/bin/bash
# build.sh — 用 NDK r27c 构建 nullgfx（mapper.nullgfx.so + allocatord）
# NDK 解压到 gfx/android-ndk-r27c，或用环境变量 NDK 指定路径
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../../.." && pwd)"
NDK="${NDK:-$ROOT/gfx/android-ndk-r27c/toolchains/llvm/prebuilt/windows-x86_64/bin}"
API=35
CXX="$NDK/clang++.exe"
TARGET="aarch64-linux-android$API"
OUT="$DIR/out"
mkdir -p "$OUT"

"$CXX" --target="$TARGET" -std=c++17 -O2 -fPIC -shared -static-libstdc++ -I"$DIR/include" \
    -o "$OUT/mapper.nullgfx.so" \
    "$DIR/mapper_nullgfx.cpp" \
    -llog -ldl

"$CXX" --target="$TARGET" -std=c++17 -O2 -static-libstdc++ -I"$DIR/include" \
    -o "$OUT/allocatord" \
    "$DIR/allocatord.cpp" \
    -L"$DIR/platform-libs" -L"$OUT" -lbinder_ndk -llog

ls -la "$OUT"
